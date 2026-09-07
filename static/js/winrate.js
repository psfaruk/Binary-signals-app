/* ============================================================================
   winrate.js — Win Rate dashboard (Aurora v3, 2026-08-31)
   ----------------------------------------------------------------------------
   NEW TAB: "উইন রেট" — per-pair, per-direction (CALL vs PUT) win rates from
   the new /api/winrate endpoint (db.get_directional_winrate).

   User requirement this implements:
     "প্রত্যেক পেয়ার ও সিগন্যাল হিস্টোরি উইন রেট আমি দেখতে পারবো। Call ও put
      কোনো সিগন্যাল গুলো কেমন win রেট দিচ্ছে। সেই গুলো আলাদা আলাদা করে নিজের
      মতো করে দেখতে পারবো।"

   Self-contained IIFE — no coupling with common.js. Communicates via:
     - fetch('/api/winrate?period=60&days=N&category=X')
     - CustomEvent 'winrate:show' dispatched by common.js switchTab()
   ============================================================================ */
(function(global){
  'use strict';

  var WR_REFRESH_MS = 20000;   // background auto-refresh
  var wrState = { market:'all', window:7, dir:'all' };
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

  function applyDirFilter(pairs){
    if(wrState.dir === 'all') return pairs;
    // "শুধু CALL" → sort by CALL win rate; "শুধু PUT" → PUT win rate.
    var key = wrState.dir === 'CALL' ? 'call' : 'put';
    return pairs.slice().sort(function(a,b){
      var av = (a[key] && a[key].win_pct != null) ? a[key].win_pct : -1;
      var bv = (b[key] && b[key].win_pct != null) ? b[key].win_pct : -1;
      return bv - av;
    });
  }

  function renderHero(overall, windowDays){
    var pctEl = $('wr-hero-pct');
    var subEl = $('wr-hero-sub');
    var winEl = $('wr-hero-window');
    if(!pctEl) return;
    // FIX (WR-DIR-FILTER-2026-09-07): in "শুধু CALL"/"শুধু PUT" mode the hero
    // shows THAT direction's overall win rate (not the blended one) — the
    // user asked for call/put win rates separately per pair.
    var bucket = overall;
    var dirSuffix = '';
    if(overall && wrState.dir !== 'all' && overall[wrState.dir.toLowerCase()]){
      bucket = overall[wrState.dir.toLowerCase()];
      dirSuffix = ' · শুধু ' + wrState.dir;
      bucket = {
        graded: bucket.total, correct: bucket.correct,
        wrong: bucket.total - bucket.correct, win_pct: bucket.win_pct,
        draws: 0,
      };
    }
    if(!overall || !bucket || bucket.graded === 0){
      pctEl.textContent = '—';
      pctEl.className = 'wr-hero-pct mid';
      if(subEl) subEl.textContent = 'এই উইন্ডোতে কোনো গ্রেডেড সিগন্যাল নেই';
      if(winEl) winEl.textContent = '';
    } else {
      var pct = bucket.win_pct;
      pctEl.textContent = fmtPct(pct);
      pctEl.className = 'wr-hero-pct ' + wrClass(pct);
      if(subEl){
        subEl.textContent = bucket.correct + ' উইন / ' + bucket.graded
          + ' গ্রেডেড' + (overall.draws && wrState.dir === 'all' ? (' · ' + overall.draws + ' ড্র') : '')
          + dirSuffix;
      }
      if(winEl){
        winEl.textContent = (windowDays === 0 ? 'সব সময়'
          // FIX (BREAKEVEN-TEXT-2026-09-07): said "~54%" while the true
          // 85%-payout breakeven is 100/185 = 54.05% (core/breakeven.py).
          // server.py:1556's hardcoded 51.8 (=93% payout) was also wrong —
          // all surfaces now quote 54.05%.
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

  function renderPairCards(pairs){
    var wrap = $('wr-cards');
    if(!wrap) return;
    if(!pairs || !pairs.length){
      wrap.innerHTML = '<div class="wr-empty">এই ফিল্টারে কোনো ডেটা নেই — '
        + 'অন্য মার্কেট/সময় উইন্ডো দেখুন</div>';
      return;
    }
    var html = '';
    var list = applyDirFilter(pairs);
    // Find the best overall pair to badge it.
    var best = null;
    for(var i = 0; i < list.length; i++){
      var p = list[i];
      if(p.graded >= 10 && (best == null || (p.win_pct || 0) > (best.win_pct || 0))) best = p;
    }
    for(var j = 0; j < list.length; j++){
      var q = list[j];
      var isBest = best && q.asset === best.asset;
      var dirMode = wrState.dir;
      // FIX (WR-DIR-FILTER-2026-09-07): in "শুধু CALL"/"শুধু PUT" mode the
      // card headline shows THAT direction's own win rate + counts (the
      // per-direction sub-stats already exist in the payload) — not the
      // blended rate. The other direction stays visible in the dir-grid.
      var showBucket = q;
      if(dirMode === 'CALL' && q.call && q.call.total > 0){
        showBucket = { win_pct: q.call.win_pct, correct: q.call.correct, graded: q.call.total };
      } else if(dirMode === 'PUT' && q.put && q.put.total > 0){
        showBucket = { win_pct: q.put.win_pct, correct: q.put.correct, graded: q.put.total };
      }
      var wr = showBucket.win_pct;
      html += '<div class="wr-pair-card">'
        + '<div class="wr-pair-head">'
        +   '<span class="wr-pair-name">' + esc(displayFor(q.asset)) + '</span>'
        +   '<span class="wr-pair-tags">'
        +     '<span class="wr-tag ' + esc(q.category) + '">' + esc(q.category === 'otc' ? 'OTC' : 'REAL') + '</span>'
        +     (isBest ? '<span class="wr-tag best">★ BEST</span>' : '')
        +   '</span>'
        + '</div>'
        + '<div class="wr-pair-wr">'
        +   '<span class="num ' + wrClass(wr) + '">' + (wr == null ? '—' : wr.toFixed(1) + '%') + '</span>'
        +   '<span class="den">' + showBucket.correct + '/' + showBucket.graded + ' গ্রেডেড'
        +     (dirMode !== 'all' ? ' · ' + dirMode : '') + '</span>'
        + '</div>'
        + '<div class="wr-dir-grid">'
        +   '<div class="wr-dir-cell call">'
        +     '<div class="wr-dir-top"><span class="wr-dir-name">▲ CALL</span>'
        +       '<span class="wr-dir-val">' + fmtPct(q.call.win_pct) + '</span></div>'
        +     '<div class="wr-dir-bar"><div class="wr-dir-fill" style="width:'
        +       (q.call.win_pct != null ? q.call.win_pct : 0) + '%"></div></div>'
        +     '<span style="font-size:9.5px;color:var(--text-faint);font-family:var(--mono)">'
        +       q.call.correct + '/' + q.call.total + '</span>'
        +   '</div>'
        +   '<div class="wr-dir-cell put">'
        +     '<div class="wr-dir-top"><span class="wr-dir-name">▼ PUT</span>'
        +       '<span class="wr-dir-val">' + fmtPct(q.put.win_pct) + '</span></div>'
        +     '<div class="wr-dir-bar"><div class="wr-dir-fill" style="width:'
        +       (q.put.win_pct != null ? q.put.win_pct : 0) + '%"></div></div>'
        +     '<span style="font-size:9.5px;color:var(--text-faint);font-family:var(--mono)">'
        +       q.put.correct + '/' + q.put.total + '</span>'
        +   '</div>'
        + '</div>'
        + '<div class="wr-pair-foot">'
        +   '<span>স্ট্রিক: '
        +     (q.streak_type
        ?       '<span class="wr-streak ' + esc(q.streak_type) + '">'
        +       (q.streak_type === 'win' ? 'W' : 'L') + q.streak_count + '</span>'
        :      '—')
        +   '</span>'
        +   '<span class="wr-last ' + esc((q.last_signal || '').toLowerCase()) + '">'
        +     (q.last_signal ? ('শেষ: ' + esc(q.last_signal)) : '')
        +     (q.last_accuracy === 'correct' ? ' ✅' : q.last_accuracy === 'wrong' ? ' ❌' : '')
        +   '</span>'
        + '</div>'
        + '</div>';
    }
    wrap.innerHTML = html;
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
          renderPairCards([]);
          return;
        }
        renderHero(data.overall, wrState.window);
        renderPairCards(data.pairs);
      })
      .catch(function(){
        wrLoading = false;
        var wrap = $('wr-cards');
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

  function initWinrate(){
    if($('wr-cards') === null) return;   // not on a page with this tab
    wireChips('wr-market-filter', 'mkt', function(v){ wrState.market = v; });
    wireChips('wr-window-filter', 'window', function(v){ wrState.window = parseInt(v, 10) || 0; });
    wireChips('wr-dir-filter', 'dir', function(v){ wrState.dir = v; });

    var refreshBtn = $('wr-refresh-btn');
    if(refreshBtn && !refreshBtn.dataset.wired){
      refreshBtn.dataset.wired = '1';
      refreshBtn.addEventListener('click', fetchWinrate);
    }

    // Refresh when the tab becomes visible (fired by common.js switchTab).
    global.addEventListener('winrate:show', function(){ fetchWinrate(); });

    // First fetch (tab may be opened before any event fires on some flows).
    fetchWinrate();

    // FIX (WR-POLL-VISIBILITY-2026-09-07, LOW): the 20s background poll ran
    // from page load whether the merged Results tab was visible or not —
    // wasted DB load (the endpoint aggregates signal_log) for data nobody
    // sees. Poll only while the pane is actually shown.
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

  // Expose for debugging.
  global._wrDebug = { state: wrState, refresh: fetchWinrate };
})(window);
