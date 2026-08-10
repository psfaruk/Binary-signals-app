/* ============================================================================
   common.js — Shared WebSocket + chart + signal rendering logic for both
   the Real Market and OTC Market pages.

   Exposes a single global function:
       window.initApp(category)   // category = "real" | "otc"

   FIXES vs. the old single-file index.html:
     - BUG-1 FIXED: renderPairs(msg) is called with the FULL payload object
       (which has real_pairs + otc_pairs), not msg.pairs.
     - Stale UI fields #ms-phase / #ms-structure / #ms-zigzag REMOVED — the
       new engine doesn't compute them and they were always set to '—'.
     - One single setCategory(newCat) function replaces the 3 duplicate
       category-switch handlers (3-dot menu, market badge, cat-tabs).
       setCategory saves to localStorage AND navigates to the other HTML page.
     - Redundant client-side WS message filter REMOVED — the server's
       broadcast() now filters by interested_cids per stream, so each
       client only receives messages for assets it actually subscribed to.
     - MODULE_NAMES single source of truth on JS side (7 modules including
       trend_follow); only the active engine's 6th module is shown.
     - Responsive chart: chart.applyOptions({width, height}) on window resize.
     - Mobile-friendly: pinch-to-zoom disabled on chart container via CSS
       touch-action:none (LightweightCharts handles touch internally).
   ============================================================================ */

(function(global){
'use strict';

/* ─── CONFIG ─────────────────────────────────────────────────────────────── */
const WS_URL = (location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws';
const RECONNECT_BASE = 1000;
const RECONNECT_MAX = 30000;
const TICK_TAPE_MAX = 40;
// FIX (AUDIT-CRITICAL #002, 2026-07-21): HISTORY_MAX was 30 but server
// returns 50. Aligned to 100 so users see a longer, accurate history.
// Also see ISSUE-001: onEoc now always refreshes history (was gated on
// msg.prediction being truthy, which never happens — feed.py broadcasts
// prediction=null on EOC and delivers the real prediction via tick a few
// seconds later). Combined with dedup-by-ctime (ISSUE-009), this fixes
// the user-reported '48-signal limit' bug where new signals stopped
// appearing after a fixed count.
const HISTORY_MAX = 100;

/* ─── MODULE NAMES — single source of truth (mirrors core.constants.MODULE_NAMES) ──
   7 modules total: 5 shared + 1 OTC-specific (otc_pattern) + 1 Real-specific
   (trend_follow). Each engine displays only its own 6 modules. */
const MODULE_NAMES = [
  'candle_reaction',
  'running_tick',
  'pattern',
  'indicator',
  'key_level',
  'otc_pattern',     // OTC engine's 6th module
  'trend_follow',    // Real engine's 6th module
];
const MODULE_DISPLAY = {
  'candle_reaction': 'Candle Reaction',
  'running_tick':    'Running Tick',
  'pattern':         'Pattern',
  'indicator':       'Indicator',
  'key_level':       'Key Level',
  'otc_pattern':     'OTC Pattern',
  'trend_follow':    'Trend Follow',
};
// Active engine's 6-module set (5 shared + engine-specific).
const OTC_MODULES  = ['candle_reaction','running_tick','pattern','indicator','key_level','otc_pattern'];
const REAL_MODULES = ['candle_reaction','running_tick','pattern','indicator','key_level','trend_follow'];

/* ─── STATE (reset on every initApp call) ────────────────────────────────── */
let ws = null, reconnectTimer = null, reconnectAttempts = 0;
let currentCategory = 'otc';         // set by initApp
let currentAsset = '';               // set by initApp based on category
let currentPeriod = 60;
let chart = null, candleSeries = null, ghostSeries = null;
let candleData = [];
let lastPrediction = null;
let signalHistory = [];
let totalCorrect = 0, totalSignals = 0;
let soundEnabled = false, audioCtx = null;
let realPairsList = [], otcPairsList = [], alltimeOtcPairsList = [], pairsList = [];
let currentMicro = null, runningConf = null;
let tapePrices = [], tapeDir = [];
let tickTimestamps = [];
let lastLivePrice = 0;
let lastTickAt = 0;
let staleTimeout = null;
let runningCandleOpenTime = 0;
let alertedCandleOpenTime = 0;
let alertedSignalDirection = null;
let lastMessageAt = 0;
let chartLoadingTimeout = null;
let _resizeTimer = null;
// FIX (DEEP-AUDIT-2026-07-26 / F-17-14 + F-17-16, HIGH): removed the unused
// `_tickRateInterval` and `_marketStatusInterval` declarations — the
// corresponding setInterval blocks were deleted (dead code targeting
// non-existent HTML elements #market-status-indicator / #tick-rate-val /
// #stat-active-cat / #stat-signals / #stat-winrate). The pagehide cleanup
// references were also removed.
let _countdownInterval = null, _keepaliveInterval = null;

/* Active tab — 'chart' (default) | 'history' | 'accuracy' */
let currentTab = 'chart';

/* Smooth-candle tween state (easeOutCubic) */
const TWEEN_MS = 480;
let _priceLines = [];
let _priceLinesRange = { lo: 0, hi: 0, step: 0 };
let _fromClose = 0, _fromHigh = 0, _fromLow = 0;
let _toClose = 0,   _toHigh = 0,   _toLow = 0;
let _rTime = 0, _rOpen = 0, _rClose = 0, _rHigh = 0, _rLow = 0;
let _tweenStart = 0, _rafActive = false;

/* DOM refs (filled in initApp so the IIFE can be re-entered if needed) */
let $ = null;
let countdownEl = null;
let detailOverlay = null, detailBody = null, detailTitle = null;

// FIX (DEEP-AUDIT-2026-07-26 / F-17-09, CRITICAL): guard flag for wireEvents()
// so re-entrant initApp calls (e.g. bfcache restore via pageshow) don't
// register duplicate event listeners. The DOM survives bfcache, so the
// previous listeners are still attached and functional.
let _eventsWired = false;
// FIX (DEEP-AUDIT-2026-07-26 / F-17-03, CRITICAL): track the active category
// so the bfcache pageshow handler can re-init the right page.
let _lastInitCategory = null;

/* ─── HELPERS ────────────────────────────────────────────────────────────── */
function esc(s){
  if(s == null) return '';
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function fmtPrice(v){
  if(v == null) return '—';
  const n = parseFloat(v);
  if(isNaN(n)) return '—';
  if(n === 0) return '0.00000';
  const abs = Math.abs(n);
  if(abs >= 1000) return n.toFixed(2);
  if(abs >= 100)  return n.toFixed(3);
  if(abs >= 10)   return n.toFixed(4);
  return n.toFixed(5);
}

/* ─── DOM HELPERS ────────────────────────────────────────────────────────── */
// FIX (UI-P0-2/3/4, 2026-07-21): null-safe DOM setter helpers. Previously
// renderPending/renderSignal/renderMicro accessed .textContent / .style
// on raw $(...) results without null checks — if any element was missing
// (or DOM not ready), the whole tick handler crashed silently. Now these
// helpers no-op on null and the render functions can't throw.
function _setText(id, text){
  const el = $(id);
  if(el) el.textContent = text;
  return el;
}
// FIX (DEAD-CODE-2026-07-21): removed _setHtml — never called. Render
// functions use innerHTML directly when needed.
function _setClass(id, cls){
  const el = $(id);
  if(el) el.className = cls;
  return el;
}
function _setStyle(id, prop, val){
  const el = $(id);
  if(el) el.style[prop] = val;
  return el;
}

/* ─── AUDIO ──────────────────────────────────────────────────────────────── */
function beep(freq=880, dur=0.12, vol=0.3){
  if(!soundEnabled) return;
  try{
    if(!audioCtx) audioCtx = new (window.AudioContext||window.webkitAudioContext)();
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.type = 'sine'; o.frequency.value = freq;
    g.gain.value = vol;
    g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + dur);
    o.connect(g); g.connect(audioCtx.destination);
    o.start(); o.stop(audioCtx.currentTime + dur);
  }catch(e){}
}
function signalBeep(){
  beep(1200, 0.08, 0.25);
  setTimeout(()=>beep(1600, 0.1, 0.25), 100);
}

/* ─── CHART ──────────────────────────────────────────────────────────────── */
function initChart(){
  const container = $('chart-container');
  if(!container) return;
  // FIX (CRASH-2026-07-30): if chart already exists (double-init via bfcache
  // restore or rapid setCategory), remove it first to prevent duplicate chart
  // instances stacked on the same container (causes visual glitch + memory leak).
  if(chart){
    try{ chart.remove(); }catch(_){}
    chart = null;
    candleSeries = null;
    ghostSeries = null;
  }
  // FIX (CRASH-2026-07-30): guard against LightweightCharts not loaded yet
  // (shouldn't happen — safeInit checks — but defensive)
  if(typeof LightweightCharts === 'undefined'){
    console.error('[chart] LightweightCharts not loaded');
    return;
  }
  chart = LightweightCharts.createChart(container, {
    autoSize: true,
    layout: { background: { color: '#131722' }, textColor: '#8b949e', fontSize: 11 },
    grid: {
      vertLines: { color: 'rgba(48,54,61,.3)' },
      horzLines: { color: 'rgba(48,54,61,.3)' },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: 'rgba(88,166,255,.3)', width: 1, style: 2, labelBackgroundColor: '#2196F3' },
      horzLine: { color: 'rgba(88,166,255,.3)', width: 1, style: 2, labelBackgroundColor: '#2196F3' },
    },
    rightPriceScale: { borderColor: '#30363d', scaleMargins: { top: 0.1, bottom: 0.1 } },
    timeScale: { borderColor: '#30363d', timeVisible: true, secondsVisible: false, rightOffset: 3 },
    // FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-13 + AUDIT-5-14): the vendored
    // LightweightCharts v4.1.3 default time formatter uses getUTCHours()/
    // getUTCMinutes() — confirmed by grep on the bundle (5× getUTCDate, 2×
    // getUTCSeconds, 2× getUTCMinutes, 2× getUTCHours, ZERO getHours/Minutes/
    // Seconds). So the chart x-axis displayed UTC by default while the
    // history list (renderHistory) and detail modal (showSignalDetail) used
    // toLocaleTimeString / toLocaleString → LOCAL time. A UTC+6 user looking
    // at a 12:00 UTC candle saw "12:00" on the chart but "18:00" in history
    // for the same signal — impossible to correlate. This timeFormatter
    // overrides the default to display LOCAL time, matching the history list
    // and modal. The `time` arg is a UTCTimestamp (UTC seconds).
    localization: {
      timeFormatter: (time) => {
        try {
          return new Date(time * 1000).toLocaleTimeString('en-US',
            { hour: '2-digit', minute: '2-digit', hour12: false });
        } catch(_) {
          return new Date(time * 1000).toISOString().substring(11, 16);
        }
      },
    },
    handleScroll: { vertTouchDrag: false },
  });
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-27, HIGH): EXPECTS LightweightCharts v4.x.
  // The vendored library at /static/lightweight-charts.js is v4.1.3 (April 2024).
  // In v5 (current stable), addCandlestickSeries() is removed in favour of
  // chart.addSeries(CandlestickSeries, options). If the library is upgraded
  // to v5, both calls below will throw — breaking the chart entirely. Pin the
  // library at v4.x explicitly (e.g., via a <script integrity> hash in HTML).
  candleSeries = chart.addCandlestickSeries({
    upColor: '#00c853', downColor: '#ff1744',
    borderUpColor: '#00c853', borderDownColor: '#ff1744',
    wickUpColor: '#00c853', wickDownColor: '#ff1744',
  });
  ghostSeries = chart.addCandlestickSeries({
    upColor: 'rgba(0,200,83,0.25)', downColor: 'rgba(255,23,68,0.25)',
    borderUpColor: 'rgba(0,200,83,0.25)', borderDownColor: 'rgba(255,23,68,0.25)',
    wickUpColor: 'rgba(0,200,83,0.25)', wickDownColor: 'rgba(255,23,68,0.25)',
  });
  chart.timeScale().subscribeVisibleTimeRangeChange(() => { refreshPriceLines(); });

  // Responsive chart — re-apply explicit width/height on window resize so the
  // chart stays in sync with the container. autoSize:true handles most cases,
  // but on mobile orientation change / browser chrome show-hide the explicit
  // resize call gives an immediate repaint (autoSize has a small delay).
  window.addEventListener('resize', () => {
    if(_resizeTimer) clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => {
      if(!chart || !container) return;
      try{
        chart.applyOptions({
          width: container.clientWidth,
          height: container.clientHeight,
        });
      }catch(_){}
      refreshPriceLines();
    }, 120);
  });
}

/* Pick a "nice" round-number step for price lines based on visible range. */
function pickPriceStep(priceRange){
  if(priceRange <= 0) return 0.00010;
  const targetLines = 20;
  const rawStep = priceRange / targetLines;
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const norm = rawStep / mag;
  if(norm <= 1)      return 1 * mag;
  if(norm <= 2)      return 2 * mag;
  if(norm <= 5)      return 5 * mag;
  return 10 * mag;
}

/* Build / refresh the dense round-number price-level grid on candleSeries.
   Only recreates lines when the visible price range shifts beyond the
   existing grid — avoids leaking price lines on every tick. */
function refreshPriceLines(){
  if(!candleSeries || !candleData.length) return;
  const visRange = chart.timeScale().getVisibleRange();
  let lo = Infinity, hi = -Infinity;
  if(visRange && visRange.from && visRange.to){
    for(const c of candleData){
      if(c.time >= visRange.from && c.time <= visRange.to){
        if(c.low < lo) lo = c.low;
        if(c.high > hi) hi = c.high;
      }
    }
  }
  if(!isFinite(lo) || !isFinite(hi)){
    const recent = candleData.slice(-100);
    lo = Math.min(...recent.map(c => c.low));
    hi = Math.max(...recent.map(c => c.high));
  }
  if(!isFinite(lo) || !isFinite(hi) || lo <= 0 || hi <= 0) return;

  const pad = (hi - lo) * 0.10;
  lo -= pad; hi += pad;

  const step = pickPriceStep(hi - lo);
  if(step <= 0) return;

  const gridLo = Math.floor(lo / step) * step;
  const gridHi = Math.ceil(hi / step) * step;

  if(_priceLinesRange.step === step &&
     _priceLinesRange.lo <= gridLo &&
     _priceLinesRange.hi >= gridHi) return;

  for(const pl of _priceLines){
    try{ candleSeries.removePriceLine(pl); }catch(_){}
  }
  _priceLines = [];

  let decimals = 5;
  if(step >= 1)        decimals = 2;
  else if(step >= 0.1) decimals = 3;
  else if(step >= 0.01) decimals = 4;
  else if(step >= 0.001) decimals = 5;
  else                 decimals = 6;

  for(let p = gridLo; p <= gridHi + step * 0.5; p += step){
    const price = Math.round(p / step) * step;
    if(price <= 0) continue;
    try{
      const pl = candleSeries.createPriceLine({
        price: price,
        color: 'rgba(139,148,158,0.25)',
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: '',
      });
      _priceLines.push(pl);
    }catch(_){}
  }
  _priceLinesRange = { lo: gridLo, hi: gridHi, step: step };
}

