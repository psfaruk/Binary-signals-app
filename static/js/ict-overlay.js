/* PHASE-L7 (2026-10) — ICT/SMC research overlay panel.
 *
 * Fetches /api/ict-research/{asset} (auto mode; honest no-data state when
 * the unauthorized live feed has no candles) and renders the research
 * state: structure/trend, zones, patterns, momentum, session, liquidity,
 * and the pre-specified L6 research votes.
 *
 * HONESTY CONTRACT (matches the server payload):
 *   - every vote is research-only, weight 0, NOT validated, NOT actionable;
 *   - replay data is labeled HISTORICAL exchange research data, NOT OTC,
 *     NOT live;
 *   - no fabricated state: absent data renders as absent data.
 */
(function () {
    'use strict';

    var REFRESH_MS = 20000;
    var REPLAY_ASSETS = ['BTCUSDT', 'ETHUSDT', 'XRPUSDT', 'BNBUSDT',
                         'ADAUSDT', 'SOLUSDT', 'DOGEUSDT', 'LTCUSDT'];
    var VOTE_LABELS = {
        ict1_ob_retest: 'OB রি-টেস্ট',
        ict2_sweep_reversal: 'সুইপ রিভার্সাল',
        ict3_amd_continuation: 'AMD কন্টিনিউয়েশন',
        ict4_fvg_fill: 'FVG ফিল',
        ict5_divergence: 'ডাইভার্জেন্স',
        ict6_kz_structure: 'কিল-জোন স্ট্রাকচার',
        ict7_vote: 'নেট ভোট'
    };
    var MODE_BADGES = {
        no_data: 'ডেটা নেই (সৎ অবস্থা)',
        live_feed: 'লাইভ ফিড (আনঅথরাইজড — লেবেলকৃত)',
        research_replay_authorized_binance_30d: 'রিসার্চ রিপ্লে (Binance 30d — ঐতিহাসিক, OTC নয়)'
    };

    function esc(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
                     '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function fmt(v, d) {
        if (v === null || v === undefined) return '—';
        if (typeof v === 'number') return v.toFixed(d === undefined ? 2 : d);
        return String(v);
    }

    function currentAsset() {
        try {
            var el = document.getElementById('pair-select');
            if (el && el.value) return String(el.value).toUpperCase();
        } catch (_) { /* ignore */ }
        return null;
    }

    function row(label, value, note) {
        return '<div class="module-row"><span class="rf-label">' + esc(label) +
               '</span><span class="rf-note">' + value +
               (note ? ' <span style="opacity:.6">(' + note + ')</span>' : '') +
               '</span></div>';
    }

    function zoneLine(z) {
        if (!z) return '—';
        return esc(z.kind) + ' @ ' + fmt(z.level_low) + '–' + fmt(z.level_high) +
               ' · dist ' + fmt(z.dist_atr) + ' ATR · ' + esc(z.status) +
               ' · বয়স ' + z.age;
    }

    function render(d) {
        var body = document.getElementById('ict-overlay-body');
        if (!body) return;
        var html = '';
        html += '<div class="module-row"><span class="rf-label">মোড</span>' +
                '<span class="rf-note">' +
                esc(MODE_BADGES[d.data_mode] || d.data_mode) + '</span></div>';
        if (d.data_mode === 'no_data') {
            html += '<div class="module-row"><span class="rf-note">' +
                    esc(d.reason || 'কারণ অজানা') + '</span></div>';
            html += '<div class="module-row"><span class="rf-note">' +
                    'রিপ্লে দেখতে: BTCUSDT-এর মতো রিসার্চ asset নির্বাচন করুন — ' +
                    'লাইভ ফিড ছাড়া কিছু বানানো হয় না।</span></div>';
        } else {
            var s = d.state || {};
            var pr = (d.provenance || {});
            html += row('টার্গেট', esc((pr.target_ts_utc || '').replace('T', ' ').slice(0, 16)) + ' UTC',
                        esc((pr.classification || '').slice(0, 48)));
            html += row('স্ট্রাকচার', esc(s['structure.trend'] || '—') +
                        ' · শেষ ব্রেক ' + esc(s['structure.last_break'] || '—'));
            html += row('সেশন', esc(s['session.target_session'] || '—') +
                        (s['session.target_kill_zone']
                             ? ' · কিল-জোন ' + esc(s['session.target_kill_zone']) : ''));
            html += row('মোমেন্টাম', 'RSI ' + fmt(s['mom.rsi']) +
                        ' · ROC ' + fmt(s['mom.roc'], 4) +
                        ' · স্টোক ' + fmt(s['mom.stoch_k']));
            html += row('সাপ্লাই জোন', zoneLine(s['poi.nearest_supply']));
            html += row('ডিমান্ড জোন', zoneLine(s['poi.nearest_demand']));
            html += row('অ্যাকটিভ জোন', 'ডিমান্ড ' + (s['poi.demand_zone_active'] || 0) +
                        ' · সাপ্লাই ' + (s['poi.supply_zone_active'] || 0) +
                        ' · OB↑ ' + (s['poi.order_block_bullish_active'] || 0) +
                        ' · OB↓ ' + (s['poi.order_block_bearish_active'] || 0) +
                        ' · FVG↑ ' + (s['poi.fvg_bullish_active'] || 0) +
                        ' · FVG↓ ' + (s['poi.fvg_bearish_active'] || 0));
            html += row('প্যাটার্ন', esc(s['pattern.last_kind'] || '—') +
                        ' (' + esc(s['pattern.last_side'] || '—') + ')' +
                        ' · ৫-ক্যান্ডেলে ↑' + (s['pattern.bull_count_5'] || 0) +
                        ' ↓' + (s['pattern.bear_count_5'] || 0));
            html += row('লিকুইডিটি', (s['liq.in_range'] === null ||
                        s['liq.in_range'] === undefined ? 'রেঞ্জ নেই' :
                        (s['liq.in_range'] ? 'রেঞ্জের ভিতরে' : 'রেঞ্জের বাইরে')) +
                        ' · শেষ সুইপ ' + esc(s['liq.last_sweep'] || '—') +
                        ' · AMD ' + esc(s['liq.last_amd'] || '—'));
            html += '<div class="module-row" style="flex-direction:column;gap:4px">' +
                    '<span class="rf-label">L6 রিসার্চ ভোট — ভ্যালিডেটেড নয়, ওজন ০</span>';
            var votes = d.research_votes || {};
            Object.keys(VOTE_LABELS).forEach(function (k) {
                var v = votes[k];
                if (!v) return;
                var dir = v.direction === 'CALL' ? '↑ CALL' :
                          v.direction === 'PUT' ? '↓ PUT' : '— নিরপেক্ষ';
                html += '<span class="rf-note">' + esc(VOTE_LABELS[k]) +
                        ': <b>' + dir + '</b> <span style="opacity:.55">' +
                        esc(v.label || '') + '</span></span>';
            });
            html += '</div>';
        }
        html += '<div class="module-row"><span class="rf-note" style="opacity:.75">' +
                '🔬 রিসার্চ ওভারলে — L6 মূল্যায়নে সব arm ব্রেক-ইভেন (৫৪.০৫%) এর নিচে; ' +
                'কোনো ভোট সিগন্যাল নয়, প্রযোজ্য নয়।</span></div>';
        body.innerHTML = html;
    }

    function refresh() {
        var asset = currentAsset();
        var mode = 'auto';
        if (!asset || REPLAY_ASSETS.indexOf(asset) === -1) {
            // honest: research replay only exists for the 8 dataset assets
            asset = asset || 'BTCUSDT';
            mode = (REPLAY_ASSETS.indexOf(asset) === -1) ? 'auto' : 'replay';
        }
        fetch('/api/ict-research/' + encodeURIComponent(asset) +
              '?mode=' + mode)
            .then(function (r) { return r.json(); })
            .then(render)
            .catch(function () {
                var body = document.getElementById('ict-overlay-body');
                if (body) {
                    body.innerHTML = '<div class="module-row">' +
                        '<span class="rf-note">রিসার্চ এন্ডপয়েন্ট অনুপলব্ধ — ' +
                        'সৎ ত্রুটি, কিছু বানানো হয়নি।</span></div>';
                }
            });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            setTimeout(refresh, 1500);
            setInterval(refresh, REFRESH_MS);
        });
    } else {
        setTimeout(refresh, 1500);
        setInterval(refresh, REFRESH_MS);
    }
    window.__ictOverlayRefresh = refresh;
})();
