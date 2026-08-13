/* app-nav.js — Navigation + tab switching for the consolidated Binary Signals app.
 *
 * PHASE-4-FIX (2026-08-13):
 *   • 4 tabs: Home, Chart Signal, History, Setting
 *   • Mobile (<1024px): bottom navigation bar
 *   • Desktop (≥1024px): left sidebar
 *   • Both share the same data-tab attribute, so one handler covers both.
 *   • Also handles: market switcher, share-signal filters, history tabs,
 *     settings (API keys, preferences), token panel modal.
 */

(function() {
    'use strict';

    const STORAGE_KEY = 'bst_prefs_v2';

    // ── Preferences ──────────────────────────────────────────────────────
    function loadPrefs() {
        try {
            return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
        } catch (e) {
            return {};
        }
    }

    function savePrefs(prefs) {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
        } catch (e) { /* ignore */ }
    }

    let prefs = loadPrefs();

    // ── Tab switching ────────────────────────────────────────────────────
    function switchTab(tabName) {
        // Update body attribute
        document.body.setAttribute('data-tab', tabName);

        // Update sidebar nav items
        document.querySelectorAll('.nav-item').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === tabName);
        });

        // Update bottom nav items
        document.querySelectorAll('.bn-item').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === tabName);
        });

        // Show/hide tab panes
        document.querySelectorAll('.tab-pane').forEach(pane => {
            pane.classList.toggle('active', pane.dataset.pane === tabName);
        });

        // Persist active tab
        prefs.activeTab = tabName;
        savePrefs(prefs);

        // Trigger tab-specific loaders
        if (tabName === 'home') loadHomeData();
        if (tabName === 'history') loadHistoryData();
        if (tabName === 'setting') loadSettingsData();

        // Notify common.js that the chart may need a resize
        if (tabName === 'chart' && typeof window.dispatchEvent === 'function') {
            setTimeout(() => window.dispatchEvent(new Event('resize')), 100);
        }
    }

    // Wire up all nav buttons (sidebar + bottom nav)
    document.addEventListener('click', (e) => {
        const navBtn = e.target.closest('[data-tab]');
        if (navBtn && (navBtn.classList.contains('nav-item') || navBtn.classList.contains('bn-item'))) {
            e.preventDefault();
            switchTab(navBtn.dataset.tab);
        }
    });

    // ── Market switcher ──────────────────────────────────────────────────
    function setMarket(market) {
        if (!['otc', 'real', 'alltime_otc'].includes(market)) return;
        localStorage.setItem('marketCategory', market);
        // Reload the page with the new market — common.js will pick it up
        const url = new URL(window.location.href);
        url.searchParams.set('market', market);
        window.location.href = url.toString();
    }

    document.addEventListener('click', (e) => {
        const mktBtn = e.target.closest('.mkt-btn');
        if (mktBtn) {
            e.preventDefault();
            setMarket(mktBtn.dataset.mkt);
        }
    });

    // Highlight current market in sidebar
    function highlightCurrentMarket() {
        const current = document.body.getAttribute('data-category') || 'otc';
        document.querySelectorAll('.mkt-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.mkt === current);
        });
    }

    // ── Home tab data ────────────────────────────────────────────────────
    async function loadHomeData() {
        // Fetch share-signals for the home summary
        try {
            const res = await fetch('/api/share-signals');
            if (!res.ok) throw new Error('fetch failed');
            const data = await res.json();

            // Update stats
            const liveEl = document.getElementById('home-live-pairs');
            if (liveEl) liveEl.textContent = `${data.live_pairs} / ${data.total_pairs}`;

            // Count signals
            const rows = data.rows || [];
            const callCount = rows.filter(r => r.signal === 'CALL').length;
            const putCount = rows.filter(r => r.signal === 'PUT').length;
            const strongCount = rows.filter(r => r.strength === 'STRONG').length;

            const todaySigs = document.getElementById('home-today-signals');
            if (todaySigs) todaySigs.textContent = rows.filter(r => r.signal !== '—').length;

            const todayCalls = document.getElementById('home-today-calls');
            if (todayCalls) todayCalls.textContent = `CALL: ${callCount} / PUT: ${putCount}`;

            const strongEl = document.getElementById('home-strong-count');
            if (strongEl) strongEl.textContent = strongCount;

            // Render top signals (sort by confidence desc, take top 10)
            const top = rows
                .filter(r => r.signal !== '—' && r.signal !== 'NEUTRAL')
                .sort((a, b) => (b.confidence || 0) - (a.confidence || 0))
                .slice(0, 10);

            const tbody = document.getElementById('home-signals-tbody');
            if (tbody) {
                if (top.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="loading-row">No active signals yet</td></tr>';
                } else {
                    tbody.innerHTML = top.map(r => renderHomeRow(r)).join('');
                }
            }
        } catch (e) {
            console.error('loadHomeData error:', e);
        }

        // Fetch win rate from /api/stats
        try {
            const res = await fetch('/api/stats');
            if (res.ok) {
                const stats = await res.json();
                const wr = stats.win_rate || stats.recent_accuracy;
                if (wr != null) {
                    const wrEl = document.getElementById('home-winrate');
                    if (wrEl) wrEl.textContent = `${Math.round(wr)}%`;
                }
            }
        } catch (e) { /* ignore */ }
    }

    function renderHomeRow(r) {
        const sigClass = r.signal === 'CALL' ? 'ss-signal-call' :
                          r.signal === 'PUT' ? 'ss-signal-put' : 'ss-signal-neutral';
        const strClass = r.strength === 'STRONG' ? 'ss-str-strong' :
                          r.strength === 'MEDIUM' ? 'ss-str-medium' : 'ss-str-weak';
        const typeClass = r.type === 'OTC' ? 'ss-type-otc' : 'ss-type-real';
        const updated = r.last_update != null ? `${Math.round(r.last_update)}s ago` : '—';
        return `<tr>
            <td>${r.pair}</td>
            <td class="${typeClass}">${r.type}</td>
            <td class="${sigClass}">${r.signal}</td>
            <td>${r.confidence > 0 ? Math.round(r.confidence) + '%' : '—'}</td>
            <td class="${strClass}">${r.strength || '—'}</td>
            <td>${updated}</td>
        </tr>`;
    }

    // ── Share signals (chart tab) ────────────────────────────────────────
    let shareSignalInterval = null;

    async function loadShareSignals() {
        try {
            const res = await fetch('/api/share-signals');
            if (!res.ok) throw new Error('fetch failed');
            const data = await res.json();
            renderShareSignals(data.rows || []);
        } catch (e) {
            console.error('loadShareSignals error:', e);
        }
    }

    function renderShareSignals(allRows) {
        const tbody = document.getElementById('share-signal-tbody');
        if (!tbody) return;

        // Apply filters
        const typeF = (document.getElementById('share-filter-type') || {}).value || '';
        const sigF = (document.getElementById('share-filter-signal') || {}).value || '';
        const search = ((document.getElementById('share-search') || {}).value || '').toLowerCase();

        let rows = allRows;
        if (typeF) rows = rows.filter(r => r.type === typeF);
        if (sigF) rows = rows.filter(r => r.signal === sigF);
        if (search) rows = rows.filter(r => (r.pair || '').toLowerCase().includes(search));

        // Hide WEAK if preference is off
        if (!prefs.showWeak) {
            rows = rows.filter(r => r.strength !== 'WEAK');
        }

        if (rows.length === 0) {
            tbody.innerHTML = '<tr><td colspan="9" class="loading-row">No signals match filters</td></tr>';
            return;
        }

        tbody.innerHTML = rows.map(r => {
            const sigClass = r.signal === 'CALL' ? 'ss-signal-call' :
                              r.signal === 'PUT' ? 'ss-signal-put' : 'ss-signal-neutral';
            const strClass = r.strength === 'STRONG' ? 'ss-str-strong' :
                              r.strength === 'MEDIUM' ? 'ss-str-medium' :
                              r.strength === 'WEAK' ? 'ss-str-weak' : '';
            const typeClass = r.type === 'OTC' ? 'ss-type-otc' : 'ss-type-real';
            const updateClass = r.live ? 'ss-update-live' :
                                 r.last_update != null ? 'ss-update-stale' : 'ss-update-offline';
            const updated = r.last_update != null ? `${Math.round(r.last_update)}s` : '—';
            const buyer = r.buyer_pct != null ? r.buyer_pct.toFixed(1) + '%' : '—';
            const seller = r.seller_pct != null ? r.seller_pct.toFixed(1) + '%' : '—';
            const conf = r.confidence > 0 ? Math.round(r.confidence) + '%' : '—';
            return `<tr>
                <td>${r.pair}</td>
                <td class="${typeClass}">${r.type}</td>
                <td>${r.time || '—'}</td>
                <td>${buyer}</td>
                <td>${seller}</td>
                <td class="${sigClass}">${r.signal}</td>
                <td>${conf}</td>
                <td class="${strClass}">${r.strength || '—'}</td>
                <td class="${updateClass}">${updated}</td>
            </tr>`;
        }).join('');
    }

    // Wire share signal buttons
    document.addEventListener('click', (e) => {
        if (e.target.closest('#share-refresh-btn')) {
            e.preventDefault();
            loadShareSignals();
        }
        if (e.target.closest('#share-save-btn')) {
            e.preventDefault();
            saveShareSnapshot();
        }
        if (e.target.closest('#share-history-btn')) {
            e.preventDefault();
            viewShareHistory();
        }
        if (e.target.closest('#share-collapse-btn')) {
            e.preventDefault();
            const wrap = document.querySelector('#pane-chart .share-panel');
            if (wrap) wrap.classList.toggle('collapsed');
        }
        if (e.target.closest('#home-refresh-btn')) {
            e.preventDefault();
            loadHomeData();
        }
    });

    // Wire share signal filters
    document.addEventListener('input', (e) => {
        if (e.target.matches('#share-filter-type, #share-filter-signal, #share-search')) {
            loadShareSignals();
        }
    });

    async function saveShareSnapshot() {
        try {
            const res = await fetch('/api/share-signals/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
            });
            // 401/403 = API key required
            if (res.status === 401 || res.status === 403) {
                alert('Saving snapshots requires an API key. Create one in Settings → API Keys.');
                return;
            }
            if (!res.ok) throw new Error('save failed');
            const data = await res.json();
            alert(`Snapshot saved: ${data.total_pairs || 0} pairs at ${new Date().toLocaleTimeString()}`);
        } catch (e) {
            alert('Save failed: ' + e.message);
        }
    }

    async function viewShareHistory() {
        try {
            const res = await fetch('/api/share-signals/history?limit=10');
            if (!res.ok) throw new Error('fetch failed');
            const data = await res.json();
            if (!data.history || data.history.length === 0) {
                alert('No saved snapshots yet. Click 💾 to save one.');
                return;
            }
            const lines = data.history.map(h => {
                const d = new Date((h.ts || 0) * 1000);
                return `${d.toLocaleString()}: ${h.live_pairs}/${h.total_pairs} live`;
            });
            alert('Recent snapshots:\n\n' + lines.join('\n'));
        } catch (e) {
            alert('History fetch failed: ' + e.message);
        }
    }

    // Auto-refresh share signals
    function startShareSignalAutoRefresh() {
        if (shareSignalInterval) clearInterval(shareSignalInterval);
        if (prefs.autoRefresh !== false) {
            shareSignalInterval = setInterval(loadShareSignals, 10000);
        }
    }

    // ── History tab ──────────────────────────────────────────────────────
    function switchHistTab(name) {
        document.querySelectorAll('.hist-tab').forEach(t => {
            t.classList.toggle('active', t.dataset.hist === name);
        });
        document.querySelectorAll('.hist-pane').forEach(p => {
            p.classList.toggle('active', p.dataset.histPane === name);
        });
    }

    document.addEventListener('click', (e) => {
        const tab = e.target.closest('.hist-tab');
        if (tab) {
            e.preventDefault();
            switchHistTab(tab.dataset.hist);
        }
    });

    async function loadHistoryData() {
        // Populate pair select
        try {
            const res = await fetch('/api/allowlist');
            if (res.ok) {
                const data = await res.json();
                const pairs = data.all || [];
                const sel = document.getElementById('hist-pair-select');
                if (sel && sel.options.length <= 1) {
                    pairs.forEach(p => {
                        const opt = document.createElement('option');
                        opt.value = p;
                        opt.textContent = p;
                        sel.appendChild(opt);
                    });
                }
                const btSel = document.getElementById('bt-pair-select');
                if (btSel && btSel.options.length <= 1) {
                    pairs.forEach(p => {
                        const opt = document.createElement('option');
                        opt.value = p;
                        opt.textContent = p;
                        btSel.appendChild(opt);
                    });
                }
            }
        } catch (e) { /* ignore */ }

        // Load recent history
        loadHistoryList();
        // Load accuracy
        loadAccuracy();
    }

    async function loadHistoryList() {
        const pair = (document.getElementById('hist-pair-select') || {}).value || '';
        const dir = (document.getElementById('hist-direction') || {}).value || '';
        const acc = (document.getElementById('hist-accuracy') || {}).value || '';
        const list = document.getElementById('history-list');
        if (!list) return;

        try {
            // Use first pair from allowlist if none selected
            const url = pair
                ? `/api/history/${encodeURIComponent(pair)}/60?limit=50`
                : `/api/share-signals`;
            const res = await fetch(url);
            if (!res.ok) throw new Error('fetch failed');
            const data = await res.json();

            let rows = [];
            if (pair) {
                rows = (data.signals || data.history || []).map(s => ({
                    time: s.ctime,
                    pair: pair,
                    direction: s.signal,
                    result: s.accuracy,
                    confidence: s.confidence,
                    strength: s.strength,
                }));
            } else {
                // Use share-signals as a snapshot
                rows = (data.rows || []).map(r => ({
                    time: null,
                    pair: r.pair,
                    direction: r.signal,
                    result: null,
                    confidence: r.confidence,
                    strength: r.strength,
                }));
            }

            // Apply filters
            if (dir) rows = rows.filter(r => r.direction === dir);
            if (acc) rows = rows.filter(r => r.result === acc);

            if (rows.length === 0) {
                list.innerHTML = '<div class="loading-row">No signals found</div>';
                return;
            }

            list.innerHTML = rows.slice(0, 50).map(r => {
                const dirClass = r.direction === 'CALL' ? 'CALL' :
                                  r.direction === 'PUT' ? 'PUT' : '';
                const resultClass = r.result === 'correct' ? 'correct' :
                                     r.result === 'wrong' ? 'wrong' :
                                     r.result === 'draw' ? 'draw' : '';
                const timeStr = r.time
                    ? new Date(r.time * 1000).toLocaleTimeString()
                    : '—';
                return `<div class="hist-row">
                    <span class="hist-time">${timeStr}</span>
                    <span class="hist-pair">${r.pair}</span>
                    <span class="hist-dir ${dirClass}">${r.direction || '—'}</span>
                    <span>${r.confidence > 0 ? Math.round(r.confidence) + '%' : '—'}</span>
                    <span>${r.strength || '—'}</span>
                    <span class="hist-result ${resultClass}">${r.result || '—'}</span>
                </div>`;
            }).join('');
        } catch (e) {
            list.innerHTML = `<div class="loading-row">Error: ${e.message}</div>`;
        }
    }

    document.addEventListener('change', (e) => {
        if (e.target.matches('#hist-pair-select, #hist-direction, #hist-accuracy')) {
            loadHistoryList();
        }
    });

    document.addEventListener('click', (e) => {
        if (e.target.closest('#hist-clear-filters')) {
            e.preventDefault();
            ['hist-pair-select', 'hist-direction', 'hist-accuracy'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.value = '';
            });
            loadHistoryList();
        }
        if (e.target.closest('#hist-load-more')) {
            e.preventDefault();
            alert('Loading older signals: this would call /api/signals/{asset}/{period}/{ctime} with the oldest visible ctime.');
        }
        if (e.target.closest('#bt-run-btn')) {
            e.preventDefault();
            runBacktest();
        }
    });

    async function loadAccuracy() {
        try {
            const res = await fetch('/api/stats');
            if (!res.ok) return;
            const stats = await res.json();
            const set = (id, val) => {
                const el = document.getElementById(id);
                if (el) el.textContent = val;
            };
            set('acc-total', stats.total_signals || '—');
            set('acc-winrate', stats.win_rate != null ? `${Math.round(stats.win_rate)}%` : '—');
            set('acc-call-wr', stats.call_win_rate != null ? `${Math.round(stats.call_win_rate)}%` : '—');
            set('acc-put-wr', stats.put_win_rate != null ? `${Math.round(stats.put_win_rate)}%` : '—');
            set('acc-best-pair', stats.best_pair || '—');
            set('acc-best-hour', stats.best_hour != null ? `${stats.best_hour}:00 UTC` : '—');
        } catch (e) { /* ignore */ }
    }

    async function runBacktest() {
        const pair = (document.getElementById('bt-pair-select') || {}).value || '';
        const candles = parseInt((document.getElementById('bt-candles') || {}).value || '100', 10);
        const results = document.getElementById('backtest-results');
        if (!results) return;

        results.innerHTML = '<p class="hint">Running backtest…</p>';

        try {
            // Use the backtest endpoint if available, else simulate
            const url = pair
                ? `/api/backtest/recent?asset=${encodeURIComponent(pair)}&limit=${candles}`
                : `/api/backtest/recent?limit=${candles}`;
            const res = await fetch(url);
            if (!res.ok) throw new Error('backtest failed');
            const data = await res.json();

            const total = data.total_signals || data.total || 0;
            const correct = data.correct || 0;
            const wrong = data.wrong || 0;
            const wr = total > 0 ? ((correct / total) * 100).toFixed(1) : '0.0';

            results.innerHTML = `
                <h4>Backtest Results</h4>
                <div class="accuracy-grid" style="margin-top:12px">
                    <div class="acc-card">
                        <div class="acc-label">Total candles</div>
                        <div class="acc-value">${candles}</div>
                    </div>
                    <div class="acc-card">
                        <div class="acc-label">Signals generated</div>
                        <div class="acc-value">${total}</div>
                    </div>
                    <div class="acc-card">
                        <div class="acc-label">Correct</div>
                        <div class="acc-value" style="color:var(--nav-green)">${correct}</div>
                    </div>
                    <div class="acc-card">
                        <div class="acc-label">Wrong</div>
                        <div class="acc-value" style="color:var(--nav-red)">${wrong}</div>
                    </div>
                    <div class="acc-card">
                        <div class="acc-label">Win rate</div>
                        <div class="acc-value">${wr}%</div>
                    </div>
                    <div class="acc-card">
                        <div class="acc-label">Signal coverage</div>
                        <div class="acc-value">${candles > 0 ? Math.round((total / candles) * 100) : 0}%</div>
                    </div>
                </div>
                <p style="margin-top:16px;color:var(--text-muted);font-size:12px">
                    ${total > 0
                        ? `✅ Signals appeared on ${total} of ${candles} candles (${Math.round((total / candles) * 100)}% coverage).`
                        : '⚠️ No signals in backtest window. Check that the engine is producing signals.'}
                </p>
            `;
        } catch (e) {
            results.innerHTML = `<p class="hint">Backtest error: ${e.message}</p>`;
        }
    }

    // ── Settings tab ─────────────────────────────────────────────────────
    async function loadSettingsData() {
        await Promise.all([loadApiKeys(), loadTokenStatus(), loadAboutInfo()]);
    }

    async function loadApiKeys() {
        const list = document.getElementById('api-key-list');
        if (!list) return;

        // Try fetching without admin auth first — if it fails, show a helpful message.
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
                return `<div class="api-key-row ${inactive}">
                    <div>
                        <div class="key-prefix">${k.key_prefix}…</div>
                        <div class="key-label">${k.label}</div>
                    </div>
                    <div class="key-stats">
                        <div>${status}</div>
                        <div>Created: ${created}</div>
                        <div>Last used: ${lastUsed}</div>
                        <div>Requests: ${k.total_requests}</div>
                    </div>
                    <div class="key-stats">Rate: ${k.rate_limit_per_min}/min</div>
                    ${k.active ? `<button class="key-revoke" data-key-id="${k.id}">Revoke</button>` : '<span></span>'}
                </div>`;
            }).join('');
        } catch (e) {
            list.innerHTML = `<div class="loading-row">Error: ${e.message}</div>`;
        }
    }

    document.addEventListener('click', async (e) => {
        if (e.target.closest('#api-key-create-btn')) {
            e.preventDefault();
            await createApiKey();
        }
        const revokeBtn = e.target.closest('.key-revoke');
        if (revokeBtn) {
            e.preventDefault();
            const keyId = revokeBtn.dataset.keyId;
            if (!confirm('Revoke this API key? This cannot be undone.')) return;
            try {
                const res = await fetch(`/api/keys/${keyId}`, { method: 'DELETE' });
                if (res.status === 401) {
                    alert('Admin auth required to revoke keys.');
                    return;
                }
                if (!res.ok) throw new Error('revoke failed');
                alert('Key revoked.');
                loadApiKeys();
            } catch (e) {
                alert('Revoke failed: ' + e.message);
            }
        }
        if (e.target.closest('#api-key-copy-btn')) {
            e.preventDefault();
            const raw = document.getElementById('api-key-raw');
            if (raw) {
                navigator.clipboard.writeText(raw.textContent).then(() => {
                    alert('API key copied to clipboard.');
                });
            }
        }
        if (e.target.closest('#api-key-modal-close')) {
            e.preventDefault();
            document.getElementById('api-key-modal').hidden = true;
        }
        if (e.target.closest('#token-manage-btn') || e.target.closest('#token-btn')) {
            e.preventDefault();
            openTokenPanel();
        }
        if (e.target.closest('#token-panel-close')) {
            e.preventDefault();
            document.getElementById('token-panel-modal').hidden = true;
        }
    });

    async function createApiKey() {
        const label = (document.getElementById('api-key-label') || {}).value || '';
        const rateLimit = parseInt((document.getElementById('api-key-ratelimit') || {}).value || '60', 10);

        if (!label.trim()) {
            alert('Label is required.');
            return;
        }

        // Try with admin key from localStorage first
        let headers = { 'Content-Type': 'application/json' };
        const adminKey = localStorage.getItem('bst_admin_key');
        if (adminKey) headers['X-Admin-Key'] = adminKey;
        // Else try PIN
        const pin = localStorage.getItem('qxTokenPin');
        if (pin) headers['X-App-Pin'] = pin;

        try {
            const res = await fetch('/api/keys', {
                method: 'POST',
                headers,
                body: JSON.stringify({ label: label.trim(), rate_limit_per_min: rateLimit }),
            });
            if (res.status === 401) {
                alert('Admin auth required. Set X-Admin-Key env var on the server, or claim a PIN via the Token panel.');
                return;
            }
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || 'create failed');
            }
            const data = await res.json();

            // Show the raw key in a modal
            const raw = document.getElementById('api-key-raw');
            const info = document.getElementById('api-key-info-box');
            const modal = document.getElementById('api-key-modal');
            if (raw) raw.textContent = data.raw_key;
            if (info) {
                info.innerHTML = `
                    <div><strong>Label:</strong> ${data.info.label}</div>
                    <div><strong>Prefix:</strong> ${data.info.key_prefix}…</div>
                    <div><strong>Rate limit:</strong> ${data.info.rate_limit_per_min}/min</div>
                `;
            }
            if (modal) modal.hidden = false;

            // Clear the form
            const labelInput = document.getElementById('api-key-label');
            if (labelInput) labelInput.value = '';

            // Refresh the list
            loadApiKeys();
        } catch (e) {
            alert('Create failed: ' + e.message);
        }
    }

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
                    ? `Expires: ${new Date(data.expires_at).toLocaleString()}`
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

        // The token-panel.js will populate this — we just trigger it.
        body.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-muted)">Loading token panel…</div>';
        modal.hidden = false;

        // Dispatch a custom event that token-panel.js can listen for
        setTimeout(() => {
            window.dispatchEvent(new CustomEvent('bst-open-token-panel', {
                detail: { container: body }
            }));
        }, 50);
    }

    async function loadAboutInfo() {
        try {
            const res = await fetch('/api/keys/verify');
            if (res.ok) {
                const data = await res.json();
                // No-op — just a smoke test
            }
        } catch (e) { /* ignore */ }

        // Update about info based on env
        const aboutPublicRead = document.getElementById('about-public-read');
        if (aboutPublicRead) {
            aboutPublicRead.textContent = 'Enabled (default)';
        }
    }

    // ── Preferences wiring ───────────────────────────────────────────────
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
                if (autoEl.checked) startShareSignalAutoRefresh();
                else if (shareSignalInterval) clearInterval(shareSignalInterval);
            });
        }
        if (weakEl) {
            weakEl.checked = prefs.showWeak !== false;
            weakEl.addEventListener('change', () => {
                prefs.showWeak = weakEl.checked;
                savePrefs(prefs);
                loadShareSignals();
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

    // ── Connection status ────────────────────────────────────────────────
    function updateConnectionStatus(state, text) {
        const dot = document.getElementById('sidebar-conn-dot');
        const txt = document.getElementById('sidebar-conn-text');
        const homeConn = document.getElementById('home-conn-status');
        if (dot) {
            dot.className = 'status-dot ' + (state || '');
        }
        if (txt) txt.textContent = text || '—';
        if (homeConn) {
            homeConn.textContent = text || '—';
            homeConn.className = 'status-badge ' + (state === 'connected' ? 'status-live' : '');
        }
    }

    // Expose for common.js to call
    window.bstSetConnStatus = updateConnectionStatus;
    window.bstRefreshShareSignals = loadShareSignals;
    window.bstRefreshHome = loadHomeData;

    // ── Init ─────────────────────────────────────────────────────────────
    function init() {
        highlightCurrentMarket();
        initPreferences();

        // Restore active tab
        const savedTab = prefs.activeTab || 'home';
        switchTab(savedTab);

        // Start auto-refresh of share signals
        startShareSignalAutoRefresh();

        // Initial load
        loadHomeData();

        // Listen for token-panel custom event
        window.addEventListener('bst-open-token-panel', (e) => {
            // token-panel.js should populate the container
            // If it doesn't, we provide a fallback
            const container = e.detail && e.detail.container;
            if (container && container.innerHTML.includes('Loading token panel')) {
                setTimeout(() => {
                    if (container.innerHTML.includes('Loading token panel')) {
                        container.innerHTML = `
                            <div style="padding:24px">
                                <h3 style="margin-bottom:16px">Token Management</h3>
                                <p style="color:var(--text-muted);margin-bottom:16px">
                                    The token panel is being loaded. If it doesn't appear, refresh the page.
                                </p>
                                <p style="color:var(--text-muted)">
                                    Visit <code>/api/token-status</code> to check current token status.
                                </p>
                            </div>
                        `;
                    }
                }, 2000);
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