function updateChart(candles, predCandle, resetView){
  // FIX (2026-07-18, chart-crash bug): sanitize incoming candle data to
  // prevent LightweightCharts from crashing on:
  //   - Duplicate timestamps (causes "Assertion failed" error)
  //   - Non-ascending timestamps (causes render corruption)
  //   - NaN/Infinity values (causes blank chart)
  //   - high < low or open <= 0 (causes assertion errors)
  // We de-duplicate by time (keep last), sort ascending, and clamp values.
  const seen = new Map();
  for(const c of (candles || [])){
    if(!c) continue;
    const t = typeof c.time === 'number' ? Math.floor(c.time) : 0;
    if(t <= 0) continue;
    const o = +c.open, h = +c.high, l = +c.low, cl = +c.close;
    if(!isFinite(o) || !isFinite(h) || !isFinite(l) || !isFinite(cl)) continue;
    if(o <= 0 || h <= 0 || l <= 0 || cl <= 0) continue;
    // Clamp OHLC to be internally consistent
    const hi = Math.max(h, o, cl);
    const lo = Math.min(l, o, cl);
    seen.set(t, { time: t, open: o, high: hi, low: lo, close: cl });
  }
  candleData = Array.from(seen.values()).sort((a, b) => a.time - b.time);
  try{
    candleSeries.setData(candleData);
  }catch(e){
    console.error('[chart] setData error:', e);
  }
  if(candleData.length) _resetRaf(candleData[candleData.length-1]);
  // FIX (CRASH-FIX-2026-07-26 / FE-002): hideChartLoading() was only called
  // when candleData.length > 0. If the server returned an empty snapshot
  // (broker feed down, asset not configured, period gap, new pair with no
  // history yet), the chart-loading overlay stayed on "Loading…" forever,
  // even though the connection was perfectly fine. Now we ALWAYS hide the
  // loading overlay after setData, regardless of whether the array is empty.
  hideChartLoading();
  // Surface empty-snapshot state to the user so they know the connection
  // is fine but no data is available (not the same as "Loading…").
  if(!candleData.length){
    try{
      const _noData = document.getElementById('chart-no-data');
      if(_noData) _noData.style.display = 'block';
    }catch(_){}
  } else {
    try{
      const _noData = document.getElementById('chart-no-data');
      if(_noData) _noData.style.display = 'none';
    }catch(_){}
  }
  if(resetView && candleData.length){
    try{ chart.timeScale().scrollToPosition(3, false); }catch(_){}
  }
  if(predCandle){
    // Sanitize predCandle too — it must have a future timestamp
    const pt = typeof predCandle.time === 'number' ? Math.floor(predCandle.time) : 0;
    const po = +predCandle.open, ph = +predCandle.high, pl = +predCandle.low, pc = +predCandle.close;
    if(pt > 0 && isFinite(po) && isFinite(ph) && isFinite(pl) && isFinite(pc) && po > 0){
      const phi = Math.max(ph, po, pc);
      const plo = Math.min(pl, po, pc);
      try{
        ghostSeries.setData([{ time: pt, open: po, high: phi, low: plo, close: pc }]);
      }catch(e){ console.error('[chart] ghostSeries.setData error:', e); }
    } else {
      try{ ghostSeries.setData([]); }catch(_){}
    }
  } else {
    try{ ghostSeries.setData([]); }catch(_){}
  }
  _priceLinesRange = { lo: 0, hi: 0, step: 0 };
  refreshPriceLines();
}

/* easeOutCubic tween — fast start, smooth deceleration. 60fps. */
function easeOutCubic(t){ return 1 - Math.pow(1 - t, 3); }

function _rafFrame(ts){
  if(!_rafActive) return;
  if(_rTime > 0 && _rOpen > 0 && candleSeries){
    const elapsed = ts - _tweenStart;
    const progress = Math.min(elapsed / TWEEN_MS, 1.0);
    const eased = easeOutCubic(progress);
    _rClose = _fromClose + (_toClose - _fromClose) * eased;
    _rHigh  = _fromHigh  + (_toHigh  - _fromHigh)  * eased;
    _rLow   = _fromLow   + (_toLow   - _fromLow)   * eased;
    // FIX (2026-07-18): ensure all values are finite before applying.
    if(!isFinite(_rClose) || !isFinite(_rHigh) || !isFinite(_rLow)){
      _rafActive = false;
      return;
    }
    const safeHigh = Math.max(_rHigh, _rClose, _rOpen);
    const safeLow  = Math.min(_rLow,  _rClose, _rOpen);
    const safeClose = Math.min(safeHigh, Math.max(safeLow, _rClose));
    if(safeHigh > 0 && safeLow > 0 && safeClose > 0){
      try{
        candleSeries.update({ time: _rTime, open: _rOpen, high: safeHigh, low: safeLow, close: safeClose });
      }catch(e){
        // Suppress LightweightCharts assertion errors — they happen when
        // the chart is in a transitional state (e.g. setData + update
        // racing). The next tick will retry.
      }
    }
    if(progress >= 1.0){ _rafActive = false; return; }
  } else {
    _rafActive = false;
    return;
  }
  requestAnimationFrame(_rafFrame);
}

function _startRaf(){
  if(_rafActive) return;
  _rafActive = true;
  _tweenStart = performance.now();
  requestAnimationFrame(_rafFrame);
}

function _setTarget(candle, perfNow){
  if(!candle || !candle.open || candle.open <= 0 || !candle.time) return;
  if(_rTime > 0 && candle.time < _rTime) return;
  const isNewCandle = (_rTime !== candle.time);
  if(isNewCandle){
    _rTime = candle.time; _rOpen = candle.open;
    _rClose = candle.open; _rHigh = candle.open; _rLow = candle.open;
  }
  _fromClose = _rClose; _fromHigh = _rHigh; _fromLow = _rLow;
  _toClose = candle.close; _toHigh = candle.high; _toLow = candle.low;
  _tweenStart = perfNow || performance.now();
}

function _resetRaf(candle){
  if(candle){
    _rTime = candle.time; _rOpen = candle.open;
    _rClose = candle.close; _rHigh = candle.high; _rLow = candle.low;
    _fromClose = _toClose = candle.close;
    _fromHigh = _toHigh = candle.high;
    _fromLow = _toLow = candle.low;
  } else { _rTime = 0; }
  _tweenStart = performance.now();
}

function updateLastCandle(candle){
  // FIX (2026-07-18, chart-crash bug): sanitize the incoming tick candle
  // before applying it. Reject NaN/Infinity, non-positive prices, and
  // clamp OHLC to be internally consistent (high >= max(open,close),
  // low <= min(open,close)). This prevents LightweightCharts.update()
  // from throwing assertion errors that would silently kill the chart.
  if(!candle) return;
  const t  = typeof candle.time  === 'number' ? Math.floor(candle.time)  : 0;
  const o  = +candle.open, h  = +candle.high, l  = +candle.low, c  = +candle.close;
  if(t <= 0) return;
  if(!isFinite(o) || !isFinite(h) || !isFinite(l) || !isFinite(c)) return;
  if(o <= 0 || h <= 0 || l <= 0 || c <= 0) return;
  const hi = Math.max(h, o, c);
  const lo = Math.min(l, o, c);
  const safeCandle = { time: t, open: o, high: hi, low: lo, close: c };

  if(!candleData.length){
    candleData.push(safeCandle);
    try{ if(candleSeries) candleSeries.setData(candleData); }catch(e){ console.error('[chart] setData(empty) error:', e); }
    _resetRaf(safeCandle);
    // FIX (CRASH-FIX-2026-07-26 / FE-003): when the first tick arrives
    // before any snapshot, the chart-loading overlay was never cleared
    // because this branch returned before hideChartLoading(). Now we
    // hide it so the user sees the tick immediately.
    hideChartLoading();
    try{
      const _noData = document.getElementById('chart-no-data');
      if(_noData) _noData.style.display = 'none';
    }catch(_){}
    return;
  }
  const last = candleData[candleData.length-1];
  if(safeCandle.time < last.time) return;  // skip old ticks
  if(safeCandle.time !== last.time){
    // New candle — append and auto-scroll to keep it visible.
    candleData.push(safeCandle);
    if(candleData.length > 500) candleData.shift();
    _setTarget(safeCandle);
    _startRaf();
    // Auto-scroll to show the new candle (only if user is at the right edge)
    try{
      if(chart && chart.timeScale().isVisible()){
        chart.timeScale().scrollToPosition(3, false);
      }
    }catch(_){}
    return;
  }
  // Same candle: backend is the source of truth — take its values directly.
  last.open  = safeCandle.open;
  last.high  = safeCandle.high;
  last.low   = safeCandle.low;
  last.close = safeCandle.close;
  _setTarget(last);
  _startRaf();
}

/* ─── SIGNAL DISPLAY ─────────────────────────────────────────────────────── */
function showChartLoading(){
  const overlay = $('chart-loading-overlay');
  if(!overlay) return;
  overlay.classList.add('show');
  clearTimeout(chartLoadingTimeout);
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-28, HIGH): on 20s timeout, surface an
  // error message instead of silently hiding the overlay. Previously, if the
  // WS connected but never delivered a snapshot (server bug, network issue),
  // the overlay disappeared after 20s and the user saw a blank chart with no
  // explanation. Now: replace the loading text with an error message AND keep
  // the overlay visible so the user knows something went wrong (the next
  // snapshot/tick arrival will hideChartLoading() naturally if data resumes).
  chartLoadingTimeout = setTimeout(() => {
    const txt = $('chart-loading-text');
    if(txt) txt.textContent = 'No data received — check connection';
    // Keep the overlay visible (don't remove 'show') so the message is seen.
  }, 20000);
}
function hideChartLoading(){
  const overlay = $('chart-loading-overlay');
  if(!overlay) return;
  clearTimeout(chartLoadingTimeout);
  overlay.classList.remove('show');
}

function renderPending(){
  // FIX (PENDING-FIX-2026-07-22): if we have a lastPrediction that's NEUTRAL,
  // show it as NEUTRAL instead of PENDING. The user complaint 'সবসময় পেন্ডিং
  // দেখায়' was because NEUTRAL predictions (from WEAK→NEUTRAL conversion)
  // were being overwritten with PENDING on every EOC. Now: if lastPrediction
  // exists and is NEUTRAL, keep showing it. Only show PENDING if there's
  // truly no prediction yet (fresh page load).
  if(lastPrediction && lastPrediction.signal === 'NEUTRAL'){
    renderSignal(lastPrediction);
    return;
  }
  // FIX (PENDING-FIX-2026-07-22): if we have ANY lastPrediction, keep showing
  // it instead of flashing to PENDING. The prediction is still valid — it's
  // just being re-evaluated for the next candle. PENDING should only show
  // on the very first load before any prediction arrives.
  if(lastPrediction){
    renderSignal(lastPrediction);
    return;
  }
  // Truly no prediction yet — show PENDING.
  _setClass('signal-box', 'pending signal-pop');
  _setText('signal-label', '⏳ PENDING');
  _setText('sig-score', '—');
  _setStyle('sig-score', 'color', 'var(--text-dim)');
  _setClass('sig-strength', 'value strength-weak');
  _setText('sig-strength', 'WAIT');
  _setText('sig-conf-val', '—');
  _setStyle('sig-conf-bar', 'width', '0%');
  _setStyle('sig-conf-bar', 'background', '#2196F3');
  _setText('sig-agree', '—');
  _setText('sig-regime', '—');

  // Market state panel
  _setText('ms-trend', '—');
  _setText('ms-zone', '—');
  _setText('ms-volatility', '—');

  // FIX (UI-P1-2): reset microstructure fields too.
  _setStyle('bs-buy', 'width', '50%');
  _setStyle('bs-sell', 'width', '50%');
  _setText('bs-buy', '50%');
  _setText('bs-sell', '50%');
  _setClass('ms-pressure', 'pressure-badge fight');
  _setText('ms-pressure', '—');
  _setClass('ms-reaction', 'react-badge none');
  _setText('ms-reaction', '—');
  _setText('ms-fight', '—');
  _setText('ms-hold', '—');
  // Reset phase cards (Early/Mid/Late)
  ['ph-early','ph-mid','ph-late'].forEach(id => {
    _setClass(id, 'phase-dir flat');
    _setText(id, '—');
  });
  _setClass('ms-last-react', 'react-badge none');
  _setText('ms-last-react', '—');
  _setClass('ms-running-conf', 'running-conf none');
  _setText('ms-running-conf', '—');
  _setText('ms-ticks', '0');
  _setText('ms-net', '—');
  _setText('ms-round', '—');

  const theoriesList = $('theories-list');
  if(theoriesList){
    theoriesList.innerHTML = '';
    theoriesList.classList.remove('open');
    const toggle = $('theories-toggle');
    if(toggle){ toggle.classList.remove('open'); toggle.setAttribute('aria-expanded','false'); }
  }

  alertedCandleOpenTime = 0;
  alertedSignalDirection = null;
}

function renderSignal(pred){
  if(!pred) return;
  const s = pred.signal || 'NEUTRAL';

  // Alert dedup — one beep per candle, plus on direction change.
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-65, MEDIUM): guard for
  // runningCandleOpenTime === 0. Before the first candle arrives,
  // runningCandleOpenTime is 0, so `candleKey !== alertedCandleOpenTime`
  // (0 !== 0 → false initially, but the very first renderSignal call sets
  // alertedCandleOpenTime = 0, so the second call IS flagged as a new candle
  // and beeps — even on a NEUTRAL prediction. Now: don't beep if no candle
  // has arrived yet (runningCandleOpenTime === 0) — wait until we actually
  // have a candle boundary to dedup against. shouldAlert stays declared at
  // function scope (referenced later for signal-pop class + signalBeep).
  const candleKey = runningCandleOpenTime || 0;
  let shouldAlert = false;
  if(!candleKey){
    // No candle yet — still record the signal direction so the first real
    // candle can detect a direction change.
    alertedSignalDirection = s;
    alertedCandleOpenTime = 0;
  } else {
    const isNewCandle = candleKey !== alertedCandleOpenTime;
    const isDirectionChange = alertedSignalDirection !== s;
    shouldAlert = isNewCandle || isDirectionChange;
    if(shouldAlert){
      alertedCandleOpenTime = candleKey;
      alertedSignalDirection = s;
    }
  }

  // FIX (UI-P0-3, 2026-07-21): null-safe setters throughout. Previously
  // any missing element crashed the entire tick handler.
  const cls = s === 'CALL' ? 'call' : s === 'PUT' ? 'put' : 'neutral';
  _setClass('signal-box', cls + (shouldAlert ? ' signal-pop' : ''));
  _setText('signal-label', s === 'CALL' ? '🟢 CALL' : s === 'PUT' ? '🔴 PUT' : '➖ NEUTRAL');

  const score = pred.score || 0;
  // FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-18): when score===0 (NEUTRAL
  // prediction with equal CALL/PUT votes), the old display showed "+0" in
  // green — green typically implies a CALL/positive bias, but score=0 is
  // genuinely neutral. Now: no "+" prefix for 0, and neutral color (text-dim)
  // for score===0 so the score-color matches the "➖ NEUTRAL" label.
  _setText('sig-score', (score > 0 ? '+' : '') + score);
  _setStyle('sig-score', 'color',
    score > 0 ? 'var(--green)' : score < 0 ? 'var(--red)' : 'var(--text-dim)');

  const str = (pred.strength || '').toUpperCase();
  const strCls = str === 'STRONG'
    ? (s === 'PUT' ? 'strength-strong-put' : 'strength-strong')
    : str === 'MEDIUM' ? 'strength-medium' : 'strength-weak';
  _setClass('sig-strength', 'value ' + strCls);
  _setText('sig-strength', str || '—');

  // FIX (UI-SIGNAL-ENHANCE, 2026-07-23): prominent strength badge on
  // the signal box itself. Color-coded by tier (strong=green glow,
  // medium=blue, neutral=grey). Updated on every tick so it stays in
  // sync with the strength field above.
  const badgeCls = str === 'STRONG' ? 'strength-badge strong'
                 : str === 'MEDIUM' ? 'strength-badge medium'
                 : 'strength-badge neutral';
  _setClass('signal-strength-badge', badgeCls);
  _setText('signal-strength-badge', str || 'NEUTRAL');

  const conf = pred.confidence || 0;
  _setText('sig-conf-val', Math.round(conf) + '%');
  _setStyle('sig-conf-bar', 'width', conf + '%');
  _setStyle('sig-conf-bar', 'background',
            s === 'CALL' ? 'var(--green)' : s === 'PUT' ? 'var(--red)' : 'var(--text-dim)');

  _setText('sig-agree', (pred.agree || 0) + '/' + (pred.total || 0) + ' modules');

  // Regime display — clean format (no debug-looking output).
  // FIX (UI-P1-15/16, 2026-07-21): drop the "(str=0.85)" debug suffix;
  // the strength number is shown in Market State as a label.
  const reg = pred.regime || {};
  const regStr = (typeof reg === 'object' && reg !== null)
    ? (reg.regime || '—')
    : String(reg || '—');
  _setText('sig-regime', regStr);

  // FIX (DEEP-AUDIT-2026-07-26 / F-17-01, CRITICAL): display algorithm strategy
  // in signal panel. Previously the JS only unhid the inner <span
  // id="sig-strategy"> but never the parent <div id="sig-strategy-row"
  // style="display:none">, so the entire feature was invisible. Now we also
  // unhide the parent row when a non-default strategy is present.
  const stratName = pred.strategy || '';
  const stratReason = pred.strategy_reason || '';
  const stratEl = $('sig-strategy');
  const stratRow = $('sig-strategy-row');
  if(stratEl){
    if(stratName && stratName !== 'default'){
      stratEl.textContent = stratName;
      stratEl.title = stratReason;
      stratEl.style.display = 'block';
      if(stratRow) stratRow.style.display = '';
    } else {
      stratEl.textContent = '—';
      stratEl.style.display = 'none';
      if(stratRow) stratRow.style.display = 'none';
    }
  }

  // Market State panel — only 3 live fields.
  const trendVal = reg.regime || '—';
  const zoneVal = reg.is_volatile ? 'VOLATILE'
                : reg.is_trending ? 'TREND'
                : reg.is_ranging  ? 'RANGE' : '—';
  const volVal = reg.volatility_pct
    ? (reg.volatility_pct > 1.3 ? 'HIGH'
       : reg.volatility_pct < 0.7 ? 'LOW' : 'NORMAL')
    : '—';

  const msTrend = $('ms-trend');
  if(msTrend){
    msTrend.textContent = trendVal;
    msTrend.style.color = trendVal === 'TREND_UP' ? 'var(--green)'
                        : trendVal === 'TREND_DOWN' ? 'var(--red)'
                        : trendVal === 'VOLATILE' ? 'var(--yellow)' : 'var(--text-dim)';
  }
  const msZone = $('ms-zone');
  if(msZone){
    msZone.textContent = zoneVal;
    msZone.style.color = zoneVal === 'TREND' ? 'var(--green)'
                       : zoneVal === 'VOLATILE' ? 'var(--yellow)'
                       : zoneVal === 'RANGE' ? 'var(--text-dim)' : 'var(--text-dim)';
  }
  const msVol = $('ms-volatility');
  if(msVol){
    msVol.textContent = volVal;
    msVol.style.color = volVal === 'HIGH' ? 'var(--red)'
                      : volVal === 'LOW' ? 'var(--text-dim)' : 'var(--text)';
  }

  // 6-Module Engine breakdown — display only the active engine's 6 modules.
  renderModuleBreakdown(pred);

  if(shouldAlert) signalBeep();

  // Active engine badge — shows which engine produced this prediction.
  const engineLabel = $('engine-label');
  if(engineLabel){
    // FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-16 + AUDIT-5-20): use
    // currentCategory as authoritative source for the engine-label decision
    // instead of pred.category. The engines router (engines/__init__.py)
    // normalizes category="alltime_otc" to "otc" before routing, so
    // pred.category is "otc" even for alltime_otc subscriptions — causing
    // the All-Time OTC page to always show "OTC ENGINE" in yellow instead
    // of "ALL-OTC ENGINE" in cyan (the alltime_otc accent color). The page's
    // currentCategory is authoritative for UI labeling. Added a third
    // branch for alltime_otc with the cyan accent color matching the rest
    // of the page (per common.js:2260 statActiveCat, alltime_otc.html
    // router link, etc.).
    const cat = currentCategory;
    if(cat === 'real'){
      engineLabel.textContent = 'REAL ENGINE';
      engineLabel.style.background = 'rgba(0,200,83,.15)';
      engineLabel.style.color = 'var(--green)';
    } else if(cat === 'alltime_otc'){
      engineLabel.textContent = 'ALL-OTC ENGINE';
      engineLabel.style.background = 'rgba(0,229,255,.15)';
      engineLabel.style.color = '#00e5ff';
    } else {
      engineLabel.textContent = 'OTC ENGINE';
      engineLabel.style.background = 'rgba(255,193,7,.15)';
      engineLabel.style.color = 'var(--yellow)';
    }
  }
}

