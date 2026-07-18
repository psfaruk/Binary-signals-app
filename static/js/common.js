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
const HISTORY_MAX = 30;

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
let realPairsList = [], otcPairsList = [], pairsList = [];
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
let _countdownInterval = null, _tickRateInterval = null, _keepaliveInterval = null;

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
    handleScroll: { vertTouchDrag: false },
  });
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
  if(candleData.length) hideChartLoading();
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
  chartLoadingTimeout = setTimeout(() => overlay.classList.remove('show'), 20000);
}
function hideChartLoading(){
  const overlay = $('chart-loading-overlay');
  if(!overlay) return;
  clearTimeout(chartLoadingTimeout);
  overlay.classList.remove('show');
}

function renderPending(){
  const signalBox = $('signal-box');
  const signalLabel = $('signal-label');
  signalBox.className = 'pending signal-pop';
  signalLabel.textContent = '⏳ PENDING';
  $('sig-score').textContent = '—';
  $('sig-score').style.color = 'var(--text-dim)';
  const strEl = $('sig-strength');
  strEl.className = 'value strength-weak';
  strEl.textContent = 'WAIT';
  $('sig-conf-val').textContent = '—';
  const confBar = $('sig-conf-bar');
  confBar.style.width = '0%';
  confBar.style.background = '#2196F3';
  $('sig-agree').textContent = '—';
  $('sig-regime').textContent = '—';

  // Reset market state panel — only trend/zone/volatility (the stale
  // phase/structure/zigzag fields have been removed entirely).
  const msTrend = $('ms-trend'); if(msTrend) msTrend.textContent = '—';
  const msZone = $('ms-zone');   if(msZone) msZone.textContent = '—';
  const msVol = $('ms-volatility'); if(msVol) msVol.textContent = '—';

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
  const candleKey = runningCandleOpenTime || 0;
  const isNewCandle = candleKey !== alertedCandleOpenTime;
  const isDirectionChange = alertedSignalDirection !== s;
  const shouldAlert = isNewCandle || isDirectionChange;
  if(shouldAlert){
    alertedCandleOpenTime = candleKey;
    alertedSignalDirection = s;
  }

  const signalBox = $('signal-box');
  const signalLabel = $('signal-label');
  const cls = s === 'CALL' ? 'call' : s === 'PUT' ? 'put' : 'neutral';
  signalBox.className = cls + (shouldAlert ? ' signal-pop' : '');
  signalLabel.textContent = s === 'CALL' ? '🟢 CALL' : s === 'PUT' ? '🔴 PUT' : '➖ NEUTRAL';

  const score = pred.score || 0;
  const sigScore = $('sig-score');
  sigScore.textContent = (score >= 0 ? '+' : '') + score;
  sigScore.style.color = score >= 0 ? 'var(--green)' : 'var(--red)';

  const str = (pred.strength || '').toUpperCase();
  const strCls = str === 'STRONG'
    ? (s === 'PUT' ? 'strength-strong-put' : 'strength-strong')
    : str === 'MEDIUM' ? 'strength-medium' : 'strength-weak';
  const sigStrength = $('sig-strength');
  sigStrength.className = 'value ' + strCls;
  sigStrength.textContent = str || '—';

  const conf = pred.confidence || 0;
  $('sig-conf-val').textContent = Math.round(conf) + '%';
  const confBar = $('sig-conf-bar');
  confBar.style.width = conf + '%';
  confBar.style.background = s === 'CALL' ? 'var(--green)' : s === 'PUT' ? 'var(--red)' : 'var(--text-dim)';

  $('sig-agree').textContent = (pred.agree || 0) + '/' + (pred.total || 0) + ' modules';

  // Regime display — the regime dict has: regime, trend_strength,
  // volatility_pct, ema9, ema21, is_trending, is_ranging, is_volatile.
  const reg = pred.regime || {};
  const regStr = (typeof reg === 'object' && reg !== null)
    ? (reg.regime || '—') + ' (str=' + (reg.trend_strength || 0) + ')'
    : String(reg || '—');
  $('sig-regime').textContent = regStr;

  // Market State panel — only 3 live fields now (phase/structure/zigzag removed).
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
    const cat = pred.category || currentCategory;
    if(cat === 'real'){
      engineLabel.textContent = 'REAL ENGINE';
      engineLabel.style.background = 'rgba(0,200,83,.15)';
      engineLabel.style.color = 'var(--green)';
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
  const bsBuy = $('bs-buy'), bsSell = $('bs-sell');
  const buyPct = Math.max(0, Math.min(100, Math.round(micro.buy_pct || 50)));
  const sellPct = 100 - buyPct;
  bsBuy.style.width = buyPct + '%';
  bsSell.style.width = sellPct + '%';
  bsBuy.textContent = buyPct + '%';
  bsSell.textContent = sellPct + '%';

  const press = (micro.pressure || '—').toUpperCase();
  const msPressure = $('ms-pressure');
  msPressure.textContent = press;
  msPressure.className = 'pressure-badge ' + (press === 'BUYER' ? 'buyer' : press === 'SELLER' ? 'seller' : 'fight');

  const react = (micro.reaction || '—').toUpperCase();
  const msReaction = $('ms-reaction');
  msReaction.textContent = react === 'BUYER' ? 'BUYER REACT' : react === 'SELLER' ? 'SELLER REACT' : '—';
  msReaction.className = 'react-badge ' + (react === 'BUYER' ? 'recovery' : react === 'SELLER' ? 'exhaust' : 'none');

  const msFight = $('ms-fight');
  if(micro.is_fight){
    msFight.innerHTML = '<span style="color:var(--yellow);font-weight:700">⚠ YES</span> <span style="color:var(--text-dim);font-size:10px">(' + (micro.crosses||0) + ' crosses)</span>';
  } else {
    msFight.innerHTML = 'No <span style="color:var(--text-dim);font-size:10px">(' + (micro.crosses||0) + ' crosses)</span>';
  }

  const msHold = $('ms-hold');
  if(micro.hold_price != null){
    msHold.innerHTML = '<span style="color:var(--blue)">' + fmtPrice(micro.hold_price) + '</span> <span style="color:var(--text-dim);font-size:10px">(' + (micro.hold_visits||0) + 'x)</span>';
  } else {
    msHold.textContent = '—';
  }

  const phases = micro.phases || [];
  ['early','mid','late'].forEach((p, i) => {
    const el = $('ph-' + p);
    if(!el) return;
    const ph = phases[i];
    const dir = (typeof ph === 'string' ? ph : (ph && ph.dir ? ph.dir : '—')).toUpperCase();
    const arrows = dir === 'UP' ? '↑↑' : dir === 'DOWN' ? '↓↓' : '→';
    el.textContent = (dir !== '—' ? dir + ' ' : '') + arrows;
    el.className = 'phase-dir ' + (dir === 'UP' ? 'up' : dir === 'DOWN' ? 'down' : 'flat');
  });

  const lr = (micro.last_react || '').toUpperCase();
  const msLastReact = $('ms-last-react');
  msLastReact.textContent = lr || '—';
  msLastReact.className = 'react-badge ' + (lr === 'EXHAUST' ? 'exhaust' : lr === 'RECOVERY' ? 'recovery' : 'none');

  const msRunningConf = $('ms-running-conf');
  msRunningConf.textContent = runningConf === 'CONFIRMING' ? '✅ CONFIRMING'
                            : runningConf === 'OPPOSING' ? '❌ OPPOSING' : '—';
  msRunningConf.className = 'running-conf ' + (runningConf === 'CONFIRMING' ? 'confirming' : runningConf === 'OPPOSING' ? 'opposing' : 'none');

  $('ms-ticks').textContent = micro.tick_count || 0;
  const net = micro.net || 0;
  const msNet = $('ms-net');
  msNet.textContent = (net >= 0 ? '+' : '') + net.toFixed(5);
  msNet.style.color = net >= 0 ? 'var(--green)' : 'var(--red)';

  const rl = micro.round || {};
  const msRound = $('ms-round');
  if(rl.near_level){
    msRound.innerHTML = '<span style="color:var(--yellow)">' + fmtPrice(rl.near_level) + '</span> <span style="color:var(--text-dim);font-size:10px">(' + (rl.near_strength || '—') + ')</span>';
  } else {
    msRound.textContent = '—';
  }
}

/* ─── TICK TAPE ──────────────────────────────────────────────────────────── */
function addTapeTick(price){
  const prev = tapePrices[tapePrices.length - 1];
  const dir = prev ? (price > prev ? 'up' : price < prev ? 'down' : 'flat') : 'flat';
  tapePrices.push(price);
  tapeDir.push(dir);
  if(tapePrices.length > TICK_TAPE_MAX){ tapePrices.shift(); tapeDir.shift(); }
  renderTape();
}
function renderTape(){
  const tickTapeInner = $('tick-tape-inner');
  if(!tickTapeInner) return;
  let html = '';
  for(let i = 0; i < tapePrices.length; i++){
    html += '<span class="tape-price ' + tapeDir[i] + '"><span class="tape-arrow">'
      + (tapeDir[i]==='up'?'↑':tapeDir[i]==='down'?'↓':'→') + '</span>'
      + fmtPrice(tapePrices[i]) + '</span>';
  }
  tickTapeInner.innerHTML = html;
}

/* ─── SIGNAL HISTORY ─────────────────────────────────────────────────────── */
function addHistory(signal, accuracy, detail){
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
  if(!sigs || !sigs.length) return;
  // Drop stale responses from a previous pair switch.
  if(asset && asset !== currentAsset) return;
  if(period && period !== currentPeriod) return;
  signalHistory = sigs.slice(-HISTORY_MAX).map(s => ({
    signal: s.signal,
    accuracy: s.accuracy,
    detail: s,
  }));
  const graded = sigs.filter(s => s.accuracy === 'correct' || s.accuracy === 'wrong');
  totalSignals = graded.length;
  totalCorrect = graded.filter(s => s.accuracy === 'correct').length;
  renderHistory();
  renderAccuracy();
  setTimeout(() => { const hl = $('history-list'); if(hl) hl.scrollTop = 0; }, 50);
}

function renderHistory(){
  const historyList = $('history-list');
  if(!historyList) return;
  if(!signalHistory.length){
    historyList.innerHTML = '<div style="color:var(--text-dim);font-size:11px;padding:8px 4px">No signals yet</div>';
    return;
  }
  const lastIdx = signalHistory.length - 1;
  let html = '';
  for(let i = lastIdx; i >= 0; i--){
    const h = signalHistory[i];
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
    const clickable = h.detail ? `onclick="window._showSignalDetail(${i})"` : '';
    html += `<div class="history-row${accCls}${recentCls}" ${clickable}>`
         +  `<span class="history-time">${time}</span>`
         +  `<span class="history-signal ${sigCls}">${h.signal}</span>`
         +  `<span class="history-strength ${strengthCls}">${strengthLetter}</span>`
         +  `<span class="history-score ${scoreCls}">${score || '—'}</span>`
         +  `<span class="history-icon">`
         +    `<span class="icon-emoji">${iconEmoji}</span>`
         +    `<span class="icon-label">${iconLabel}</span>`
         +  `</span>`
         +  `</div>`;
  }
  historyList.innerHTML = html;
}

function showSignalDetail(idx){
  const h = signalHistory[idx];
  if(!h || !h.detail || !detailBody || !detailOverlay) return;
  const d = h.detail;
  const sig = d.signal || '—';
  const acc = d.accuracy || '—';
  const accIcon = acc === 'correct' ? '✅ WIN' : acc === 'wrong' ? '❌ LOSS' : '➖ DRAW';
  const accColor = acc === 'correct' ? 'var(--green)' : acc === 'wrong' ? 'var(--red)' : 'var(--text-dim)';

  detailTitle.innerHTML = `<span style="color:${sig==='CALL'?'var(--green)':'var(--red)'}">${sig}</span> <span style="color:${accColor};font-size:13px;margin-left:8px">${accIcon}</span>`;

  let rows = '';
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
  const accPct = $('acc-pct');
  const accDetail = $('acc-detail');
  if(!accPct) return;
  if(!totalSignals){ accPct.textContent = '—'; if(accDetail) accDetail.textContent = ''; return; }
  const pct = Math.round((totalCorrect / totalSignals) * 100);
  accPct.textContent = pct + '%';
  if(accDetail) accDetail.textContent = '(' + totalCorrect + '/' + totalSignals + ')';
  accPct.style.color = pct >= 60 ? 'var(--green)' : pct >= 40 ? 'var(--yellow)' : 'var(--red)';
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
  pairsList = realPairsList.concat(otcPairsList);

  const pairSelect = $('pair-select');
  const pairsCount = $('pairs-count');

  // Render only the active category's pairs in the dropdown.
  const activeList = currentCategory === 'real' ? realPairsList : otcPairsList;
  pairSelect.innerHTML = '';
  if(activeList.length === 0){
    const opt = document.createElement('option');
    opt.value = '';
    if(currentCategory === 'real'){
      opt.textContent = '⚠ Real Market closed (weekend/bank holiday)';
    } else {
      opt.textContent = 'No OTC pairs available';
    }
    opt.disabled = true;
    pairSelect.appendChild(opt);
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

  if(pairsCount){
    pairsCount.textContent = (currentCategory === 'real' ? 'Real: ' : 'OTC: ') + activeList.length;
  }

  // If the currently-selected asset is NOT in the active category's list,
  // auto-switch to the first available pair.
  const stillThere = activeList.find(p => p.asset === currentAsset && !p.locked);
  if(!stillThere && activeList.length > 0){
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

  // Update payout label for the currently-selected pair.
  const payoutLabel = $('payout-label');
  const cur = pairsList.find(p => p.asset === currentAsset);
  if(cur && payoutLabel){
    payoutLabel.textContent = 'Payout: ' + (cur.payout || '—');
  }
}

/* ─── MARKET STATUS INDICATOR ──────────────────────────────────────────────
   Updates the small LIVE / CLOSED / 24/7 pill in row 2.
   Real market is closed on weekends (Sat/Sun UTC). OTC runs 24/7. */
function updateMarketStatusIndicator(){
  const el = $('market-status-indicator');
  if(!el) return;
  if(currentCategory === 'real'){
    // Real market: closed on weekends (UTC Sat=6, Sun=0).
    const day = new Date().getUTCDay();
    const closed = (day === 0 || day === 6);
    if(closed){
      el.className = 'market-status closed';
      el.innerHTML = '<span class="status-dot"></span>REAL MARKET — CLOSED';
      el.title = 'Real forex market is closed on weekends (UTC). OTC market is open 24/7.';
    } else {
      el.className = 'market-status live';
      el.innerHTML = '<span class="status-dot"></span>REAL MARKET — LIVE';
      el.title = 'Real market is live.';
    }
  } else {
    el.className = 'market-status always';
    el.innerHTML = '<span class="status-dot"></span>OTC MARKET — 24/7';
    el.title = 'OTC market runs 24 hours a day, 7 days a week.';
  }
}

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
  if(newCat !== 'real' && newCat !== 'otc') return;
  if(newCat === currentCategory) return;
  try{ localStorage.setItem('marketCategory', newCat); }catch(_){}
  const target = newCat === 'real' ? '/static/real.html' : '/static/otc.html';
  // replace() — don't leave the old page in history so the browser back
  // button doesn't bounce the user between the two market pages.
  window.location.replace(target);
}

/* ─── WEBSOCKET ──────────────────────────────────────────────────────────── */
function connect(){
  if(ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
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
    try { handleMsg(JSON.parse(e.data)); } catch(err){ console.error('msg parse error', err); }
  };
  ws.onclose = () => { setStatus('disconnected'); scheduleReconnect(); };
  ws.onerror = () => { try{ ws.close(); }catch(_){} };
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
  if(c.length) addTapeTick(c[c.length-1].close);
  if(c.length){
    runningCandleOpenTime = c[c.length-1].time;
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
        livePriceEl.textContent = (typeof newPrice === 'number')
          ? newPrice.toFixed(newPrice > 100 ? 3 : 5)
          : '—';
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
        lastLivePrice = newPrice;
      }
    }
    // Tick rate tracking.
    const nowMs = Date.now();
    tickTimestamps.push(nowMs);
    lastTickAt = nowMs;
    while(tickTimestamps.length > 0 && nowMs - tickTimestamps[0] > 1000){
      tickTimestamps.shift();
    }
  }
  if(c) addTapeTick(c.close);
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
  if(c.length) addTapeTick(c[c.length-1].close);
  if(c.length){
    runningCandleOpenTime = c[c.length-1].time;
  }
  if(msg.prediction){
    lastPrediction = msg.prediction;
    renderSignal(msg.prediction);
  } else {
    renderPending();
  }
  if(lastPrediction){
    addHistory(lastPrediction.signal || 'NEUTRAL', msg.accuracy || 'draw');
    setTimeout(loadServerHistory, 500);
  }
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
    countdownEl.textContent = '--:--';
    countdownEl.className = 'idle';
    return;
  }
  const closeAt = (runningCandleOpenTime + currentPeriod) * 1000;
  const remainingMs = closeAt - Date.now();
  if(remainingMs <= 0){
    countdownEl.textContent = '00:00';
    countdownEl.className = 'critical';
    return;
  }
  const totalSec = Math.ceil(remainingMs / 1000);
  const mm = Math.floor(totalSec / 60);
  const ss = totalSec % 60;
  countdownEl.textContent = String(mm).padStart(2,'0') + ':' + String(ss).padStart(2,'0');
  if(totalSec <= 5)       countdownEl.className = 'critical';
  else if(totalSec <= 10) countdownEl.className = 'warn';
  else                    countdownEl.className = '';
}

