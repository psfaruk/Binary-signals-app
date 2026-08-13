/* app-nav.js — Navigation + Settings for the consolidated Binary Signals app.
 *
 * UI-FIX-2026-08-13 (v2):
 *   • Tab switching is now done by common.js's switchTab() — app-nav.js
 *     only handles the Settings tab (API keys, preferences, token panel).
 *   • Share signal table is owned by common.js (lines 2743-2927).
 *   • History list is owned by common.js (renderHistory).
 *   • This file ONLY handles: Settings tab interactions, API key CRUD UI,
 *     Home tab data refresh, token panel modal, and preference persistence.
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
        try {
            const res = await fetch('/api/stats');
            if (res.ok) {
                const stats = await res.json();
                const wr = stats.win_rate || stats.recent_accuracy;
                if (wr != null) {
                    const wrEl = document.getElementById('home-winrate');
                    if (wrEl) wrEl.textContent = Math.round(wr) + '%';
                }
            }
        } catch (e) { /* ignore */ }
    }

    // ── Settings: API Keys ──────────────────────────────────────────────
    async function loadApiKeys() {
        const list = document.getElementById('api-key-list');
        if (!list) return;
        try {
            const res = await fetch('/api/keys');
            if (res.status === 401) {
                list.innerHTML = '<div class="loading-row">Admin auth required: set X-Admin-Key env var or claim a PIN via the Token panel.</div>';
                return;
            }
            if (!res.ok) throw new Error('fetch failed');
            const data = await res.json();
            if (!data.keys || data.keys.length === 0) {
                list.innerHTML = '<div class="loading-row">No API keys yet. Create one above.</div>';
                return;
            }
            list.innerHTML = data.keys.map(k => {
                const created = new Date(k.created * 1000).toLocaleDateString();
                const lastUsed = k.last_used ? new Date(k.last_used * 1000).toLocaleString() : 'never';
                const inactive = k.active ? '' : 'key-inactive';
                const status = k.active ? 'Active' : 'Revoked';
                return '<div class="api-key-row ' + inactive + '">' +
                    '<div><div class="key-prefix">' + k.key_prefix + '…</div>' +
                    '<div class="key-label">' + k.label + '</div></div>' +
                    '<div class="key-stats"><div>' + status + '</div>' +
                    '<div>Created: ' + created + '</div>' +
                    '<div>Last used: ' + lastUsed + '</div>' +
                    '<div>Requests: ' + k.total_requests + '</div></div>' +
                    '<div class="key-stats">Rate: ' + k.rate_limit_per_min + '/min</div>' +
                    (k.active ? '<button class="key-revoke" data-key-id="' + k.id + '" type="button">Revoke</button>' : '<span></span>') +
                    '</div>';
            }).join('');
        } catch (e) {
            list.innerHTML = '<div class="loading-row">Error: ' + e.message + '</div>';
        }
    }

    async function createApiKey() {
        const label = (document.getElementById('api-key-label') || {}).value || '';
        const rateLimit = parseInt((document.getElementById('api-key-ratelimit') || {}).value || '60', 10);
        if (!label.trim()) { alert('Label is required.'); return; }

        let headers = { 'Content-Type': 'application/json' };
        const adminKey = localStorage.getItem('bst_admin_key');
        if (adminKey) headers['X-Admin-Key'] = adminKey;
        const pin = localStorage.getItem('qxTokenPin');
        if (pin) headers['X-App-Pin'] = pin;

        try {
            const res = await fetch('/api/keys', {
                method: 'POST', headers,
                body: JSON.stringify({ label: label.trim(), rate_limit_per_min: rateLimit }),
            });
            if (res.status === 401) {
                alert('Admin auth required. Set X-Admin-Key env var or claim a PIN.');
                return;
            }
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || 'create failed');
            }
            const data = await res.json();
            const raw = document.getElementById('api-key-raw');
            const info = document.getElementById('api-key-info-box');
            const modal = document.getElementById('api-key-modal');
            if (raw) raw.textContent = data.raw_key;
            if (info) {
                info.innerHTML = '<div><strong>Label:</strong> ' + data.info.label + '</div>' +
                    '<div><strong>Prefix:</strong> ' + data.info.key_prefix + '…</div>' +
                    '<div><strong>Rate limit:</strong> ' + data.info.rate_limit_per_min + '/min</div>';
            }
            if (modal) modal.hidden = false;
            const labelInput = document.getElementById('api-key-label');
            if (labelInput) labelInput.value = '';
            loadApiKeys();
        } catch (e) {
            alert('Create failed: ' + e.message);
        }
    }

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
            if (data.has_token || data.active) {
                icon.textContent = '●';
                icon.className = 'token-status-icon ok';
                label.textContent = 'Token active';
                sub.textContent = data.expires_at
                    ? 'Expires: ' + new Date(data.expires_at).toLocaleString()
                    : 'Live session';
            } else {
                icon.textContent = '●';
                icon.className = 'token-status-icon warn';
                label.textContent = 'No active token';
                sub.textContent = 'Click "Manage token" to import one.';
            }
        } catch (e) { /* ignore */ }
    }

    function openTokenPanel() {
        const modal = document.getElementById('token-panel-modal');
        const body = document.getElementById('token-panel-body');
        if (!modal || !body) return;
        body.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-muted)">Loading token panel…</div>';
        modal.hidden = false;
        setTimeout(() => {
            window.dispatchEvent(new CustomEvent('bst-open-token-panel', { detail: { container: body } }));
        }, 50);
    }

    // ── Preferences ─────────────────────────────────────────────────────
    function initPreferences() {
        const soundEl = document.getElementById('pref-sound');
        const autoEl = document.getElementById('pref-autorefresh');
        const weakEl = document.getElementById('pref-show-weak');
        const mktEl = document.getElementById('pref-default-market');

        if (soundEl) {
            soundEl.checked = prefs.sound !== false;
            soundEl.addEventListener('change', () => {
                prefs.sound = soundEl.checked;
                savePrefs(prefs);
            });
        }
        if (autoEl) {
            autoEl.checked = prefs.autoRefresh !== false;
            autoEl.addEventListener('change', () => {
                prefs.autoRefresh = autoEl.checked;
                savePrefs(prefs);
            });
        }
        if (weakEl) {
            weakEl.checked = prefs.showWeak !== false;
            weakEl.addEventListener('change', () => {
                prefs.showWeak = weakEl.checked;
                savePrefs(prefs);
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
        // API key create
        if (e.target.closest('#api-key-create-btn')) {
            e.preventDefault();
            createApiKey();
            return;
        }
        // API key revoke
        var revokeBtn = e.target.closest('.key-revoke');
        if (revokeBtn) {
            e.preventDefault();
            var keyId = revokeBtn.dataset.keyId;
            if (!confirm('Revoke this API key? This cannot be undone.')) return;
            fetch('/api/keys/' + keyId, { method: 'DELETE' })
                .then(r => r.ok ? r.json() : Promise.reject(new Error('revoke failed: ' + r.status)))
                .then(() => { alert('Key revoked.'); loadApiKeys(); })
                .catch(e => alert('Revoke failed: ' + e.message));
            return;
        }
        // API key copy
        if (e.target.closest('#api-key-copy-btn')) {
            e.preventDefault();
            var raw = document.getElementById('api-key-raw');
            if (raw && navigator.clipboard) {
                navigator.clipboard.writeText(raw.textContent).then(() => alert('Copied.'));
            }
            return;
        }
        // Modals close
        if (e.target.closest('#api-key-modal-close')) {
            e.preventDefault();
            var m = document.getElementById('api-key-modal');
            if (m) m.hidden = true;
            return;
        }
        if (e.target.closest('#token-panel-close')) {
            e.preventDefault();
            var m = document.getElementById('token-panel-modal');
            if (m) m.hidden = true;
            return;
        }
        // Token panel open
        if (e.target.closest('#token-manage-btn') || e.target.closest('#token-btn')) {
            e.preventDefault();
            openTokenPanel();
            return;
        }
        // Signal detail close
        var detailClose = e.target.closest('#signal-detail-close');
        if (detailClose) {
            e.preventDefault();
            var overlay = document.getElementById('signal-detail-overlay');
            if (overlay) overlay.hidden = true;
            var app = document.getElementById('app');
            if (app) app.removeAttribute('inert');
            return;
        }
        // Market switcher buttons (sidebar)
        var mktBtn = e.target.closest('.mkt-btn');
        if (mktBtn) {
            e.preventDefault();
            var mkt = mktBtn.dataset.mkt;
            if (mkt && typeof window.setCategory === 'function') {
                window.setCategory(mkt);
            }
            return;
        }
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
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Expose for external use
    window.bstRefreshHome = loadHomeData;
    window.bstLoadApiKeys = loadApiKeys;
    window.bstLoadTokenStatus = loadTokenStatus;
})();