/* Display the 6-module engine breakdown. Uses the active category's 6-module
   list (5 shared + 1 engine-specific). Falls back to raw reasons if no module
   breakdown was computed. */
function renderModuleBreakdown(pred){
  const theoriesList = $('theories-list');
  const theoriesToggle = $('theories-toggle');
  if(!theoriesList) return;

  theoriesList.innerHTML = '';
  const modules = pred.modules || {};

  // Pick the active category's 6-module set. If the prediction's category
  // field is set and differs from currentCategory, trust the prediction's
  // category (it's authoritative — that's the engine that produced it).
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-32, HIGH): document that all-time OTC
  // uses the OTC engine (intentional — engines/__init__.py routes
  // category='alltime_otc' through the OTC engine). So OTC_MODULES (with
  // otc_pattern as the 6th module) is the correct module set for the
  // alltime_otc page. The predCat fallback to currentCategory doesn't help
  // because predCat === 'otc' matches first (the engines router normalizes
  // category='alltime_otc' to 'otc' before routing, so pred.category is
  // 'otc' even for alltime_otc subscriptions).
  const predCat = pred.category || currentCategory;
  const moduleSet = predCat === 'real' ? REAL_MODULES : OTC_MODULES;

  let hasModuleVote = false;
  moduleSet.forEach(mname => {
    const m = modules[mname];
    if(!m || !m.fired) return;
    hasModuleVote = true;
    const dir = (m.direction || 'NEUTRAL').toUpperCase();
    const score = m.score || 0;
    const label = MODULE_DISPLAY[mname] || mname;
    const div = document.createElement('div');
    div.className = 'theory-item ' + (dir === 'CALL' ? 'call-vote' : dir === 'PUT' ? 'put-vote' : '');
    const scoreStr = score > 0 ? ' (' + (dir === 'CALL' ? '+' : '') + score + ')' : '';
    div.innerHTML = '<span class="theory-name">' + esc(label) + '</span>'
                  + '<span class="theory-vote ' + dir.toLowerCase() + '">' + dir + scoreStr + '</span>';
    theoriesList.appendChild(div);
    if(m.reasons && m.reasons.length){
      m.reasons.forEach(r => {
        const rdiv = document.createElement('div');
        rdiv.className = 'theory-item reason-detail';
        rdiv.innerHTML = '<span class="theory-name" style="padding-left:8px;font-size:10px;color:var(--text-dim)">↳ ' + esc(r) + '</span>';
        theoriesList.appendChild(rdiv);
      });
    }
  });

  if(hasModuleVote){
    theoriesList.classList.add('open');
    theoriesToggle.classList.add('open');
    theoriesToggle.setAttribute('aria-expanded','true');
  } else {
    // Fallback: show raw reasons if no module breakdown fired.
    const reasons = pred.reasons || [];
    if(reasons.length){
      theoriesList.classList.add('open');
      theoriesToggle.classList.add('open');
      theoriesToggle.setAttribute('aria-expanded','true');
      reasons.forEach(r => {
        const ru = String(r).toUpperCase();
        const isPut = ru.includes('PUT') || ru.includes('BEAR') || ru.includes('SELLER') || ru.includes('RESISTANCE');
        const div = document.createElement('div');
        div.className = 'theory-item ' + (isPut ? 'put-vote' : 'call-vote');
        div.innerHTML = '<span class="theory-name">' + esc(r) + '</span>';
        theoriesList.appendChild(div);
      });
    }
  }
}

/* ─── MICROSTRUCTURE ─────────────────────────────────────────────────────── */
function renderMicro(micro){
  if(!micro) return;
  currentMicro = micro;
  // FIX (UI-P0-4, 2026-07-21): null-safe throughout.
  const buyPct = Math.max(0, Math.min(100, Math.round(micro.buy_pct || 50)));
  const sellPct = 100 - buyPct;
  _setStyle('bs-buy',  'width', buyPct + '%');
  _setStyle('bs-sell', 'width', sellPct + '%');
  _setText('bs-buy',  buyPct + '%');
  _setText('bs-sell', sellPct + '%');

  const press = (micro.pressure || '—').toUpperCase();
  _setText('ms-pressure', press);
  _setClass('ms-pressure', 'pressure-badge ' + (press === 'BUYER' ? 'buyer' : press === 'SELLER' ? 'seller' : 'fight'));

  const react = (micro.reaction || '—').toUpperCase();
  _setText('ms-reaction', react === 'BUYER' ? 'BUYER REACT' : react === 'SELLER' ? 'SELLER REACT' : '—');
  _setClass('ms-reaction', 'react-badge ' + (react === 'BUYER' ? 'recovery' : react === 'SELLER' ? 'exhaust' : 'none'));

  const msFight = $('ms-fight');
  if(msFight){
    // FIX (DEEP-AUDIT-2026-07-26 / F-17-13, HIGH): XSS — escape micro.crosses
    // before interpolation into msFight.innerHTML. The server could send
    // arbitrary JSON; previously the value was inserted raw.
    const crossesStr = esc(micro.crosses != null ? String(micro.crosses) : '0');
    if(micro.is_fight){
      msFight.innerHTML = '<span style="color:var(--yellow);font-weight:700">⚠ YES</span> <span style="color:var(--text-dim);font-size:10px">(' + crossesStr + ' crosses)</span>';
    } else {
      msFight.innerHTML = 'No <span style="color:var(--text-dim);font-size:10px">(' + crossesStr + ' crosses)</span>';
    }
  }

  const msHold = $('ms-hold');
  if(msHold){
    if(micro.hold_price != null){
      // FIX (DEEP-AUDIT-2026-07-26 / F-17-13, HIGH): XSS — escape micro.hold_visits
      // (and fmtPrice already produces a finite number string for hold_price).
      const holdVisitsStr = esc(micro.hold_visits != null ? String(micro.hold_visits) : '0');
      msHold.innerHTML = '<span style="color:var(--blue)">' + esc(fmtPrice(micro.hold_price)) + '</span> <span style="color:var(--text-dim);font-size:10px">(' + holdVisitsStr + 'x)</span>';
    } else {
      msHold.textContent = '—';
    }
  }

  const phases = micro.phases || [];
  ['early','mid','late'].forEach((p, i) => {
    const el = $('ph-' + p);
    if(!el) return;
    // FIX (DEEP-AUDIT-2026-07-26 / F-17-62, MEDIUM): bounds-check the phases
    // array. Previously `const ph = phases[i]` assumed phases always had 3
    // elements — if the server sent `phases: ['UP', 'DOWN']` (only 2),
    // `phases[2]` was undefined, dir fell back to '—', and the late phase
    // silently showed '—' instead of an error.
    const ph = (i < phases.length) ? phases[i] : undefined;
    const dir = (typeof ph === 'string' ? ph : (ph && ph.dir ? ph.dir : '—')).toUpperCase();
    const arrows = dir === 'UP' ? '↑↑' : dir === 'DOWN' ? '↓↓' : '→';
    el.textContent = (dir !== '—' ? dir + ' ' : '') + arrows;
    el.className = 'phase-dir ' + (dir === 'UP' ? 'up' : dir === 'DOWN' ? 'down' : 'flat');
  });

  const lr = (micro.last_react || '').toUpperCase();
  _setText('ms-last-react', lr || '—');
  _setClass('ms-last-react', 'react-badge ' + (lr === 'EXHAUST' ? 'exhaust' : lr === 'RECOVERY' ? 'recovery' : 'none'));

  _setText('ms-running-conf', runningConf === 'CONFIRMING' ? '✅ CONFIRMING'
                          : runningConf === 'OPPOSING' ? '❌ OPPOSING' : '—');
  _setClass('ms-running-conf', 'running-conf ' + (runningConf === 'CONFIRMING' ? 'confirming' : runningConf === 'OPPOSING' ? 'opposing' : 'none'));

  _setText('ms-ticks', micro.tick_count || 0);
  const net = micro.net || 0;
  _setText('ms-net', (net >= 0 ? '+' : '') + net.toFixed(5));
  _setStyle('ms-net', 'color', net >= 0 ? 'var(--green)' : 'var(--red)');

  const rl = micro.round || {};
  const msRound = $('ms-round');
  if(msRound){
    if(rl.near_level){
      // FIX (DEEP-AUDIT-2026-07-26 / F-17-13, HIGH): XSS — escape rl.near_strength
      // before interpolation (it could be a non-numeric string from the server).
      const nearStrengthStr = esc(rl.near_strength != null ? String(rl.near_strength) : '—');
      msRound.innerHTML = '<span style="color:var(--yellow)">' + esc(fmtPrice(rl.near_level)) + '</span> <span style="color:var(--text-dim);font-size:10px">(' + nearStrengthStr + ')</span>';
    } else {
      msRound.textContent = '—';
    }
  }
}

/* ─── TICK TAPE ────────────────────────────────────────────────────────────
   FIX (DEEP-AUDIT-2026-07-26 / F-17-15, HIGH): DELETED addTapeTick() and
   renderTape(). The #tick-tape-inner element was removed from HTML but the
   JS populating logic wasn't removed. addTapeTick() ran on every tick
   (pushing to tapePrices[] / tapeDir[] up to TICK_TAPE_MAX=40) and called
   renderTape() which early-returned via `if(!tickTapeInner) return;` —
   pure dead code wasting CPU on every tick. Call sites in onSnapshot,
   onTick, onEoc, and the pair-select change handler also removed below.
   The tapePrices / tapeDir arrays and TICK_TAPE_MAX constant remain
   (referenced by reset logic in initApp + pair-select handler) but are
   no longer populated or read. */

/* ─── SIGNAL HISTORY ─────────────────────────────────────────────────────── */
// FIX (AUDIT-CRITICAL #009, 2026-07-21): dedup by ctime to prevent the
// same candle's signal from being added twice (e.g. when a watchdog
// restart re-broadcasts an EOC). Returns true if added, false if dup.
function _historyHasCtime(ctime){
  if(!ctime) return false;
  for(let i = signalHistory.length - 1; i >= 0; i--){
    const h = signalHistory[i];
    if(h && h.detail && h.detail.ctime === ctime) return true;
    // Only scan the most recent 5 — older entries are far enough back.
    if(signalHistory.length - i > 5) break;
  }
  return false;
}

function addHistory(signal, accuracy, detail){
  // FIX (AUDIT-CRITICAL #009): skip duplicate ctime entries.
  if(detail && detail.ctime && _historyHasCtime(detail.ctime)){
    // Update the existing entry's accuracy instead of duplicating.
    for(let i = signalHistory.length - 1; i >= 0; i--){
      const h = signalHistory[i];
      if(h && h.detail && h.detail.ctime === detail.ctime){
        h.accuracy = accuracy || h.accuracy || 'pending';
        h.detail = Object.assign({}, h.detail, detail);
        renderHistory();
        return;
      }
    }
    return;
  }
  signalHistory.push({ signal, accuracy: accuracy || 'pending', detail: detail || null });
  if(signalHistory.length > HISTORY_MAX) signalHistory.shift();
  renderHistory();
  setTimeout(() => { const hl = $('history-list'); if(hl) hl.scrollTop = 0; }, 50);
}

function loadServerHistory(){
  if(!ws || ws.readyState !== WebSocket.OPEN) return;
  send({ type: 'signals', asset: currentAsset, period: currentPeriod });
}

function onServerSignals(sigs, asset, period){
  if(!sigs) return;
  // Drop stale responses from a previous pair switch.
  if(asset && asset !== currentAsset) return;
  if(period && period !== currentPeriod) return;
  if(!sigs.length){
    // Empty list — render the empty state but don't wipe locally-added
    // entries that haven't been persisted yet (e.g. just-graded candle).
    if(!signalHistory.length) renderHistory();
    return;
  }
  // FIX (AUDIT-CRITICAL #007, 2026-07-21): merge instead of wholesale
  // replace. Previously a fresh server fetch WIPED any locally-added
  // entries (e.g. a just-graded candle whose row hadn't been written
  // yet) — causing the newest signal to vanish for ~500ms until the
  // next refresh. Now we dedupe by ctime and keep newer local data.
  // FIX (AUDIT-CORE #106, 2026-07-21): also support pagination. If the
  // server response includes `before_ctime` (from a Load-more request),
  // the new sigs are PREPENDED to signalHistory instead of replacing.
  const serverByCtime = {};
  const orderedCtimes = [];
  for(const s of sigs){
    const key = s.ctime || ('idx_' + orderedCtimes.length);
    serverByCtime[key] = s;
    orderedCtimes.push(key);
  }
  // Build the incoming server entries (oldest→newest).
  const incoming = [];
  for(const key of orderedCtimes){
    const s = serverByCtime[key];
    incoming.push({ signal: s.signal, accuracy: s.accuracy, detail: s });
  }
  // Determine if this is a pagination response (server sent before_ctime
  // in the response — server.py includes it when the request had it).
  // We detect by checking if the newest incoming ctime is OLDER than the
  // oldest existing local ctime — if so, this is a Load-more response.
  const isNewestIncomingOlder = (signalHistory.length > 0
    && signalHistory[0] && signalHistory[0].detail
    && signalHistory[0].detail.ctime
    && incoming.length > 0
    && incoming[incoming.length - 1].detail
    && incoming[incoming.length - 1].detail.ctime
    && incoming[incoming.length - 1].detail.ctime < signalHistory[0].detail.ctime);

  let merged;
  if(isNewestIncomingOlder){
    // Pagination response — prepend incoming to existing history.
    // Dedupe by ctime (in case of overlap at the boundary).
    const existingCtimes = new Set(
      signalHistory.filter(h => h && h.detail && h.detail.ctime)
                   .map(h => h.detail.ctime));
    const uniqueIncoming = incoming.filter(
      h => !h.detail || !h.detail.ctime || !existingCtimes.has(h.detail.ctime));
    merged = uniqueIncoming.concat(signalHistory);
  } else {
    // Normal refresh — preserve local entries whose ctime isn't in the
    // server response (they're newer than the server's snapshot).
    const preserved = [];
    for(const h of signalHistory){
      if(h && h.detail && h.detail.ctime && !serverByCtime[h.detail.ctime]){
        preserved.push(h);
      }
    }
    merged = incoming.concat(preserved);
  }
  // Cap at HISTORY_MAX (keep the newest).
  if(merged.length > HISTORY_MAX) merged.splice(0, merged.length - HISTORY_MAX);
  signalHistory = merged;
  const graded = sigs.filter(s => s.accuracy === 'correct' || s.accuracy === 'wrong');
  totalSignals = graded.length;
  totalCorrect = graded.filter(s => s.accuracy === 'correct').length;
  renderHistory();
  renderAccuracy();
  // FIX (UI-P1-7, 2026-07-21): only reset scroll to top if the user was
  // already near the top. Previously every refresh forced scrollTop=0,
  // disrupting users who were browsing older signals (especially after
  // clicking "Load older signals").
  setTimeout(() => {
    const hl = $('history-list');
    if(hl && hl.scrollTop < 80) hl.scrollTop = 0;
  }, 50);
}

