/* ============================================================================
   token-panel.js — 🔑 Token import panel.

   PROBLEM THIS SOLVES
   -------------------
   The Quotex session token expires roughly daily. Until now the only way to
   give the app a new one was Railway → Variables → redeploy: a full container
   rebuild, ~2 minutes of downtime, and no signals in the meantime.

   This panel pushes the token straight into the running app:

       paste token → POST /api/set-token → feed reconnects (~5-10s) → signals

   and the server persists it to the Railway volume, so the next redeploy comes
   back live on its own without anyone pasting anything.

   Access control: the endpoint lives on a public URL, so it is gated by a PIN
   the operator claims on first use (POST /api/auth/claim). ADMIN_KEY, when set
   as an env var, also works as a master key. See core/token_store.py.

   Self-contained: builds its own markup, so a page only needs
       <link rel="stylesheet" href="/static/css/token-panel.css">
       <script defer src="/static/js/token-panel.js"></script>
   ============================================================================ */
(function () {
  'use strict';

  var PIN_KEY = 'qxTokenPin';        // localStorage: remembered PIN
  var SEEN_KEY = 'qxTokenPanelSeen'; // sessionStorage: auto-open once per tab
  var POLL_MS = 20000;               // idle status refresh
  var WATCH_MS = 2000;               // post-import status polling
  var WATCH_TIMEOUT_MS = 75000;

  var el = {};
  var state = { status: null, auth: null, watching: false, pollTimer: null };

  /* ─── helpers ──────────────────────────────────────────────────────────── */

  function h(tag, attrs, kids) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === 'class') node.className = attrs[k];
        else if (k === 'text') node.textContent = attrs[k];
        else if (k === 'html') node.innerHTML = attrs[k];
        else if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
      });
    }
    (kids || []).forEach(function (kid) { if (kid) node.appendChild(kid); });
    return node;
  }

  function getPin() {
    try { return localStorage.getItem(PIN_KEY) || ''; } catch (_) { return ''; }
  }
  function setPin(pin, remember) {
    try {
      if (remember && pin) localStorage.setItem(PIN_KEY, pin);
      else localStorage.removeItem(PIN_KEY);
    } catch (_) { /* private mode */ }
  }

  function ago(ts) {
    if (!ts) return 'never';
    var secs = Math.max(0, Date.now() / 1000 - ts);
    if (secs < 90) return Math.round(secs) + 's ago';
    if (secs < 5400) return Math.round(secs / 60) + ' min ago';
    if (secs < 172800) return Math.round(secs / 3600) + ' h ago';
    return Math.round(secs / 86400) + ' d ago';
  }

  function fetchJSON(url, opts) {
    return fetch(url, opts).then(function (r) {
      return r.json()
        .catch(function () { return {}; })
        .then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
    });
  }

  /* ─── markup ───────────────────────────────────────────────────────────── */

  function buildButton() {
    var btn = h('button', {
      id: 'token-btn', type: 'button', title: 'Quotex token / live data status',
      'aria-haspopup': 'dialog', 'aria-expanded': 'false', 'aria-label': 'Import Quotex token'
    }, [
      h('span', { class: 'tk-dot', 'aria-hidden': 'true' }),
      h('span', { class: 'tk-text', text: 'Token' })
    ]);
    btn.addEventListener('click', function () { open(); });

    var anchor = document.getElementById('market-switch-btn');
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(btn, anchor);
    else {
      var row = document.getElementById('topbar-row1');
      if (row) row.appendChild(btn);
      else { btn.classList.add('tk-floating'); document.body.appendChild(btn); }
    }
    return btn;
  }

  function buildModal() {
    var claimSection = h('div', { class: 'tk-section', id: 'tk-claim', hidden: 'hidden' }, [
      h('div', { class: 'tk-section-title', text: '① First run — set an access PIN' }),
      h('div', {
        class: 'tk-hint',
        text: 'This app is on a public URL. Pick a PIN (6+ characters) so only you ' +
              'can import tokens. It is stored hashed on the Railway volume and ' +
              'survives redeploys.'
      }),
      h('div', { class: 'tk-row' }, [
        h('input', { class: 'tk-input', id: 'tk-new-pin', type: 'password',
                     autocomplete: 'new-password', placeholder: 'Choose a PIN…' }),
        h('button', { class: 'tk-btn tk-btn-primary', id: 'tk-claim-btn', type: 'button', text: 'Set PIN' })
      ])
    ]);

    var dialog = h('div', { class: 'tk-dialog', role: 'dialog', 'aria-modal': 'true',
                            'aria-labelledby': 'tk-title' }, [
      h('div', { class: 'tk-head' }, [
        h('span', { class: 'tk-title', id: 'tk-title', text: '🔑 Quotex Token' }),
        h('span', { class: 'tk-spacer' }),
        h('button', { class: 'tk-close', id: 'tk-close', type: 'button',
                      'aria-label': 'Close', text: '✕' })
      ]),
      h('div', { class: 'tk-body' }, [
        h('div', { class: 'tk-status', id: 'tk-status' }, [
          h('div', { class: 'tk-status-row' }, [
            h('span', { class: 'tk-badge', id: 'tk-badge', text: '…' }),
            h('span', { class: 'tk-meta', id: 'tk-streams', text: '' })
          ]),
          h('div', { class: 'tk-status-msg', id: 'tk-msg', text: 'Checking live-data status…' }),
          h('div', { class: 'tk-meta', id: 'tk-stored', text: '' })
        ]),
        claimSection,
        h('div', { class: 'tk-field' }, [
          h('label', { class: 'tk-label', for: 'tk-token',
                       html: 'Quotex session token <span class="tk-req">*</span>' }),
          h('textarea', {
            class: 'tk-textarea', id: 'tk-token', spellcheck: 'false',
            autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off',
            placeholder: 'Paste the token — or the whole frame:\n42["authorization",{"session":"…","isDemo":1}]'
          }),
          h('div', { class: 'tk-hint',
                     text: 'Both forms work: the bare session value, or the full ' +
                           'authorization frame copied from DevTools. The server ' +
                           'extracts the session for you.' })
        ]),
        h('div', { class: 'tk-field', id: 'tk-pin-field' }, [
          h('label', { class: 'tk-label', for: 'tk-pin', text: 'Access PIN' }),
          h('div', { class: 'tk-row' }, [
            h('input', { class: 'tk-input', id: 'tk-pin', type: 'password',
                         autocomplete: 'current-password', placeholder: 'Your PIN' }),
            h('label', { class: 'tk-check' }, [
              h('input', { type: 'checkbox', id: 'tk-remember', checked: 'checked' }),
              h('span', { text: 'Remember on this device' })
            ])
          ])
        ]),
        h('div', { class: 'tk-result', id: 'tk-result', hidden: 'hidden' }),
        h('div', { class: 'tk-actions' }, [
          h('button', { class: 'tk-btn tk-btn-primary', id: 'tk-import', type: 'button',
                        text: 'Import & Go Live' }),
          h('button', { class: 'tk-btn', id: 'tk-recheck', type: 'button', text: 'Re-check' }),
          h('span', { class: 'tk-spacer' }),
          h('button', { class: 'tk-btn tk-btn-ghost', id: 'tk-dismiss', type: 'button', text: 'Close' })
        ]),
        h('details', { class: 'tk-help' }, [
          h('summary', { text: 'How do I get the token?' }),
          h('ol', {}, [
            h('li', { html: 'Log in to <code>qxbroker.com</code> (or your Quotex mirror) in Chrome.' }),
            h('li', { html: 'Open DevTools → <code>Network</code> → filter <code>WS</code> → click the socket.io connection.' }),
            h('li', { html: 'Open <code>Messages</code> and find the outgoing frame starting with <code>42["authorization"</code>.' }),
            h('li', { html: 'Copy that whole frame (or just the <code>session</code> value) and paste it above.' }),
            h('li', { html: 'Press <b>Import &amp; Go Live</b> — the feed reconnects in ~5-10s, no redeploy needed.' }),
            h('li', { html: 'The token is stored on the Railway volume, so the next deploy starts live automatically.' })
          ])
        ])
      ])
    ]);

    var modal = h('div', { id: 'token-modal', hidden: 'hidden' }, [dialog]);
    modal.addEventListener('mousedown', function (ev) { if (ev.target === modal) close(); });
    document.body.appendChild(modal);
    return modal;
  }

  /* ─── rendering ────────────────────────────────────────────────────────── */

  function stateClass(s) {
    if (!s) return 'wait';
    if (s.live) return 'live';
    if (s.token_dead || s.status === 'no_credentials') return 'dead';
    return 'wait';
  }

  function render() {
    var s = state.status;
    var cls = stateClass(s);
    var label = { live: 'Live', wait: 'Connecting', dead: 'No Data' }[cls];

    if (el.btn) {
      var floating = el.btn.classList.contains('tk-floating');
      el.btn.className = 'state-' + cls + (floating ? ' tk-floating' : '');
      var txt = el.btn.querySelector('.tk-text');
      if (txt) txt.textContent = label === 'Live' ? 'Live' : 'Token';
      el.btn.title = s ? (s.message || label) : 'Quotex token status';
    }
    if (!el.modal) return;

    el.statusBox.className = 'tk-status is-' + cls;
    el.badge.className = 'tk-badge is-' + cls;
    el.badge.textContent = label;
    el.msg.textContent = s ? (s.message || '') : 'Checking…';
    el.streams.textContent = s && s.streams ? s.streams + ' streams' : '';

    var stored = (s && s.stored_token) || {};
    var bits = [];
    if (stored.stored) {
      bits.push('saved token ' + (stored.preview || '') + ' · ' + ago(stored.saved_at));
      bits.push(stored.persistent ? 'survives redeploy ✓' : '⚠ NOT on a persistent volume');
    } else {
      bits.push('no token saved yet');
    }
    el.stored.textContent = bits.join(' · ');

    var claimed = state.auth && state.auth.claimed;
    el.claim.hidden = !!claimed;
    el.pinField.hidden = !claimed;
  }

  function result(kind, text) {
    if (!el.result) return;
    el.result.hidden = false;
    el.result.className = 'tk-result ' + kind;
    el.result.textContent = text;
  }

  /* ─── server calls ─────────────────────────────────────────────────────── */

  function refresh() {
    return fetchJSON('/api/token-status')
      .then(function (r) {
        state.status = r.body || null;
        state.auth = (r.body && r.body.auth) || state.auth;
        render();
        return state.status;
      })
      .catch(function () { /* offline — keep last known state */ });
  }

  function claim() {
    var pin = (el.newPin.value || '').trim();
    if (pin.length < 6) { result('err', 'PIN must be at least 6 characters.'); return; }
    el.claimBtn.disabled = true;
    result('busy', 'Setting PIN…');
    fetchJSON('/api/auth/claim', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin: pin })
    }).then(function (r) {
      el.claimBtn.disabled = false;
      if (!r.ok || !r.body.ok) { result('err', (r.body && r.body.error) || 'Could not set PIN.'); return; }
      state.auth = { claimed: true };
      el.pin.value = pin;
      setPin(pin, el.remember.checked);
      el.newPin.value = '';
      result('ok', 'PIN set. Now paste the Quotex token and press Import.');
      render();
      el.token.focus();
    }).catch(function (e) {
      el.claimBtn.disabled = false;
      result('err', 'Network error: ' + e);
    });
  }

  function importToken() {
    var token = (el.token.value || '').trim();
    var pin = (el.pin.value || '').trim();
    if (!token) { result('err', 'Paste the Quotex token first.'); el.token.focus(); return; }
    if (state.auth && !state.auth.claimed) { result('err', 'Set an access PIN first (step ① above).'); return; }
    if (!pin) { result('err', 'Enter your access PIN.'); el.pin.focus(); return; }

    el.importBtn.disabled = true;
    result('busy', 'Sending token to the server…');

    fetchJSON('/api/set-token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-App-Pin': pin },
      body: JSON.stringify({ token: token, pin: pin, source: 'ui' })
    }).then(function (r) {
      el.importBtn.disabled = false;
      var body = r.body || {};
      if (!r.ok || !body.ok) {
        result('err', '❌ ' + (body.error || body.detail || ('HTTP ' + r.status)));
        if (r.status === 403) { setPin('', false); state.auth = body.auth || state.auth; render(); }
        return;
      }
      setPin(pin, el.remember.checked);
      el.token.value = '';
      var note = body.persisted
        ? (body.persistent_storage
            ? 'Saved to the persistent volume — the next redeploy starts live on its own.'
            : '⚠ Saved, but this deployment has no persistent volume — it will be lost on redeploy.')
        : ('⚠ Could not persist: ' + (body.persist_error || 'unknown'));
      var fmt = (body.normalized && body.normalized.input_format) || 'raw';
      result('busy', '✅ Token accepted (' + body.preview + ', format: ' + fmt + ').\n' +
                     note + '\nWaiting for Quotex to authorize…');
      watchUntilLive();
    }).catch(function (e) {
      el.importBtn.disabled = false;
      result('err', 'Network error: ' + e);
    });
  }

  function watchUntilLive() {
    if (state.watching) return;
    state.watching = true;
    var deadline = Date.now() + WATCH_TIMEOUT_MS;

    (function tick() {
      refresh().then(function (s) {
        if (s && s.live) {
          state.watching = false;
          result('ok', '🟢 LIVE — Quotex authorized the token. ' +
                       (s.streams ? s.streams + ' streams running. ' : '') +
                       'Candles are updating and signals will appear as they fire.');
          return;
        }
        if (s && s.token_dead) {
          state.watching = false;
          result('err', '⛔ Quotex rejected this token ' + (s.consecutive_rejects || '') +
                        'x — it is expired or revoked. Grab a fresh one from DevTools ' +
                        'and import again.');
          return;
        }
        if (Date.now() > deadline) {
          state.watching = false;
          result('err', '⏱ Still not authorized after 75s. Status: ' +
                        ((s && s.connection_status) || 'unknown') +
                        '. The token may be expired — try a fresh one.');
          return;
        }
        result('busy', '⏳ Connecting to Quotex… (' +
                       ((state.status && state.status.connection_status) || '…') + ')');
        setTimeout(tick, WATCH_MS);
      });
    })();
  }

  /* ─── open / close ─────────────────────────────────────────────────────── */

  function open() {
    if (!el.modal) return;
    el.modal.hidden = false;
    el.btn.setAttribute('aria-expanded', 'true');
    var saved = getPin();
    if (saved && !el.pin.value) el.pin.value = saved;
    el.remember.checked = !!saved || el.remember.checked;
    refresh();
    setTimeout(function () {
      (state.auth && !state.auth.claimed ? el.newPin : el.token).focus();
    }, 30);
  }

  function close() {
    if (!el.modal) return;
    el.modal.hidden = true;
    el.btn.setAttribute('aria-expanded', 'false');
  }

  /* ─── boot ─────────────────────────────────────────────────────────────── */

  function init() {
    el.btn = buildButton();
    el.modal = buildModal();
    el.statusBox = document.getElementById('tk-status');
    el.badge = document.getElementById('tk-badge');
    el.msg = document.getElementById('tk-msg');
    el.streams = document.getElementById('tk-streams');
    el.stored = document.getElementById('tk-stored');
    el.claim = document.getElementById('tk-claim');
    el.claimBtn = document.getElementById('tk-claim-btn');
    el.newPin = document.getElementById('tk-new-pin');
    el.pinField = document.getElementById('tk-pin-field');
    el.pin = document.getElementById('tk-pin');
    el.remember = document.getElementById('tk-remember');
    el.token = document.getElementById('tk-token');
    el.result = document.getElementById('tk-result');
    el.importBtn = document.getElementById('tk-import');

    el.claimBtn.addEventListener('click', claim);
    el.importBtn.addEventListener('click', importToken);
    document.getElementById('tk-recheck').addEventListener('click', function () {
      result('busy', 'Re-checking…');
      refresh().then(function (s) {
        if (s && s.live) result('ok', '🟢 LIVE — ' + (s.message || ''));
        else result('busy', (s && s.message) || 'No status available.');
      });
    });
    document.getElementById('tk-close').addEventListener('click', close);
    document.getElementById('tk-dismiss').addEventListener('click', close);
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && !el.modal.hidden) close();
    });
    el.token.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) importToken();
    });

    refresh().then(function (s) {
      // Nag exactly once per tab when there is nothing to trade on.
      var broken = s && (s.status === 'no_credentials' || s.token_dead ||
                         (state.auth && !state.auth.claimed && !s.has_token));
      var seen;
      try { seen = sessionStorage.getItem(SEEN_KEY); } catch (_) { seen = null; }
      if (broken && !seen) {
        try { sessionStorage.setItem(SEEN_KEY, '1'); } catch (_) {}
        open();
        result('err', 'No live Quotex data — import a token to start receiving signals.');
      }
    });
    state.pollTimer = setInterval(function () { if (!state.watching) refresh(); }, POLL_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
