/* ============================================================================
   token-panel.js — 🔑 Token import panel (TOKEN-ONLY EDITION, 2026-08-17).

   USER REQUIREMENT (2026-08-17):
     "টোকেন মেনইয়ালি অ্যাপ ui তে শুধু মাত্র টোকেন দিলেই যেনো অ্যাপ টি লাইভ
      ডেটা কানেক্ট হয়, কোনো এডমিন key, অন্যান্য key এই গুলো fronted এ থাকবে
      না। শুধু মাত্র টোকেন ইমপোর্ট করার ব্যবস্থা থাকবে। আর টোকেন দিলেই
      ডেটা আসবে।"

   Translation: paste a token → app goes live. NO admin key, NO PIN, NO
   other key in the frontend. Only a token import box.

   What was removed vs the previous version:
     • The "set an access PIN" first-run claim section.
     • The "Access PIN" input + "Remember on this device" checkbox.
     • All X-App-Pin / X-Admin-Key headers on outgoing requests.
     • The api-keys.js companion file (removed from app.html).

   What was kept:
     • The token textarea + "Import & Go Live" button (the core flow).
     • The live-status indicator + auto-refresh (cookies) section — useful
       for the operator, but it's an opt-in <details>, not the primary UI.
       Cookies here are imported without a PIN too (server-side gate is
       open by design).
   ============================================================================ */
(function () {
  'use strict';

  var POLL_MS = 20000;               // idle status refresh
  var NAG_DELAY_MS = 12000;          // grace period before "no live data" nag
  var WATCH_MS = 2000;               // post-import status polling
  var WATCH_TIMEOUT_MS = 75000;

  var el = {};
  var state = { status: null, watching: false, pollTimer: null };

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
    var dialog = h('div', { class: 'tk-dialog', role: 'dialog', 'aria-modal': 'true',
                            'aria-labelledby': 'tk-title' }, [
      h('div', { class: 'tk-head' }, [
        h('span', { class: 'tk-title', id: 'tk-title', text: '🔑 Quotex Token' }),
        h('span', { class: 'tk-spacer' }),
        h('button', { class: 'tk-close', id: 'tk-close', type: 'button',
                      'aria-label': 'Close', text: '✕' })
      ]),
      h('div', { class: 'tk-body' }, [
        h('div', { class: 'tk-status' }, [
          h('div', { class: 'tk-status-row' }, [
            h('span', { class: 'tk-badge', id: 'tk-badge', text: '…' }),
            h('span', { class: 'tk-meta', id: 'tk-streams', text: '' })
          ]),
          h('div', { class: 'tk-status-msg', id: 'tk-msg', text: 'Checking live-data status…' }),
          h('div', { class: 'tk-meta', id: 'tk-stored', text: '' })
        ]),
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
                           'extracts the session for you. No PIN, no admin key — ' +
                           'just the token.' })
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
                    'until you log out of that browser. No PIN needed.'
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
    } else if (a.login_blocked) {
      head = '🔁 Auto-refresh — LOGIN BLOCKED';
      var blkAt = a.login_block_detail && a.login_block_detail.blocked_at;
      var blkReason = a.login_block_detail && a.login_block_detail.reason;
      detail = 'Email/password login was permanently blocked after the first ' +
               'failure (Quotex bans accounts that retry). ' +
               (blkReason ? 'Last reason: ' + blkReason + '. ' : '') +
               'Cookie replay still works — import fresh cookies from a ' +
               'browser session to clear the block and re-arm password login.';
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
        render();
        return state.status;
      })
      .catch(function () { /* offline — keep last known state */ });
  }

  function importToken() {
    var token = (el.token.value || '').trim();
    if (!token) { result('err', 'Paste the Quotex token first.'); el.token.focus(); return; }

    el.importBtn.disabled = true;
    result('busy', 'Sending token to the server…');

    // NOTE: NO X-App-Pin / X-Admin-Key header. USER REQ 2026-08-17.
    fetchJSON('/api/set-token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token, source: 'ui' })
    }).then(function (r) {
      el.importBtn.disabled = false;
      var body = r.body || {};
      if (!r.ok || !body.ok) {
        result('err', '❌ ' + (body.error || body.detail || ('HTTP ' + r.status)));
        return;
      }
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

  function saveCookies() {
    var cookies = (el.cookies.value || '').trim();
    if (!cookies) {
      result('err', 'Paste your Quotex cookies first (document.cookie).');
      el.cookies.focus();
      return;
    }

    el.cookiesSave.disabled = true;
    result('busy', 'Saving cookies and minting a test token…');
    // NOTE: NO X-App-Pin / X-Admin-Key header. USER REQ 2026-08-17.
    fetchJSON('/api/session/cookies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cookies: cookies, source: 'ui' })
    }).then(function (r) {
      el.cookiesSave.disabled = false;
      var body = r.body || {};
      if (!r.ok || !body.ok) {
        result('err', '❌ ' + (body.error || body.detail || ('HTTP ' + r.status)) +
                      (body.hint ? '\n' + body.hint : ''));
        return;
      }
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
    el.refreshNow.disabled = true;
    result('busy', 'Minting a fresh token from the stored cookies…');
    // NOTE: NO X-App-Pin / X-Admin-Key header. USER REQ 2026-08-17.
    fetchJSON('/api/session/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
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
    refresh();
    setTimeout(function () { el.token.focus(); }, 30);
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
    el.token = document.getElementById('tk-token');
    el.result = document.getElementById('tk-result');
    el.importBtn = document.getElementById('tk-import');
    el.auto = document.getElementById('tk-auto');
    el.autoSummary = document.getElementById('tk-auto-summary');
    el.autoDetail = document.getElementById('tk-auto-detail');
    el.cookies = document.getElementById('tk-cookies');
    el.cookiesSave = document.getElementById('tk-cookies-save');
    el.refreshNow = document.getElementById('tk-refresh-now');

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

    // Direct link support: .../#token opens the panel straight away.
    if (location.hash === '#token') { refresh().then(open); return; }

    refresh().then(function (s) {
      if (s && s.live) return;
      setTimeout(function () {
        refresh().then(function (s2) {
          if (s2 && s2.live) return;
          var auto = s2 && s2.auto_session;
          if (auto && auto.enabled && auto.configured &&
              auto.consecutive_failures < 3) return;
          // Auto-open the panel once per tab when there's no live data.
          // (Removed the sessionStorage gate so the panel is reachable
          // even on a returning tab — USER REQ: token-only flow.)
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