function renderHistory(){
  const historyList = $('history-list');
  if(!historyList) return;

  // When the history tab is active, only show signals from the last 1 hour
  // (3600s). HISTORY_MAX (100) holds more than an hour of 60s candles, so this
  // filter keeps the view focused on what the user actually wants to see.
  // When the history tab is NOT active, render the full list (the rendering is
  // invisible but stays in sync so switching tabs is instant).
  const filterLastHour = (currentTab === 'history');
  const nowSec = Math.floor(Date.now() / 1000);
  const oneHourAgo = nowSec - 3600;
  const displayHistory = filterLastHour
    ? signalHistory.filter(h => h && h.detail && h.detail.ctime && h.detail.ctime >= oneHourAgo)
    : signalHistory;

  if(!displayHistory.length){
    historyList.innerHTML = '<div style="color:var(--text-dim);font-size:11px;padding:8px 4px">'
      + (filterLastHour ? 'No signals in the last hour' : 'No signals yet')
      + '</div>';
    return;
  }
  const lastIdx = displayHistory.length - 1;
  let html = '';
  for(let i = lastIdx; i >= 0; i--){
    const h = displayHistory[i];
    const isRecent = (i === lastIdx);
    // FIX (2026-07-18): clearer win/loss icons with text labels.
    //   correct  → ✅ WIN   (green)
    //   wrong    → ❌ LOSS  (red)
    //   draw     → ➖ DRAW  (yellow)
    //   pending  → ⏳ WAIT  (blue)
    // On phones (<400px) the text label is hidden via CSS — only the emoji shows.
    let iconEmoji, iconLabel;
    if(h.accuracy === 'correct'){
      iconEmoji = '✅'; iconLabel = 'WIN';
    } else if(h.accuracy === 'wrong'){
      iconEmoji = '❌'; iconLabel = 'LOSS';
    } else if(h.accuracy === 'draw'){
      iconEmoji = '➖'; iconLabel = 'DRAW';
    } else {
      iconEmoji = '⏳'; iconLabel = 'WAIT';
    }
    const sigCls = h.signal === 'CALL' ? 'call' : h.signal === 'PUT' ? 'put' : 'neutral';
    const strength = h.detail ? h.detail.strength : '';
    const strengthCls = strength === 'STRONG' ? 'STRONG' : strength === 'MEDIUM' ? 'MEDIUM' : 'WEAK';
    const strengthLetter = strength ? strength[0] : '·';
    // FIX (DEEP-AUDIT-2026-07-26 / F-17-42, HIGH): add a title attribute so
    // hovering reveals the full word. Previously the single-letter strength
    // indicator (S/M/W) was ambiguous — "S" could mean "Strong" or "Sell"
    // in trading context. Now the tooltip clarifies.
    const strengthTitle = strength ? ('Strength: ' + strength) : 'No strength';
    const scoreVal = h.detail ? h.detail.score : null;
    const score = scoreVal != null ? ((scoreVal >= 0 ? '+' : '') + scoreVal) : '';
    const scoreCls = scoreVal != null ? (scoreVal >= 0 ? 'pos' : 'neg') : '';
    const accCls = h.accuracy === 'correct' ? ' correct'
                 : h.accuracy === 'wrong'   ? ' wrong'
                 : h.accuracy === 'draw'    ? ' draw'
                 : h.accuracy === 'pending' ? ' pending'
                 : '';
    let recentCls = '';
    if(isRecent){
      recentCls = ' history-row-recent';
      if(h.signal === 'CALL') recentCls += ' history-call-tint';
      else if(h.signal === 'PUT') recentCls += ' history-put-tint';
    }
    const time = (h.detail && h.detail.ctime)
      ? new Date(h.detail.ctime * 1000).toLocaleTimeString('en-US', {hour:'2-digit',minute:'2-digit',hour12:false})
      : '--:--';
    // FIX (AUDIT-CORE #74, 2026-07-21): use ctime as the lookup key (not
    // the array index) so a click always opens the right signal even if
    // signalHistory was mutated between render and click. Index-based
    // lookup was fragile — pair-switch / addHistory / onServerSignals
    // could all shift indices.
    const clickKey = (h.detail && h.detail.ctime) ? String(h.detail.ctime) : '';
    const clickable = clickKey ? `onclick="window._showSignalDetail('${clickKey}')" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();window._showSignalDetail('${clickKey}')}"` : '';
    html += `<div class="history-row${accCls}${recentCls}" ${clickable}>`
         +  `<span class="history-time">${time}</span>`
         // FIX (DEEP-AUDIT-2026-07-26 / F-17-11, HIGH): XSS — escape h.signal before
         // interpolation. The signal field comes from the WS server response
         // (onServerSignals → s.signal) and could contain arbitrary HTML if the
         // server were compromised or a buggy payload sent. Previously it was
         // interpolated raw into historyList.innerHTML, allowing script
         // injection. esc() already existed but wasn't applied here.
         +  `<span class="history-signal ${sigCls}">${esc(h.signal)}</span>`
         +  `<span class="history-strength ${strengthCls}" title="${esc(strengthTitle)}">${strengthLetter}</span>`
         +  `<span class="history-score ${scoreCls}">${score || '—'}</span>`
         +  `<span class="history-icon">`
         +    `<span class="icon-emoji">${iconEmoji}</span>`
         +    `<span class="icon-label">${iconLabel}</span>`
         +  `</span>`
         +  `</div>`;
  }
  // FIX (AUDIT-CORE #106, 2026-07-21): add a "Load more" button at the
  // bottom so users can page through older signals beyond HISTORY_MAX.
  // The button uses the oldest visible signal's ctime as the cursor and
  // requests the next 100 signals from the server via the WS `signals`
  // message with a `before_ctime` field (newly supported by server.py).
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-02, CRITICAL): the previous logic
  // rendered the button only when `!filterLastHour` (i.e. NOT on the
  // History tab). But `#history-list` lives inside `#tab-history`, which
  // is `display:none` whenever another tab is active — so when the
  // button WAS rendered (on Chart/Stats tabs) it was invisible, and when
  // the History tab was actually shown the button was suppressed. The
  // pagination feature was completely unreachable. The intent of "skip
  // load-more when filtering to last 1 hour" is wrong — the History tab
  // is precisely where users want to load OLDER signals (which are
  // outside the 1-hour window). Now: render the button ONLY on the
  // History tab (where it's visible) using the full signalHistory (not
  // the filtered displayHistory) to find the oldest ctime cursor.
  if(filterLastHour){
    const oldestCtime = signalHistory.length && signalHistory[0] && signalHistory[0].detail
      ? signalHistory[0].detail.ctime : 0;
    if(oldestCtime){
      html += `<div id="history-load-more" class="history-load-more" `
           +  `onclick="window._loadMoreHistory()" role="button" tabindex="0" `
           +  `onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();window._loadMoreHistory()}">`
           +  `Load older signals ↓</div>`;
    }
  }
  historyList.innerHTML = html;
}

// FIX (AUDIT-CORE #74 + #106, 2026-07-21): ctime-based signal detail
// lookup + Load-more helper. Both are exposed on window so inline
// onclick handlers can reach them.
window._showSignalDetail = function(ctimeKey){
  if(ctimeKey == null || ctimeKey === '') return;
  const ctime = parseInt(ctimeKey, 10);
  // Find the entry by ctime — robust to array reordering.
  let foundIdx = -1;
  for(let i = 0; i < signalHistory.length; i++){
    if(signalHistory[i] && signalHistory[i].detail
       && signalHistory[i].detail.ctime === ctime){
      foundIdx = i; break;
    }
  }
  if(foundIdx === -1) return;
  // FIX (UI-P1-10 + P3-5, 2026-07-21): remember the focused row so we
  // can restore focus when the modal closes (Escape key or close button).
  // Also mark the background #app as inert so Tab can't escape into it.
  window._lastFocusedRow = document.activeElement;
  showSignalDetail(foundIdx);
  // Mark background as inert (WCAG: modal should trap focus).
  const appEl = document.getElementById('app');
  if(appEl && appEl.setAttribute) appEl.setAttribute('inert','');
  // Focus the close button so the modal opens with focus inside.
  setTimeout(() => {
    const closeBtn = document.getElementById('detail-close');
    if(closeBtn) closeBtn.focus();
  }, 50);
};

window._loadMoreHistory = function(){
  if(!ws || ws.readyState !== WebSocket.OPEN) return;
  if(!signalHistory.length || !signalHistory[0] || !signalHistory[0].detail
     || !signalHistory[0].detail.ctime) return;
  const beforeCtime = signalHistory[0].detail.ctime;
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-97, MEDIUM): validate beforeCtime type.
  // Previously a console user could call window._loadMoreHistory() with a
  // malformed ctime (e.g., signalHistory[0].detail.ctime = 'malicious') — not
  // a real attack vector (no public surface) but fragile. Now: bail if it's
  // not a positive finite number.
  if(typeof beforeCtime !== 'number' || !isFinite(beforeCtime) || beforeCtime <= 0) return;
  // Send the signals request with before_ctime — server returns the
  // 100 signals older than the cursor. onServerSignals merges them.
  send({ type: 'signals', asset: currentAsset, period: currentPeriod,
         before_ctime: beforeCtime, limit: 100 });
  // Optimistically disable the load-more button to prevent double-clicks.
  const btn = $('history-load-more');
  if(btn){ btn.textContent = 'Loading…'; btn.style.opacity = '0.6'; }
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-59, MEDIUM): re-enable the load-more
  // button after 5s if no server response arrived. Previously the button
  // was optimistically disabled (`btn.textContent = 'Loading…'`) but never
  // re-enabled if the server failed — the user couldn't retry. If
  // onServerSignals succeeds, renderHistory() rebuilds the button
  // (resetting its text); if it fails, this timeout re-enables it.
  setTimeout(() => {
    const btn2 = $('history-load-more');
    if(btn2){
      btn2.textContent = 'Load older signals ↓';
      btn2.style.opacity = '';
    }
  }, 5000);
};

function showSignalDetail(idx){
  const h = signalHistory[idx];
  if(!h || !h.detail || !detailBody || !detailOverlay) return;
  const d = h.detail;
  const sig = d.signal || '—';
  const acc = d.accuracy || '—';
  const accIcon = acc === 'correct' ? '✅ WIN' : acc === 'wrong' ? '❌ LOSS' : '➖ DRAW';
  const accColor = acc === 'correct' ? 'var(--green)' : acc === 'wrong' ? 'var(--red)' : 'var(--text-dim)';

  // FIX (DEEP-AUDIT-2026-07-26 / F-17-12, HIGH): XSS — escape `sig` before
  // interpolation into detailTitle.innerHTML. `sig = d.signal || '—'` comes
  // from the WS server response and could contain arbitrary HTML if the
  // server were compromised. Previously interpolated raw, allowing script
  // injection into the modal title. accIcon is a static emoji string so
  // doesn't need escaping, but escape it too for defence-in-depth.
  detailTitle.innerHTML = `<span style="color:${sig==='CALL'?'var(--green)':'var(--red)'}">${esc(sig)}</span> <span style="color:${accColor};font-size:13px;margin-left:8px">${esc(accIcon)}</span>`;

  let rows = '';
  // FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-14): toLocaleString() displays
  // LOCAL time. This was already local — the bug was the CHART (UTC by default).
  // Once the chart's localization.timeFormatter was set to local (AUDIT-5-13
  // fix above), chart x-axis, history list, and this detail modal all agree
  // on LOCAL time. No code change needed here — kept comment for clarity.
  const dateStr = d.ctime ? new Date(d.ctime * 1000).toLocaleString() : '—';
  rows += detailRow('Time', dateStr);
  rows += detailRow('Asset', currentAsset);
  rows += detailRow('Score', d.score != null ? (d.score >= 0 ? '+' : '') + d.score : '—');
  rows += detailRow('Confidence', d.confidence != null ? Math.round(d.confidence) + '%' : '—');
  rows += detailRow('Strength', d.strength || '—');
  rows += detailRow('Agree', d.agree != null ? d.agree + ' modules' : '—');
  rows += detailRow('Actual', d.actual || '—');
  rows += detailRow('Regime', (d.regime || '—') + '/' + (d.zone || '—'));
  if(d.a_open != null && d.a_close != null){
    const move = d.a_close - d.a_open;
    const movePct = d.a_open ? (move / d.a_open * 100).toFixed(3) + '%' : '—';
    rows += detailRow('Candle Move', move.toFixed(5) + ' (' + movePct + ')');
  }

  let tagsHtml = '';
  if(d.tags){
    const tags = d.tags.split(',').filter(t => t.trim());
    if(tags.length){
      tagsHtml = '<div class="detail-tags">' + tags.map(t => `<span class="detail-tag">${esc(t)}</span>`).join('') + '</div>';
    }
  }

  let theoriesHtml = '';
  const rightCodes = (d.right_codes || '').split(',').filter(t => t.trim());
  const wrongCodes = (d.wrong_codes || '').split(',').filter(t => t.trim());
  if(rightCodes.length || wrongCodes.length){
    theoriesHtml = '<div class="detail-theories">';
    rightCodes.forEach(t => {
      theoriesHtml += `<div class="theory-pill right"><span>${esc(t)}</span><span>✓</span></div>`;
    });
    wrongCodes.forEach(t => {
      theoriesHtml += `<div class="theory-pill wrong"><span>${esc(t)}</span><span>✗</span></div>`;
    });
    theoriesHtml += '</div>';
  }

  let pmHtml = '';
  if(d.postmortem){
    pmHtml = '<div style="margin-top:8px"><div style="font-size:11px;color:var(--text-dim);margin-bottom:4px">WIN/LOSS REASON:</div><div class="detail-postmortem">' + esc(d.postmortem) + '</div></div>';
  }

  detailBody.innerHTML = rows + tagsHtml + theoriesHtml + pmHtml;
  detailOverlay.classList.add('show');
}

function detailRow(label, value){
  return `<div class="detail-row"><span class="label">${label}</span><span class="value">${esc(String(value))}</span></div>`;
}

function renderAccuracy(){
  // Legacy: update the topbar mini-stat (elements may be gone — null-safe).
  const accPct = $('acc-pct');
  const accDetail = $('acc-detail');
  if(accPct){
    if(!totalSignals){ accPct.textContent = '—'; if(accDetail) accDetail.textContent = ''; }
    else {
      const pct = Math.round((totalCorrect / totalSignals) * 100);
      accPct.textContent = pct + '%';
      if(accDetail) accDetail.textContent = '(' + totalCorrect + '/' + totalSignals + ')';
      accPct.style.color = pct >= 60 ? 'var(--green)' : pct >= 40 ? 'var(--yellow)' : 'var(--red)';
    }
  }
  // Always refresh the accuracy TAB content too — it's cheap, and the tab
  // may be hidden (no-op visually) but ready for instant display on switch.
  renderAccuracyTab();
}