/* ─── EVENT WIRING ───────────────────────────────────────────────────────── */
function wireEvents(){
  // Pair selector change → re-subscribe.
  const pairSelect = $('pair-select');
  if(pairSelect){
    pairSelect.addEventListener('change', () => {
      currentAsset = pairSelect.value;
      const cur = pairsList.find(p => p.asset === currentAsset);
      const payoutLabel = $('payout-label');
      if(cur && payoutLabel){
        payoutLabel.textContent = 'Payout: ' + (cur.payout || '—');
      }
      // Sanity check: the selected asset must belong to the current page's
      // category. (Server enforces this too — better to bail out client-side
      // than hit a category/asset mismatch error.)
      const newCat = currentAsset.endsWith('_otc') ? 'otc' : 'real';
      if(newCat !== currentCategory){
        // Should not happen (dropdown is filtered), but if it does, redirect.
        setCategory(newCat);
        return;
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
      const tickTapeInner = $('tick-tape-inner');
      if(tickTapeInner) tickTapeInner.innerHTML = '<span class="tape-price flat">Waiting for data...</span>';
      send({ type: 'subscribe', asset: currentAsset, period: currentPeriod,
             category: currentCategory });
      setTimeout(loadServerHistory, 500);
    });
  }

  // Sound toggle.
  const soundBtn = $('sound-btn');
  if(soundBtn){
    soundBtn.addEventListener('click', () => {
      soundEnabled = !soundEnabled;
      soundBtn.textContent = soundEnabled ? '🔔' : '🔇';
      soundBtn.setAttribute('aria-pressed', String(soundEnabled));
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
    });
  }
  if(detailOverlay){
    detailOverlay.addEventListener('click', (e) => {
      if(e.target === detailOverlay) detailOverlay.classList.remove('show');
    });
  }

  // Market switch button → setCategory to the OTHER market.
  const switchBtn = $('market-switch-btn');
  if(switchBtn){
    switchBtn.addEventListener('click', () => {
      setCategory(currentCategory === 'real' ? 'otc' : 'real');
    });
  }

  // Expose signal detail opener for onclick= attributes in history rows.
  window._showSignalDetail = showSignalDetail;
}

/* ─── INIT ───────────────────────────────────────────────────────────────── */
function safeInit(){
  if(typeof LightweightCharts === 'undefined'){
    if(!safeInit._retries) safeInit._retries = 0;
    safeInit._retries++;
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
  if(category !== 'real' && category !== 'otc'){
    console.error('initApp: invalid category', category);
    return;
  }
  // Reset module state — prevents cross-page leakage if initApp is somehow
  // called twice (it shouldn't be, but defensive programming is cheap).
  ws = null; reconnectTimer = null; reconnectAttempts = 0;
  chart = null; candleSeries = null; ghostSeries = null;
  candleData = []; lastPrediction = null;
  signalHistory = []; totalCorrect = 0; totalSignals = 0;
  realPairsList = []; otcPairsList = []; pairsList = [];
  currentMicro = null; runningConf = null;
  tapePrices = []; tapeDir = [];
  tickTimestamps = []; lastLivePrice = 0; lastTickAt = 0;
  runningCandleOpenTime = 0; alertedCandleOpenTime = 0; alertedSignalDirection = null;
  lastMessageAt = Date.now();
  _priceLines = []; _priceLinesRange = { lo:0, hi:0, step:0 };
  _rafActive = false; _rTime = 0;

  currentCategory = category;
  // Default asset depends on the page's category.
  currentAsset = (category === 'real') ? 'EURUSD' : 'EURUSD_otc';

  // Persist the user's choice so the router index.html picks the right
  // page on next visit.
  try{ localStorage.setItem('marketCategory', category); }catch(_){}

  // Local $ helper bound to document.
  $ = id => document.getElementById(id);

  // Cache countdown + modal refs.
  countdownEl = $('candle-countdown');
  detailOverlay = $('signal-detail-overlay');
  detailBody    = $('detail-body');
  detailTitle   = $('detail-title');

  // Wire up DOM events.
  wireEvents();

  // Update market status indicator (LIVE / CLOSED / 24/7).
  updateMarketStatusIndicator();
  // Refresh it every 30s — Real market status flips at weekend boundary.
  setInterval(updateMarketStatusIndicator, 30000);

  // Boot.
  safeInit();

  // Periodic intervals — countdown, tick rate display, keepalive.
  _countdownInterval = setInterval(updateCandleCountdown, 200);

  _tickRateInterval = setInterval(() => {
    const nowMs = Date.now();
    while(tickTimestamps.length > 0 && nowMs - tickTimestamps[0] > 1000){
      tickTimestamps.shift();
    }
    const tickRateVal = $('tick-rate-val');
    if(tickRateVal) tickRateVal.textContent = tickTimestamps.length;

    const statActiveCat = $('stat-active-cat');
    if(statActiveCat){
      statActiveCat.textContent = currentCategory.toUpperCase();
      statActiveCat.style.color = currentCategory === 'real' ? 'var(--green)' : 'var(--yellow)';
    }
    const statSignals = $('stat-signals');
    if(statSignals) statSignals.textContent = totalSignals;

    const statWinrate = $('stat-winrate');
    if(statWinrate){
      if(totalSignals > 0){
        const pct = Math.round((totalCorrect / totalSignals) * 100);
        statWinrate.textContent = pct + '%';
        statWinrate.style.color = pct >= 60 ? 'var(--green)' : pct >= 40 ? 'var(--yellow)' : 'var(--red)';
      } else {
        statWinrate.textContent = '—';
      }
    }

    const statCountdown = $('stat-countdown');
    if(statCountdown && runningCandleOpenTime && currentPeriod){
      const closeAt = (runningCandleOpenTime + currentPeriod) * 1000;
      const remaining = Math.max(0, Math.floor((closeAt - Date.now()) / 1000));
      const m = Math.floor(remaining / 60);
      const s = remaining % 60;
      statCountdown.textContent = (m > 0 ? m + ':' : '') + (s < 10 ? '0' : '') + s;
      if(remaining <= 5)       statCountdown.style.color = 'var(--red)';
      else if(remaining <= 10) statCountdown.style.color = 'var(--yellow)';
      else                     statCountdown.style.color = 'var(--text)';
    }
  }, 250);

  _keepaliveInterval = setInterval(() => {
    if(ws && ws.readyState === WebSocket.OPEN){
      send({ type: 'status' });
      // Dead-connection check — if no message in 45s, force-close.
      if(Date.now() - lastMessageAt > 45000){
        console.warn('[ws] no message in 45s — force-closing dead connection');
        try{ ws.close(); }catch(_){}
      }
    }
  }, 15000);

  // Clean up on page hide — clear intervals + close WS + dispose chart.
  window.addEventListener('pagehide', () => {
    try{
      if(_countdownInterval){ clearInterval(_countdownInterval); _countdownInterval = null; }
      if(_tickRateInterval){ clearInterval(_tickRateInterval); _tickRateInterval = null; }
      if(_keepaliveInterval){ clearInterval(_keepaliveInterval); _keepaliveInterval = null; }
      if(staleTimeout){ clearTimeout(staleTimeout); staleTimeout = null; }
      if(chartLoadingTimeout){ clearTimeout(chartLoadingTimeout); chartLoadingTimeout = null; }
      if(reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer = null; }
      if(_resizeTimer){ clearTimeout(_resizeTimer); _resizeTimer = null; }
      _rafActive = false;
      if(ws){ try{ ws.close(); }catch(_){} }
      if(chart){ try{ chart.remove(); }catch(_){} chart = null; }
    }catch(_){}
  });
}

// Expose initApp + setCategory globally so the page-specific bootstrap
// (real.js / otc.js) can call it.
global.initApp = initApp;
global.setCategory = setCategory;

})(window);
