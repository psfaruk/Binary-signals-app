/* models-panel.js — মডেল tab (MODEL-RUN-FIX 2026-09-12).
 *
 * USER REQ (verbatim): "আর কি কি মডেল কত টুকু ট্রেইন হলো, রেজাল্ট কি আমি
 * fronted এ দেখতে পারবো" — this panel answers it LIVE from
 * /api/prediction/overview (server.py):
 *   1. daemon strip  → trainer running? last run? next run? errors?
 *   2. per-pair grid → কোন পেয়ার কত ক্যান্ডেল/রো, কী স্ট্যাটাস, T+1/T+2
 *      walk-forward accuracy vs baseline, model version
 *   3. results       → প্রতিটি ফ্রিজ করা প্রেডিকশনের গ্রেডেড ফলাফল
 *      (direction accuracy — emit হোক বা না হোক)
 *
 * Owned refresh: switchTab('models') → ModelsPanel.refresh(); while the tab
 * is visible a gentle 15s poll keeps it current. Force button →
 * POST /api/prediction/bootstrap (same as the old hidden endpoint).
 */
(function() {
    'use strict';

    var POLL_MS = 15000;
    var _pollTimer = null;
    var _inflight = false;
    var _lastData = null;

    function $(id) { return document.getElementById(id); }

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function agoStr(ts) {
        if (!ts) return '—';
        var d = Date.now() / 1000 - ts;
        if (d < 0) d = 0;
        if (d < 60) return Math.round(d) + 'সে আগে';
        if (d < 3600) return Math.round(d / 60) + 'মিনিট আগে';
        if (d < 86400) return Math.round(d / 3600) + 'ঘ আগে';
        return Math.round(d / 86400) + 'দিন আগে';
    }

    function inStr(secs) {
        if (secs == null) return '—';
        if (secs <= 0) return 'এখনই';
        if (secs < 60) return Math.round(secs) + ' সেকেন্ডে';
        if (secs < 3600) return Math.round(secs / 60) + ' মিনিটে';
        return (secs / 3600).toFixed(1) + ' ঘণ্টায়';
    }

    // ── status badge (single source of truth for pair rendering) ────────
    function statusBadge(st) {
        var map = {
            verified:    ['ok',   '✅ ভেরিফায়েড'],
            provisional: ['prov', 'প্রোভিশনাল'],
            rejected:    ['bad',  'বাদ দেওয়া হয়েছে'],
            skipped:     ['warn', 'ডেটা কম'],
            blocked:     ['bad',  'ব্লকড'],
            no_data:     ['warn', 'ডেটা নেই'],
            training:    ['prov', 'ট্রেইনিং চলছে…'],
            error:       ['bad',  'ত্রুটি']
        };
        var m = map[st] || ['warn', st || 'অপেক্ষায়'];
        return '<span class="mdl-badge mdl-badge-' + m[0] + '">' + esc(m[1]) + '</span>';
    }

    function accStr(h) {
        if (!h || h.acc == null) return '—';
        var s = h.acc.toFixed(1) + '%';
        if (h.baseline != null) s += ' <span class="mdl-base">(base ' +
            h.baseline.toFixed(1) + '%)</span>';
        return s;
    }

    // ── renderers ────────────────────────────────────────────────────────
    function renderDaemon(d) {
        var st = d.daemon || {};
        var chip = $('mdl-state-chip');
        if (chip) {
            var cls = 'idle';
            var txt = 'স্লিপ মোড';
            if (st.running) { cls = 'run'; txt = 'ট্রেইনিং চলছে…'; }
            else if (st.blocked) { cls = 'err'; txt = 'ব্লকড'; }
            else if (st.runs > 0 && (st.result || {}).pairs_registered &&
                     st.result.pairs_registered.length) {
                cls = 'ok'; txt = 'মডেল প্রস্তুত';
            }
            chip.className = 'mdl-state-chip mdl-chip-' + cls;
            chip.textContent = txt;
        }

        var warn = $('mdl-warn');
        var warnText = $('mdl-warn-text');
        var warnMsg = '';
        if (st.blocked) warnMsg = '⛔ ' + st.blocked +
            ' — Railway এ deploy হলে এই লাইন দেখা মানে requirements.txt এ ' +
            'scikit-learn/numpy নেই। রিডিপ্লোয়ে ঠিক হয়ে যাবে।';
        else if (st.last_error) warnMsg = '⚠️ শেষ রানে সমস্যা: ' + st.last_error;
        if (warnMsg) {
            warn.style.display = '';
            warnText.textContent = warnMsg;
        } else {
            warn.style.display = 'none';
        }

        $('mdl-runs').textContent = st.runs != null ? st.runs : '—';
        $('mdl-lastrun').textContent = st.last_run_ago != null
            ? agoStr(Date.now() / 1000 - st.last_run_ago) : '—';
        $('mdl-nextrun').textContent = st.running
            ? 'রান চলছে' : inStr(st.next_run_in);

        var activeN = 0;
        (d.models || []).forEach(function(m) { if (m.active) activeN++; });
        $('mdl-regcount').textContent = activeN;

        // last-run summary line: registered pairs + fetch report
        var note = $('mdl-lastrun-note');
        var parts = [];
        var res = st.result || {};
        if (res.pairs_registered && res.pairs_registered.length) {
            parts.push('সর্বশেষ রানে রেজিস্টার: ' +
                esc(res.pairs_registered.join(', ')));
        } else if (st.runs > 0) {
            parts.push('সর্বশেষ রানে কোনো মডেল রেজিস্টার হয়নি — ' +
                '১০ মিনিট পরে আবার চেষ্টা হবে');
        }
        var fetch = res.fetch || {};
        if (fetch && fetch.fetched) {
            var okRows = 0, errN = 0;
            Object.keys(fetch.results || {}).forEach(function(k) {
                var r = fetch.results[k];
                if (r.status === 'ok') okRows += (r.added || 0);
                else errN++;
            });
            if (okRows || errN) parts.push('হিস্টোরি টপ-আপ: +' + okRows +
                ' ক্যান্ডেল' + (errN ? ', ' + errN + ' পেয়ারে ত্রুটি' : ''));
        } else if (fetch && fetch.reason) {
            parts.push('টপ-আপ: ' + fetch.reason);
        }
        note.textContent = parts.join(' · ');
    }

    function renderPairs(d) {
        var tbody = $('mdl-pair-tbody');
        if (!tbody) return;
        var pairs = d.daemon && d.daemon.pairs ? d.daemon.pairs : {};
        var candles = d.candles || {};
        // active registry model per pair (walk-forward numbers may be newer
        // in the registry than the daemon's per-run cache — prefer registry)
        var active = {};
        var globalM = null;
        (d.models || []).forEach(function(m) {
            if (!m.active) return;
            if (m.scope === 'pair' && m.name) active[m.name] = m;
            if (m.scope === 'global') globalM = m;
        });

        var names = Object.keys(pairs);
        if (!names.length) {
            // daemon never ran yet — still show candle counts per pair
            names = Object.keys(candles);
        }
        names.sort();

        if (!names.length) {
            tbody.innerHTML = '<tr><td colspan="8" class="mdl-loading">' +
                'এখনো কোনো রান হয়নি — "এখনই ট্রেইন করুন" চাপুন</td></tr>';
            return;
        }

        var html = '';
        names.forEach(function(a) {
            if (a === '__global__') return;
            var p = pairs[a] || {};
            var m = active[a];
            var status = (m && m.status) || p.status;
            var rows = (m && m.trained_rows) || p.rows;
            var t1 = (m && m.t1) || p.t1;
            var t2 = (m && m.t2) || p.t2;
            var version = (m && m.version) || p.version;
            var noteBits = [];
            if (p.reason) noteBits.push(esc(p.reason));
            if (p.fetch_error) noteBits.push('fetch: ' + esc(p.fetch_error));
            if (p.candles != null && !status) noteBits.push('রান অপেক্ষায়');
            var modelTxt = version ? esc(version) : '—';
            if (m && m.t1 && m.t1.model) modelTxt += ' · ' + esc(m.t1.model);
            else if (m && m.t2 && m.t2.model) modelTxt += ' · ' + esc(m.t2.model);
            html += '<tr>' +
                '<td class="mdl-pair-name">' + esc(a.replace('_otc', '')) + '</td>' +
                '<td>' + (candles[a] != null ? candles[a] : (p.candles != null ? p.candles : '—')) + '</td>' +
                '<td>' + statusBadge(status) + '</td>' +
                '<td>' + (rows != null ? rows : '—') + '</td>' +
                '<td>' + accStr(t1) + '</td>' +
                '<td>' + accStr(t2) + '</td>' +
                '<td class="mdl-ver">' + modelTxt + '</td>' +
                '<td class="mdl-note-cell">' + (noteBits.join(' · ') || '—') + '</td>' +
                '</tr>';
        });
        if (globalM) {
            html += '<tr class="mdl-global-row">' +
                '<td class="mdl-pair-name">GLOBAL (ফলব্যাক)</td>' +
                '<td>—</td>' +
                '<td>' + statusBadge(globalM.status) + '</td>' +
                '<td>' + (globalM.trained_rows != null ? globalM.trained_rows : '—') + '</td>' +
                '<td>' + accStr(globalM.t1) + '</td>' +
                '<td>' + accStr(globalM.t2) + '</td>' +
                '<td class="mdl-ver">' + esc(globalM.version || '—') + '</td>' +
                '<td class="mdl-note-cell">সব পেয়ার মিলিয়ে ট্রেইন করা ফলব্যাক মডেল</td>' +
                '</tr>';
        }
        tbody.innerHTML = html;
    }

    function renderAnalytics(d) {
        var a = d.analytics || {};
        $('mdl-a-total').textContent = a.dir_total != null
            ? a.dir_total : '—';
        $('mdl-a-t1').textContent =
            (a.t1 && a.t1.dir_win_rate != null)
                ? a.t1.dir_win_rate + '% (' + (a.t1.dir_n || 0) + ')' : '—';
        $('mdl-a-t2').textContent =
            (a.t2 && a.t2.dir_win_rate != null)
                ? a.t2.dir_win_rate + '% (' + (a.t2.dir_n || 0) + ')' : '—';
        $('mdl-a-wr').textContent = a.win_rate != null
            ? a.win_rate + '% (' + (a.wins || 0) + 'W/' + (a.losses || 0) + 'L)'
            : (a.total_signals ? '—' : 'সিগন্যাল নেই');

        var noteEl = $('mdl-analytics-note');
        if (!a.dir_total) {
            noteEl.textContent = 'এখনো কোনো প্রেডিকশন সেটেল হয়নি। মডেল রেজিস্টার ' +
                'হওয়ার পর প্রতিটি ক্যান্ডেলে T+1/T+2 প্রেডিকশন ফ্রিজ হয় এবং ' +
                'ক্যান্ডেল ক্লোজে গ্রেড হয় — তখন এখানে সত্যিকারের অ্যাকুরেসি দেখা যাবে।';
        } else {
            noteEl.textContent = 'ট্র্যাক করা প্রেডিকশনের দিক-নির্ভুলতা ' +
                '(সিগন্যাল এমিট হোক বা না হোক — প্রতিটি প্রেডিকশন ফ্রিজ ও ' +
                'গ্রেড করা হয়)। সিগন্যাল এমিশন তখনই হয় যখন মডেল ' +
                'ভেরিফায়েড + স্কোর গেট পাস করে।';
        }
    }

    // ── data ─────────────────────────────────────────────────────────────
    function refresh() {
        if (_inflight) return;
        _inflight = true;
        fetch('/api/prediction/overview', { cache: 'no-store' })
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(d) {
                if (!d) return;
                _lastData = d;
                renderDaemon(d);
                renderPairs(d);
                renderAnalytics(d);
            })
            .catch(function() { /* transient — next poll retries */ })
            .finally(function() { _inflight = false; });
    }

    function forceTrain() {
        var btn = $('mdl-force-btn');
        if (btn) { btn.disabled = true; btn.textContent = 'কমান্ড পাঠানো হচ্ছে…'; }
        fetch('/api/prediction/bootstrap', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function() {
                setTimeout(function() {
                    if (btn) { btn.disabled = false;
                        btn.textContent = 'এখনই ট্রেইন করুন'; }
                    refresh();
                }, 1500);
            })
            .catch(function() {
                if (btn) { btn.disabled = false;
                    btn.textContent = 'এখনই ট্রেইন করুন'; }
            });
    }

    function init() {
        var btn = $('mdl-force-btn');
        if (btn) btn.addEventListener('click', forceTrain);
        // poll only while the pane is visible
        setInterval(function() {
            var pane = $('pane-models');
            if (pane && pane.classList.contains('active')) refresh();
        }, POLL_MS);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.ModelsPanel = { refresh: refresh };
})();