/* ─── ACCURACY TAB ─────────────────────────────────────────────────────────
   Renders the full breakdown into #tab-accuracy:
     - Hero: overall win rate % (large, colored)
     - Stat grid: total / wins / losses / draws
     - Per-strength breakdown: STRONG / MEDIUM / WEAK win rates
     - Per-regime breakdown: win rate grouped by market regime
   Uses signalHistory as the source (already kept in sync by addHistory +
   onServerSignals). Recomputes on every call — cheap for ≤100 entries. */
function renderAccuracyTab(){
  const graded = signalHistory.filter(h => h && (
    h.accuracy === 'correct' || h.accuracy === 'wrong' || h.accuracy === 'draw'
  ));
  const correct = graded.filter(h => h.accuracy === 'correct').length;
  const wrong   = graded.filter(h => h.accuracy === 'wrong').length;
  const draws   = graded.filter(h => h.accuracy === 'draw').length;
  const total   = graded.length;
  const pending = signalHistory.filter(h => h && h.accuracy === 'pending').length;

  // Hero pct
  const heroPct = $('acc-hero-pct');
  const heroDetail = $('acc-hero-detail');
  if(heroPct){
    if(total === 0){
      heroPct.textContent = '—';
      heroPct.style.color = 'var(--text-dim)';
    } else {
      const pct = Math.round((correct / total) * 100);
      heroPct.textContent = pct + '%';
      heroPct.style.color = pct >= 60 ? 'var(--green)' : pct >= 40 ? 'var(--yellow)' : 'var(--red)';
    }
  }
  if(heroDetail){
    if(total === 0){
      heroDetail.textContent = pending > 0
        ? pending + ' signal' + (pending === 1 ? '' : 's') + ' pending'
        : 'No graded signals yet';
    } else {
      heroDetail.textContent = correct + ' correct / ' + total + ' total'
        + (pending ? ' · ' + pending + ' pending' : '');
    }
  }

  // Stat grid
  _setText('acc-total',  String(total));
  _setText('acc-wins',   String(correct));
  _setText('acc-losses', String(wrong));
  _setText('acc-draws',  String(draws));

  // Per-strength + per-regime breakdowns
  _renderStrengthBreakdown();
  _renderRegimeBreakdown();
}

function _renderStrengthBreakdown(){
  const container = $('acc-strength-breakdown');
  if(!container) return;
  const strengths = ['STRONG', 'MEDIUM', 'WEAK'];
  let html = '';
  let hasAny = false;
  strengths.forEach(s => {
    const subset = signalHistory.filter(h =>
      h && h.detail && (h.detail.strength || '').toUpperCase() === s
      && (h.accuracy === 'correct' || h.accuracy === 'wrong'));
    const t = subset.length;
    const c = subset.filter(h => h.accuracy === 'correct').length;
    if(t > 0) hasAny = true;
    const pct = t > 0 ? Math.round((c / t) * 100) : 0;
    const pctCls = t === 0 ? 'none' : pct >= 60 ? 'good' : pct >= 40 ? 'mid' : 'bad';
    const barWidth = t > 0 ? pct : 0;
    html += `<div class="accuracy-breakdown-row">`
         +  `<span class="breakdown-label">${s}</span>`
         +  `<div class="breakdown-bar"><div class="breakdown-bar-fill correct" style="width:${barWidth}%"></div></div>`
         +  `<span class="breakdown-pct ${pctCls}">`
         +    `<span class="pct-num">${t > 0 ? pct + '%' : '—'}</span>`
         +    `<span class="breakdown-meta">${c}/${t}</span>`
         +  `</span>`
         +  `</div>`;
  });
  if(!hasAny){
    html = '<div class="accuracy-empty">No graded signals yet</div>';
  }
  container.innerHTML = html;
}

function _renderRegimeBreakdown(){
  const container = $('acc-regime-breakdown');
  if(!container) return;
  // Group graded signals by regime label.
  const regimeMap = {};
  signalHistory.forEach(h => {
    if(!h || !h.detail) return;
    if(h.accuracy !== 'correct' && h.accuracy !== 'wrong') return;
    const reg = h.detail.regime;
    let label;
    if(typeof reg === 'object' && reg !== null){
      label = reg.regime || 'UNKNOWN';
    } else {
      label = String(reg || 'UNKNOWN');
    }
    if(!regimeMap[label]) regimeMap[label] = { correct: 0, total: 0 };
    regimeMap[label].total++;
    if(h.accuracy === 'correct') regimeMap[label].correct++;
  });
  const regimes = Object.keys(regimeMap).sort();
  if(regimes.length === 0){
    container.innerHTML = '<div class="accuracy-empty">No graded signals yet</div>';
    return;
  }
  let html = '';
  regimes.forEach(r => {
    const stats = regimeMap[r];
    const pct = stats.total > 0 ? Math.round((stats.correct / stats.total) * 100) : 0;
    const pctCls = pct >= 60 ? 'good' : pct >= 40 ? 'mid' : 'bad';
    html += `<div class="accuracy-breakdown-row">`
         +  `<span class="breakdown-label">${esc(r)}</span>`
         +  `<div class="breakdown-bar"><div class="breakdown-bar-fill correct" style="width:${pct}%"></div></div>`
         +  `<span class="breakdown-pct ${pctCls}">`
         +    `<span class="pct-num">${pct}%</span>`
         +    `<span class="breakdown-meta">${stats.correct}/${stats.total}</span>`
         +  `</span>`
         +  `</div>`;
  });
  container.innerHTML = html;
}

/* ─── TAB SWITCHING ────────────────────────────────────────────────────────
   switchTab(name): toggles the .active class on the 3 tab buttons and the
   3 tab panes. Also fires tab-specific refresh logic so the just-shown
   pane is up-to-date (cheap re-render from in-memory state). */
function switchTab(tabName){
  if(tabName !== 'chart' && tabName !== 'history' && tabName !== 'accuracy') return;
  currentTab = tabName;

  // Update tab buttons
  const tabBtns = document.querySelectorAll('.tab-btn');
  tabBtns.forEach(btn => {
    const isActive = btn.dataset.tab === tabName;
    btn.classList.toggle('active', isActive);
    btn.setAttribute('aria-selected', String(isActive));
  });

  // Update tab panes
  ['chart', 'history', 'accuracy'].forEach(name => {
    const pane = $('tab-' + name);
    if(pane) pane.classList.toggle('active', name === tabName);
  });

  // Tab-specific refresh — ensures the just-shown pane is current.
  if(tabName === 'history'){
    renderHistoryPairSelect();
    // Refresh from server (no-op if WS not open) — gives the user fresh
    // last-1-hour data every time they open the tab.
    loadServerHistory();
    renderHistory();
    setTimeout(() => { const hl = $('history-list'); if(hl) hl.scrollTop = 0; }, 50);
  } else if(tabName === 'accuracy'){
    renderAccuracyTab();
  } else if(tabName === 'chart'){
    // The chart's autoSize handles hidden→visible resizes, but on some
    // browsers the chart needs an explicit nudge after being display:none.
    if(chart){
      try{
        const container = $('chart-container');
        if(container){
          chart.applyOptions({
            width: container.clientWidth,
            height: container.clientHeight,
          });
        }
        if(chart.timeScale) chart.timeScale().fitContent();
      }catch(_){}
    }
  }
}

/* renderHistoryPairSelect(): mirrors the topbar #pair-select into the
   history tab's #history-pair-select dropdown. Called from renderPairs()
   (so the dropdown stays in sync) and from switchTab('history') (so the
   dropdown is populated on first tab open even before pairs arrive). */
function renderHistoryPairSelect(){
  const histPairSelect = $('history-pair-select');
  if(!histPairSelect) return;
  let activeList;
  if(currentCategory === 'real')              activeList = realPairsList;
  else if(currentCategory === 'alltime_otc')  activeList = alltimeOtcPairsList;
  else                                        activeList = otcPairsList;

  // Build new options only if the set actually changed — avoids clobbering
  // the user's in-progress dropdown interaction on every renderPairs call.
  const newOptions = [];
  activeList.forEach(p => {
    newOptions.push({
      value: p.asset,
      text: p.display + (p.payout ? ' (' + p.payout + '%)' : ''),
      locked: !!p.locked,
      selected: p.asset === currentAsset,
    });
  });
  // Quick diff: if same length + same values + same selected, skip rebuild.
  const sameCount = histPairSelect.options.length === newOptions.length;
  let sameContent = sameCount;
  if(sameCount){
    for(let i = 0; i < newOptions.length; i++){
      const opt = histPairSelect.options[i];
      if(opt.value !== newOptions[i].value
         || opt.textContent !== newOptions[i].text
         || opt.disabled !== newOptions[i].locked){
        sameContent = false; break;
      }
    }
  }
  if(sameContent){
    // Just ensure the selected state is right.
    if(histPairSelect.value !== currentAsset) histPairSelect.value = currentAsset;
    return;
  }
  // Rebuild.
  histPairSelect.innerHTML = '';
  if(newOptions.length === 0){
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = 'No pairs available';
    opt.disabled = true;
    histPairSelect.appendChild(opt);
    return;
  }
  newOptions.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.value;
    opt.textContent = o.text;
    opt.disabled = o.locked;
    if(o.selected) opt.selected = true;
    histPairSelect.appendChild(opt);
  });
}

/* ─── PAIRS ────────────────────────────────────────────────────────────────
   FIX BUG-1: renderPairs now accepts the FULL server payload
   `{type:"pairs", real_pairs:[...], otc_pairs:[...], ...}` directly —
   the caller must pass `msg`, NOT `msg.pairs`. The function reads
   msg.real_pairs / msg.otc_pairs directly. The OLD code called
   `renderPairs(msg.pairs)` which discarded the structured split and pushed
   ALL pairs (real + OTC) into the OTC list.
   Backward-compat: still accepts a legacy flat array. */
function renderPairs(payload){
  if(Array.isArray(payload)){
    realPairsList = [];
    otcPairsList = payload;
  } else {
    realPairsList = payload.real_pairs || [];
    otcPairsList  = payload.otc_pairs  || [];
  }
  // FIX (DATA-FLOW-2026-07-22): support alltime_otc category.
  alltimeOtcPairsList = payload.alltime_otc_pairs || [];
  pairsList = realPairsList.concat(otcPairsList).concat(alltimeOtcPairsList);

  const pairSelect = $('pair-select');

  // Render only the active category's pairs in the dropdown.
  let activeList;
  if(currentCategory === 'real')         activeList = realPairsList;
  else if(currentCategory === 'alltime_otc') activeList = alltimeOtcPairsList;
  else                                   activeList = otcPairsList;

  pairSelect.innerHTML = '';
  if(activeList.length === 0){
    const opt = document.createElement('option');
    opt.value = '';
    if(currentCategory === 'real'){
      opt.textContent = '⚠ Real Market closed (weekend/bank holiday)';
    } else if(currentCategory === 'alltime_otc'){
      opt.textContent = 'No All-Time OTC pairs available';
    } else {
      opt.textContent = 'No OTC pairs available';
    }
    opt.disabled = true;
    pairSelect.appendChild(opt);
    // FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-17): when the active list is
    // empty (e.g., Real market on weekend, or alltime_otc list temporarily
    // empty), update the chart-loading-overlay with a clear market-closed
    // message instead of leaving it stuck on "Loading…" indefinitely. The
    // initial onopen subscribe already fired for currentAsset (which has no
    // stream), so we can't get a snapshot to dismiss the overlay naturally.
    // This is the "graceful empty-list handler" the audit proposed — we
    // keep the onopen subscribe (it's harmless — the server doesn't
    // validate against the pair list) but surface the closed state to the
    // user. The dead-connection keepalive check at 45s will eventually
    // close the WS and reconnect, but the user sees a clear message
    // immediately instead of an infinite spinner.
    const overlay = $('chart-loading-overlay');
    if(overlay){
      overlay.classList.add('show');
      const txt = $('chart-loading-text');
      if(txt){
        if(currentCategory === 'real'){
          txt.textContent = 'Real market closed (weekend). Switch to OTC for 24/7 signals.';
        } else if(currentCategory === 'alltime_otc'){
          txt.textContent = 'No All-Time OTC pairs available right now.';
        } else {
          txt.textContent = 'No OTC pairs available right now.';
        }
      }
    }
  } else {
    activeList.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.asset;
      opt.textContent = p.display
        + (p.payout ? ' (' + p.payout + '%)' : '')
        + (p.locked ? ' 🔒' : '');
      if(p.locked) opt.classList.add('locked');
      if(p.asset === currentAsset) opt.selected = true;
      opt.disabled = !!p.locked;
      pairSelect.appendChild(opt);
    });
  }

  // FIX (DEEP-AUDIT-2026-07-26 / F-17-17, HIGH): removed the dead `pairsCount`
  // block. The #pairs-count element doesn't exist in any HTML file — the
  // block `if(pairsCount){ pairsCount.textContent = label + activeList.length; }`
  // was a no-op on every renderPairs() call (frequent — runs on every pair-list
  // refresh and after every category switch).

  // If the currently-selected asset is NOT in the active category's list,
  // auto-switch to the first available pair.
  // FIX (ALLTIME-OTC-FIX 2026-07-30): on alltime_otc page, don't auto-switch
  // away if the current pair is locked or temporarily missing — the user
  // explicitly selected it. Only auto-switch on real/otc pages where a
  // missing pair means the market closed.
  const stillThere = activeList.find(p => p.asset === currentAsset && !p.locked);
  if(!stillThere && activeList.length > 0){
    // On alltime_otc: if the pair IS in the list (even if locked), keep it.
    // Only switch if the pair is completely absent from the list.
    if(currentCategory === 'alltime_otc'){
      const pairExists = activeList.find(p => p.asset === currentAsset);
      if(pairExists){
        // Pair exists but is locked — keep selection, don't auto-switch.
        // The user will see the locked indicator and can pick another.
      } else {
        // Pair completely absent — switch to first available.
        const firstOk = activeList.find(p => !p.locked) || activeList[0];
        if(firstOk){
          currentAsset = firstOk.asset;
          pairSelect.value = currentAsset;
          if(ws && ws.readyState === WebSocket.OPEN){
            send({ type: 'subscribe', asset: currentAsset, period: currentPeriod,
                   category: currentCategory });
            loadServerHistory();
          }
        }
      }
    } else {
      // Real/OTC page: original behavior — auto-switch to first available.
      const firstOk = activeList.find(p => !p.locked) || activeList[0];
      if(firstOk){
        currentAsset = firstOk.asset;
        pairSelect.value = currentAsset;
        if(ws && ws.readyState === WebSocket.OPEN){
          send({ type: 'subscribe', asset: currentAsset, period: currentPeriod,
                 category: currentCategory });
          loadServerHistory();
        }
      }
    }
  }

  // Update payout label for the currently-selected pair.
  const payoutLabel = $('payout-label');
  const cur = pairsList.find(p => p.asset === currentAsset);
  if(cur && payoutLabel){
    payoutLabel.textContent = 'Payout: ' + (cur.payout || '—');
  }

  // FIX (DATA-FLOW-2026-07-22): update the switch button label to show
  // which market the user will switch to next.
  _updateSwitchButtonLabel();

  // TAB BAR: keep the history tab's pair dropdown in sync with the topbar's.
  renderHistoryPairSelect();
}

// FIX (DATA-FLOW-2026-07-22): update the switch button's label/title
// based on what the NEXT market is.
function _updateSwitchButtonLabel(){
  const btn = $('market-switch-btn');
  if(!btn) return;
  const next = _nextCategory(currentCategory);
  const labels = {
    'real':         { text: 'Switch to Real',  title: 'Real Market (live forex, weekdays only)' },
    'otc':          { text: 'Switch to OTC',   title: 'OTC Market (standard OTC pairs, 24/7)' },
    'alltime_otc':  { text: 'Switch to All-OTC', title: 'All-Time OTC (exotic pairs, 24/7, no payout floor)' },
  };
  const lbl = labels[next] || labels.otc;
  const textEl = btn.querySelector('.switch-text');
  if(textEl) textEl.textContent = lbl.text;
  btn.setAttribute('aria-label', lbl.text);
  btn.title = lbl.title;
}

/* ─── MARKET STATUS INDICATOR ──────────────────────────────────────────────
   FIX (DEEP-AUDIT-2026-07-26 / F-17-14, HIGH): DELETED. The entire
   updateMarketStatusIndicator() function and its 30s setInterval were
   dead code — the #market-status-indicator element was removed from
   all HTML files during the "CLEAN LAYOUT OVERRIDES" CSS refactor but the
   JS function and interval were never deleted. The function early-returned
   via `if(!el) return;` every 30s, wasting CPU and leaking interval
   handles on each initApp re-entry. Call site (initApp) and pagehide
   cleanup also removed. */

