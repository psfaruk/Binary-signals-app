/* ============================================================================
   winrate.js — RESULT tab, View A: all-pairs CALL/PUT win-rate list
   (Aurora v3 · RESULT-VIEW-FIX 2026-09-07)
   ----------------------------------------------------------------------------
   USER REQ (2026-09-07, verbatim):
     "রেজাল্ট ট্যাব এ ক্লিক করলেই সব গুলো পেয়ার এর call put এর রেজাল্ট
      উইন রেট গুলো দেখাবে। তার পর আমি কোনো নির্দিষ্ট পেয়ার এ ক্লিক করলে
      তখন শুধু সেই একক পেয়ার এর জন্য লগ গুলো দেখাবে, লাইভ সিগন্যাল সহ।"

   This file renders VIEW A (the all-pairs list). The heavy cards were
   replaced with clean one-line ROWS: pair | CALL win% | PUT win% | →.
   Clicking a row dispatches 'wr:openpair' — common.js owns VIEW B
   (pair drill-in: that pair's logs + live signal).

   Data: fetch('/api/winrate?period=60&days=N&category=X')
         (db.get_directional_winrate — graded-only, draws excluded)
   ============================================================================ */
(function(global){
  'use strict';

  var WR_REFRESH_MS = 20000;   // background auto-refresh (pane visible only)
  var wrState = { market:'all', window:7 };
  var wrTimer = null;
  var wrLoading = false;

  function $(id){ return document.getElementById(id); }

  function esc(s){
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  function wrClass(pct){
    if(pct == null) return '';
    if(pct >= 60) return 'good';
    if(pct >= 45) return 'mid';
    return 'bad';
  }

  function fmtPct(pct){
    return (pct == null) ? '—' : pct.toFixed(1) + '%';
  }

  function displayFor(asset){
    // Mirror the server's display mapping (feed.py): _otc → "X/Y OTC".
    var a = asset || '';
    if(a === 'BRLUSD_otc') return 'BRL/USD';
    var base = a.replace(/_otc$/,'');
    if(base.length === 6) base = base.slice(0,3) + '/' + base.slice(3);
    else if(base.length === 7) base = base.slice(0,3) + '/' + base.slice(3);
    return base + (a.endsWith('_otc') ? ' OTC' : '');
  }

  /* Sort for View A: best blended win rate first (nulls last), so the user
     sees the strongest pairs at the top of the রেজাল্ট list. */
  function sortPairs(pairs){
    return pairs.slice().sort(function(a,b){
      var av = (a.win_pct != null) ? a.win_pct : -1;
      var bv = (b.win_pct != null) ? b.win_pct : -1;
      if(bv !== av) return bv - av;
      // tie-break: more graded signals = more trustworthy stat, first.
      return (b.graded || 0) - (a.graded || 0);
    });
  }

  function renderHero(overall, windowDays){
    var pctEl = $('wr-hero-pct');
    var subEl = $('wr-hero-sub');
    var winEl = $('wr-hero-window');
    if(!pctEl) return;
    if(!overall || overall.graded === 0){
      pctEl.textContent = '—';
      pctEl.className = 'wr-hero-pct mid';
      if(subEl) subEl.textContent = 'এই উইন্ডোতে কোনো গ্রেডেড সিগন্যাল নেই';
      if(winEl) winEl.textContent = '';
    } else {
      var pct = overall.win_pct;
      pctEl.textContent = fmtPct(pct);
      pctEl.className = 'wr-hero-pct ' + wrClass(pct);
      if(subEl){
        subEl.textContent = overall.correct + ' উইন / ' + overall.graded
          + ' গ্রেডেড' + (overall.draws ? (' · ' + overall.draws + ' ড্র') : '');
      }
      if(winEl){
        winEl.textContent = (windowDays === 0 ? 'সব সময়'
          // FIX (BREAKEVEN-TEXT-2026-09-07): the true 85%-payout breakeven is
          // 100/185 = 54.05% (core/breakeven.py) — quoted identically on
          // every surface.
          : 'শেষ ' + windowDays + ' দিন') + ' · ব্রেকইভেন ৫৪.০৫% (৮৫% পেআউট)';
      }
    }
    // CALL / PUT split bars
    var callPct = $('wr-call-pct'), putPct = $('wr-put-pct');
    var callBar = $('wr-call-bar'), putBar = $('wr-put-bar');
    if(callPct && putPct && callBar && putBar){
      if(!overall || (overall.call.total === 0 && overall.put.total === 0)){
        callPct.textContent = '—'; putPct.textContent = '—';
        callBar.style.width = '0%'; putBar.style.width = '0%';
      } else {
        callPct.textContent = fmtPct(overall.call.win_pct)
          + ' (' + overall.call.correct + '/' + overall.call.total + ')';
        putPct.textContent  = fmtPct(overall.put.win_pct)
          + ' (' + overall.put.correct + '/' + overall.put.total + ')';
        callBar.style.width = (overall.call.win_pct != null ? overall.call.win_pct : 0) + '%';
        putBar.style.width  = (overall.put.win_pct != null ? overall.put.win_pct : 0) + '%';
      }
    }
  }

  /* ─── VIEW A: all-pairs rows ──────────────────────────────────────────────
     One row per pair:  [name + tag] [▲ CALL wr (w/t)] [▼ PUT wr (w/t)] [→]
     The entire row is a tap target → 'wr:openpair' → common.js drill-in. */
  function renderPairRows(pairs){
    var wrap = $('wr-pairlist');
    if(!wrap) return;
    if(!pairs || !pairs.length){
      wrap.innerHTML = '<div class="wr-empty">এই ফিল্টারে কোনো ডেটা নেই — '
        + 'অন্য মার্কেট/সময় উইন্ডো দেখুন</div>';
      return;
    }
    var html = '';
    var list = sortPairs(pairs);
    for(var j = 0; j < list.length; j++){
      var q = list[j];
      var callPct = q.call ? q.call.win_pct : null;
      var putPct  = q.put  ? q.put.win_pct  : null;
      var callStats = q.call ? (q.call.correct + '/' + q.call.total) : '—';
      var putStats  = q.put  ? (q.put.correct  + '/' + q.put.total)  : '—';
      // Live pulse: mark the pair the user is currently watching live.
      var isLive = (typeof currentAsset === 'string' && currentAsset === q.asset);
      html += '<div class="wr-pair-row' + (isLive ? ' live' : '') + '"'
        + ' data-asset="' + esc(q.asset) + '" role="button" tabindex="0">'
        + '<div class="wr-row-name">'
        +   '<span class="wr-pair-name">' + esc(displayFor(q.asset)) + '</span>'
        +   '<span class="wr-pair-tags">'
        +     '<span class="wr-tag ' + esc(q.category) + '">' + esc(q.category === 'otc' ? 'OTC' : 'REAL') + '</span>'
        +     (isLive ? '<span class="wr-tag live">● লাইভ</span>' : '')
        +   '</span>'
        + '</div>'
        + '<div class="wr-row-dir call">'
        +   '<span class="wr-row-dir-label">CALL</span>'
        +   '<span class="wr-row-dir-val ' + wrClass(callPct) + '">' + fmtPct(callPct) + '</span>'
        +   '<span class="wr-row-dir-cnt">' + callStats + '</span>'
        + '</div>'
        + '<div class="wr-row-dir put">'
        +   '<span class="wr-row-dir-label">PUT</span>'
        +   '<span class="wr-row-dir-val ' + wrClass(putPct) + '">' + fmtPct(putPct) + '</span>'
        +   '<span class="wr-row-dir-cnt">' + putStats + '</span>'
        + '</div>'
        + '<div class="wr-row-chev" aria-hidden="true">›</div>'
        + '</div>';
    }
    wrap.innerHTML = html;
  }

  /* Row tap → ask common.js to open the pair drill-in view. Delegated on the
     container so re-renders never need re-wiring. Keyboard accessible. */
  function wirePairRows(){
    var wrap = $('wr-pairlist');
    if(!wrap || wrap.dataset.wired === '1') return;
    wrap.dataset.wired = '1';
    var open = function(asset){
      if(!asset) return;
      try{ global.dispatchEvent(new CustomEvent('wr:openpair', { detail: { asset: asset } })); }
      catch(_){}
    };
    wrap.addEventListener('click', function(ev){
      var row = ev.target.closest('.wr-pair-row');
      if(row) open(row.getAttribute('data-asset'));
    });
    wrap.addEventListener('keydown', function(ev){
      if(ev.key !== 'Enter' && ev.key !== ' ') return;
      var row = ev.target.closest('.wr-pair-row');
      if(row){ ev.preventDefault(); open(row.getAttribute('data-asset')); }
    });
  }

  function fetchWinrate(){
    if(wrLoading) return;
    wrLoading = true;
    var qs = 'period=60';
    if(wrState.window > 0) qs += '&days=' + wrState.window;
    if(wrState.market !== 'all') qs += '&category=' + wrState.market;
    fetch('/api/winrate?' + qs)
      .then(function(r){ return r.json(); })
      .then(function(data){
        wrLoading = false;
        if(!data || !data.ok){
          renderPairRows([]);
          return;
        }
        // Cache the payload — common.js reads it to build View B's pair hero
        // (window._wrPayload.pairs.find(asset) → CALL/PUT/streak/last signal).
        global._wrPayload = { overall: data.overall, pairs: data.pairs || [],
                              window: wrState.window, market: wrState.market };
        renderHero(data.overall, wrState.window);
        renderPairRows(data.pairs);
        // Pair view open? refresh its hero with the fresh numbers.
        if(typeof global._wrPairHeroRefresh === 'function'){
          try{ global._wrPairHeroRefresh(); }catch(_){}
        }
      })
      .catch(function(){
        wrLoading = false;
        var wrap = $('wr-pairlist');
        if(wrap) wrap.innerHTML = '<div class="wr-empty">⚠ সার্ভার থেকে ডেটা আসেনি — '
          + 'কিছুক্ষণ পরে আবার চেষ্টা করুন</div>';
      });
  }

  function wireChips(containerId, attr, cb){
    var box = $(containerId);
    if(!box || box.dataset.wired === '1') return;
    box.dataset.wired = '1';
    box.addEventListener('click', function(ev){
      var chip = ev.target.closest('.wr-chip');
      if(!chip) return;
      var chips = box.querySelectorAll('.wr-chip');
      for(var i = 0; i < chips.length; i++) chips[i].classList.remove('active');
      chip.classList.add('active');
      cb(chip.dataset[attr]);
      fetchWinrate();
    });
  }

  /* ─── PSYCHOLOGY-FIX (2026-09-11): discipline panel ────────────────────
     Renders /api/psychology — the trading-psychology rules computed from
     THIS app's own ledger. Fails silently (panel stays hidden). */
  function fmtHour(h, pct, n){
    return esc(String(h).padStart(2, '0') + ':00 · ' + pct.toFixed(1) + '% (n=' + n + ')');
  }

  function fmtPairRow(p){
    var line = esc(displayFor(p.asset)) + ' · ' + fmtPct(p.win_pct)
      + ' · Wilson LB ' + fmtPct(p.wilson_lb_pct) + ' (n=' + p.graded + ')';
    if(p.call_win_pct != null && p.put_win_pct != null){
      line += '<br><span class="psy-dir-split">CALL ' + fmtPct(p.call_win_pct)
        + ' / PUT ' + fmtPct(p.put_win_pct) + '</span>';
    }
    return line;
  }

  function renderPsychology(data){
    var panel = $('psy-panel');
    if(!panel || !data || !data.ok) return;
    panel.hidden = false;

    var econ = $('psy-economics');
    if(econ){
      var eco = data.economics || {};
      var otc = eco.otc || {}, real = eco.real || {};
      econ.innerHTML =
        'OTC পেআউট ~' + (otc.typical_payout_pct != null ? otc.typical_payout_pct : '—')
        + '% → ব্রেকইভেন উইন রেট <b>' + (otc.breakeven_win_pct != null ? otc.breakeven_win_pct : '54.05') + '%</b><br>'
        + 'Real পেআউট ~' + (real.typical_payout_pct != null ? real.typical_payout_pct : '—')
        + '% → ব্রেকইভেন <b>' + (real.breakeven_win_pct != null ? real.breakeven_win_pct : '58.82') + '%</b><br>'
        + '<span class="psy-warn">৫০.২% উইন রেট = লস — "প্রায় প্রফিটেবল" বলে কিছু নেই</span>';
    }

    var stk = $('psy-streaks');
    if(stk){
      var s = data.streaks || {};
      stk.innerHTML =
        'চলতি স্ট্রিক: <b class="psy-bad">' + (s.current || 0) + ' লস</b><br>'
        + 'সর্বোচ্চ স্ট্রিক (৭ দিন): <b>' + (s.max || 0) + ' লস</b><br>'
        + '<span class="psy-warn">৩ লসের পর ৩০ মিনিট বিরতি — রেভেঞ্জ ট্রেড = ধ্বংস</span>';
    }

    var hrs = $('psy-hours');
    if(hrs){
      var best = (data.best_hours || []).map(function(h){
        return fmtHour(h.utc_hour, h.win_pct, h.n);
      }).join('<br>');
      var worst = (data.worst_hours || []).map(function(h){
        return fmtHour(h.utc_hour, h.win_pct, h.n);
      }).join('<br>');
      hrs.innerHTML = 'সেরা:<br>' + (best || '—') + '<br>খারাপ:<br>'
        + (worst || '—') + '<br><span class="psy-dir-split">বাংলাদেশ সময় = UTC+6</span>';
    }

    var rec = $('psy-rec');
    if(rec){
      var rl = (data.recommended_pairs || []).map(fmtPairRow);
      rec.innerHTML = rl.length ? rl.join('<br>')
        : '<span class="psy-warn">এখনো কোনো পেয়ার Wilson-verified প্রফিটেবল নয় — সিলেক্টিভ থাকুন</span>';
    }

    var av = $('psy-avoid');
    if(av){
      var al = (data.avoid_pairs || []).map(fmtPairRow);
      av.innerHTML = al.length ? al.join('<br>') : '—';
    }

    var st = $('psy-stake');
    if(st){
      var sr = data.stake_rules || {};
      st.innerHTML =
        'প্রতি ট্রেড: <b>' + esc(sr.fixed_stake_pct || '1–2%') + '</b><br>'
        + 'মার্টিঙ্গেল: <b class="psy-bad">' + esc(sr.martingale || 'কখনোই না') + '</b><br>'
        + 'দৈনিক লস লিমিট: <b>' + esc(sr.daily_loss_limit_pct || 10) + '%</b>';
    }

    var rules = $('psy-rules');
    if(rules){
      var rs = data.psychology_rules || [];
      rules.innerHTML = rs.map(function(r){
        return '<div class="psy-rule"><b>' + esc(r.title) + '</b> — '
          + esc(r.detail) + '</div>';
      }).join('');
    }

    var honest = $('psy-honest');
    if(honest){ honest.textContent = data.honest_summary || ''; }
  }

  var psyLoading = false;
  function fetchPsychology(){
    if(psyLoading) return;
    psyLoading = true;
    fetch('/api/psychology?period=60&days=7')
      .then(function(r){ return r.json(); })
      .then(function(data){ psyLoading = false; renderPsychology(data); })
      .catch(function(){ psyLoading = false; });
  }

  function initWinrate(){
    if($('wr-pairlist') === null) return;   // not on a page with this tab
    wireChips('wr-market-filter', 'mkt', function(v){ wrState.market = v; });
    wireChips('wr-window-filter', 'window', function(v){ wrState.window = parseInt(v, 10) || 0; });
    wirePairRows();
    fetchPsychology();   // PSYCHOLOGY-FIX 2026-09-11

    var refreshBtn = $('wr-refresh-btn');
    if(refreshBtn && !refreshBtn.dataset.wired){
      refreshBtn.dataset.wired = '1';
      refreshBtn.addEventListener('click', function(){
        fetchWinrate();
        fetchPsychology();
      });
    }

    // Refresh when the tab becomes visible (fired by common.js switchTab).
    global.addEventListener('winrate:show', function(){ fetchWinrate(); fetchPsychology(); });

    // First fetch (tab may be opened before any event fires on some flows).
    fetchWinrate();

    // FIX (WR-POLL-VISIBILITY-2026-09-07, LOW): poll only while the pane is
    // actually shown — wasted DB load otherwise.
    if(wrTimer) clearInterval(wrTimer);
    wrTimer = setInterval(function(){
      var pane = document.getElementById('pane-winrate');
      if(pane && pane.classList.contains('active')) fetchWinrate();
    }, WR_REFRESH_MS);
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', initWinrate);
  } else {
    initWinrate();
  }

  // Expose for debugging + common.js hooks.
  global._wrDebug = { state: wrState, refresh: fetchWinrate,
                      displayFor: displayFor, wrClass: wrClass, fmtPct: fmtPct };
})(window);
