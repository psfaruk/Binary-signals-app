/* ============================================================================
   token-panel.js — 🔑 Token import panel (PHASE-J SECURED EDITION, 2026-10).

   HISTORY (USER REQ 2026-08-17): this panel used to be TOKEN-ONLY —
   paste a token, get data, no PIN, no admin key. That open-by-design
   behavior was audited in Phase D as release blocker B3: on a public
   deployment ANYONE with the URL could swap the broker token or push
   session cookies. The owner has now authorized closing it
   (2026-10: "সব গুলো deeply সমাধান করো").

   PHASE-J SECURITY MODEL (fail-closed):
     • Token/cookie pushes now require the operator PIN (X-App-Pin header).
     • First run: the server prints a ONE-TIME SETUP CODE on its console;
       this panel turns it into your PIN (POST /api/auth/claim).
     • The PIN is remembered on this device (localStorage) — day-to-day
       usage stays one paste + one click.
     • The GET /api/set-token?token=... URL form is REMOVED (it leaked
       the full token into access logs); only POST with JSON body + PIN.
   ============================================================================ */
(function () {
  'use strict';

  var POLL_MS = 20000;               // idle status refresh
  var WATCH_MS = 2000;               // post-import status polling
  var WATCH_TIMEOUT_MS = 75000;

  /* ── Auto-open policy (TOKEN-NAG-FIX-2026-09-12) ───────────────────────
     USER REQ: "frontend এ যেখানে টোকেন পেষ্ট করি, সেই উইন্ডো টি বার বার
     খুলে… টোকেন expire হলেই বা ডেটা না আসলেই যেনো এটা ওপেন হয়।"

     The panel opens ONLY in these cases:
       (a) token DEAD (expired / rejected by Quotex) — immediately,
           re-nagged at most once per NAG_RETRY_MS while it stays dead;
       (b) NO token stored at all — once per tab session (sessionStorage),
           after a short grace;
       (c) data does NOT come — a stored, not-dead token that stays
           non-live for NO_DATA_GRACE_MS (transient "Connecting…" NEVER
           opens the panel), at most once per NAG_RETRY_MS.
     Closing an auto-opened panel is remembered for NAG_RETRY_MS so it
     stops popping up on every reload (the old code opened it after just
     12s on EVERY page load — the "বার বার খুলে" complaint).
     Importing a token clears the memory so a REAL later failure re-nags. */
  var NAG_RETRY_MS = 30 * 60 * 1000;      // 30 min between auto-opens
  var NO_DATA_GRACE_MS = 3 * 60 * 1000;   // 3 min sustained no-data
  var NO_TOKEN_GRACE_MS = 12000;          // 12s before the "no token" nag
  var SS_NAG = 'bst_tok_nag_tab';         // once-per-tab-session flag
  var LS_DISMISS = 'bst_tok_nag_dismissed_at';

  var el = {};
  var state = { status: null, watching: false, pollTimer: null,
                autoOpened: false, notLiveSince: 0, lastDeadNag: 0,
                lastNodataNag: 0, authState: null };
  var LS_PIN = 'bst_app_pin';

  function storedPin() {
    try { return localStorage.getItem(LS_PIN) || ''; } catch (_e) { return ''; }
  }

  function rememberPin(pin) {
    try { localStorage.setItem(LS_PIN, pin); } catch (_e) {}
  }

  function currentPin() {
    var typed = (el.pin && el.pin.value || '').trim();
    return typed || storedPin();
  }

  /* PHASE-J: every token-push call carries the operator PIN. */
  function authHeaders(extra) {
    var h = extra || {};
    var pin = currentPin();
    if (pin) h['X-App-Pin'] = pin;
    return h;
  }

  /* 401 handling: reveal the first-run claim section when the server says
     no PIN exists yet (setup_code_required), else prompt for the PIN. */
  function handleAuthFailure(r) {
    var body = (r && r.body) || {};
    var detail = body.detail || body.error || '';
    if (body.setup_code_required || /setup code/i.test(detail)) {
      showClaim('সার্ভারে এখনো PIN সেট হয়নি। সার্ভার কনসোলে প্রিন্ট হওয়া '
              + 'এককালীন SETUP CODE দিয়ে নিচে PIN সেট করুন, তারপর আবার টোকেন '
              + 'দিন।');
      return 'PIN সেট করা দরকার — নিচের সেটআপ সেকশন দেখুন।';
    }
    if (/wrong operator PIN/i.test(detail)) {
      if (el.pin) el.pin.value = '';
      return 'ভুল PIN — আবার সঠিক PIN দিন।';
    }
    return detail || 'অথরাইজেশন প্রয়োজন — X-App-Pin হেডার দিন।';
  }

  function showClaim(msg) {
    if (el.claim) el.claim.hidden = false;
    if (msg && el.result) result('err', msg);
  }

  function claimPin() {
    var pin = (el.claimPin && el.claimPin.value || '').trim();
    var code = (el.setupCode && el.setupCode.value || '').trim();
    if (!pin || !code) {
      result('err', 'SETUP CODE এবং নতুন PIN — দুটোই দিন।');
      return;
    }
    if (pin.length < 6) {
      result('err', 'PIN অন্তত ৬ অক্ষরের হতে হবে।');
      return;
    }
    el.claimBtn.disabled = true;
    result('busy', 'PIN সেট করা হচ্ছে…');
    fetchJSON('/api/auth/claim', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin: pin, setup_code: code })
    }).then(function (r) {
      el.claimBtn.disabled = false;
      var body = r.body || {};
      if (!r.ok || !body.ok) {
        result('err', '❌ ' + (body.error || body.detail || ('HTTP ' + r.status)));
        return;
      }
      rememberPin(pin);
      if (el.pin) el.pin.value = pin;
      if (el.claim) el.claim.hidden = true;
      result('ok', '✅ PIN সেট হয়েছে এবং এই ডিভাইসে মনে রাখা হলো। '
                  + 'এখন টোকেন পেস্ট করে Import চাপুন।');
      refreshAuth();
    }).catch(function (e) {
      el.claimBtn.disabled = false;
      result('err', 'Network error: ' + e);
    });
  }

  function refreshAuth() {
    return fetchJSON('/api/auth/state').then(function (r) {
      if (!r.ok) return null;
      state.authState = r.body || null;
      if (state.authState && state.authState.setup_code_required) {
        showClaim('এই ডিপ্লয়মেন্টে এখনো PIN নেই — সার্ভার কনসোলের '
                + 'এককালীন SETUP CODE দিয়ে PIN সেট করুন।');
      } else if (el.claim) {
        el.claim.hidden = true;
      }
      return state.authState;
    }).catch(function () { return null; });
  }

  function dismissedRecently() {
    try {
      var t = parseInt(localStorage.getItem(LS_DISMISS) || '0', 10);
      return t && (Date.now() - t) < NAG_RETRY_MS;
    } catch (_e) { return false; }
  }

  function clearDismissMemory() {
    try { localStorage.removeItem(LS_DISMISS); } catch (_e) {}
    state.notLiveSince = 0;
    state.lastNodataNag = 0;
  }

  /* Central auto-open decision — runs after EVERY status refresh. */
  function maybeAutoOpen(s) {
    if (!s) return;
    var now = Date.now();
    if (s.live) {                       // healthy — reset everything
      state.notLiveSince = 0;
      return;
    }
    var stored = s.stored_token && s.stored_token.stored;

    // (a) token expired / rejected → open immediately (USER REQ)
    if (s.token_dead) {
      if (now - state.lastDeadNag > NAG_RETRY_MS) {
        state.lastDeadNag = now;
        open(true);
        result('err', '⛔ টোকেন এক্সপায়ার্ড — Quotex আর অথরাইজ করছে না। ' +
                      'নতুন টোকেন পেস্ট করুন।');
      }
      return;
    }

    // (b) no token at all → once per tab session, short grace
    if (!stored) {
      var seen = false;
      try { seen = !!sessionStorage.getItem(SS_NAG); } catch (_e) {}
      if (!seen) {
        try { sessionStorage.setItem(SS_NAG, '1'); } catch (_e) {}
        setTimeout(function () {
          refresh().then(function (s2) {
            if (!s2 || s2.live || s2.token_dead) return; // dead path opened it
            if (s2.stored_token && s2.stored_token.stored) return; // race
            open(true);
            result('err', 'কোনো টোকেন সংরক্ষিত নেই — লাইভ ডেটার জন্য ' +
                          'Quotex টোকেন পেস্ট করুন।');
          });
        }, NO_TOKEN_GRACE_MS);
      }
      return;
    }

    // (c) stored token, still not live → only after a SUSTAINED outage
    if (!state.notLiveSince) state.notLiveSince = now;
    if (now - state.notLiveSince < NO_DATA_GRACE_MS) return; // transient
    if (dismissedRecently()) return;                         // user closed it
    // FIX (FALSE-TOKEN-EXPIRED-2026-09-18 / FT-11 UI): if the backend says
    // this is a transient_disconnect (rejects below the dead threshold),
    // do NOT tell the operator to paste a new token — the token is valid
    // and auto-retry is healing it. Only nag for a real sustained outage.
    if (s.status === 'transient_disconnect'
        && (s.consecutive_rejects || 0) < 3) {
      return;
    }
    if (now - state.lastNodataNag > NAG_RETRY_MS) {
      state.lastNodataNag = now;
      open(true);
      result('err', '⚠ দীর্ঘক্ষণ লাইভ ডেটা আসছে না (' +
                    Math.max(1, Math.round((now - state.notLiveSince) / 60000)) +
                    ' মিনিট)। টোকেন সংরক্ষিত আছে — নতুন টোকেন দিন বা ' +
                    'auto-refresh (🔁) সেট আপ করুন।');
    }
  }

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
    // FIX (AURORA-V3-2026-08-31): the page now ships a static #token-btn in
    // the topbar. Reuse it instead of creating a floating duplicate (which
    // produced duplicate IDs and a stray fixed-position button).
    var existing = document.getElementById('token-btn');
    if (existing) {
      existing.setAttribute('aria-haspopup', 'dialog');
      existing.setAttribute('aria-expanded', 'false');
      existing.setAttribute('aria-label', 'Import Quotex token');
      existing.addEventListener('click', function () { open(); });
      return existing;
    }
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
                           'extracts the session for you.' })
        ]),

        /* ── PHASE-J: operator PIN (required for every push) ── */
        h('div', { class: 'tk-field' }, [
          h('label', { class: 'tk-label', for: 'tk-pin',
                       html: 'Access PIN <span class="tk-req">*</span>' }),
          h('input', {
            class: 'tk-input', id: 'tk-pin', type: 'password',
            autocomplete: 'current-password', spellcheck: 'false',
            placeholder: storedPin() ? '•••••• (saved on this device)' : 'Operator PIN'
          }),
          h('div', { class: 'tk-hint',
                     text: 'PHASE-J: টোকেন/কুকি পুশের জন্য PIN লাগবে। একবার দিলে ' +
                           'এই ডিভাইসে মনে রাখা হয় — তারপর আগের মতোই শুধু ' +
                           'পেস্ট + Import।' })
        ]),

        /* ── PHASE-J: first-run claim (server console setup code) ── */
        h('div', { class: 'tk-field tk-claim', id: 'tk-claim', hidden: 'hidden' }, [
          h('div', { class: 'tk-hint tk-claim-note',
                     text: 'সার্ভারে এখনো PIN নেই। সার্ভার কনসোলে (লগ) প্রিন্ট হওয়া ' +
                           'এককালীন SETUP CODE দিয়ে নিচে PIN সেট করুন।' }),
          h('input', {
            class: 'tk-input', id: 'tk-setup-code', type: 'text',
            autocomplete: 'off', spellcheck: 'false',
            placeholder: 'Setup code (e.g. A1B2-C3D4-E5F6)'
          }),
          h('input', {
            class: 'tk-input', id: 'tk-claim-pin', type: 'password',
            autocomplete: 'new-password',
            placeholder: 'New PIN (min 6 chars)'
          }),
          h('button', { class: 'tk-btn tk-btn-primary', id: 'tk-claim-btn',
                        type: 'button', text: 'PIN সেট করুন' })
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
      // FIX (AURORA-V3-2026-08-31): toggle state classes instead of clobbering
      // className — the static Aurora topbar button carries its own design
      // classes (.icon-btn .token-btn) that must survive state updates.
      el.btn.classList.remove('state-live', 'state-wait', 'state-dead');
      el.btn.classList.add('state-' + cls);
      if (floating) el.btn.classList.add('tk-floating');
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

    // NOTE (PHASE-J 2026-10): token pushes now carry X-App-Pin — the
    // open-by-design USER REQ 2026-08-17 was closed by owner authorization
    // (see file header). authHeaders() attaches the PIN.
    clearDismissMemory();               // a fresh token re-arms the nagging
    fetchJSON('/api/set-token', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ token: token, source: 'ui' })
    }).then(function (r) {
      el.importBtn.disabled = false;
      var body = r.body || {};
      if (!r.ok) {
        result('err', '❌ ' + handleAuthFailure(r));
        return;
      }
      if (!body.ok) {
        result('err', '❌ ' + (body.error || body.detail || ('HTTP ' + r.status)));
        return;
      }
      if (currentPin()) rememberPin(currentPin());
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
    // NOTE (PHASE-J 2026-10): cookies import now carries X-App-Pin.
    fetchJSON('/api/session/cookies', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ cookies: cookies, source: 'ui' })
    }).then(function (r) {
      el.cookiesSave.disabled = false;
      var body = r.body || {};
      if (!r.ok) {
        result('err', '❌ ' + handleAuthFailure(r));
        return;
      }
      if (!body.ok) {
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
    // NOTE (PHASE-J 2026-10): session refresh now carries X-App-Pin.
    fetchJSON('/api/session/refresh', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({})
    }).then(function (r) {
      el.refreshNow.disabled = false;
      var body = r.body || {};
      if (!r.ok) {
        result('err', '❌ ' + handleAuthFailure(r));
        return;
      }
      if (!body.ok) {
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

  function open(auto) {
    if (!el.modal) return;
    el.modal.hidden = false;
    // auto=true marks an AUTO-OPENED panel: closing it now records a
    // dismissal so the policy stops re-opening (manual opens never do).
    state.autoOpened = auto === true;
    // FIX (AURORA-V3-2026-08-31): expose for app-nav.js openTokenPanel() so
    // BOTH the static topbar button and the sidebar "টোকেন ইমপোর্ট" button
    // open this same canonical modal.
    try { window.__tokenPanelOpen = open; } catch (_e) {}
    el.btn.setAttribute('aria-expanded', 'true');
    refresh();
    refreshAuth();          // PHASE-J: show claim section on fresh deploys
    setTimeout(function () { el.token.focus(); }, 30);
  }

  function close() {
    if (!el.modal) return;
    el.modal.hidden = true;
    el.btn.setAttribute('aria-expanded', 'false');
    if (state.autoOpened) {
      state.autoOpened = false;
      try { localStorage.setItem(LS_DISMISS, String(Date.now())); }
      catch (_e) {}
    }
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
    /* PHASE-J: PIN + first-run claim wiring */
    el.pin = document.getElementById('tk-pin');
    el.claim = document.getElementById('tk-claim');
    el.setupCode = document.getElementById('tk-setup-code');
    el.claimPin = document.getElementById('tk-claim-pin');
    el.claimBtn = document.getElementById('tk-claim-btn');
    if (el.claimBtn) el.claimBtn.addEventListener('click', claimPin);
    if (el.claimPin) el.claimPin.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') claimPin();
    });
    el.auto = document.getElementById('tk-auto');
    el.autoSummary = document.getElementById('tk-auto-summary');
    el.autoDetail = document.getElementById('tk-auto-detail');
    el.cookies = document.getElementById('tk-cookies');
    el.cookiesSave = document.getElementById('tk-cookies-save');
    el.refreshNow = document.getElementById('tk-refresh-now');

    el.importBtn.addEventListener('click', importToken);
    el.cookiesSave.addEventListener('click', saveCookies);
    el.refreshNow.addEventListener('click', refreshNow);
    // FIX (TOKEN-OPEN-EXPOSURE-2026-09-07, HIGH): __tokenPanelOpen was only
    // assigned INSIDE open() — i.e. it existed only AFTER the first open,
    // so the Settings "টোকেন ইমপোর্ট" button (app-nav.js openTokenPanel)
    // could never find it on first click and fell back to a dead static
    // modal. Expose it as soon as the panel is built, and listen for the
    // legacy 'bst-open-token-panel' event app-nav.js dispatches as fallback.
    try { window.__tokenPanelOpen = open; } catch (_e) {}
    document.addEventListener('bst-open-token-panel', function () { open(); });
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
    // (open() takes an `auto` flag — swallow the status arg so this counts
    // as a MANUAL open, not an auto-open.)
    if (location.hash === '#token') { refresh().then(function () { open(); }); return; }

    // TOKEN-NAG-FIX-2026-09-12: ALL auto-open decisions flow through
    // maybeAutoOpen() — the panel no longer pops up after every reload.
    refresh().then(maybeAutoOpen);
    state.pollTimer = setInterval(function () {
      if (state.watching) return;   // import watcher is polling already
      refresh().then(maybeAutoOpen);
    }, POLL_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