/* ─── setCategory(newCat) ──────────────────────────────────────────────────
   THE single category-switch function. Replaces the 3 duplicate handlers
   (3-dot menu / market badge / cat-tabs) from the old index.html.

   - Validates the new category ("real" or "otc").
   - Saves it to localStorage as 'marketCategory'.
   - Navigates to the other HTML file (real.html / otc.html).

   Because each market lives on its own HTML page, switching category =
   switching page. There's no in-page category swap anymore — that
   eliminates the BUG-1 class of issues entirely (each page only ever
   subscribes to pairs of its own category). */
function setCategory(newCat){
  // FIX (DATA-FLOW-2026-07-22): added 'alltime_otc' as a 3rd category.
  // Cycle: real → otc → alltime_otc → real.
  if(newCat !== 'real' && newCat !== 'otc' && newCat !== 'alltime_otc') return;
  if(newCat === currentCategory) return;

  // FIX (CRASH-2026-07-30): prevent crash when switching markets rapidly.
  // User complaint: "Otc মার্কেট, real market and all time OTC সুইচ করলে
  // অ্যাপ ক্র্যাশ করে". Root cause: setCategory navigates to a new page
  // via window.location.href, but the OLD page's safeInit() may still be
  // polling (setTimeout retry loop waiting for LightweightCharts). When the
  // new page loads, the old page's pending timers fire on a torn-down DOM,
  // causing errors. Also, initChart() on the new page may run before the
  // old page's chart.remove() completes, causing "Cannot read property of
  // null" crashes. Fix: run full cleanup (same as onPageHide) BEFORE
  // navigation, and mark a global flag so safeInit's retry loop aborts.
  try{
    // Abort any pending safeInit retries on this page
    if(typeof safeInit !== 'undefined' && safeInit._retries){
      safeInit._retries = 999;  // force it to bail on next tick
    }
    // Clear all intervals/timeouts
    if(typeof _countdownInterval !== 'undefined' && _countdownInterval){ clearInterval(_countdownInterval); _countdownInterval = null; }
    if(typeof _keepaliveInterval !== 'undefined' && _keepaliveInterval){ clearInterval(_keepaliveInterval); _keepaliveInterval = null; }
    if(typeof staleTimeout !== 'undefined' && staleTimeout){ clearTimeout(staleTimeout); staleTimeout = null; }
    if(typeof chartLoadingTimeout !== 'undefined' && chartLoadingTimeout){ clearTimeout(chartLoadingTimeout); chartLoadingTimeout = null; }
    if(typeof reconnectTimer !== 'undefined' && reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer = null; }
    if(typeof _resizeTimer !== 'undefined' && _resizeTimer){ clearTimeout(_resizeTimer); _resizeTimer = null; }
    // Close WebSocket
    if(typeof ws !== 'undefined' && ws){
      try{ ws.onclose = null; ws.onerror = null; ws.onopen = null; ws.onmessage = null; ws.close(); }catch(_){}
      ws = null;
    }
    // Dispose chart
    if(typeof chart !== 'undefined' && chart){
      try{ chart.remove(); }catch(_){}
      chart = null;
    }
  }catch(_){}

  try{ localStorage.setItem('marketCategory', newCat); }catch(_){}
  const targets = {
    'real':         '/static/real.html',
    'otc':          '/static/otc.html',
    'alltime_otc':  '/static/alltime_otc.html',
  };
  const target = targets[newCat] || '/static/otc.html';
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-46, HIGH): use location.href instead of
  // location.replace(). Previously `replace()` removed the current page from
  // browser history, breaking the back button — user on Real clicks "Switch
  // to OTC", lands on OTC, presses back, and goes to whatever page was BEFORE
  // Real (not back to Real as expected). Now: `href` pushes a new history
  // entry so back returns to the previous market page. The original "bouncing
  // between market pages" concern is moot — the cycle real→otc→alltime_otc→real
  // is exactly what users want when navigating between markets.
  window.location.href = target;
}

// FIX (DATA-FLOW-2026-07-22): cycle through the 3 markets on switch click.
// The switch button's label is updated by renderPairs() based on the
// current category — see _updateSwitchButtonLabel().
function _nextCategory(cat){
  if(cat === 'real') return 'otc';
  if(cat === 'otc')  return 'alltime_otc';
  return 'real';  // alltime_otc → real
}

/* ─── WEBSOCKET ──────────────────────────────────────────────────────────── */
function connect(){
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-21, HIGH): also bail if ws is in CLOSING
  // state. Previously the guard only checked OPEN + CONNECTING, so calling
  // connect() while ws.readyState === CLOSING created a NEW WebSocket — and
  // the old socket's onclose fired later, calling scheduleReconnect() while
  // a new socket was already connecting. The reconnect timer may fire later
  // and create a THIRD socket. Now: only proceed if ws is null or fully CLOSED.
  if(ws && ws.readyState !== WebSocket.CLOSED) return;
  setStatus('connecting');
  try { ws = new WebSocket(WS_URL); } catch(e){ scheduleReconnect(); return; }
  ws.onopen = () => {
    setStatus('connected');
    reconnectAttempts = 0;
    send({ type: 'pairs' });
    send({ type: 'subscribe', asset: currentAsset, period: currentPeriod,
           category: currentCategory });
    setTimeout(loadServerHistory, 800);
  };
  ws.onmessage = e => {
    lastMessageAt = Date.now();
    let msg;
    // FIX (DEEP-AUDIT-2026-07-26 / F-17-04, CRITICAL): surface malformed WS
    // frames to the user via showError() instead of just console.error.
    // Previously a malformed frame was logged and silently dropped — users
    // on a flaky connection saw a frozen chart with no indication of why.
    // Still return after showing the error (one bad frame shouldn't kill
    // the whole connection — the next frame may be valid).
    try { msg = JSON.parse(e.data); } catch(err){
      console.error('msg parse error', err);
      showError('Decoding feed error — retrying');
      return;
    }
    // FIX (UI-P3-18, 2026-07-21): wrap handleMsg in try/catch so a throw
    // inside any render function (renderSignal/renderMicro/renderHistory)
    // doesn't kill the WS message handler and break all subsequent messages.
    try { handleMsg(msg); }
    catch(err){ console.error('[ws] handleMsg error', err, msg); }
  };
  ws.onclose = (event) => {
    // FIX (CRASH-FIX-2026-07-26 / FE-001): onclose ignored event.code /
    // event.reason, so the user never knew WHY the connection dropped.
    // On Railway, the most common close codes are:
    //   1008  → Policy Violation (origin rejected by ALLOWED_WS_ORIGINS)
    //   1011  → Internal server error (feed task crashed)
    //   1013  → Try Again Later (server at capacity / starting up)
    // For 1008 specifically, retrying forever won't help — the origin
    // is blocked server-side. Surface the cause to the user with a
    // persistent error overlay so they can fix the env var or contact
    // the operator. For 1011 (feed crashed), the server auto-restarts
    // the feed task, so retrying is fine but should be visible.
    // For 1013, immediate retry is appropriate.
    let _permError = '';
    if(event && event.code === 1008){
      _permError = 'Connection rejected by server (code 1008). ' +
        'The WebSocket origin is not in ALLOWED_WS_ORIGINS. ' +
        'Contact the operator to add this domain.';
    } else if(event && event.code === 1011){
      _permError = 'Server error (code 1011). The feed task may have ' +
        'crashed — auto-restart in progress. Retrying…';
    } else if(event && event.code === 1006){
      _permError = 'Connection closed abnormally (code 1006). ' +
        'Possible causes: network drop, server restart, or Cloudflare ' +
        'intervention. Retrying…';
    }
    if(_permError){
      try{ showError(_permError); }catch(_){}
    }
    setStatus('disconnected');
    scheduleReconnect();
  };
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-20, HIGH): call scheduleReconnect()
  // directly in onerror. Previously the comment assumed onclose would fire
  // after ws.close(), but if ws.close() throws (already closed) or onclose
  // is somehow not invoked (DNS failure, immediate close before onclose
  // reliably fires), no reconnect was scheduled and the page was permanently
  // stuck on "Connecting". scheduleReconnect() is idempotent (guarded by
  // `if(reconnectTimer) return;`), so calling it directly is safe.
  ws.onerror = () => {
    try{ ws.close(); }catch(_){}
    scheduleReconnect();
  };
}

function send(obj){
  if(ws && ws.readyState === WebSocket.OPEN){
    try{ ws.send(JSON.stringify(obj)); }catch(_){}
  }
}

function scheduleReconnect(){
  if(reconnectTimer) return;
  const delay = Math.min(RECONNECT_BASE * Math.pow(2, reconnectAttempts), RECONNECT_MAX);
  reconnectAttempts++;
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, delay);
}

function setStatus(s){
  const connDot = $('conn-dot');
  const connLabel = $('conn-label');
  if(connDot) connDot.className = s;
  if(connLabel) connLabel.textContent = s.charAt(0).toUpperCase() + s.slice(1);
}

function handleMsg(msg){
  // The server's broadcast() now filters by interested_cids per stream —
  // each client only receives messages for assets it actually subscribed
  // to. The old redundant client-side filter (drop msg if msg.asset !==
  // currentAsset) has been REMOVED. This eliminates the bug where a
  // momentarily-stale currentAsset (during a pair switch) would silently
  // drop the very first snapshot for the new pair.

  // Only clear stale overlay for data-carrying messages — pairs/status
  // pings arrive even when the feed is dead, so they must NOT clear it.
  if(['snapshot','tick','eoc'].includes(msg.type)) clearStale();

  switch(msg.type){
    case 'snapshot': onSnapshot(msg); break;
    case 'tick':     onTick(msg); break;
    case 'eoc':      onEoc(msg); break;
    case 'signals':  onServerSignals(msg.signals, msg.asset, msg.period); break;
    case 'pairs':    renderPairs(msg); break;             // ← BUG-1 FIX: pass msg, not msg.pairs
    case 'stale':    onStale(msg); break;
    case 'status':   break;   // keepalive pong — silently consume
    case 'error':
      showError(msg.error || 'Unknown error');
      break;
    default:
      // subscribe_result with ok:false — surface the error to the user.
      if(msg.ok === false && msg.status){
        let statusText = msg.status.toUpperCase();
        let reasonText = msg.reason || '';
        if(statusText === 'COOLDOWN'){
          statusText = 'RECONNECTING';
          reasonText = 'Reconnecting to broker (' + Math.round(msg.retry_after || 30) + 's)';
        }
        showChartLoading();
        const staleOverlay = $('stale-overlay');
        const staleMsg = $('stale-msg');
        if(staleOverlay){
          staleOverlay.classList.add('show');
          if(staleMsg) staleMsg.textContent = '⚠ ' + statusText + (reasonText ? (': ' + reasonText) : '');
        }
      }
      break;
  }
}

function showError(text){
  const staleOverlay = $('stale-overlay');
  const staleMsg = $('stale-msg');
  if(staleOverlay && staleMsg){
    staleOverlay.classList.add('show');
    staleMsg.textContent = '⚠ ' + text;
  }
  console.error('[ws error]', text);
}

function onSnapshot(msg){
  const c = msg.candles || [];
  updateChart(c, (msg.prediction && msg.prediction.candle) || null, true);
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-15, HIGH): removed addTapeTick(c[c.length-1].close) — dead code.
  if(c.length){
    runningCandleOpenTime = c[c.length-1].time;
    // FIX (DEEP-AUDIT-2026-07-26 / F-17-07, CRITICAL): initialise lastLivePrice
    // from the snapshot's last close. Previously the live-price colour stayed
    // 'flat' (grey) on the very first tick because `if(lastLivePrice > 0)`
    // guarded the class-update branch — lastLivePrice was 0 until the first
    // tick ran, so the up/down arrow didn't render until the SECOND tick.
    // Now: seed lastLivePrice from the snapshot so the first tick compares
    // against the last snapshot close (the correct "previous price").
    const lastClose = c[c.length-1].close;
    if(typeof lastClose === 'number' && isFinite(lastClose) && lastClose > 0){
      lastLivePrice = lastClose;
    }
  }
  if(msg.prediction){
    lastPrediction = msg.prediction;
    renderSignal(msg.prediction);
  }
}

function onTick(msg){
  const c = msg.candle;
  if(c){
    updateLastCandle(c);
    runningCandleOpenTime = c.time;
    // Update live price ticker on every tick.
    if(c.close !== undefined && c.close !== null){
      const newPrice = c.close;
      const livePriceEl = $('live-price');
      const livePriceArrow = $('live-price-arrow');
      if(livePriceEl){
        // FIX (DEEP-AUDIT-2026-07-26 / F-17-24, HIGH): use fmtPrice() for
        // decimal consistency. Previously the live-price used ad-hoc logic
        // (3 decimals for >100, 5 for ≤100) which didn't match the tick tape,
        // market-state #ms-net, or signal-detail modal — all of which used
        // fmtPrice()'s 4-tier magnitude-based formatting. The same asset's
        // price displayed differently in different parts of the UI.
        livePriceEl.textContent = (typeof newPrice === 'number')
          ? fmtPrice(newPrice)
          : '—';
        // FIX (DEEP-AUDIT-2026-07-26 / F-17-66, MEDIUM): move lastLivePrice
        // update OUTSIDE the `if(livePriceEl)` block. Previously it was set
        // inside, so if livePriceEl was missing (DOM not ready) the value
        // stayed stale and subsequent ticks compared against the old value.
        if(lastLivePrice > 0){
          if(newPrice > lastLivePrice){
            livePriceEl.className = 'live-price-val up';
            if(livePriceArrow){ livePriceArrow.className = 'live-price-arrow up'; livePriceArrow.textContent = '▲'; }
          } else if(newPrice < lastLivePrice){
            livePriceEl.className = 'live-price-val down';
            if(livePriceArrow){ livePriceArrow.className = 'live-price-arrow down'; livePriceArrow.textContent = '▼'; }
          } else {
            livePriceEl.className = 'live-price-val flat';
            if(livePriceArrow){ livePriceArrow.className = 'live-price-arrow flat'; livePriceArrow.textContent = '◆'; }
          }
        }
      }
      // FIX (F-17-66): state update must happen regardless of DOM presence.
      if(typeof newPrice === 'number' && isFinite(newPrice)) lastLivePrice = newPrice;
    }
    // Tick rate tracking.
    const nowMs = Date.now();
    tickTimestamps.push(nowMs);
    lastTickAt = nowMs;
    while(tickTimestamps.length > 0 && nowMs - tickTimestamps[0] > 1000){
      tickTimestamps.shift();
    }
    // FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-19): proactive stale detection.
    // The variable `staleTimeout` was declared (line 86) and cleared in
    // clearStale() (line ~1869) but NEVER SET anywhere — confirmed by the
    // comment at the keepalive block ("previously the proactive stale-
    // detection (staleTimeout) was scheduled but never set"). The only
    // stale-detection path was the 45s dead-connection check inside the
    // 15s keepalive interval (45-60s actual latency). Now schedule a 10s
    // staleTimeout on every tick — cleared by the next tick (or by
    // clearStale() on snapshot/eoc/tick). If no tick arrives in 10s,
    // show the stale overlay immediately so the user knows the feed is
    // frozen (broker disconnect, network hiccup, etc.) instead of waiting
    // 45-60s for the keepalive to fire. The keepalive check remains as a
    // backstop (it closes the WS for reconnect).
    if(staleTimeout) clearTimeout(staleTimeout);
    staleTimeout = setTimeout(() => {
      const staleOverlay = $('stale-overlay');
      const staleMsg = $('stale-msg');
      if(staleOverlay) staleOverlay.classList.add('show');
      if(staleMsg) staleMsg.textContent = '⚠ No ticks for 10s — feed may be stale';
    }, 10000);
  }
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-15, HIGH): removed addTapeTick(c.close) call — dead code (#tick-tape-inner doesn't exist).
  if(msg.micro) renderMicro(msg.micro);
  if(msg.running_conf){
    runningConf = msg.running_conf;
    if(currentMicro) renderMicro(currentMicro);
  }
  // Delayed prediction arrives on a tick message once the gate opens.
  if(msg.prediction){
    lastPrediction = msg.prediction;
    renderSignal(msg.prediction);
  }
}

