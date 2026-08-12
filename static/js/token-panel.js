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
  var NAG_DELAY_MS = 12000;          // grace period before "no live data" nag
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

    // Sit right next to the connection indicator — it reports the same thing
    // (are we live?) and, unlike the end of the row, that spot is never the
    // first thing pushed out of view when the topbar runs out of width.
    var conn = document.querySelector('#topbar-row1 .conn-group');
    if (conn && conn.parentNode) conn.parentNode.insertBefore(btn, conn.nextSibling);
    else {
      var row = document.getElementById('topbar-row1');
      if (row) row.insertBefore(btn, row.firstChild);
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

        /* ── Auto-refresh: the whole point is never needing the box above ── */
        h('details', { class: 'tk-help tk-auto', id: 'tk-auto' }, [
          h('summary', { id: 'tk-auto-summary', text: '🔁 Auto-refresh — checking…' }),
          h('div', { class: 'tk-hint', id: 'tk-auto-detail', text: '' }),
          h('div', { class: 'tk-field' }, [
            h('label', { class: 'tk-label', for: 'tk-cookies', text: 'Session cookies' }),
            h('textarea', {
              class: 'tk-textarea', id: 'tk-cookies', spellcheck: 'false',
              autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off',
              placeholder: 'Paste document.cookie here — must include remember_web_…'
            }),
            h('div', {
              class: 'tk-hint',
              html: 'Log in to Quotex in your browser → <code>F12</code> → ' +
                    '<code>Console</code> → run <code>copy(document.cookie)</code> → ' +
                    'paste here. The app then mints its own token every time the ' +
                    'old one expires, so you never have to touch this again ' +
                    'until you log out of that browser.'
            })
          ]),
          h('div', { class: 'tk-actions' }, [
            h('button', { class: 'tk-btn tk-btn-primary', id: 'tk-cookies-save',
                          type: 'button', text: 'Save cookies & verify' }),
            h('button', { class: 'tk-btn', id: 'tk-refresh-now', type: 'button',
                          text: 'Refresh token now' })
          ])
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
    // No credentials, a token Quotex rejected, or a token that simply is not
    // connecting — from the user's side these are one thing: no live data.
    if (s.token_dead || s.status === 'no_credentials' ||
        s.connection_status === 'disconnected') return 'dead';
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
      // Not live = the button IS the call to action, so it says what to do.
      if (txt) txt.textContent = cls === 'live' ? 'Live'
                              : cls === 'dead' ? 'Set Token' : 'Token';
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

    renderAuto((s && s.auto_session) || null);
  }

  function renderAuto(a) {
    if (!el.autoSummary) return;
    if (!a) {
      el.autoSummary.textContent = '🔁 Auto-refresh — checking…';
      el.autoDetail.textContent = '';
      return;
    }
    var head, detail;
    if (!a.enabled) {
      head = '🔁 Auto-refresh — OFF';
      detail = 'Disabled by QX_AUTO_REFRESH=0. Tokens must be pasted by hand.';
    } else if (!a.configured) {
      head = '🔁 Auto-refresh — not set up';
      detail = 'No session cookies stored, so an expired token still needs a ' +
               'manual paste. Add the cookies below once to fix that for good.';
    } else if (a.consecutive_failures >= 3) {
      head = '🔁 Auto-refresh — FAILING (' + a.consecutive_failures + 'x)';
      detail = 'The stored cookies stopped working: ' + (a.last_error || 'unknown error') +
               '\nLog in to Quotex in your browser again and paste fresh cookies below.';
    } else {
      head = '🔁 Auto-refresh — ARMED';
      detail = 'The app mints its own token when the current one expires' +
               (a.account_email ? ' (account ' + a.account_email + ')' : '') + '. ' +
               (a.refresh_count ? a.refresh_count + ' refresh(es) so far, last ' +
                                  ago(a.last_success) + '. ' : '') +
               (a.persistent ? 'Cookies survive redeploys ✓' :
                               '⚠ Cookies are NOT on a persistent volume.');
      if (a.last_error) detail += '\nLast error: ' + a.last_error;
    }
    el.autoSummary.textContent = head;
    el.autoDetail.textContent = detail;
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

  function requirePin() {
    var pin = (el.pin.value || '').trim();
    if (state.auth && !state.auth.claimed) {
      result('err', 'Set an access PIN first (step ① above).');
      return null;
    }
    if (!pin) { result('err', 'Enter your access PIN.'); el.pin.focus(); return null; }
    return pin;
  }

  function saveCookies() {
    var cookies = (el.cookies.value || '').trim();
    if (!cookies) {
      result('err', 'Paste your Quotex cookies first (document.cookie).');
      el.cookies.focus();
      return;
    }
    var pin = requirePin();
    if (!pin) return;

    el.cookiesSave.disabled = true;
    result('busy', 'Saving cookies and minting a test token…');
    fetchJSON('/api/session/cookies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-App-Pin': pin },
      body: JSON.stringify({ cookies: cookies, pin: pin, source: 'ui' })
    }).then(function (r) {
      el.cookiesSave.disabled = false;
      var body = r.body || {};
      if (!r.ok || !body.ok) {
        result('err', '❌ ' + (body.error || body.detail || ('HTTP ' + r.status)) +
                      (body.hint ? '\n' + body.hint : ''));
        return;
      }
      setPin(pin, el.remember.checked);
      el.cookies.value = '';
      renderAuto(body.status);
      result('busy', '✅ ' + body.message + '\nWaiting for Quotex to authorize…');
      watchUntilLive();
    }).catch(function (e) {
      el.cookiesSave.disabled = false;
      result('err', 'Network error: ' + e);
    });
  }

  function refreshNow() {
    var pin = requirePin();
    if (!pin) return;
    el.refreshNow.disabled = true;
    result('busy', 'Minting a fresh token from the stored cookies…');
    fetchJSON('/api/session/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-App-Pin': pin },
      body: JSON.stringify({ pin: pin })
    }).then(function (r) {
      el.refreshNow.disabled = false;
      var body = r.body || {};
      if (!r.ok || !body.ok) {
        result('err', '❌ ' + (body.error || ('HTTP ' + r.status)));
        renderAuto(body.status);
        return;
      }
      renderAuto(body.status);
      result('busy', '✅ ' + body.message);
      watchUntilLive();
    }).catch(function (e) {
      el.refreshNow.disabled = false;
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
    el.auto = document.getElementById('tk-auto');
    el.autoSummary = document.getElementById('tk-auto-summary');
    el.autoDetail = document.getElementById('tk-auto-detail');
    el.cookies = document.getElementById('tk-cookies');
    el.cookiesSave = document.getElementById('tk-cookies-save');
    el.refreshNow = document.getElementById('tk-refresh-now');

    el.claimBtn.addEventListener('click', claim);
    el.importBtn.addEventListener('click', importToken);
    el.cookiesSave.addEventListener('click', saveCookies);
    el.refreshNow.addEventListener('click', refreshNow);
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

    // Direct link support: .../otc.html#token opens the panel straight away.
    if (location.hash === '#token') { refresh().then(open); return; }

    refresh().then(function (s) {
      if (s && s.live) return;
      // Not live. Could just be the few seconds after a container boot, so
      // re-check before nagging — then open exactly once per tab.
      setTimeout(function () {
        refresh().then(function (s2) {
          if (s2 && s2.live) return;
          // Auto-refresh armed = the app is already fixing this by itself.
          // Popping a "paste a token" dialog at the operator would be asking
          // for work that is about to happen without them.
          var auto = s2 && s2.auto_session;
          if (auto && auto.enabled && auto.configured &&
              auto.consecutive_failures < 3) return;
          var seen;
          try { seen = sessionStorage.getItem(SEEN_KEY); } catch (_) { seen = null; }
          if (seen) return;
          try { sessionStorage.setItem(SEEN_KEY, '1'); } catch (_) {}
          open();
          result('err', 'No live Quotex data right now — paste a fresh token ' +
                        'below, or set up auto-refresh (🔁 section) so this ' +
                        'never happens again.');
        });
      }, NAG_DELAY_MS);
    });
    state.pollTimer = setInterval(function () { if (!state.watching) refresh(); }, POLL_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
