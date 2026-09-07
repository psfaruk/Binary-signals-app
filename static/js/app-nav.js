/* app-nav.js — Navigation + Settings for the consolidated Binary Signals app.
 *
 * UI-FIX-2026-08-13 (v2):
 *   • Tab switching is now done by common.js's switchTab() — app-nav.js
 *     only handles the Settings tab (token panel, preferences).
 *   • Share signal table is owned by common.js (lines 2743-2927).
 *   • History list is owned by common.js (renderHistory).
 *   • This file ONLY handles: Settings tab interactions, token panel
 *     modal, Home tab data refresh, and preference persistence.
 *
 * TOKEN-ONLY EDITION (2026-08-17):
 *   • Removed all API key CRUD UI (loadApiKeys, createApiKey, revoke).
 *   • Removed all references to /api/keys endpoints.
 *   • The api-keys.js companion file was deleted; app.html no longer
 *     loads it. USER REQ: "কোনো এডমিন key, অন্যান্য key এই গুলো fronted এ
 *     থাকবে না" — no admin/other keys on the frontend.
 */

(function() {
    'use strict';

    const STORAGE_KEY = 'bst_prefs_v2';

    function loadPrefs() {
        try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); }
        catch (e) { return {}; }
    }
    function savePrefs(prefs) {
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs)); }
        catch (e) { /* ignore */ }
    }
    let prefs = loadPrefs();

    // ── Home tab data ────────────────────────────────────────────────────
    async function loadHomeData() {
        try {
            const res = await fetch('/api/share-signals');
            if (!res.ok) return;
            const data = await res.json();

            const liveEl = document.getElementById('home-live-pairs');
            if (liveEl) liveEl.textContent = data.live_pairs + ' / ' + data.total_pairs;

            const rows = data.rows || [];
            const callCount = rows.filter(r => r.signal === 'CALL').length;
            const putCount = rows.filter(r => r.signal === 'PUT').length;
            const strongCount = rows.filter(r => r.strength === 'STRONG').length;
            const directionalCount = rows.filter(r => r.signal === 'CALL' || r.signal === 'PUT').length;

            const todaySigs = document.getElementById('home-today-signals');
            if (todaySigs) todaySigs.textContent = directionalCount;
            const todayCalls = document.getElementById('home-today-calls');
            if (todayCalls) todayCalls.textContent = 'CALL: ' + callCount + ' / PUT: ' + putCount;
            const strongEl = document.getElementById('home-strong-count');
            if (strongEl) strongEl.textContent = strongCount;

            // Top signals sorted by confidence
            const top = rows
                .filter(r => r.signal === 'CALL' || r.signal === 'PUT')
                .sort((a, b) => (b.confidence || 0) - (a.confidence || 0))
                .slice(0, 10);

            const tbody = document.getElementById('home-signals-tbody');
            if (tbody) {
                if (top.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="loading-row">No active signals yet</td></tr>';
                } else {
                    tbody.innerHTML = top.map(r => {
                        const sigClass = r.signal === 'CALL' ? 'ss-signal-call' :
                                          r.signal === 'PUT' ? 'ss-signal-put' : 'ss-signal-neutral';
                        const strClass = r.strength === 'STRONG' ? 'ss-str-strong' :
                                          r.strength === 'MEDIUM' ? 'ss-str-medium' : 'ss-str-weak';
                        const typeClass = r.type === 'OTC' ? 'ss-type-otc' : 'ss-type-real';
                        const updated = r.last_update != null ? Math.round(r.last_update) + 's ago' : '—';
                        return '<tr>' +
                            '<td>' + r.pair + '</td>' +
                            '<td class="' + typeClass + '">' + r.type + '</td>' +
                            '<td class="' + sigClass + '">' + r.signal + '</td>' +
                            '<td>' + (r.confidence > 0 ? Math.round(r.confidence) + '%' : '—') + '</td>' +
                            '<td class="' + strClass + '">' + (r.strength || '—') + '</td>' +
                            '<td>' + updated + '</td>' +
                            '</tr>';
                    }).join('');
                }
            }
        } catch (e) { console.error('loadHomeData:', e); }

        // Win rate from /api/stats
        // FIX (HOME-WINRATE-2026-09-07): the server returns
        // `overall_win_pct` (core/stats.py). The old code read
        // `stats.win_rate || stats.recent_accuracy` — keys that did not
        // exist — so the Home win-rate card showed "—%" forever. Now we
        // read the real key (with the aliases as fallbacks).
        try {
            const res = await fetch('/api/stats');
            if (res.ok) {
                const stats = await res.json();
                const wr = stats.overall_win_pct ?? stats.win_rate ?? stats.recent_accuracy;
                if (wr != null) {
                    const wrEl = document.getElementById('home-winrate');
                    if (wrEl) wrEl.textContent = Math.round(wr) + '%';
                }
            }
        } catch (e) { /* ignore */ }
    }

    // ── Settings: API Keys section was REMOVED (USER REQ 2026-08-17).
    //    Token import is now the only auth surface on the frontend —
    //    see static/js/token-panel.js. The backend /api/keys endpoints
    //    are still mounted for programmatic clients but no longer reachable
    //    from the UI.

    // ── Settings: Token status ──────────────────────────────────────────
    async function loadTokenStatus() {
        try {
            const res = await fetch('/api/token-status');
            if (!res.ok) return;
            const data = await res.json();
            const icon = document.getElementById('token-status-icon');
            const label = document.getElementById('token-status-label');
            const sub = document.getElementById('token-status-sub');
            if (!icon || !label) return;
            // FIX (TOKEN-STATUS-KEYS-2026-09-07, MEDIUM): the old code read
            // data.has_token / data.active / data.expires_at — keys the
            // endpoint never returns (it sends live / token_dead / message /
            // stored_token), so the row could never reflect reality. The
            // function itself was also never invoked (now called from init()
            // and exposed as window._refreshTokenStatus for common.js).
            const live = !!data.live;
            const dead = !!data.token_dead;
            const stored = data.stored_token && data.stored_token.stored;
            if (live) {
                icon.textContent = '●';
                icon.className = 'token-status-icon ok';
                label.textContent = 'টোকেন লাইভ — ডেটা আসছে';
                sub.textContent = data.message
                    || ('Streams: ' + (data.streams != null ? data.streams : '—'));
            } else if (dead) {
                icon.textContent = '●';
                icon.className = 'token-status-icon warn';
                label.textContent = 'টোকেন এক্সপায়ার্ড';
                sub.textContent = data.message || 'নতুন টোকেন ইমপোর্ট করুন।';
            } else if (stored) {
                icon.textContent = '●';
                icon.className = 'token-status-icon warn';
                label.textContent = 'টোকেন সংরক্ষিত — কানেকশন চেষ্টারত';
                sub.textContent = data.message || 'কিছুক্ষণ অপেক্ষা করুন।';
            } else {
                icon.textContent = '●';
                icon.className = 'token-status-icon warn';
                label.textContent = 'কোনো টোকেন নেই';
                sub.textContent = '"টোকেন ইমপোর্ট" চেপে টোকেন দিন।';
            }
        } catch (e) { /* ignore */ }
    }

    function openTokenPanel() {
        // FIX (AURORA-V3-2026-08-31): token-panel.js owns a fully-featured
        // dynamic modal and binds the static #token-btn itself. Prefer its
        // open() — exposed at init since TOKEN-OPEN-EXPOSURE-2026-09-07.
        if (typeof window.__tokenPanelOpen === 'function') {
            window.__tokenPanelOpen();
            return;
        }
        // token-panel.js may still be building its modal (DOMContentLoaded
        // race) — retry briefly, then ask it via the event it listens to.
        var tries = 0;
        var iv = setInterval(function () {
            tries++;
            if (typeof window.__tokenPanelOpen === 'function') {
                clearInterval(iv);
                window.__tokenPanelOpen();
            } else if (tries >= 10) {
                clearInterval(iv);
                window.dispatchEvent(new CustomEvent('bst-open-token-panel'));
            }
        }, 100);
    }

    // ── Preferences ─────────────────────────────────────────────────────
    // FIX (PREFS-WIRING-2026-09-07, HIGH): the four switches persisted their
    // values but nothing consumed them — the sound checkbox said ON while
    // the app stayed muted. Each change now drives the matching behavior in
    // common.js (window.__setSoundEnabled / __setShareAutoRefresh /
    // __setShowWeak), and the initial state is applied on load too. The
    // default-market preference is consumed by the boot() script in
    // app.html (localStorage bst_prefs_v2 → prefs.defaultMarket).
    function initPreferences() {
        const soundEl = document.getElementById('pref-sound');
        const autoEl = document.getElementById('pref-autorefresh');
        const weakEl = document.getElementById('pref-show-weak');
        const mktEl = document.getElementById('pref-default-market');

        if (soundEl) {
            soundEl.checked = prefs.sound !== false;
            if (typeof window.__setSoundEnabled === 'function') {
                window.__setSoundEnabled(soundEl.checked);
            }
            soundEl.addEventListener('change', () => {
                prefs.sound = soundEl.checked;
                savePrefs(prefs);
                if (typeof window.__setSoundEnabled === 'function') {
                    window.__setSoundEnabled(soundEl.checked);
                }
            });
        }
        if (autoEl) {
            autoEl.checked = prefs.autoRefresh !== false;
            if (typeof window.__setShareAutoRefresh === 'function') {
                window.__setShareAutoRefresh(autoEl.checked);
            }
            autoEl.addEventListener('change', () => {
                prefs.autoRefresh = autoEl.checked;
                savePrefs(prefs);
                if (typeof window.__setShareAutoRefresh === 'function') {
                    window.__setShareAutoRefresh(autoEl.checked);
                }
            });
        }
        if (weakEl) {
            weakEl.checked = prefs.showWeak !== false;
            if (typeof window.__setShowWeak === 'function') {
                window.__setShowWeak(weakEl.checked);
            }
            weakEl.addEventListener('change', () => {
                prefs.showWeak = weakEl.checked;
                savePrefs(prefs);
                if (typeof window.__setShowWeak === 'function') {
                    window.__setShowWeak(weakEl.checked);
                }
            });
        }
        if (mktEl) {
            mktEl.value = prefs.defaultMarket || 'otc';
            mktEl.addEventListener('change', () => {
                prefs.defaultMarket = mktEl.value;
                savePrefs(prefs);
            });
        }
    }

    // ── Click delegation (lightweight — avoids duplicate handlers) ─────
    document.addEventListener('click', function(e) {
        // Market dropdown toggle (mobile)
        var dropdownToggle = e.target.closest('#mobile-menu-toggle');
        if (dropdownToggle) {
            e.preventDefault();
            e.stopPropagation();
            var menu = document.getElementById('mkt-dropdown-menu');
            if (menu) {
                menu.hidden = !menu.hidden;
                dropdownToggle.setAttribute('aria-expanded', String(!menu.hidden));
            }
            return;
        }
        // Market dropdown item click
        var dropdownItem = e.target.closest('.mkt-dropdown-item');
        if (dropdownItem) {
            e.preventDefault();
            var mkt = dropdownItem.dataset.mkt;
            var menu = document.getElementById('mkt-dropdown-menu');
            if (menu) menu.hidden = true;
            if (mkt && typeof window.setCategory === 'function') {
                window.setCategory(mkt);
            }
            return;
        }
        // Close dropdown when clicking outside
        var dropdown = document.querySelector('.mkt-dropdown');
        if (dropdown && !dropdown.contains(e.target)) {
            var menu = document.getElementById('mkt-dropdown-menu');
            if (menu) menu.hidden = true;
            var toggle = document.getElementById('mobile-menu-toggle');
            if (toggle) toggle.setAttribute('aria-expanded', 'false');
        }

        // Home refresh
        if (e.target.closest('#home-refresh-btn')) {
            e.preventDefault();
            loadHomeData();
            return;
        }
        // FIX (DEAD-BRANCHES-2026-09-07): the #token-panel-close branch was
        // removed — the static #token-panel-modal it referenced no longer
        // exists (token-panel.js builds its own #token-modal).
        // Token panel open (Settings row + topbar 🔑 button)
        if (e.target.closest('#token-manage-btn') || e.target.closest('#token-btn')) {
            e.preventDefault();
            openTokenPanel();
            return;
        }
        // Signal detail close — FIX (DETAIL-CLOSE-ID-2026-09-07): the real id
        // is #detail-close (app.html); #signal-detail-close never matched, so
        // this branch was dead. Also close via the .show class common.js uses
        // (the hidden attribute alone doesn't hide a .modal-overlay.show).
        var detailClose = e.target.closest('#detail-close');
        if (detailClose) {
            e.preventDefault();
            var overlay = document.getElementById('signal-detail-overlay');
            if (overlay) overlay.classList.remove('show');
            var app = document.getElementById('app');
            if (app) app.removeAttribute('inert');
            return;
        }
        // FIX (DOUBLE-SETCATEGORY-2026-09-07, LOW): the .mkt-btn branch was
        // removed — common.js wireEvents() already binds every .mkt-btn, so
        // both handlers fired per click and the market switch ran TWICE
        // (double WS teardown + double navigation).
    });

    // ── Home tab auto-refresh ───────────────────────────────────────────
    var homeInterval = null;
    function startHomeAutoRefresh() {
        if (homeInterval) clearInterval(homeInterval);
        homeInterval = setInterval(function() {
            // Only refresh if Home tab is active
            var homePane = document.getElementById('pane-home');
            if (homePane && homePane.classList.contains('active')) {
                loadHomeData();
            }
        }, 15000);
    }

    // ── Init ────────────────────────────────────────────────────────────
    function init() {
        initPreferences();
        loadHomeData();
        startHomeAutoRefresh();
        // FIX (TOKEN-STATUS-NEVER-CALLED-2026-09-07, MEDIUM): loadTokenStatus
        // existed but was never invoked — the Settings token row showed
        // "চেক হচ্ছে…" forever. Run it at startup; common.js switchTab
        // re-runs it whenever the Settings tab opens.
        loadTokenStatus();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Expose for external use
    window.bstRefreshHome = loadHomeData;
    window.bstLoadTokenStatus = loadTokenStatus;
    // common.js switchTab('setting') calls this to refresh the token row.
    window._refreshTokenStatus = loadTokenStatus;
})();