function onEoc(msg){
  const c = msg.candles || [];
  updateChart(c, (msg.prediction && msg.prediction.candle) || null);
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-15, HIGH): removed addTapeTick(c[c.length-1].close) — dead code.
  if(c.length){
    runningCandleOpenTime = c[c.length-1].time;
  }
  // FIX (AUDIT-CRITICAL #001, 2026-07-21): the previous block ONLY called
  // addHistory and loadServerHistory when msg.prediction was truthy. But
  // feed.py ALWAYS broadcasts prediction=null on EOC (the real prediction
  // arrives 3s later via tick). So this code was DEAD — the frontend's
  // signalHistory never updated from EOC, freezing the display at whatever
  // was loaded at pair-switch. This is the ROOT CAUSE of the user-reported
  // '48-signal limit' bug.
  //
  // Now: ALWAYS trigger a server history refresh on EOC. Use the previous
  // prediction (lastPrediction) for the optimistic local add so the user
  // sees the entry immediately; the server refresh corrects it 500ms later.
  // Don't null-out lastPrediction on EOC (the prediction is still valid —
  // it's just being re-evaluated; nulling it causes the signal panel to
  // flash to PENDING confusingly).
  const accuracyForHistory = msg.accuracy || 'pending';
  if(msg.prediction){
    lastPrediction = msg.prediction;
    renderSignal(msg.prediction);
    addHistory(lastPrediction.signal || 'NEUTRAL', accuracyForHistory, msg.prediction);
  } else if(lastPrediction){
    // EOC grades the PREVIOUS candle's prediction against the just-closed
    // candle. Update the existing history entry's accuracy in place.
    addHistory(lastPrediction.signal || 'NEUTRAL', accuracyForHistory, lastPrediction);
    // Don't renderSignal — keep showing the previous prediction until the
    // new one arrives via tick (prevents PENDING flash).
  } else {
    renderPending();
  }
  // ALWAYS refresh from server — the DB has the authoritative graded row
  // with postmortem, tags, regime, etc. that the live optimistic add lacks.
  setTimeout(loadServerHistory, 500);
  currentMicro = null;
  runningConf = null;
}

function onStale(msg){
  const staleOverlay = $('stale-overlay');
  const staleMsg = $('stale-msg');
  if(staleOverlay) staleOverlay.classList.add('show');
  if(staleMsg) staleMsg.textContent = '⚠ ' + (msg.asset || '') + ' FEED STALE';
}

function clearStale(){
  const staleOverlay = $('stale-overlay');
  if(staleOverlay) staleOverlay.classList.remove('show');
  if(staleTimeout){ clearTimeout(staleTimeout); staleTimeout = null; }
}

/* ─── CANDLE COUNTDOWN ───────────────────────────────────────────────────── */
function updateCandleCountdown(){
  if(!countdownEl) return;
  if(!runningCandleOpenTime || !currentPeriod){
    countdownEl.textContent = '⏱ --:--';
    countdownEl.className = 'idle';
    updateNextSignalTimer(0, true);  // also reset next-signal timer
    return;
  }
  const closeAt = (runningCandleOpenTime + currentPeriod) * 1000;
  const remainingMs = closeAt - Date.now();
  if(remainingMs <= 0){
    countdownEl.textContent = '⏱ 00:00';
    countdownEl.className = 'critical';
    updateNextSignalTimer(0, false);
    return;
  }
  const totalSec = Math.ceil(remainingMs / 1000);
  const mm = Math.floor(totalSec / 60);
  const ss = totalSec % 60;
  countdownEl.textContent = '⏱ ' + String(mm).padStart(2,'0') + ':' + String(ss).padStart(2,'0');
  if(totalSec <= 5)       countdownEl.className = 'critical';
  else if(totalSec <= 10) countdownEl.className = 'warn';
  else                    countdownEl.className = '';

  // FIX (UI-SIGNAL-ENHANCE, 2026-07-23): update the next-signal timer
  // in the signal panel at the same time. The "next signal" arrives at
  // the next candle close, which is the same time as the countdown ends.
  // We show it as "next: MM:SS" in the signal box so the user knows
  // when a new prediction will be produced.
  updateNextSignalTimer(totalSec, false);
}

/* ─── NEXT SIGNAL TIMER ─────────────────────────────────────────────────── */
/* FIX (UI-SIGNAL-ENHANCE, 2026-07-23): a dedicated "next signal" timer
   shown directly inside the signal box. Tells the user when the next
   prediction will arrive (at the next candle close). */
function updateNextSignalTimer(totalSec, idle){
  const el = $('next-signal-timer');
  if(!el) return;
  if(idle || totalSec <= 0){
    el.textContent = '⏭ next: --:--';
    el.className = 'next-signal-timer';
    return;
  }
  const mm = Math.floor(totalSec / 60);
  const ss = totalSec % 60;
  const formatted = String(mm).padStart(2,'0') + ':' + String(ss).padStart(2,'0');
  el.textContent = '⏭ next: ' + formatted;
  // Pulse in the critical zone (last 5s before new signal)
  el.className = (totalSec <= 5)
    ? 'next-signal-timer critical'
    : 'next-signal-timer';
}

/* ─── EVENT WIRING ───────────────────────────────────────────────────────── */
function wireEvents(){
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-09, CRITICAL): guard against duplicate
  // listener registration on re-entrant initApp calls. Previously, calling
  // initApp more than once (e.g. via the pageshow bfcache handler, or via
  // console) registered ANOTHER listener for every addEventListener call —
  // pair-select change fired twice, sound button toggled twice per click,
  // modal close fired twice, etc. Only the Escape-key listener was guarded
  // (by the _escapeWired flag). Now: skip the whole wiring block on re-entry;
  // the DOM survives bfcache so the previous listeners are still attached.
  if(_eventsWired) return;
  _eventsWired = true;
  // Pair selector change → re-subscribe.
  const pairSelect = $('pair-select');
  if(pairSelect){
    pairSelect.addEventListener('change', () => {
      // FIX (DEEP-AUDIT-2026-07-26 / F-17-22, HIGH): bail early if the user
      // re-selected the SAME pair (Chrome doesn't fire change on re-select,
      // but Safari does). Previously the heavy reset (wipe signalHistory,
      // candleData, etc.) ran unnecessarily — wiping the chart and history.
      if(pairSelect.value === currentAsset) return;
      currentAsset = pairSelect.value;
      const cur = pairsList.find(p => p.asset === currentAsset);
      const payoutLabel = $('payout-label');
      if(cur && payoutLabel){
        payoutLabel.textContent = 'Payout: ' + (cur.payout || '—');
      }
      // Sanity check: the selected asset must belong to the current page's
      // category. (Server enforces this too — better to bail out client-side
      // than hit a category/asset mismatch error.)
      // FIX (ALLTIME-OTC-FIX 2026-07-30): user complaint — "All time OTC তে
      // পেয়ার চেঞ্জ করলে OTC তে পাঠিয়ে দেয়"। Root cause: when a pair from
      // alltime_otc was selected, the lookup in pairsList sometimes failed
      // (pair not yet loaded), and the fallback logic at the bottom used the
      // _otc suffix heuristic which redirected to 'otc' page.
      // Now: ALL alltime_otc pairs STAY on the alltime_otc page — no redirect.
      // Only redirect if a REAL pair is selected on an OTC page, or vice versa.
      const isOtcAsset = currentAsset.endsWith('_otc');

      // FIX (ALLTIME-OTC-FIX): if we're on alltime_otc page, NEVER redirect.
      // All alltime_otc pairs end with _otc and belong on this page.
      if(currentCategory === 'alltime_otc'){
        // All pairs on this page are OTC-type — stay here, no redirect needed.
        // Just proceed to re-subscribe.
      } else if(currentCategory === 'real'){
        // On Real page: if user selected an _otc pair, redirect to OTC page
        if(isOtcAsset){
          setCategory('otc'); return;
        }
      } else {
        // On OTC page: if user selected a non-_otc pair, redirect to Real
        if(!isOtcAsset){
          setCategory('real'); return;
        }
      }
      signalHistory = []; totalCorrect = 0; totalSignals = 0;
      renderHistory(); renderAccuracy();
      candleData = []; tapePrices = []; tapeDir = [];
      if(candleSeries) candleSeries.setData([]);
      if(ghostSeries) ghostSeries.setData([]);
      showChartLoading();
      currentMicro = null; runningConf = null;
      runningCandleOpenTime = 0;
      lastPrediction = null;
      alertedCandleOpenTime = 0;
      alertedSignalDirection = null;
      renderPending();
      // FIX (DEEP-AUDIT-2026-07-26 / F-17-15, HIGH): removed dead tick-tape
      // reset (`const tickTapeInner = $('tick-tape-inner'); if(tickTapeInner)
      // tickTapeInner.innerHTML = '...'`) — #tick-tape-inner doesn't exist in HTML.
      send({ type: 'subscribe', asset: currentAsset, period: currentPeriod,
             category: currentCategory });
      setTimeout(loadServerHistory, 500);
    });
  }

  // ── TAB BAR: switch between Live Chart / Signal History / Accuracy Stats
  // The 3 buttons live in #tab-bar; clicking one calls switchTab() which
  // toggles the .active class on both the button and the matching pane.
  const tabBtns = document.querySelectorAll('.tab-btn');
  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const tabName = btn.dataset.tab;
      if(tabName) switchTab(tabName);
    });
    btn.addEventListener('keydown', e => {
      if(e.key === 'Enter' || e.key === ' '){
        e.preventDefault(); btn.click();
      }
    });
  });

  // ── History tab pair-select — mirrors the topbar #pair-select. When the
  // user picks a different pair here, we sync the topbar select and dispatch
  // its change event so the existing re-subscribe logic runs unchanged.
  const histPairSelect = $('history-pair-select');
  if(histPairSelect){
    histPairSelect.addEventListener('change', () => {
      const topbarSelect = $('pair-select');
      if(!topbarSelect) return;
      if(topbarSelect.value !== histPairSelect.value){
        topbarSelect.value = histPairSelect.value;
        // Dispatch a change event so the existing handler re-subscribes.
        try{
          topbarSelect.dispatchEvent(new Event('change', { bubbles: true }));
        }catch(_){
          // Fallback for older browsers — invoke the change handler directly.
          if(topbarSelect.onchange) topbarSelect.onchange();
        }
      }
    });
  }

  // Sound toggle.
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-34, HIGH): update aria-label dynamically
  // so screen readers announce "Unmute sounds" / "Mute sounds" based on state.
  // Previously aria-label was static "Sound" — screen reader users had no
  // audible cue whether sound was on or off (only the emoji changed visually).
  const soundBtn = $('sound-btn');
  if(soundBtn){
    soundBtn.addEventListener('click', () => {
      soundEnabled = !soundEnabled;
      soundBtn.textContent = soundEnabled ? '🔔' : '🔇';
      soundBtn.setAttribute('aria-pressed', String(soundEnabled));
      soundBtn.setAttribute('aria-label', soundEnabled ? 'Mute sounds' : 'Unmute sounds');
      soundBtn.classList.toggle('on', soundEnabled);
      if(soundEnabled){
        if(!audioCtx) audioCtx = new (window.AudioContext||window.webkitAudioContext)();
        if(audioCtx.state === 'suspended'){
          audioCtx.resume().catch(()=>{});
        }
      }
    });
  }

  // 6-Module Engine toggle (collapse/expand).
  const theoriesToggle = $('theories-toggle');
  if(theoriesToggle){
    theoriesToggle.addEventListener('click', () => {
      const theoriesList = $('theories-list');
      if(!theoriesList) return;
      const isOpen = theoriesList.classList.toggle('open');
      theoriesToggle.classList.toggle('open', isOpen);
      theoriesToggle.setAttribute('aria-expanded', String(isOpen));
    });
    theoriesToggle.addEventListener('keydown', e => {
      if(e.key === 'Enter' || e.key === ' '){
        e.preventDefault(); theoriesToggle.click();
      }
    });
  }

  // Signal detail modal — close button + click-outside.
  const detailClose = $('detail-close');
  if(detailClose){
    detailClose.addEventListener('click', () => {
      if(detailOverlay) detailOverlay.classList.remove('show');
      // FIX (UI-P3-5, 2026-07-21): remove inert from background + restore focus.
      const appEl = document.getElementById('app');
      if(appEl && appEl.removeAttribute) appEl.removeAttribute('inert');
      if(window._lastFocusedRow){
        try{ window._lastFocusedRow.focus(); }catch(_){}
        window._lastFocusedRow = null;
      }
    });
  }
  if(detailOverlay){
    detailOverlay.addEventListener('click', (e) => {
      if(e.target === detailOverlay){
        detailOverlay.classList.remove('show');
        const appEl = document.getElementById('app');
        if(appEl && appEl.removeAttribute) appEl.removeAttribute('inert');
        if(window._lastFocusedRow){
          try{ window._lastFocusedRow.focus(); }catch(_){}
          window._lastFocusedRow = null;
        }
      }
    });
  }

  // Market switch button → cycle to the NEXT market.
  // FIX (DATA-FLOW-2026-07-22): cycle real → otc → alltime_otc → real.
  const switchBtn = $('market-switch-btn');
  if(switchBtn){
    switchBtn.addEventListener('click', () => {
      setCategory(_nextCategory(currentCategory));
    });
  }

  // FIX (DEEP-AUDIT-2026-07-26 / F-17-18 + F-17-19, HIGH): DELETED the
  // entire drawerToggle (#history-drawer-toggle) mobile-history-drawer
  // wiring block (~18 lines). The mobile history drawer feature was
  // removed (history is now a tab pane, not a bottom panel) — both
  // #history-drawer-toggle and #history-panel no longer exist in any HTML
  // file, so the whole `if(drawerToggle){...}` block was dead code that
  // ran on every initApp call without effect. Also deleted the
  // desktop-collapsed-removal branch.

  // FIX (UI-P1-3, 2026-07-21): Microstructure section collapse toggle.
  // On mobile it's collapsed by default (saves ~200px vertical space);
  // tap the section title to expand.
  const microSection = document.querySelector('.panel-section--micro');
  if(microSection){
    const microTitle = microSection.querySelector('.section-title');
    if(microTitle){
      microTitle.style.cursor = 'pointer';
      microTitle.addEventListener('click', (e) => {
        // FIX (DEEP-AUDIT-2026-07-26 / F-17-36, HIGH): use contains() instead
        // of strict !== so clicks on child elements (icons, badges) still
        // toggle collapse. Previously `if(e.target !== microTitle) return;`
        // silently ignored clicks on any future child element of the title.
        if(!microTitle.contains(e.target)) return;
        microSection.classList.toggle('collapsed');
      });
    }
  }

  // FIX (UI-P0-1, 2026-07-21): previously this line OVERWROTE the
  // ctime-aware `window._showSignalDetail` defined earlier (line ~929)
  // with the raw `showSignalDetail(idx)` function. History rows render
  // with `onclick="window._showSignalDetail('1721817600')"` (a ctime
  // STRING), but the overridden function expected an integer INDEX —
  // so signalHistory["1721817600"] was undefined and the modal never
  // opened. The ctime-aware version is the correct one; we now SKIP
  // the override entirely (only set it if not already defined).
  if(!window._showSignalDetail){
    window._showSignalDetail = showSignalDetail;
  }

  // FIX (UI-P1-10, 2026-07-21): Escape key closes the modal + focus trap.
  if(!window._escapeWired){
    window._escapeWired = true;
    document.addEventListener('keydown', (e) => {
      if(e.key === 'Escape' && detailOverlay && detailOverlay.classList.contains('show')){
        detailOverlay.classList.remove('show');
        // FIX (UI-P3-5): remove inert + restore focus.
        const appEl = document.getElementById('app');
        if(appEl && appEl.removeAttribute) appEl.removeAttribute('inert');
        if(window._lastFocusedRow){
          try{ window._lastFocusedRow.focus(); }catch(_){}
          window._lastFocusedRow = null;
        }
      }
    });
  }
}

/* ─── INIT ───────────────────────────────────────────────────────────────── */
function safeInit(){
  // FIX (CRASH-2026-07-30): abort if retries exceeded (set by setCategory
  // cleanup before navigation). Prevents pending safeInit retries from
  // running on a torn-down DOM after the user switched to another market.
  if(safeInit._retries && safeInit._retries > 100){
    return;
  }
  if(typeof LightweightCharts === 'undefined'){
    if(!safeInit._retries) safeInit._retries = 0;
    safeInit._retries++;
    // FIX (DEEP-AUDIT-2026-07-26 / F-17-33, HIGH): show chart-loading overlay
    // with a "Loading chart library…" message during retries. Previously
    // the 5-second retry window was invisible to the user — the chart
    // container showed nothing while safeInit polled every 100ms. Now:
    // surface a loading message so the user knows the chart is loading
    // (not broken). The overlay is dismissed by hideChartLoading() once a
    // snapshot arrives, or by the 20s timeout in showChartLoading().
    if(safeInit._retries === 1){
      const overlay = $('chart-loading-overlay');
      const txt = $('chart-loading-text');
      if(overlay) overlay.classList.add('show');
      if(txt) txt.textContent = 'Loading chart library…';
    }
    if(safeInit._retries > 50){
      console.error('LightweightCharts failed to load after 5s');
      const el = $('chart-container');
      if(el) el.innerHTML = '<div style="color:#ff1744;padding:20px;font-size:13px">Chart library failed to load. Check your network connection.</div>';
      return;
    }
    setTimeout(safeInit, 100);
    return;
  }
  initChart();
  renderHistory();
  renderAccuracy();
  showChartLoading();
  connect();
}

/* initApp(category) — entry point called by real.js / otc.js on DOMContentLoaded.
   Resets all module state (so the IIFE can be re-entered safely), wires up
   DOM events, starts the chart + WebSocket. */
function initApp(category){
  // FIX (P0-ISSUE-001, 2026-07-22): was `category !== 'real' && category !== 'otc'`
  // — rejected 'alltime_otc', so the All-Time OTC page's initApp() returned
  // immediately without setting up WS / chart / pairs. THE root cause of
  // "All-time OTC দেখায় কিন্তু ডেটা আসে না". Now accepts all 3 categories.
  if(category !== 'real' && category !== 'otc' && category !== 'alltime_otc'){
    console.error('initApp: invalid category', category);
    return;
  }
  // Reset module state — prevents cross-page leakage if initApp is somehow
  // called twice (it shouldn't be, but defensive programming is cheap).
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-31, HIGH): detach ws handlers BEFORE
  // null-out. Previously, if a reconnect timer was already scheduled from
  // a previous pagehide lifecycle, the timer fires connect() which
  // re-creates ws — but the new ws had stale onclose/onerror handlers from
  // the previous closure that referenced outdated currentAsset. Now we
  // explicitly detach handlers and close any in-flight WS before reset.
  if(reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer = null; }
  if(ws){
    try{ ws.onclose = null; ws.onerror = null; ws.onopen = null; ws.onmessage = null; ws.close(); }catch(_){}
    ws = null;
  }
  reconnectAttempts = 0;
  chart = null; candleSeries = null; ghostSeries = null;
  candleData = []; lastPrediction = null;
  signalHistory = []; totalCorrect = 0; totalSignals = 0;
  realPairsList = []; otcPairsList = []; alltimeOtcPairsList = []; pairsList = [];
  currentMicro = null; runningConf = null;
  tapePrices = []; tapeDir = [];
  tickTimestamps = []; lastLivePrice = 0; lastTickAt = 0;
  runningCandleOpenTime = 0; alertedCandleOpenTime = 0; alertedSignalDirection = null;
  lastMessageAt = Date.now();
  _priceLines = []; _priceLinesRange = { lo:0, hi:0, step:0 };
  _rafActive = false; _rTime = 0;
  // Reset tab state — always start on the Live Chart tab.
  currentTab = 'chart';
  // Force the DOM back to the chart tab in case bfcache left another tab active.
  // (Use document.getElementById directly — the local $ helper isn't bound yet.)
  document.querySelectorAll('.tab-btn').forEach(btn => {
    const isActive = btn.dataset.tab === 'chart';
    btn.classList.toggle('active', isActive);
    btn.setAttribute('aria-selected', String(isActive));
  });
  ['chart', 'history', 'accuracy'].forEach(name => {
    const pane = document.getElementById('tab-' + name);
    if(pane) pane.classList.toggle('active', name === 'chart');
  });

  currentCategory = category;
  // Default asset depends on the page's category.
  // FIX (DATA-FLOW-2026-07-22): alltime_otc defaults to USDBDT_otc
  // (first of the 6 exotic pairs the user requested).
  // NOTE: USD/BRL is listed as BRLUSD_otc on Quotex — using the Quotex
  // symbol so the stream actually subscribes correctly.
  if(category === 'real')               currentAsset = 'EURUSD';
  else if(category === 'alltime_otc')   currentAsset = 'USDBDT_otc';
  else                                  currentAsset = 'EURUSD_otc';

  // Persist the user's choice so the router index.html picks the right
  // page on next visit.
  try{ localStorage.setItem('marketCategory', category); }catch(_){}

  // FIX (DEEP-AUDIT-2026-07-26 / F-17-03, CRITICAL): remember the active
  // category so the pageshow bfcache-restore handler can re-init the right
  // page (the IIFE preserves module state across bfcache, so we must track
  // it ourselves — DOMContentLoaded + bootstrap only fires on the initial
  // load, not on bfcache restore).
  _lastInitCategory = category;

  // Local $ helper bound to document.
  $ = id => document.getElementById(id);

  // Cache countdown + modal refs.
  countdownEl = $('candle-countdown');
  detailOverlay = $('signal-detail-overlay');
  detailBody    = $('detail-body');
  detailTitle   = $('detail-title');

  // Wire up DOM events.
  wireEvents();

  // FIX (DEEP-AUDIT-2026-07-26 / F-17-14, HIGH): removed updateMarketStatusIndicator()
  // call and the 30s _marketStatusInterval. The #market-status-indicator
  // element was removed from HTML during the "CLEAN LAYOUT OVERRIDES" CSS
  // refactor but the JS function and interval were never deleted — wasted
  // CPU every 30s and leaked interval handles on each initApp re-entry.

  // Boot.
  safeInit();

  // Periodic intervals — countdown + keepalive.
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-23, HIGH): countdown interval widened
  // from 200ms (5Hz) to 500ms (2Hz) — the MM:SS display only changes once
  // per second, and 500ms gives smooth color transitions in the critical /
  // warn zones without 5x wasted CPU. Also FIX (F-17-16): the entire
  // _tickRateInterval (250ms loop updating #tick-rate-val / #stat-active-cat
  // / #stat-signals / #stat-winrate — none of which exist in HTML) is dead
  // code; removed below. tickTimestamps array is still maintained by onTick
  // (for future use) but no interval polls it.
  _countdownInterval = setInterval(updateCandleCountdown, 500);

  // FIX (BROKER-TIME 2026-07-30): broker time clock — updates every 1 second.
  // Shows UTC HH:MM:SS so user can verify candles update at exact broker
  // time boundaries. User requirement: "প্রত্যেকটি পেয়ার এর সাথে টাইম সেকেন্ড
  // যোগ করতে হবে, এবং ব্রোকার টাইম অনুযায়ী ক্যান্ডেল গুলো আপডেট হবে।
  // মিলি সেকেন্ড ও কম বেশি থাকবে না।"
  _brokerTimeInterval = setInterval(() => {
    const el = $('broker-time');
    if(!el) return;
    const now = new Date();
    const h = String(now.getUTCHours()).padStart(2, '0');
    const m = String(now.getUTCMinutes()).padStart(2, '0');
    const s = String(now.getUTCSeconds()).padStart(2, '0');
    el.textContent = `🕐 ${h}:${m}:${s}`;
  }, 1000);

  _keepaliveInterval = setInterval(() => {
    if(ws && ws.readyState === WebSocket.OPEN){
      send({ type: 'status' });
      // Dead-connection check — if no message in 45s, force-close.
      if(Date.now() - lastMessageAt > 45000){
        console.warn('[ws] no message in 45s — force-closing dead connection');
        // FIX (AUDIT-FRONTEND #1, 2026-07-19): show the stale overlay
        // BEFORE closing — previously the proactive stale-detection
        // (staleTimeout) was scheduled but never set, so users saw a
        // frozen chart with no indication of staleness until reconnect.
        const staleOverlay = $('stale-overlay');
        const staleMsg = $('stale-msg');
        if(staleOverlay) staleOverlay.classList.add('show');
        if(staleMsg) staleMsg.textContent = '⚠ Feed stale — no data for 45s, reconnecting...';
        try{ ws.close(); }catch(_){}
      }
    }
  }, 15000);

  // Clean up on page hide — clear intervals + close WS + dispose chart.
  // FIX (DEEP-AUDIT-2026-07-26 / F-17-14 + F-17-16): removed cleanup for
  // the deleted _marketStatusInterval and _tickRateInterval.
  // FIX (F-17-03): named + guarded so initApp re-entry doesn't stack
  // duplicate pagehide/pageshow listeners on window.
  if(!_pageLifecycleWired){
    _pageLifecycleWired = true;
    window.addEventListener('pagehide', onPageHide);

    // FIX (DEEP-AUDIT-2026-07-26 / F-17-03, CRITICAL): bfcache restore handler.
    // On mobile Safari/Chrome, switching tabs and returning via back-forward
    // cache (bfcache) leaves the page frozen — pagehide cleared all intervals,
    // nullified timers, closed the WS, and disposed the chart. Without this
    // pageshow handler, the page stayed frozen until hard-refresh because
    // initApp is only called once via boot() on the initial DOMContentLoaded.
    // Now: on persisted bfcache restore, re-run initApp(category) which
    // re-establishes the WS, recreates the chart, and restarts intervals.
    // wireEvents() is guarded by _eventsWired so it skips re-registering
    // listeners (the DOM survives bfcache, so the previous listeners are
    // still attached and functional).
    window.addEventListener('pageshow', onPageShow);
  }
}

// FIX (DEEP-AUDIT-2026-07-26 / F-17-03, CRITICAL): named pagehide/pageshow
// handlers so we can guard against duplicate registration on initApp
// re-entry. Previously the pagehide listener was an anonymous arrow added
// on every initApp call — stacking listeners.
let _pageLifecycleWired = false;
let _brokerTimeInterval = null;
function onPageHide(){
  try{
    if(_countdownInterval){ clearInterval(_countdownInterval); _countdownInterval = null; }
    if(_brokerTimeInterval){ clearInterval(_brokerTimeInterval); _brokerTimeInterval = null; }
    if(_keepaliveInterval){ clearInterval(_keepaliveInterval); _keepaliveInterval = null; }
    if(staleTimeout){ clearTimeout(staleTimeout); staleTimeout = null; }
    if(chartLoadingTimeout){ clearTimeout(chartLoadingTimeout); chartLoadingTimeout = null; }
    if(reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer = null; }
    if(_resizeTimer){ clearTimeout(_resizeTimer); _resizeTimer = null; }
    _rafActive = false;
    if(ws){ try{ ws.onclose = null; ws.onerror = null; ws.onopen = null; ws.onmessage = null; ws.close(); }catch(_){} }
    if(chart){ try{ chart.remove(); }catch(_){} chart = null; }
  }catch(_){}
}
function onPageShow(e){
  // e.persisted === true means the page was restored from the bfcache
  // (back-forward cache). The pagehide handler ran cleanup — we need to
  // re-establish the WS, chart, and intervals. The DOM is intact.
  if(!e.persisted) return;
  if(_lastInitCategory){
    try{ initApp(_lastInitCategory); }catch(err){ console.error('[pageshow] initApp error', err); }
  }
}

// Expose initApp + setCategory globally so the page-specific bootstrap
// (real.js / otc.js) can call it.
global.initApp = initApp;
global.setCategory = setCategory;

/* ═══════════════════════════════════════════════════════════════════════
   SHARE SIGNAL TABLE (2026-07-30)
   User requirement: "একটি টেবিল সেকশন বানাতে হবে। যার নাম থাকবে শেয়ার
   সিগন্যাল। Pair name - otc or real - time - close candle data buyer
   Sellar - prediction signal call put. সব গুলো পেয়ার এর লাস্ট এর
   আপডেট থাকবে। তারপর এই গুলো হিস্টোরিতে গিয়ে সেভ হবে।"
   ═══════════════════════════════════════════════════════════════════════ */
let _shareSignalInterval = null;

async function fetchShareSignals(){
  try{
    const resp = await fetch('/api/share-signals');
    if(!resp.ok) return null;
    return await resp.json();
  }catch(e){ return null; }
}

function renderShareSignalTable(data){
  const tbody = document.getElementById('share-signal-tbody');
  const countEl = document.getElementById('share-signal-count');
  if(!tbody) return;

  if(!data || !data.rows || data.rows.length === 0){
    tbody.innerHTML = '<tr><td colspan="9" class="share-signal-loading">No data</td></tr>';
    return;
  }

  if(countEl){
    countEl.textContent = `${data.live_pairs} / ${data.total_pairs} live`;
  }

  let html = '';
  for(const row of data.rows){
    const typeClass = row.type === 'OTC' ? 'ss-type-otc' : 'ss-type-real';
    const signalClass = row.signal === 'CALL' ? 'ss-signal-call'
      : row.signal === 'PUT' ? 'ss-signal-put' : 'ss-signal-neutral';
    const strClass = row.strength === 'STRONG' ? 'ss-str-strong'
      : row.strength === 'MEDIUM' ? 'ss-str-medium'
      : 'ss-str-weak';

    let updateClass = 'ss-update-offline';
    let updateText = '—';
    if(row.live && row.last_update !== null){
      updateClass = 'ss-update-live';
      updateText = `${row.last_update.toFixed(0)}s ago`;
    } else if(row.last_update !== null){
      updateClass = 'ss-update-stale';
      updateText = `${(row.last_update/60).toFixed(0)}m ago`;
    }

    const buyer = row.buyer_pct !== null ? row.buyer_pct.toFixed(1) + '%' : '—';
    const seller = row.seller_pct !== null ? row.seller_pct.toFixed(1) + '%' : '—';
    const conf = row.confidence > 0 ? row.confidence.toFixed(0) : '—';

    // Highlight buyer/seller dominance
    let buyerHtml = `<span class="ss-buyer">${buyer}</span>`;
    let sellerHtml = `<span class="ss-seller">${seller}</span>`;
    if(row.buyer_pct !== null && row.seller_pct !== null){
      if(row.buyer_pct > row.seller_pct + 10){
        buyerHtml = `<span class="ss-buyer" style="font-weight:700;text-shadow:0 0 4px rgba(0,200,83,.4)">${buyer}</span>`;
      } else if(row.seller_pct > row.buyer_pct + 10){
        sellerHtml = `<span class="ss-seller" style="font-weight:700;text-shadow:0 0 4px rgba(255,23,68,.4)">${seller}</span>`;
      }
    }

    html += `<tr>
      <td class="ss-pair">${row.pair}</td>
      <td class="${typeClass}">${row.type}</td>
      <td class="ss-time">${row.time}</td>
      <td>${buyerHtml}</td>
      <td>${sellerHtml}</td>
      <td><span class="${signalClass}">${row.signal}</span></td>
      <td class="ss-conf">${conf}</td>
      <td class="${strClass}">${row.strength || '—'}</td>
      <td class="${updateClass}">${updateText}</td>
    </tr>`;
  }
  tbody.innerHTML = html;
}

async function refreshShareSignals(){
  const data = await fetchShareSignals();
  if(data) renderShareSignalTable(data);
}

async function saveShareSignals(){
  try{
    const resp = await fetch('/api/share-signals/save', {method:'POST'});
    const result = await resp.json();
    if(result.ok){
      const btn = document.getElementById('share-signal-save');
      if(btn){
        const orig = btn.textContent;
        btn.textContent = '✅';
        setTimeout(() => { btn.textContent = orig; }, 2000);
      }
    }
  }catch(e){ console.error('save share signals error', e); }
}

async function viewShareSignalHistory(){
  try{
    const resp = await fetch('/api/share-signals/history?limit=20');
    const data = await resp.json();
    if(data.history && data.history.length > 0){
      // Show most recent snapshot in the table
      const latest = data.history[0];
      if(latest.rows && latest.rows.length > 0){
        renderShareSignalTable({rows: latest.rows, live_pairs: latest.live_pairs, total_pairs: latest.total_pairs});
      }
    }
  }catch(e){ console.error('view history error', e); }
}

function initShareSignalTable(){
  // Initial fetch
  refreshShareSignals();
  // Auto-refresh every 10 seconds
  if(_shareSignalInterval) clearInterval(_shareSignalInterval);
  _shareSignalInterval = setInterval(refreshShareSignals, 10000);

  // Wire buttons
  const refreshBtn = document.getElementById('share-signal-refresh');
  if(refreshBtn) refreshBtn.addEventListener('click', refreshShareSignals);

  const saveBtn = document.getElementById('share-signal-save');
  if(saveBtn) saveBtn.addEventListener('click', saveShareSignals);

  const histBtn = document.getElementById('share-signal-history');
  if(histBtn) histBtn.addEventListener('click', viewShareSignalHistory);
}

// Auto-init after DOM ready (runs on every page load)
if(document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', () => {
    setTimeout(initShareSignalTable, 1500); // wait for other init
  });
} else {
  setTimeout(initShareSignalTable, 1500);
}

})(window);
