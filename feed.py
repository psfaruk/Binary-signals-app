"""Quotex live data feed — multi-asset concurrent version."""
from collections.abc import Callable  # noqa: E402 — used in @dataclass below
from collections import deque  # noqa: E402
import db as _db                                  # noqa: E402
import alerts as _alerts                          # noqa: E402
from core.analysis import _key_levels, _round_level  # noqa: E402
import os      # noqa: E402
import re      # noqa: E402
import time    # noqa: E402
import asyncio  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
PAYOUT_FLOOR_REAL = int(os.environ.get("QX_PAYOUT_FLOOR_REAL", "70"))
PAYOUT_FLOOR_OTC  = int(os.environ.get("QX_PAYOUT_FLOOR_OTC",
                                       os.environ.get("QX_PAYOUT_FLOOR", "85")))

def _payout_floor_for(asset: str) -> int:
    """Return the appropriate payout floor for an asset based on category."""
    return PAYOUT_FLOOR_OTC if asset.endswith("_otc") else PAYOUT_FLOOR_REAL
ENABLE_LIVE_THEORY   = os.environ.get("ENABLE_LIVE_REEVAL",  "1") == "1"
ENABLE_STRENGTH_GATE = os.environ.get("ENABLE_STRENGTH_GATE", "1") == "1"
SIGNAL_DELAY_SEC = float(os.environ.get("SIGNAL_DELAY_SEC", "3.0"))
MICRO_RECALC_EVERY = int(os.environ.get("MICRO_RECALC_EVERY", "5"))
SKIP_REDUNDANT_BROADCAST = os.environ.get("SKIP_REDUNDANT_BROADCAST", "1") == "1"
STALE_SECS = int(os.environ.get("STALE_SECS", "90"))
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "300"))
ERROR_WINDOW    = int(os.environ.get("ERROR_WINDOW",    "60"))
ERROR_THRESHOLD = int(os.environ.get("ERROR_THRESHOLD", "10"))
ERROR_COOLDOWN  = int(os.environ.get("ERROR_COOLDOWN",  "30"))
MAX_CANDLES = int(os.environ.get("MAX_CANDLES", "500"))
TRUNCATE_TO = int(os.environ.get("TRUNCATE_TO", "400"))
SNAPSHOT_CANDLES = int(os.environ.get("SNAPSHOT_CANDLES", "300"))
RECENT_ACCURACY_N = int(os.environ.get("RECENT_ACCURACY_N", "50"))
TIMER_GRACE = float(os.environ.get("TIMER_GRACE", "7.0"))
PER_STREAM_STALE_SECS = int(os.environ.get("PER_STREAM_STALE_SECS", "60"))
HOUSEKEEP_SECS = int(os.environ.get("HOUSEKEEP_SECS", "5"))
WATCHDOG_INTERVAL = float(os.environ.get("WATCHDOG_INTERVAL", "30.0"))
GLOBAL_STALE_SECS = int(os.environ.get("GLOBAL_STALE_SECS", "180"))
MAX_ALWAYS_ON_STREAMS = int(os.environ.get("MAX_ALWAYS_ON_STREAMS", "30"))
# Loss-cluster cooldown: 5 wrong in a row → 30-min cooldown.
LOSS_COOLDOWN_SEC = int(os.environ.get("QX_LOSS_COOLDOWN_SEC", "1800"))
LOSS_COOLDOWN_THRESHOLD = int(os.environ.get("QX_LOSS_THRESHOLD", "999"))
# Brain analysis runs every N graded signals.
BRAIN_ANALYZE_INTERVAL = int(os.environ.get("QX_BRAIN_ANALYZE_INTERVAL", "50"))
# Days of history used by recompute_from_signal_log.
PATTERN_RECOMPUTE_DAYS = int(os.environ.get("QX_PATTERN_RECOMPUTE_DAYS", "3"))
PAYOUT_REFRESH_SEC = float(os.environ.get("QX_PAYOUT_REFRESH_SEC", "60.0"))
PAYOUT_RETRY_SLEEP = float(os.environ.get("QX_PAYOUT_RETRY_SLEEP", "3.0"))
# Postmortem tag thresholds (multipliers of ATR).
NOISE_CANDLE_ATR_MULT = float(os.environ.get("QX_NOISE_CANDLE_ATR_MULT", "0.40"))
BIG_MOVE_ATR_MULT = float(os.environ.get("QX_BIG_MOVE_ATR_MULT", "0.80"))
# Buyer/seller pressure threshold (default 62%).
BUYER_PCT_THRESHOLD = int(os.environ.get("QX_BUYER_PCT_THRESHOLD", "62"))
DISABLE_LIVE_REEVAL = os.environ.get("QX_DISABLE_LIVE_REEVAL", "1") == "1"
LIVE_REEVAL_MIN_TICKS = int(os.environ.get("QX_LIVE_REEVAL_MIN_TICKS", "15"))
LIVE_REEVAL_INTERVAL_CRITICAL = int(os.environ.get("QX_LIVE_REEVAL_INTERVAL_CRITICAL", "10"))
LIVE_REEVAL_INTERVAL_LAST_10S = int(os.environ.get("QX_LIVE_REEVAL_INTERVAL_LAST_10S", "15"))
LIVE_REEVAL_INTERVAL_LAST_30S = int(os.environ.get("QX_LIVE_REEVAL_INTERVAL_LAST_30S", "30"))
LIVE_REEVAL_INTERVAL_MID = int(os.environ.get("QX_LIVE_REEVAL_INTERVAL_MID", "100"))
# Strength-gate window (last N seconds of candle).
STRENGTH_GATE_LAST_SECS = int(os.environ.get("QX_STRENGTH_GATE_LAST_SECS", "30"))
# _running_confirmation minimum ticks.
RUNCONF_MIN_TICKS = int(os.environ.get("QX_RUNCONF_MIN_TICKS", "5"))
# _apply_strength_gate minimum ticks.
STRENGTH_GATE_MIN_TICKS = int(os.environ.get("QX_STRENGTH_GATE_MIN_TICKS", "10"))

def _api_to_display(api_name: str) -> str:
    """Convert a Quotex forex asset code to a readable display string, e.g."""
    base = api_name[:-4] if api_name.endswith("_otc") else api_name
    if len(base) == 6 and base.isalpha():
        return base[:3] + "/" + base[3:]
    return base
_OTC_SUFFIX_RE = re.compile(r"\s*\(otc\)\s*$", re.IGNORECASE)

def _clean_display(raw_display: str) -> str:
    """Strip Quotex's own "(OTC)" suffix from its raw instrument display"""
    return _OTC_SUFFIX_RE.sub("", raw_display.replace("\n", "")).strip()
_FOREX_OTC = [
    "USDMXN_otc", "USDTRY_otc", "USDPKR_otc", "USDCOP_otc",
    "USDBDT_otc", "USDARS_otc", "USDDZD_otc",
    "USDIDR_otc", "USDBRL_otc", "BRLUSD_otc",
    "INRUSD_otc", "USDINR_otc",
]
# Forex majors — REAL market only (no _otc variant offered).
_FOREX_REAL = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD",
]
_CANONICAL_DISPLAY = {
    "BRLUSD_otc": "USD/BRL",
    "INRUSD_otc": "INR/USD",
    "USDINR_otc": "USD/INR",
}
_FOREX_BASES = set(_FOREX_REAL) | {a[:-4] for a in _FOREX_OTC if a.endswith("_otc")}
_FALLBACK_ASSETS = _FOREX_OTC
_DEFAULT_PAIRS: list[dict] = [
    {"asset": a, "display": _api_to_display(a), "status": "otc",
     "payout": None, "locked": False}
    for a in _FALLBACK_ASSETS
]

def _atr(candles: list[dict], n: int = 20) -> float:
    """True Range ATR — properly accounts for overnight gaps."""
    if not candles:
        return 0.0001
    if len(candles) < 2:
        rng = candles[0]["high"] - candles[0]["low"]
        if rng > 0:
            return rng
        ref = candles[0].get("close", 0) or 1.0
        return ref * 0.0001
    recent = candles[-n:] if len(candles) >= n else candles
    trs = []
    for i in range(1, len(recent)):
        c, prev = recent[i], recent[i - 1]
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - prev["close"]),
            abs(c["low"] - prev["close"]),
        )
        trs.append(tr)
    avg = (sum(trs) / len(trs)) if trs else 0.0
    if avg <= 0:
        # Price-relative fallback for non-forex / flat pairs
        ref = candles[-1]["close"] or 1.0
        return ref * 0.0001
    return avg

def _pred_candle(candles: list[dict], signal: str, period: int, actual_open: float | None = None) -> dict:
    if not candles:
        return None
    last = candles[-1]
    op   = actual_open if actual_open is not None else last["close"]
    atr  = _atr(candles[-20:]) if len(candles) >= 20 else (last["high"] - last["low"]) or 0.0001
    t    = last["time"] + period
    body = atr * 0.45   # ~45% of ATR — typical for a moderately strong candle
    wick = atr * 0.25
    tail = atr * 0.15   # ~15% of ATR — opposite wick (from open)
    if signal == "CALL":
        return {"time":  t, "open":  op,
                "high":  round(op + body + wick, 6),
                "low":   round(op - tail, 6),
                "close": round(op + body, 6)}
    return {"time":  t, "open":  op,
            "high":  round(op + tail, 6),
            "low":   round(op - body - wick, 6),
            "close": round(op - body, 6)}

def _normalise(raw) -> list[dict]:
    """Accept whatever format pyquotex returns, produce sorted OHLC list."""
    if not raw:
        return []
    if isinstance(raw, dict):
        for key in ("candles", "data", "history"):
            if key in raw:
                raw = raw[key]; break
        else:
            raw = list(raw.values())[0]
    seen: dict[int, dict] = {}
    for c in raw:
        try:
            if not all(k in c for k in ("open", "high", "low", "close")):
                continue
            bar = {
                "time":  int(c.get("time",  c.get("from", 0))),
                "open":  float(c["open"]),
                "high":  float(c["high"]),
                "low":   float(c["low"]),
                "close": float(c["close"]),
            }
            if bar["time"] == 0:
                continue
            if bar["high"] < max(bar["open"], bar["close"]) or \
               bar["low"]  > min(bar["open"], bar["close"]):
                continue   # invalid OHLC — skip
            seen[bar["time"]] = bar   # deduplicate: later entry wins
        except (TypeError, ValueError):
            continue
    return sorted(seen.values(), key=lambda x: x["time"])

def _drop_price_contamination(candles: list[dict]) -> list[dict]:
    """Defend against a stale/wrong-asset candle batch getting spliced into a"""
    if len(candles) < 3:
        return candles
    ranges = sorted(c["high"] - c["low"] for c in candles if c["high"] > c["low"])
    if not ranges:
        return candles
    median_rng = ranges[len(ranges) // 2]
    cut = 0
    _contam_mult = float(os.environ.get("QX_CONTAM_JUMP", "10.0"))
    for i in range(1, len(candles)):
        jump = abs(candles[i]["close"] - candles[i - 1]["close"])
        gap  = abs(candles[i]["open"]  - candles[i - 1]["close"])
        if jump > median_rng * _contam_mult or gap > median_rng * _contam_mult:
            cut = i   # keep updating — we want the LAST contamination point
    suffix_cut = len(candles)
    for i in range(len(candles) - 1, 0, -1):
        jump = abs(candles[i]["close"] - candles[i - 1]["close"])
        gap  = abs(candles[i]["open"]  - candles[i - 1]["close"])
        if jump > median_rng * _contam_mult or gap > median_rng * _contam_mult:
            suffix_cut = i
            break
    if cut >= suffix_cut:
        suffix_cut = len(candles)
    if cut:
        print(f"[feed] dropped {cut} contaminated candle(s) "
              f"(price gap > 10x median range) before index {cut}")
    if suffix_cut < len(candles):
        print(f"[feed] dropped {len(candles) - suffix_cut} contaminated candle(s) "
              f"(suffix price gap > 10x median range) from index {suffix_cut}")
    return candles[cut:suffix_cut]

def _floor_to_period(ts: float, period: int) -> int:
    """Floor a Unix timestamp to the start of its candle period."""
    if ts > 10_000_000_000:  # ms mode
        ts = ts / 1000
    return (int(ts) // period) * period

def _ema_simple(prices: list[float], period: int) -> float:
    """Simple EMA calculation for HTF trend detection."""
    if not prices:
        return 0.0
    k = 2 / (period + 1)
    seed_n = min(period, len(prices))
    ema = sum(prices[:seed_n]) / seed_n
    for p in prices[seed_n:]:
        ema = p * k + ema * (1 - k)
    return ema

def _aggregate_5m_closes(candles_1m: list[dict], period: int = 60) -> list[float]:
    """Aggregate 1m candles into 5m closes by timestamp-boundary alignment."""
    if not candles_1m:
        return []
    sample_ts_first = candles_1m[0].get("time", 0)
    sample_ts_last  = candles_1m[-1].get("time", 0)
    ms_mode = (sample_ts_first > 10_000_000_000
               or sample_ts_last  > 10_000_000_000)  # > year 2286 in seconds
    bucket_seconds = 5 * period
    closes_5m: list[float] = []
    current_bucket: int | None = None
    prev_close: float = 0.0
    for c in candles_1m:
        t = c.get("time", 0)
        if ms_mode:
            t = t / 1000
        bucket = (int(t) // bucket_seconds) * bucket_seconds
        if current_bucket is None or bucket != current_bucket:
            if current_bucket is not None:
                closes_5m.append(prev_close)
            current_bucket = bucket
        prev_close = c["close"]
    if current_bucket is not None:
        closes_5m.append(prev_close)
    return closes_5m
@dataclass
class _AssetStream:
    asset: str
    period: int
    candles: list = field(default_factory=list)
    ticks: deque = field(default_factory=lambda: deque(maxlen=2000))
    candle_open_time: int = 0
    candle_open_price: float = 0.0
    candle_open_is_real: bool = False
    last_tick_ts: float = 0.0
    last_real_tick_wall: float = 0.0
    prediction: dict | None = None
    zone_streak: dict = field(
        default_factory=lambda: {"regime": None, "zone": None, "losses": 0})
    payout: int | None = None
    sub_started: bool = False
    task: "asyncio.Task | None" = None
    always_on: bool = False
    interested_cids: set = field(default_factory=set)
    idle_since: float | None = None
    created_at: float = field(default_factory=time.time)
    base_candles: list = field(default_factory=list)
    base_ticks: list = field(default_factory=list)
    _live_reeval_ticks: int = 0   # last tick-count LIVE re-eval fired at
    _option_b_fired: bool = False
    _last_payout_refresh: float = 0.0
    cached_accuracy: tuple = field(default_factory=lambda: (None, 0))
    signal_delay_until: float = 0.0
    _consecutive_losses: int = 0
    _loss_cooldown_until: float = 0.0
    _sub_client_id: int | None = None
    tick_queue: "asyncio.Queue" = field(default_factory=lambda: asyncio.Queue(maxsize=500))
    tick_callback: Callable | None = None
    _micro_cache: dict | None = None
    _micro_cache_at_tick: int = 0  # len(stream.ticks) when cache was built
    _micro_cache_high: float = 0.0
    _micro_cache_low: float = 0.0
    _micro_cache_close: float = 0.0
    _last_bcast_high: float = 0.0
    _last_bcast_low: float = 0.0
    _last_bcast_close: float = 0.0
ZONE_LOSS_GUARD = int(os.environ.get("ZONE_LOSS_GUARD", "999"))

class QuotexFeed:
    def __init__(self):
        self._client              = None
        self._connected           = False
        self._reconnect_attempts  = 0        # for exponential backoff
        self._broadcast           = None     # set once in run()
        self._last_error          = None
        self._last_error_time     = 0        # wall time of last error
        self._consecutive_rejects = 0
        self._token_dead_at       = 0
        self._streams: dict[tuple[str, int], _AssetStream] = {}
        self._stream_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._max_streams     = int(os.environ.get("QX_MAX_STREAMS", "60"))
        self._connected_event = asyncio.Event()
        self._new_stream_gate = asyncio.Semaphore(1)
        self._stagger_gap     = float(os.environ.get("QX_STAGGER_GAP_SEC", "1.5"))
        self._recent_errors: list[float] = []
        self._cooldown_until: float = 0.0
        self._cooldown_reason: str  = ""
        self._pairs_list: list[dict] = list(_DEFAULT_PAIRS)
        self._real_pairs_list: list[dict] = []   # populated by _load_pairs
        self._otc_pairs_list:  list[dict] = list(_DEFAULT_PAIRS)
        self._last_pairs_refresh: float = 0.0
        self._last_db_cleanup: float = 0.0
        self._htf_cache: dict[tuple[str, int], dict] = {}

    async def _get_htf_trend(self, asset: str, stream: '_AssetStream' = None) -> str:
        """Get the 5-minute trend for an asset (Higher Timeframe Confluence)."""
        period = stream.period if stream is not None else 60
        cache_key = (asset, period)
        # Check cache (60s TTL)
        cached = self._htf_cache.get(cache_key)
        if cached and (time.time() - cached["fetched_at"]) < 60:
            return cached.get("trend", "SIDEWAYS")
        candles_1m = stream.candles if stream is not None else []
        if not candles_1m:
            return "SIDEWAYS"
        try:
            window = candles_1m[-105:]
            closes_5m = _aggregate_5m_closes(window, period)
            n5 = len(closes_5m)
            if n5 < 9:
                return "SIDEWAYS"
            ema9  = _ema_simple(closes_5m, 9)
            ema21 = _ema_simple(closes_5m, 21)
            sep = abs(ema9 - ema21) / ema21 if ema21 > 0 else 0
            if n5 >= 21:
                thresh = 0.0003
            elif n5 >= 14:
                thresh = 0.0005
            else:  # 9 <= n5 < 14
                thresh = 0.0008
            if ema9 > ema21 and sep > thresh:
                trend = "UPTREND"
            elif ema9 < ema21 and sep > thresh:
                trend = "DOWNTREND"
            else:
                trend = "SIDEWAYS"
            self._htf_cache[cache_key] = {
                "trend": trend,
                "fetched_at": time.time(),
                "ema9": ema9,
                "ema21": ema21,
                "n_5m_closes": n5,
                "threshold": thresh,
            }
            return trend
        except Exception as exc:
            print(f"[feed] HTF trend fetch failed for {asset} (period={period}): {exc}")
            return "SIDEWAYS"

    def available_pairs(self) -> dict:
        """Return the current forex pair lists and payout floors for /api/pairs."""
        active_real = [p for p in self._real_pairs_list if p["status"] == "live"]
        active_otc  = [p for p in self._otc_pairs_list  if p["status"] == "otc"]
        return {
            "real_pairs": active_real,
            "otc_pairs":  active_otc,
            "payout_floor_real": PAYOUT_FLOOR_REAL,
            "payout_floor_otc":  PAYOUT_FLOOR_OTC,
            # Backward compat: combined list (real + otc)
            "pairs":        active_real + active_otc,
            "payout_floor": PAYOUT_FLOOR_OTC,
        }

    async def _load_pairs(self, broadcast=None) -> None:
        """Fetch all Quotex instruments and build TWO separate forex pair lists:"""
        try:
            instruments = await self._client.get_instruments()
            if not instruments:
                return
            _OPEN_IDX   = 14   # bool: instrument is open for trading
            _PAYOUT_IDX = -9   # 1-minute payout %, same field pyquotex's
            by_base: dict[str, dict] = {}
            for i in instruments:
                name   = i[1]
                is_otc = name.endswith("_otc")
                base   = name[:-4] if is_otc else name
                if base not in _FOREX_BASES:
                    continue
                is_open = bool(i[_OPEN_IDX])
                payout  = i[_PAYOUT_IDX]
                try:              # own get_payout_by_asset()/get_payment() read
                    payout = int(payout) if payout is not None else None
                except (TypeError, ValueError):
                    payout = None
                if base not in by_base:
                    by_base[base] = {}
                key = "otc" if is_otc else "real"
                by_base[base][key] = {
                    "asset":   name,
                    "display": _clean_display(i[2]) or _api_to_display(name),
                    "open":    is_open,
                    "payout":  payout,
                }
            real_pairs: list[dict] = []
            otc_pairs:  list[dict] = []
            for base, v in by_base.items():
                real = v.get("real")
                otc  = v.get("otc")
                _REAL_ONLY_BASES = set(_FOREX_REAL)  # EURUSD, GBPUSD, ...
                _OTC_ONLY_BASES  = {a[:-4] for a in _FOREX_OTC if a.endswith("_otc")
                                    and a[:-4] not in _REAL_ONLY_BASES}
                if real and base in _REAL_ONLY_BASES:
                    status = "live" if real["open"] else "closed"
                    floor = PAYOUT_FLOOR_REAL
                    payout = real["payout"]
                    locked = (status == "live"
                              and (payout is None or payout < floor)) \
                            or status == "closed"
                    real_pairs.append({
                        "asset":   real["asset"],
                        "display": real["display"],
                        "status":  status,
                        "payout":  payout,
                        "locked":  locked,
                        "category": "real",
                    })
                if otc and base in _OTC_ONLY_BASES:
                    status = "otc"
                    floor = PAYOUT_FLOOR_OTC
                    payout = otc["payout"]
                    locked = False  # OTC pairs bypass payout floor lock
                    otc_pairs.append({
                        "asset":   otc["asset"],
                        "display": otc["display"],
                        "status":  status,
                        "payout":  payout,
                        "locked":  locked,
                        "category": "otc",
                    })

            def _sort_key(x):
                return (x["status"] == "closed", x["locked"],
                        -(x["payout"] or 0), x["display"].upper())
            real_pairs.sort(key=_sort_key)
            otc_pairs.sort(key=_sort_key)
            self._real_pairs_list = real_pairs
            self._otc_pairs_list  = otc_pairs
            for p in otc_pairs:
                if p["asset"] in _CANONICAL_DISPLAY:
                    p["display"] = _CANONICAL_DISPLAY[p["asset"]]
            for p in real_pairs:
                real_key = p["asset"]
                otc_key = real_key + "_otc"
                if otc_key in _CANONICAL_DISPLAY:
                    p["display"] = _CANONICAL_DISPLAY[otc_key]
            self._pairs_list = real_pairs + otc_pairs
            self._last_pairs_refresh = time.time()
            print(f"[feed] pairs loaded: "
                  f"{len(real_pairs)} real ({sum(1 for p in real_pairs if p['status']=='live')} live, "
                  f"{sum(1 for p in real_pairs if p['locked'])} locked <{PAYOUT_FLOOR_REAL}%) | "
                  f"{len(otc_pairs)} OTC ({sum(1 for p in otc_pairs if p['status']=='otc')} open, "
                  f"{sum(1 for p in otc_pairs if p['locked'])} locked <{PAYOUT_FLOOR_OTC}%) | "
                  f"all OTC pairs are always-on (no payout floor)")
            if broadcast:
                await broadcast({
                    "type": "pairs",
                    "pairs":  self._pairs_list,            # backward compat
                    "real_pairs": real_pairs,
                    "otc_pairs":  otc_pairs,
                    "payout_floor_real": PAYOUT_FLOOR_REAL,
                    "payout_floor_otc":  PAYOUT_FLOOR_OTC,
                    "payout_floor": PAYOUT_FLOOR_OTC,      # backward compat
                })
        except Exception as exc:
            print(f"[feed] pairs load error: {exc}")

    def snapshot(self, asset: str, period: int) -> dict | None:
        """Return a recent-candles + prediction snapshot for an active (asset, period) stream."""
        stream = self._streams.get((asset, period))
        if not stream or not stream.candles:
            return None
        return {
            "type":       "snapshot",
            "asset":      stream.asset,
            "period":     stream.period,
            "candles":    stream.candles[-SNAPSHOT_CANDLES:],
            "prediction": stream.prediction,
        }

    async def ensure_stream(self, asset: str, period: int,
                            cid: str | None = None) -> dict:
        """Called from /api/subscribe. Starts a stream for (asset, period) if one"""
        _qx_token = os.environ.get("QX_TOKEN", "").strip()
        _qx_email = os.environ.get("QX_EMAIL", "").strip()
        if not _qx_token and not _qx_email:
            err = ("no Quotex credentials — sim mode is disabled. "
                   "Set QX_TOKEN via Railway Variables or visit "
                   "/api/set-token?token=YOUR_TOKEN to provision at runtime.")
            print(f"[feed] ❌ ensure_stream({asset}@{period}s): {err}")
            return {
                "ok": False,
                "status": "no_credentials",
                "error": err,
                "action": "set_token",
            }
        key = (asset, period)
        if key not in self._stream_locks:
            self._stream_locks[key] = asyncio.Lock()
        async with self._stream_locks[key]:
            stream = self._streams.get(key)
            if stream is not None:
                if cid:
                    stream.interested_cids.add(cid)
                    for k, s in list(self._streams.items()):
                        if k != key:
                            s.interested_cids.discard(cid)
                stream.idle_since = None
                gated_prediction = stream.prediction
                if (stream.signal_delay_until > 0
                        and time.time() < stream.signal_delay_until):
                    gated_prediction = None
                return {"type": "snapshot", "ok": True, "status": "streaming",
                        "asset": asset, "period": period,
                        "candles": stream.candles[-SNAPSHOT_CANDLES:], "prediction": gated_prediction}
            pair = next((p for p in self._pairs_list if p["asset"] == asset), None)
            is_otc_asset = asset.endswith("_otc")
            if pair and pair.get("locked") and not is_otc_asset:
                floor = _payout_floor_for(asset)
                return {"ok": False, "status": "locked", "payout": pair.get("payout"),
                        "reason": f"Needs {floor}% payout "
                                  f"(currently {pair.get('payout', '?')}%)"}
            if time.time() < self._cooldown_until:
                return {"ok": False, "status": "cooldown",
                        "retry_after": round(self._cooldown_until - time.time(), 1),
                        "reason": self._cooldown_reason}
            _user_stream_count = sum(
                1 for s in self._streams.values() if not s.always_on)
            if _user_stream_count >= self._max_streams:
                return {"ok": False, "status": "at_capacity", "max": self._max_streams}
            stream = _AssetStream(asset=asset, period=period)
            if cid:
                stream.interested_cids.add(cid)
            self._streams[key] = stream
            stream.task = asyncio.create_task(self._run_stream(stream))
        if self._connected:
            asyncio.create_task(self._warn_if_stuck(asset, period, stream))
        return {"ok": True, "status": "starting"}

    async def _warn_if_stuck(self, asset: str, period: int,
                              stream: '_AssetStream') -> None:
        """Log + broadcast a clear warning if a stream stays stuck."""
        try:
            await asyncio.sleep(30)
            last_live = getattr(stream, 'last_real_tick_wall', 0.0)
            if last_live > 0 and (time.time() - last_live) < 30:
                return  # live ticks arrived recently, all good
            if not stream.candles and not stream.ticks:
                pass  # nothing loaded at all — warn below
            elif last_live == 0:
                pass  # history loaded but no live ticks — warn below
            else:
                return  # have live ticks — all good
            err = (f"stream {asset}@{period}s stuck (no live ticks after 30s) "
                   "— likely a silent subscription drop by Quotex server "
                   "(NOT necessarily token expiry). Auto-re-arming subscription. "
                   "If this persists across multiple pairs, refresh token via /api/set-token.")
            print(f"[feed] ⚠️  {err}")
            self._last_error = err
            self._last_error_time = time.time()
            if self._broadcast:
                try:
                    await self._broadcast({
                        "type": "error",
                        "asset": asset,
                        "period": period,
                        "error": "stuck_stream_no_sim_fallback",
                        "message": err,
                        "action": "refresh_token",
                    })
                except Exception as _be:
                    print(f"[feed] _warn_if_stuck broadcast failed: {_be}")
            try:
                if self._client and getattr(stream, 'sub_started', False):
                    print(f"[feed] _warn_if_stuck: auto re-arming {asset}@{period}s")
                    asyncio.create_task(self._rearm_stream(stream))
                    stream.last_real_tick_wall = time.time()
            except Exception as _re:
                print(f"[feed] _warn_if_stuck re-arm failed: {_re}")
        except asyncio.CancelledError as _e:
            print(f"[silent-except] feed.py:1378 {type(_e).__name__}: {_e}")
            pass
        except Exception as exc:
            print(f"[feed] _warn_if_stuck error: {exc}")

    async def drop_interest(self, cid: str) -> None:
        """A viewer disconnected — stop counting it toward any stream's"""
        for s in list(self._streams.values()):
            s.interested_cids.discard(cid)

    async def _aggressive_reconnect(self) -> None:
        """FIX (RECONNECT-2026-07-23): aggressive auto-reconnect."""
        try:
            while True:
                await asyncio.sleep(60)
                _all_stale = False
                if self._streams:
                    now = time.time()
                    _stale_count = sum(
                        1 for s in self._streams.values()
                        if s.last_real_tick_wall > 0
                        and (now - s.last_real_tick_wall) > 120
                    )
                    _all_stale = _stale_count >= len(self._streams)
                if (not self._streams or _all_stale) and not getattr(self, '_abandoned', False):
                    if _all_stale:
                        print(f"[feed] aggressive_reconnect: all {_stale_count} streams "
                              f"stale (no ticks >120s) — triggering reconnect")
                    else:
                        print("[feed] aggressive_reconnect: 0 streams - triggering reconnect")
                    self._connected = False
                    self._connected_event.clear()
                    if self._reconnect_attempts > 5:
                        self._reconnect_attempts = 5
                    try:
                        existing_mgr = getattr(self, '_manager_task', None)
                        if (existing_mgr is None or existing_mgr.done()) and self._broadcast is not None:
                            print("[feed] aggressive_reconnect: manager task dead — restarting")
                            self._manager_task = asyncio.create_task(
                                self.run(self._broadcast))
                            self._reconnect_attempts = 0  # fresh start
                    except Exception as _re:
                        print(f"[feed] aggressive_reconnect: manager restart failed: {_re}")
                    continue
                # If abandoned, try to clear and retry
                if getattr(self, '_abandoned', False):
                    print("[feed] aggressive_reconnect: feed abandoned - retrying real connection")
                    self._abandoned = False
                    self._connected = False
                    self._connected_event.clear()
                    os.environ["USE_SIM"] = "0"
                    self._reconnect_attempts = 0
                    try:
                        existing = getattr(self, '_manager_task', None)
                        if existing is None or existing.done():
                            if self._broadcast is not None:
                                self._manager_task = asyncio.create_task(
                                    self.run(self._broadcast))
                                print("[feed] aggressive_reconnect: restarted manager task")
                            else:
                                print("[feed] aggressive_reconnect: cannot restart "
                                      "manager — no broadcast fn set yet")
                    except Exception as _re:
                        print(f"[feed] aggressive_reconnect: manager restart "
                              f"failed: {_re}")
        except asyncio.CancelledError as _e:
            print(f"[silent-except] feed.py:1471 {type(_e).__name__}: {_e}")
            pass
        except Exception as exc:
            print(f"[feed] aggressive_reconnect error: {exc}")

    def stream_status(self) -> dict:
        """Return active stream count and capacity info for the status endpoint."""
        now = time.time()
        # Start with our own streams
        all_streams = list(self._streams.values())
        return {
            "active": [{"asset": s.asset, "period": s.period,
                        "viewers": len(s.interested_cids),
                        "age_sec": round(now - s.created_at)}
                       for s in all_streams],
            "count": len(all_streams),
            "max":   self._max_streams,
            "cooldown_until":  self._cooldown_until if self._cooldown_until > now else None,
            "cooldown_reason": self._cooldown_reason if self._cooldown_until > now else None,
            "sim_mode": False,  # sim mode permanently disabled
        }

    async def shutdown(self) -> None:
        for s in list(self._streams.values()):
            if s.task:
                s.task.cancel()
        _rt = getattr(self, '_reconnect_task', None)
        if _rt is not None and not _rt.done():
            _rt.cancel()

    def _remember_token(self) -> None:
        """Cache the latest working SSID so reconnects reuse it (no manual token)."""
        try:
            tok = (self._client.session_data or {}).get("token")
            if tok:
                os.environ["QX_TOKEN"] = tok
        except Exception as _e:
            print(f"[silent-except] feed.py:1521 {type(_e).__name__}: {_e}")
            pass

    def _clear_stale_token(self) -> None:
        """Auto-heal the "authorization/reject" loop (documented project issue):"""
        import json as _json
        _root = os.environ.get("QX_ROOT", "")
        candidates = []
        if _root:
            candidates.append(os.path.join(_root, "session.json"))
        candidates.append(os.path.join(os.getcwd(), "session.json"))
        cleared_any = False
        last_err = None
        for path in candidates:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                changed = False
                for acct in data.values():
                    if isinstance(acct, dict) and acct.get("token"):
                        acct["token"] = None
                        changed = True
                if changed:
                    _tmp = path + ".tmp"
                    with open(_tmp, "w", encoding="utf-8") as f:
                        _json.dump(data, f)
                    os.replace(_tmp, path)
                    print(f"[feed] cleared stale session token at {path} "
                          f"after auth rejection — next retry will do a fresh login")
                    cleared_any = True
                    break  # success — don't try other paths
            except FileNotFoundError:
                continue
            except Exception as _e:
                last_err = _e
                continue
        if not cleared_any and last_err is not None:
            print(f"[feed] could not clear stale token (all paths failed): {last_err}")

    def _make_client(self, ua: str, root: str):
        """Build a Quotex client."""
        if os.environ.get("QX_USE_RAW_WS", "0") == "1":
            from quotex_ws import QuotexWSClient
            print("[feed] using RAW WebSocket backend (quotex_ws.QuotexWSClient)")
            _host = os.environ.get("QX_HOST", "market-qx.trade")
            return QuotexWSClient(
                email    = os.environ.get("QX_EMAIL",    ""),
                password = os.environ.get("QX_PASSWORD", ""),
                host     = _host,
                lang     = "en",
                root_path= root,
            )
        from pyquotex.stable_api import Quotex
        from pyquotex.types import ReconnectPolicy
        from pyquotex.network.login import Login
        _host = os.environ.get("QX_HOST", "market-qx.trade")
        Login.base_url = _host
        Login.https_base_url = f"https://{_host}"
        ua_src = "env QX_UA" if os.environ.get("QX_UA", "").strip() else "default Firefox"
        print(f"[feed] using vendored pyquotex (Firefox TLS — Cloudflare bypass, UA: {ua_src})")
        return Quotex(
            email    = os.environ.get("QX_EMAIL",    ""),
            password = os.environ.get("QX_PASSWORD", ""),
            host     = _host,
            lang     = "en",
            root_path= root,
            reconnect_policy=ReconnectPolicy(
                enabled=True, max_attempts=0,
                base_delay=2.0, max_delay=30.0, stale_timeout=45.0),
        )
    @staticmethod
    async def _close_client(client) -> None:
        """Best-effort close — pyquotex versions vary on the API."""
        for meth in ("close", "disconnect", "close_connect"):
            fn = getattr(client, meth, None)
            if callable(fn):
                try:
                    result = fn()
                    if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                        await asyncio.wait_for(result, timeout=3)
                    return   # success — stop trying alternatives
                except Exception as _e:
                    print(f"[feed] _close_client {meth}() failed: "
                          f"{type(_e).__name__}: {_e}")
                    continue   # this method failed — try the next one

    async def _connect(self) -> bool:
        """Connect to Quotex using ONLY the QX_TOKEN environment variable."""
        try:
            _USE_RAW_WS = os.environ.get("QX_USE_RAW_WS", "0") == "1"
            if not _USE_RAW_WS:
                from pyquotex.types import ReconnectPolicy
            import tempfile
            root = os.environ.get(
                "QX_ROOT", os.path.join(tempfile.gettempdir(), "plybit_cache")
            )
            ua = os.environ.get("QX_UA", "").strip() or (
                "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) "
                "Gecko/20100101 Firefox/119.0")
            env_token = os.environ.get("QX_TOKEN", "").strip()
            if not env_token:
                now = time.time()
                last_warn = getattr(self, "_last_no_token_warn", 0)
                if now - last_warn > 300:
                    print("[feed] ⚠️  QX_TOKEN not set — please push a token "
                          "via Railway Variables or POST /api/set-token "
                          "{\"token\":\"...\"}. Email/password login is "
                          "disabled (Cloudflare blocks Railway IPs).")
                    self._last_no_token_warn = now
                return False
            self._client = self._make_client(ua, root)
            self._client.set_session(user_agent=ua, ssid=env_token)
            print(f"[feed] connecting with session token=…{env_token[-4:]}")
            ok = False
            try:
                ok, reason = await asyncio.wait_for(
                    self._client.connect(), timeout=30)
                if ok:
                    self._remember_token()
                    print(f"[feed] connect -> ok=True  reason={reason}")
                    was_dead = bool(self._token_dead_at)
                    self._consecutive_rejects = 0
                    self._token_dead_at = 0
                    if was_dead:
                        _alerts.feed_recovered()
                    return True
                print(f"[feed] connect -> ok=False  reason={reason}")
                if reason and "rejected by quotex" in str(reason).lower():
                    self._consecutive_rejects += 1
                    if self._consecutive_rejects >= 3 and not self._token_dead_at:
                        self._token_dead_at = time.time()
                        print(f"[feed] ⛔ token marked DEAD after "
                              f"{self._consecutive_rejects} consecutive rejects")
                        _alerts.token_dead(self._consecutive_rejects)
            except Exception as _te:
                print(f"[feed] token attempt error: {_te}")
                ok = False
            finally:
                if not ok:
                    await self._close_client(self._client)
                    self._client = None
            return False
        except Exception as exc:
            err_msg = f"connect error: {exc}"
            print(f"[feed] {err_msg}")
            self._last_error = err_msg[:500]
            self._last_error_time = time.time()
            return False

    async def _load_history(self, asset: str, period: int) -> list[dict]:
        """Fetch candle history with adaptive candle count per timeframe."""
        # How many candles to target per timeframe
        if period <= 60:
            target = 200
        elif period <= 300:
            target = 150
        else:
            target = 100
        window = target * period
        try:
            raw = await asyncio.wait_for(
                self._client.get_historical_candles(
                    asset,
                    amount_of_seconds = window,
                    period            = period,
                    max_workers       = 1,
                ),
                timeout = 15.0,
            )
            candles = _normalise(raw)
            if candles:
                result = _drop_price_contamination(candles[-target:])
                print(f"[feed] history: {len(result)} candles for {asset}@{period}s")
                return result
        except asyncio.TimeoutError:
            print(f"[feed] history timeout (batch) for {asset}@{period}s")
        except Exception as exc:
            print(f"[feed] history batch error: {exc}")
        # Strategy 2: single get_candles fallback
        try:
            raw = await asyncio.wait_for(
                self._client.get_candles(
                    asset,
                    end_from_time = None,
                    offset        = window,
                    period        = period,
                ),
                timeout = 10.0,
            )
            candles = _normalise(raw)
            if candles:
                result = _drop_price_contamination(candles[-target:])
                print(f"[feed] history (single): {len(result)} candles for {asset}@{period}s")
                return result
        except asyncio.TimeoutError:
            print(f"[feed] history timeout (single) for {asset}@{period}s")
        except Exception as exc:
            print(f"[feed] history single error: {exc}")
        print(f"[feed] history FAILED for {asset}@{period}s")
        return []

    async def _analyze_core(self, asset: str, period: int, candles: list[dict],
                      ticks: list[float],
                      running_ticks: list[float] | None = None,
                      stream: _AssetStream | None = None,
                      live_only: bool = False
                      ) -> tuple[dict | None, list]:
        """Shared EOC analysis — runs candle_reaction.predict_from_candle on"""
        if len(candles) < 5:
            return None, []
        if live_only:
            micro_hist = []
        else:
            micro_hist = await asyncio.to_thread(
                _db.get_micro_history,
                asset, period, 5, candles[-1]["time"])
        htf_trend = "SIDEWAYS"
        try:
            htf_trend = await self._get_htf_trend(asset, stream=stream)
        except Exception as _e:
            print(f"[feed] HTF fetch failed for {asset}: {_e}")
        from engines import predict as predict_from_candle
        from core.microstructure import build_micro as _build_micro_for_pred
        _micro_for_pred = None
        if ticks and len(ticks) >= 10:
            _micro_for_pred = _build_micro_for_pred(
                list(ticks), candles[-1]["open"] if candles else ticks[0])
        _period_for_pred = stream.period if stream is not None else period
        _recent_acc = getattr(stream, 'cached_accuracy', None) if stream is not None else None
        result = await asyncio.to_thread(
            predict_from_candle, list(candles),
            ticks=list(ticks) if ticks else [],
            micro=_micro_for_pred, asset=asset,
            htf_trend=htf_trend, period=_period_for_pred,
            recent_accuracy=_recent_acc)
        return result, micro_hist

    async def _run_eoc(self, stream: _AssetStream,
                actual_open: float | None = None) -> dict | None:
        closed = list(stream.candles)
        base_ticks = list(stream.ticks)
        try:
            cooldown_until = getattr(stream, '_loss_cooldown_until', 0)
            if cooldown_until and time.time() < cooldown_until:
                import math as _math
                remaining = max(0, int(_math.ceil((cooldown_until - time.time()) / 60)))
                print(f"[feed] {stream.asset} in loss cooldown ({remaining} min remaining) — skipping prediction")
                stream.prediction = None
                return None
        except Exception as _e:
            print(f"[silent-except] feed.py:1975 {type(_e).__name__}: {_e}")
            pass  # never let cooldown check break the feed
        try:
            _acc_n = RECENT_ACCURACY_N
        except (TypeError, ValueError):
            _acc_n = 50
        _acc_n = max(8, min(_acc_n, 200))
        try:
            stream.cached_accuracy = await asyncio.to_thread(
                _db.recent_accuracy, stream.asset, stream.period, n=_acc_n)
        except Exception as _e:
            print(f"[feed] recent_accuracy DB query failed for "
                  f"{stream.asset}@{stream.period}s: {_e}")
            stream.cached_accuracy = (None, 0)
        result, micro_hist = await self._analyze_core(
            stream.asset, stream.period, closed, base_ticks,
            running_ticks=None, stream=stream)
        if result is None:
            return None
        stream.base_candles = [dict(c) for c in closed]
        stream.base_ticks   = base_ticks
        stream._live_reeval_ticks = 0

        # ── SIGNAL VERIFIER AGENT (PROD-2026-08-06) ──────────────────────
        # Real-time multi-layer verification on actual candle + tick data.
        # Sits between engine prediction and broadcast. Issues VETO/WEAKEN/
        # CONFIRM verdict that can modify or kill the signal.
        # Env toggle: QX_SIGNAL_VERIFIER=1 enables (default OFF for safety).
        if os.environ.get("QX_SIGNAL_VERIFIER", "0") == "1" and \
                result.get("signal") in ("CALL", "PUT"):
            try:
                from core.signal_verifier import verify_signal
                from datetime import datetime, timezone
                _verify_hour = datetime.now(timezone.utc).hour
                _tick_prices = [t for t in base_ticks if isinstance(t, (int, float))]
                if hasattr(_tick_prices, '__len__') and len(_tick_prices) > 0 and \
                        isinstance(_tick_prices[0], dict):
                    _tick_prices = [t.get('price', t.get('close', 0))
                                    for t in _tick_prices]
                _verify = verify_signal(result, closed, _tick_prices,
                                        stream.asset, _verify_hour)
                _verdict = _verify.get("verdict", "PASS")
                _adj = _verify.get("confidence_adjustment", 1.0)
                if _verdict == "VETO":
                    result["signal"] = "NEUTRAL"
                    result["strength"] = "NEUTRAL"
                    result["confidence"] = 0
                    result.setdefault("reasons", []).append(
                        f"_VERIFIER_VETO: {_verify.get('reason', '')[:200]}")
                elif _verdict == "WEAKEN":
                    _orig_conf = result.get("confidence", 0)
                    result["confidence"] = int(_orig_conf * _adj)
                    result.setdefault("reasons", []).append(
                        f"_VERIFIER_WEAKEN: {_verify.get('reason', '')[:200]} "
                        f"(conf {_orig_conf} -> {result['confidence']})")
                elif _verdict == "CONFIRM":
                    _orig_conf = result.get("confidence", 0)
                    result["confidence"] = min(100, int(_orig_conf * _adj))
                    result.setdefault("reasons", []).append(
                        f"_VERIFIER_CONFIRM: {_verify.get('reason', '')[:200]}")
            except Exception as _ve:
                print(f"[feed] signal_verifier error for {stream.asset}: {_ve}")

        _reg = result.get("regime") or {}
        _regime = _reg.get("regime")
        if _reg.get("is_volatile"):
            _zone = "VOLATILE"
        elif _reg.get("is_trending"):
            _zone = "TREND"
        elif _reg.get("is_ranging"):
            _zone = "RANGE"
        else:
            _zone = "UNKNOWN"
        _key = (_regime, _zone)
        if (result["signal"] != "NEUTRAL"
                and _key == (stream.zone_streak["regime"], stream.zone_streak["zone"])
                and stream.zone_streak["losses"] >= ZONE_LOSS_GUARD):
            _losses = stream.zone_streak['losses']
            result["signal"] = "NEUTRAL"
            result["strength"] = "NEUTRAL"
            result["confidence"] = 0
            result.setdefault("reasons", []).append(
                f"CHOP GUARD (BACKTEST-FIX): {_key[0]}/{_key[1]} wrong "
                f"{_losses}x running → NEUTRAL (skip). "
                f"Backtest: WEAK signals won 4.2% — skipping is +EV.")
        if False and result.get("signal") in ("CALL", "PUT") and result.get("strength") == "WEAK":
            _weak_conf = result.get("confidence", 0)
            _orig_signal = result.get("signal")
            result["signal"] = "NEUTRAL"
            result["strength"] = "NEUTRAL"
            result["confidence"] = 0
            result.setdefault("reasons", []).append(
                f"WEAK→NEUTRAL (Option A): backtest showed 4.2% win rate "
                f"(confidence was {_weak_conf}) — skip is +EV. "
                f"opposed original {_orig_signal}")
        if result["signal"] == "NEUTRAL":
            return {**result, "candle": None, "payout": stream.payout}
        return {**result, "candle": _pred_candle(closed, result["signal"], stream.period, actual_open),
                "payout": stream.payout}

    def _accuracy(self, just_closed: dict, pred: dict | None,
                  period: int = 60) -> str | None:
        if not pred:
            return None
        pred_signal = pred.get("signal")
        if pred_signal not in ("CALL", "PUT"):
            return None
        if just_closed["close"] == just_closed["open"]:
            if (just_closed.get("high") == just_closed.get("low")
                    == just_closed["open"] == just_closed["close"]):
                return "skip"  # data drop — don't log
            return "draw"  # genuine doji (range > 0 but close == open)
        if abs(just_closed["close"] - just_closed["open"]) < 1e-9:
            if (just_closed.get("high") == just_closed.get("low")
                    == just_closed["open"] == just_closed["close"]):
                return "skip"  # data drop — don't log
            return "draw"  # genuine doji (range > 0 but close == open)
        actual_up = just_closed["close"] > just_closed["open"]
        pred_up   = pred_signal == "CALL"
        return "correct" if actual_up == pred_up else "wrong"

    def _grade_and_log(self, asset: str, period: int, closed: dict,
                       prediction: dict | None, micro_snap: dict | None,
                       candles: list[dict]) -> str | None:
        """Grade `closed` against the prediction that was made FOR it and write"""
        accuracy = self._accuracy(closed, prediction, period=period)
        if accuracy == "skip":
            return None  # don't log data-drop candles
        if not prediction:
            return accuracy
        pred_signal = prediction.get("signal")
        if pred_signal == "NEUTRAL" or pred_signal is None:
            reasons = prediction.get("reasons", [])
            reasons_text = " ".join(str(r) for r in reasons)
            import re as _re
            m = _re.search(r'opposed original (CALL|PUT)', reasons_text)
            if m:
                orig_signal = m.group(1)
                _rec_pred = {"signal": orig_signal}
                accuracy = self._accuracy(closed, _rec_pred, period=period)
                if accuracy == "skip":
                    accuracy = "draw"
                prediction = dict(prediction)
                prediction["signal"] = orig_signal
                prediction["_was_weak_neutral"] = True
                if prediction.get("confidence", 0) == 0:
                    prediction["confidence"] = 15
                    prediction["_confidence_recovered"] = True
                    prediction.setdefault("reasons", []).append(
                        "_RECOVERED_CONFIDENCE: 0→15 (WEAK→NEUTRAL recovery)")
                if prediction.get("strength") == "NEUTRAL":
                    prediction["strength"] = "WEAK"
            else:
                return accuracy
        try:
            import json as _json
            reasons   = prediction.get("reasons", [])
            is_draw   = closed["close"] == closed["open"]
            actual_up = closed["close"] > closed["open"]
            if not accuracy:
                return accuracy
            move  = closed["close"] - closed["open"]
            c_rng = closed["high"] - closed["low"]
            _hist = candles[-11:-1]
            atr   = (_atr(_hist) if _hist else c_rng)
            _reg  = (prediction.get("regime") or {})
            regime = _reg.get("regime")
            if _reg.get("is_volatile"):
                zone = "VOLATILE"
            elif _reg.get("is_trending"):
                zone = "TREND"
            elif _reg.get("is_ranging"):
                zone = "RANGE"
            else:
                zone = "UNKNOWN"
            sig   = prediction["signal"]
            tags = []
            if is_draw:
                tags.append("DRAW")              # zero move = broker refund
            if atr > 0 and c_rng < atr * NOISE_CANDLE_ATR_MULT:
                tags.append("NOISE_CANDLE")      # sub-noise range: coin flip
            if atr > 0 and abs(move) >= atr * BIG_MOVE_ATR_MULT:
                tags.append("BIG_MOVE")
            if regime in ("TREND_UP", "TREND_DOWN"):
                if ((regime == "TREND_UP" and sig == "PUT") or
                        (regime == "TREND_DOWN" and sig == "CALL")):
                    tags.append("COUNTER_REGIME")
                elif ((regime == "TREND_UP" and sig == "CALL") or
                        (regime == "TREND_DOWN" and sig == "PUT")):
                    tags.append("WITH_REGIME")
            if micro_snap and micro_snap.get("last_react") == "EXHAUST":
                tags.append("LATE_FLIP")         # candle flipped at the close
            _atr_note = (f" ({abs(move) / atr * 100:.0f}% of ATR)"
                         if atr > 0 else "")
            _actual_lbl = ("FLAT" if is_draw
                           else "UP" if actual_up else "DOWN")
            pm = (
                f"{sig} s={prediction.get('score', 0):+d}"
                f" {prediction.get('strength')}"
                f" agree={prediction.get('agree')}"
                f" | actual {_actual_lbl}"
                f" move={move:+.5f}{_atr_note}"
                f" | {accuracy.upper()}"
                f" | regime {regime}/{zone}"
                f"{' | ' + ','.join(tags) if tags else ''}"
            )
            if sig in ("CALL", "PUT"):
                _db.log_signal(
                    asset, period, closed["time"],
                    sig, prediction.get("score", 0),
                    prediction.get("confidence", 0), "",
                    _actual_lbl, accuracy,
                    strength=prediction.get("strength"),
                    agree=prediction.get("agree"),
                    reasons=_json.dumps(reasons),
                    a_open=closed["open"], a_close=closed["close"],
                    regime=regime, zone=zone,
                    tags=",".join(tags), postmortem=pm,
                    signal_quality=prediction.get("signal_quality"),
                )
        except Exception as _e:
            print(f"[db] log_signal error: {_e}")
        return accuracy

    def _save_micro(self, asset: str, period: int, closed: dict,
                    micro_snap: dict, candles: list[dict],
                    ticks: list[float]) -> None:
        """Persist a closed candle's microstructure + gap classification + key"""
        try:
            _gap_pct  = 0.0
            _gap_type = "NONE"
            if len(candles) >= 2:
                _pc = candles[-2]["close"]
                if _pc > 0:
                    _raw_gap = closed["open"] - _pc
                    _gp      = _raw_gap / _pc          # signed %
                    if abs(_gp) >= 0.0001:             # ≥ 0.01% threshold
                        _gap_pct  = _gp
                        _gap_up   = _gp > 0
                        _is_bull_c = closed["close"] >= closed["open"]
                        _w_fill = ((_gap_up and closed["low"]  <= _pc) or
                                   (not _gap_up and closed["high"] >= _pc))
                        _b_fill = ((_gap_up and closed["close"] <= _pc) or
                                   (not _gap_up and closed["close"] >= _pc))
                        if _b_fill:
                            _gap_type = "FILLED"
                        elif _w_fill:
                            _gap_type = ("REJECTED"
                                         if _gap_up == _is_bull_c
                                         else "WICK_FILL")
                        elif _gap_up == _is_bull_c:
                            _gap_type = "PURE"
                        else:
                            _gap_type = "FLIP"
            micro_snap["gap_pct"]   = _gap_pct
            micro_snap["gap_type"]  = _gap_type
            micro_snap["key_levels"] = _key_levels(candles)
            import json as _tick_json
            _tl = list(ticks)
            if len(_tl) > 240:
                _st = len(_tl) / 240
                _tl = [_tl[min(len(_tl) - 1, int(i * _st))] for i in range(240)]
            micro_snap["ticks_json"] = _tick_json.dumps(
                [round(x, 6) for x in _tl])
            _db.save(asset, period, closed, micro_snap)
        except Exception as _me:
            print(f"[db] micro save error: {_me}")

    def _analyze_microstructure(self, ticks: list[float],
                                open_price: float) -> dict | None:
        """Real-time tick microstructure analysis of the running candle."""
        ticks = list(ticks)
        if len(ticks) < 3:
            return None
        if len(ticks) < 10:
            cur = ticks[-1]
            op = open_price
            return {
                "buy_pct": 50, "sell_pct": 50, "pressure": "FIGHT",
                "is_fight": False, "crosses": 0,
                "hold_price": cur, "hold_visits": len(ticks),
                "phases": ["FLAT"], "reaction": None,
                "net": round(cur - op, 6), "tick_count": len(ticks),
                "last_react": None, "round": {},
                "ending_direction": {"direction": "FLAT", "buy_pct": 50,
                                    "dominance": "FIGHT", "move": 0, "tick_count": len(ticks)},
            }
        op  = open_price
        hi  = max(ticks)
        lo  = min(ticks)
        cur = ticks[-1]
        rng = hi - lo
        up_t = sum(1 for i in range(1, len(ticks)) if ticks[i] > ticks[i - 1])
        dn_t = sum(1 for i in range(1, len(ticks)) if ticks[i] < ticks[i - 1])
        moves = up_t + dn_t
        buy_pct  = round(up_t / moves * 100) if moves else 50
        sell_pct = 100 - buy_pct
        if buy_pct >= BUYER_PCT_THRESHOLD:
            pressure = "BUYER"
        elif sell_pct >= BUYER_PCT_THRESHOLD:
            pressure = "SELLER"
        else:
            pressure = "FIGHT"
        mid     = (hi + lo) / 2
        crosses = sum(
            1 for i in range(1, len(ticks))
            if (ticks[i - 1] < mid) != (ticks[i] < mid)
        )
        is_fight = crosses >= 4
        hold_price = None
        if rng > 0:
            bin_size = rng / 8
            bins: dict[int, int] = {}
            for t in ticks:
                b = min(7, int((t - lo) / bin_size))
                bins[b] = bins.get(b, 0) + 1
            top_bin    = max(bins, key=bins.get)
            hold_price = round(lo + top_bin * bin_size + bin_size / 2, 6)
            hold_visits = bins[top_bin]
        else:
            hold_price  = round(cur, 6)
            hold_visits = len(ticks)
        n  = len(ticks)
        t3 = max(n // 3, 1)
        early = ticks[t3]     - ticks[0]
        mid_m = ticks[2 * t3] - ticks[t3]
        late  = ticks[-1]     - ticks[2 * t3]

        def _dir(v: float) -> str:
            return "UP" if v > 0 else ("DOWN" if v < 0 else "FLAT")
        phases = [_dir(early), _dir(mid_m), _dir(late)]
        _REACT_FROM_EXTREME = float(os.environ.get("QX_REACT_FROM_EXTREME", "0.50"))
        reaction = None
        if rng > 0:
            from_hi   = (hi  - cur) / rng
            from_lo   = (cur - lo)  / rng
            net       = cur - op
            late_q    = max(n // 4, 2)
            late_move = ticks[-1] - ticks[-late_q]
            if from_hi > _REACT_FROM_EXTREME and late_move <= 0 and net < 0:
                reaction = "SELLER"
            elif from_lo > _REACT_FROM_EXTREME and late_move >= 0 and net > 0:
                reaction = "BUYER"
        last_react = None
        if n >= 15:
            last_n2 = max(n // 6, 6)   # min 6 so fi_tot can reach 5
            fin2    = ticks[-last_n2:]
            fi2_up  = sum(1 for i in range(1, len(fin2)) if fin2[i] > fin2[i - 1])
            fi2_dn  = sum(1 for i in range(1, len(fin2)) if fin2[i] < fin2[i - 1])
            fi2_tot = fi2_up + fi2_dn
            if fi2_tot >= 3:
                fbp2       = fi2_up / fi2_tot
                net_run    = cur - op
                is_bull_rt = net_run > 0
                if is_bull_rt:
                    if fbp2 <= 0.30:
                        last_react = "EXHAUST"
                    elif fi2_tot >= 5 and fbp2 >= 0.90:
                        last_react = "EXHAUST"
                    elif 0.55 <= fbp2 <= 0.85 and fi2_dn >= 2:
                        last_react = "RECOVERY"
                elif net_run < 0:
                    if fbp2 >= 0.70:
                        last_react = "EXHAUST"
                    elif fi2_tot >= 5 and fbp2 <= 0.10:
                        last_react = "EXHAUST"
                    elif 0.15 <= fbp2 <= 0.45 and fi2_up >= 2:
                        last_react = "RECOVERY"

        def _rnd(p):
            lvl, _, str_ = _round_level(p)
            return (lvl, str_) if str_ != "NONE" else (None, None)
        cur_lvl, cur_str = _rnd(cur)
        hi_lvl,  hi_str  = _rnd(hi)
        lo_lvl,  lo_str  = _rnd(lo)
        round_info = {
            "near_level":    cur_lvl,
            "near_strength": cur_str,
            "hi_level":      hi_lvl  if hi_str  in ("BIG", "MID") else None,
            "hi_strength":   hi_str  if hi_str  in ("BIG", "MID") else None,
            "lo_level":      lo_lvl  if lo_str  in ("BIG", "MID") else None,
            "lo_strength":   lo_str  if lo_str  in ("BIG", "MID") else None,
        }
        _ed_n = len(ticks)
        if _ed_n >= 3:
            _ed_end = ticks[-min(10, _ed_n):]
            _ed_en = len(_ed_end)
            _ed_buy = 0.0
            _ed_sell = 0.0
            for _i in range(1, _ed_en):
                _d = _ed_end[_i] - _ed_end[_i-1]
                if _d > 0:
                    _ed_buy += _d
                elif _d < 0:
                    _ed_sell += abs(_d)
            _ed_total = _ed_buy + _ed_sell
            _ed_bp = round(_ed_buy / _ed_total * 100) if _ed_total > 0 else 50
            _ed_move = _ed_end[-1] - _ed_end[0]
            _ed_dir = "UP" if _ed_move > 0 else "DOWN" if _ed_move < 0 else "FLAT"
            _ed_dom = "BUYER" if _ed_bp >= 65 else "SELLER" if _ed_bp <= 35 else "FIGHT"
            ending_direction = {
                "direction": _ed_dir,
                "buy_pct": _ed_bp,
                "dominance": _ed_dom,
                "move": round(_ed_move, 6),
                "tick_count": _ed_en,
            }
        else:
            ending_direction = {"direction": "FLAT", "buy_pct": 50,
                                "dominance": "FIGHT", "move": 0, "tick_count": _ed_n}
        return {
            "buy_pct":    buy_pct,
            "sell_pct":   sell_pct,
            "pressure":   pressure,
            "is_fight":   is_fight,
            "crosses":    crosses,
            "hold_price": hold_price,
            "hold_visits":hold_visits,
            "phases":     phases,
            "reaction":   reaction,
            "net":        round(cur - op, 6),
            "tick_count": len(ticks),
            "last_react": last_react,
            "round":      round_info,
            "ending_direction": ending_direction,
        }

    def _running_confirmation(self, stream: _AssetStream) -> str | None:
        """Check if the running candle's tick movement confirms the current prediction."""
        if not stream.prediction or len(stream.ticks) < 5:
            return None
        pred = stream.prediction.get("signal")
        if pred == "NEUTRAL":
            return None
        ticks  = list(stream.ticks)[-100:]
        open_p = stream.candle_open_price
        # Overall direction from open
        net = ticks[-1] - open_p
        # Momentum consistency: first half vs second half
        mid         = len(ticks) // 2
        first_half  = ticks[mid] - ticks[0]
        second_half = ticks[-1]  - ticks[mid]
        # Strong momentum: both halves same direction
        if first_half > 0 and second_half > 0:
            running_dir = "UP"
        elif first_half < 0 and second_half < 0:
            running_dir = "DOWN"
        else:
            # Mixed — use net direction from open
            running_dir = "UP" if net >= 0 else "DOWN"
        tick_max = max(ticks)
        tick_min = min(ticks)
        max_up_exc = tick_max - open_p
        max_dn_exc = open_p - tick_min
        max_exc = max(max_up_exc, max_dn_exc)
        _REJECT_THRESHOLD = float(os.environ.get("QX_REJECT_THRESHOLD", "0.30"))
        if max_exc > 0:
            if abs(net) < max_exc * _REJECT_THRESHOLD:
                if max_up_exc > max_dn_exc:
                    running_dir = "DOWN"  # rejected the highs → bearish
                else:
                    running_dir = "UP"    # rejected the lows → bullish
        if (pred == "CALL" and running_dir == "UP") or \
           (pred == "PUT"  and running_dir == "DOWN"):
            return "CONFIRMING"
        return "OPPOSING"

    def _apply_strength_gate(self, stream: _AssetStream,
                             prediction: dict) -> dict:
        """Method B (2026-07-10, untested) — gate prediction strength using the"""
        if not prediction or prediction.get("signal") not in ("CALL", "PUT"):
            return prediction
        conf = self._running_confirmation(stream)
        if conf is None:
            return prediction
        tick_count = len(stream.ticks)
        if tick_count < 10:
            return prediction  # not enough live evidence yet
        current = prediction.get("strength", "WEAK")
        new_strength = current
        gate_tag = None
        gate_reason = None
        if current == "WEAK" and conf == "CONFIRMING":
            new_strength = "MEDIUM"
            gate_tag = "RUNCONF_UP"
            gate_reason = (f"RUNCONF: WEAK + 10+ confirming ticks "
                          f"({tick_count}) -> upgraded to MEDIUM")
        elif current == "MEDIUM" and conf == "OPPOSING":
            new_strength = "WEAK"
            gate_tag = "RUNCONF_DOWN"
            gate_reason = (f"RUNCONF: MEDIUM + 10+ opposing ticks "
                          f"({tick_count}) -> demoted to WEAK")
        elif current == "STRONG" and conf == "OPPOSING":
            new_strength = "MEDIUM"
            gate_tag = "RUNCONF_DOWN"
            gate_reason = (f"RUNCONF: STRONG + 10+ opposing ticks "
                          f"({tick_count}) -> demoted to MEDIUM")
        elif current == "WEAK" and conf == "OPPOSING":
            new_strength = "WEAK"  # unchanged — triggers Option B suppression
            gate_tag = "RUNCONF_NEUTRAL"
            gate_reason = (f"RUNCONF: WEAK + 10+ opposing ticks "
                          f"({tick_count}) -> demoted to NEUTRAL (suppressed)")
        else:
            return prediction  # no change
        new_pred = dict(prediction)
        new_pred["strength"] = new_strength
        new_pred["reasons"] = [*prediction.get("reasons", []), gate_reason]
        new_pred["_runconf_tag"] = gate_tag
        return new_pred

    def _reset_micro_cache(self, stream: _AssetStream) -> None:
        """Clear the microstructure cache + last-broadcast snapshot. Call"""
        stream._micro_cache = None
        stream._micro_cache_at_tick = 0
        stream._micro_cache_high = 0.0
        stream._micro_cache_low = 0.0
        stream._micro_cache_close = 0.0
        stream._last_bcast_high = 0.0
        stream._last_bcast_low = 0.0
        stream._last_bcast_close = 0.0
        stream._tracked_high = None
        stream._tracked_low = None

    def _running_candle(self, stream: _AssetStream) -> dict:
        """Build the current running candle OHLC."""
        op = stream.candle_open_price
        if not stream.ticks:
            return {"time": stream.candle_open_time, "open": op,
                    "high": op, "low": op, "close": op}
        cur_close = stream.ticks[-1]
        cur_high = getattr(stream, '_tracked_high', None)
        cur_low = getattr(stream, '_tracked_low', None)
        if cur_high is None or cur_low is None:
            ticks_list = list(stream.ticks)
            cur_high = max(ticks_list)
            cur_low = min(ticks_list)
            stream._tracked_high = cur_high
            stream._tracked_low = cur_low
        return {
            "time":  stream.candle_open_time,
            "open":  op,
            "high":  cur_high,
            "low":   cur_low,
            "close": cur_close,
        }
    @staticmethod
    def _track_tick(stream: '_AssetStream', price: float) -> None:
        """Update tracked high/low for ONE appended tick. Called by the"""
        h = getattr(stream, '_tracked_high', None)
        l = getattr(stream, '_tracked_low', None)
        if h is None or price > h:
            stream._tracked_high = price
        if l is None or price < l:
            stream._tracked_low = price

    async def _close_running_and_start_new(self, stream: _AssetStream,
                                     new_open_time: int, first_tick: float,
                                     open_is_real: bool = True):
        """Finalize the running candle and begin a new one."""
        if new_open_time <= stream.candle_open_time:
            return None
        closed = self._running_candle(stream)
        if stream.candles and stream.candles[-1]["time"] == closed["time"]:
            stream.candles[-1] = closed
        elif not stream.candles or stream.candles[-1]["time"] < closed["time"]:
            stream.candles.append(closed)
        # Keep list bounded
        if len(stream.candles) > MAX_CANDLES:
            stream.candles = stream.candles[-TRUNCATE_TO:]
        _micro_snap = (self._analyze_microstructure(stream.ticks, stream.candle_open_price)
                       if len(stream.ticks) >= 3 else None)
        old_prediction = stream.prediction
        stream.prediction = None
        accuracy = await asyncio.to_thread(
            self._grade_and_log, stream.asset, stream.period, closed,
            old_prediction, _micro_snap, stream.candles)
        try:
            if accuracy == "wrong":
                stream._consecutive_losses = getattr(stream, '_consecutive_losses', 0) + 1
                if stream._consecutive_losses >= LOSS_COOLDOWN_THRESHOLD:
                    stream._loss_cooldown_until = time.time() + LOSS_COOLDOWN_SEC
                    stream._consecutive_losses = 0
                    print(f"[feed] {stream.asset} hit {LOSS_COOLDOWN_THRESHOLD} consecutive "
                          f"losses — cooling down for {LOSS_COOLDOWN_SEC//60} min (counter reset)")
            elif accuracy == "correct":
                stream._consecutive_losses = 0
            if (stream._loss_cooldown_until > 0
                    and time.time() > stream._loss_cooldown_until):
                stream._consecutive_losses = 0
                stream._loss_cooldown_until = 0
        except Exception as _e:
            print(f"[silent-except] feed.py:2963 {type(_e).__name__}: {_e}")
            pass
        if accuracy in ("correct", "wrong"):
            try:
                from engines.otc.config import weight_adapter as _otc_adapter
                from engines.real.config import weight_adapter as _real_adapter
                _otc_adapter.invalidate_cache(stream.asset, stream.period)
                _real_adapter.invalidate_cache(stream.asset, stream.period)
            except Exception as _e:
                print(f"[silent-except] feed.py:2981 {type(_e).__name__}: {_e}")
                pass  # adapters not loaded (e.g. test context) — skip
            try:
                from core.brain import record_prediction
                actual_dir = "UP" if closed["close"] > closed["open"] else (
                    "DRAW" if closed["close"] == closed["open"] else "DOWN")
                await asyncio.to_thread(
                    record_prediction,
                    old_prediction or {}, stream.asset, stream.period,
                    closed["time"], actual_dir, accuracy, closed, _micro_snap)
            except Exception as _e:
                print(f"[silent-except] feed.py:2993 {type(_e).__name__}: {_e}")
                pass
            try:
                _brain_counter = getattr(self, '_brain_analyze_counter', 0) + 1
                self._brain_analyze_counter = _brain_counter
                if _brain_counter % BRAIN_ANALYZE_INTERVAL == 0:
                    from core.brain import analyze_and_learn
                    await asyncio.to_thread(analyze_and_learn)
                    try:
                        from core.time_patterns import recompute_from_signal_log
                        await asyncio.to_thread(recompute_from_signal_log, PATTERN_RECOMPUTE_DAYS)
                    except Exception as _pe:
                        print(f"[feed] pattern refresh skipped: {_pe}")
                    try:
                        from core.auto_tune import apply_tuned_weights_to_engines
                        await asyncio.to_thread(apply_tuned_weights_to_engines)
                    except Exception as _te:
                        print(f"[feed] auto-tune skipped: {_te}")
            except Exception as _e:
                print(f"[silent-except] feed.py:3021 {type(_e).__name__}: {_e}")
                pass
        if accuracy in ("correct", "wrong"):
            _reg = (old_prediction or {}).get("regime") or {}
            _regime = _reg.get("regime")
            if _reg.get("is_volatile"):
                _zone = "VOLATILE"
            elif _reg.get("is_trending"):
                _zone = "TREND"
            elif _reg.get("is_ranging"):
                _zone = "RANGE"
            else:
                _zone = "UNKNOWN"
            _key = (_regime, _zone)
            if _key == (stream.zone_streak["regime"], stream.zone_streak["zone"]):
                stream.zone_streak["losses"] = (
                    stream.zone_streak["losses"] + 1 if accuracy == "wrong" else 0)
            else:
                stream.zone_streak = {"regime": _key[0], "zone": _key[1],
                                      "losses": 1 if accuracy == "wrong" else 0}
        stream.prediction = await self._run_eoc(stream, actual_open=first_tick)
        if stream.prediction and stream.prediction.get("signal") in ("CALL", "PUT"):
            stream._locked_direction = stream.prediction["signal"]
        stream.signal_delay_until = float(new_open_time) + SIGNAL_DELAY_SEC
        if _micro_snap:
            await asyncio.to_thread(
                self._save_micro, stream.asset, stream.period, closed,
                _micro_snap, stream.candles, list(stream.ticks))
        try:
            from core.algorithm_monitor import record_candle
            _now = time.time()
            if _now - stream._last_payout_refresh > PAYOUT_REFRESH_SEC:
                try:
                    pay = self._client.get_payout_by_asset(stream.asset)
                    if pay is not None:
                        stream.payout = int(pay)
                except Exception as _e:
                    print(f"[silent-except] feed.py:3097 {type(_e).__name__}: {_e}")
                    pass
                stream._last_payout_refresh = _now
            payout = getattr(stream, 'payout', None) or 0
            tick_count = int(_micro_snap.get('tick_count', 0)) if _micro_snap else 0
            record_candle(
                asset=stream.asset, ctime=closed.get('time', 0),
                payout=payout,
                open_=closed.get('open', 0), high=closed.get('high', 0),
                low=closed.get('low', 0), close=closed.get('close', 0),
                tick_count=tick_count)
        except Exception as _e:
            print(f"[feed] algorithm_monitor record_candle failed: {_e}")
        # Start new candle
        stream.candle_open_time    = new_open_time
        stream.candle_open_price   = first_tick
        stream.candle_open_is_real = open_is_real
        stream.ticks.clear()
        stream.ticks.append(first_tick)
        self._track_tick(stream, first_tick)   # keep tracked high/low fresh
        self._reset_micro_cache(stream)
        stream._locked_direction = None
        stream._option_b_fired = False
        return accuracy

    async def _smart_sleep(self, stream: _AssetStream) -> None:
        """Sleep until next tick poll, but wake up early at candle boundary."""
        if stream.candle_open_time > 0:
            close_at     = stream.candle_open_time + stream.period
            until_close  = close_at - time.time()
            sleep_dur    = max(0.01, min(0.05, until_close))
        else:
            sleep_dur = 0.05
        await asyncio.sleep(sleep_dur)

    async def _start_stream(self, stream: _AssetStream) -> None:
        """Subscribe + load history for one stream. Raises on failure so the"""
        if self._client is None:
            raise RuntimeError("Quotex client not connected yet")
        asset, period = stream.asset, stream.period
        print(f"[feed] starting stream {asset}@{period}s"
              + (f" (ALWAYS-ON — 85%+ payout)" if stream.always_on else ""))
        await self._client.start_candles_stream(asset, period)
        stream.sub_started = True
        stream._sub_client_id = id(self._client)
        if hasattr(self._client, 'register_tick_callback'):
            _loop = asyncio.get_running_loop()
            def _on_tick(tick_dict, _stream=stream, _loop=_loop):
                try:
                    _loop.call_soon_threadsafe(
                        _stream.tick_queue.put_nowait, tick_dict)
                except Exception:
                    pass
            self._client.register_tick_callback(asset, _on_tick)
            stream.tick_callback = _on_tick
            print(f"[feed] event-driven ticks enabled for {asset}@{period}s")
        await asyncio.sleep(1)  # let first ticks arrive
        try:
            pay = self._client.get_payout_by_asset(asset)
            if pay is None:
                await asyncio.sleep(PAYOUT_RETRY_SLEEP)
                pay = self._client.get_payout_by_asset(asset)
            stream.payout = int(pay) if pay is not None else None
        except Exception:
            stream.payout = None
        if self._connected and stream.always_on:
            try:
                asyncio.create_task(self._warn_if_stuck(asset, period, stream))
            except Exception as _e:
                print(f"[silent-except] feed.py:3233 {type(_e).__name__}: {_e}")
                pass
        history = await self._load_history(asset, period)
        if not history:
            print(f"[feed] no history for {asset}@{period}s "
                  f"— starting from ticks only")
            if not stream.candles:
                await self._broadcast({
                    "type":       "snapshot",
                    "asset":      asset,
                    "period":     period,
                    "candles":    [],
                    "prediction": None,
                })
            return
        if stream.candles:
            preserved_last_time = stream.candles[-1].get("time", 0)
            new_candles = [c for c in history if c.get("time", 0) > preserved_last_time]
            if new_candles:
                stream.candles.extend(new_candles)
                if len(stream.candles) > MAX_CANDLES:
                    stream.candles = stream.candles[-TRUNCATE_TO:]
                print(f"[feed] watchdog-merged {len(new_candles)} new candles "
                      f"into preserved {len(stream.candles) - len(new_candles)} "
                      f"for {asset}@{period}s")
            new_last = stream.candles[-1]
            new_open_time = new_last["time"] + period
            if new_open_time > stream.candle_open_time:
                stream.candle_open_time  = new_open_time
                stream.candle_open_price = new_last["close"]
                stream.candle_open_is_real = False
            if not stream.ticks:
                stream.ticks.append(new_last["close"])
                self._track_tick(stream, new_last["close"])
            self._reset_micro_cache(stream)
            stream.prediction = await self._run_eoc(stream, actual_open=new_last["close"])
            stream.signal_delay_until = 0.0
            await self._broadcast({
                "type":       "snapshot",
                "asset":      asset,
                "period":     period,
                "candles":    stream.candles[-SNAPSHOT_CANDLES:],
                "prediction": stream.prediction,
            })
            return
        last = history[-1]
        stream.candles           = history
        stream.candle_open_time  = last["time"] + period
        stream.candle_open_price = last["close"]
        stream.ticks.clear()
        stream.ticks.append(last["close"])
        self._track_tick(stream, last["close"])
        stream.candle_open_is_real = False
        stream.last_tick_ts         = 0.0
        self._reset_micro_cache(stream)
        stream.prediction = await self._run_eoc(stream, actual_open=last["close"])
        stream.signal_delay_until = 0.0
        try:
            if self._broadcast is not None:
                await self._broadcast({
                    "type":       "snapshot",
                    "asset":      asset,
                    "period":     period,
                    "candles":    history,
                    "prediction": stream.prediction,
                })
        except Exception as _bcast_err:
            print(f"[feed] initial snapshot broadcast failed (non-fatal): {_bcast_err}")

    async def _stream_loop(self, stream: _AssetStream) -> None:
        """Runs 'forever' for one (asset, period) — timer-close fallback,"""
        while True:
            try:
                if (stream.signal_delay_until > 0
                        and time.time() >= stream.signal_delay_until
                        and stream.prediction):
                    stream.signal_delay_until = 0.0
                    running = self._running_candle(stream)
                    await self._broadcast({
                        "type":       "tick",
                        "asset":      stream.asset,
                        "period":     stream.period,
                        "candle":     running,
                        "prediction": stream.prediction,
                    })
                if (stream.last_real_tick_wall > 0
                        and time.time() - stream.last_real_tick_wall > STALE_SECS):
                    print(f"[feed] STALE: {stream.asset}@{stream.period}s "
                          f"— re-arming stream")
                    try:
                        if self._client:
                            await self._client.start_candles_stream(
                                stream.asset, stream.period)
                    except Exception as _e:
                        print(f"[silent-except] feed.py:3380 {type(_e).__name__}: {_e}")
                        pass
                    stream.last_real_tick_wall = time.time()  # re-arm debounce
                    await self._broadcast({"type": "stale", "asset": stream.asset,
                                           "period": stream.period})
                    await asyncio.sleep(2)
                    continue
                now = time.time()
                if (stream.candle_open_time > 0
                        and now >= stream.candle_open_time + stream.period + TIMER_GRACE):
                    expected_new = _floor_to_period(now, stream.period)
                    if expected_new > stream.candle_open_time:
                        last_px = (list(stream.ticks)[-1] if stream.ticks
                                   else stream.candle_open_price)
                        print(f"[feed] timer-close {stream.asset}@{stream.period}s "
                              f"{stream.candle_open_time} -> {expected_new}")
                        accuracy = await self._close_running_and_start_new(
                            stream, expected_new, last_px, open_is_real=False)
                        running  = self._running_candle(stream)
                        all_c    = stream.candles + [running]
                        await self._broadcast({
                            "type":       "eoc",
                            "asset":      stream.asset,
                            "period":     stream.period,
                            "candles":    all_c[-SNAPSHOT_CANDLES:],
                            "prediction": None,   # gated — arrives via tick
                            "accuracy":   accuracy,
                        })
                if self._client is None:
                    await asyncio.sleep(1)
                    continue
                if stream.tick_callback is not None:
                    try:
                        first = await asyncio.wait_for(
                            stream.tick_queue.get(), timeout=0.05)
                        new_ticks = [first]
                        while not stream.tick_queue.empty():
                            try:
                                new_ticks.append(stream.tick_queue.get_nowait())
                            except Exception:
                                break
                    except asyncio.TimeoutError:
                        continue
                else:
                    price_data = await self._client.get_realtime_price(stream.asset)
                    if not price_data:
                        await self._smart_sleep(stream)
                        continue
                    new_ticks = list(price_data)
                if stream.last_tick_ts <= 0.0:
                    stream.last_tick_ts = max(
                        (float(p["time"]) for p in new_ticks if float(p["time"]) > 0),
                        default=0.0,
                    )
                else:
                    new_ticks = [
                        p for p in new_ticks
                        if float(p["time"]) > stream.last_tick_ts
                    ]
                if not new_ticks:
                    if stream.tick_callback is None:
                        await self._smart_sleep(stream)
                    continue
                # Mark all these ticks as seen
                stream.last_tick_ts = float(new_ticks[-1]["time"])
                stream.last_real_tick_wall = time.time()   # feed is alive
                boundary_idx = None
                for i, t in enumerate(new_ticks):
                    t_open = _floor_to_period(float(t["time"]), stream.period)
                    if stream.candle_open_time > 0 and t_open != stream.candle_open_time:
                        boundary_idx = i
                        break
                if boundary_idx is not None:
                    remaining = new_ticks
                    last_accuracy = None
                    last_eoc_candles = None
                    for _iter in range(10):
                        if not remaining:
                            break
                        b_idx = None
                        for i, t in enumerate(remaining):
                            t_open = _floor_to_period(float(t["time"]), stream.period)
                            if stream.candle_open_time > 0 and t_open != stream.candle_open_time:
                                b_idx = i
                                break
                        if b_idx is None:
                            for t in remaining:
                                stream.ticks.append(float(t["price"]))
                                self._track_tick(stream, float(t["price"]))
                            break
                        b_tick = remaining[b_idx]
                        tick_new_open = _floor_to_period(
                            float(b_tick["time"]), stream.period)
                        if tick_new_open <= stream.candle_open_time:
                            cur = [
                                t for t in remaining
                                if _floor_to_period(float(t["time"]), stream.period)
                                == stream.candle_open_time
                            ]
                            n_drop = len(remaining) - len(cur)
                            if n_drop:
                                print(f"[feed] dropped {n_drop} late tick(s) from "
                                      f"closed candle ({stream.asset}@{stream.period}s)")
                            reanchored = False
                            if cur and not stream.candle_open_is_real:
                                real_open = float(cur[0]["price"])
                                stream.candle_open_price   = real_open
                                stream.candle_open_is_real = True
                                stream.ticks.clear()
                                stream.ticks.append(real_open)
                                self._track_tick(stream, real_open)
                                cur = cur[1:]
                                self._reset_micro_cache(stream)
                                if stream.prediction:
                                    stream.prediction["candle"] = _pred_candle(
                                        stream.candles, stream.prediction["signal"],
                                        stream.period, real_open)
                                reanchored = True
                            for t in cur:
                                stream.ticks.append(float(t["price"]))
                                self._track_tick(stream, float(t["price"]))
                            remaining = []   # consumed
                            break
                        for t in remaining[:b_idx]:
                            stream.ticks.append(float(t["price"]))
                            self._track_tick(stream, float(t["price"]))
                        first_px = float(b_tick["price"])
                        if _iter == 0:
                            print(f"[feed] tick-close  {stream.asset}@{stream.period}s "
                                  f"{stream.candle_open_time} -> {tick_new_open}  "
                                  f"(ticks: {len(stream.ticks)})")
                        else:
                            print(f"[feed] tick-close  {stream.asset}@{stream.period}s "
                                  f"{stream.candle_open_time} -> {tick_new_open}  "
                                  f"(multi-boundary iter {_iter+1})")
                        last_accuracy = await self._close_running_and_start_new(
                            stream, tick_new_open, first_px, open_is_real=True)
                        last_eoc_candles = (stream.candles + [self._running_candle(stream)])[-SNAPSHOT_CANDLES:]
                        remaining = remaining[b_idx + 1:]
                    if last_accuracy is not None and last_eoc_candles is not None:
                        await self._broadcast({
                            "type":       "eoc",
                            "asset":      stream.asset,
                            "period":     stream.period,
                            "candles":    last_eoc_candles,
                            "prediction": None,   # gated — arrives via tick
                            "accuracy":   last_accuracy,
                        })
                else:
                    if stream.candle_open_time == 0 and new_ticks:
                        ft = new_ticks[0]
                        stream.candle_open_time    = _floor_to_period(
                            float(ft["time"]), stream.period)
                        stream.candle_open_price   = float(ft["price"])
                        stream.candle_open_is_real = True
                        print(f"[feed] bootstrapped candle from tick "
                              f"({stream.asset}@{stream.period}s): "
                              f"t={stream.candle_open_time} "
                              f"open={stream.candle_open_price}")
                    reanchored = False
                    if (not stream.candle_open_is_real) and new_ticks:
                        real_open = float(new_ticks[0]["price"])
                        stream.candle_open_price   = real_open
                        stream.candle_open_is_real = True
                        stream.ticks.clear()
                        stream.ticks.append(real_open)
                        self._track_tick(stream, real_open)
                        new_ticks = new_ticks[1:]   # first tick became the open
                        self._reset_micro_cache(stream)
                        if stream.prediction:
                            stream.prediction["candle"] = _pred_candle(
                                stream.candles, stream.prediction["signal"],
                                stream.period, real_open)
                        reanchored = True
                    for t in new_ticks:
                        p = float(t["price"])
                        stream.ticks.append(p)
                        self._track_tick(stream, p)
                    running = self._running_candle(stream)
                    if not stream.candles:
                        stream.candles.append(running)
                    elif stream.candles[-1]["time"] < running["time"]:
                        stream.candles.append(running)
                    pred_changed = False
                    if (not DISABLE_LIVE_REEVAL
                            and ENABLE_LIVE_THEORY and stream.base_candles
                            and len(stream.ticks) >= LIVE_REEVAL_MIN_TICKS):
                        time_to_close = -1
                        if stream.candle_open_time > 0:
                            time_to_close = (stream.candle_open_time
                                             + stream.period) - time.time()
                            if time_to_close < 5:
                                reeval_interval = LIVE_REEVAL_INTERVAL_CRITICAL
                            elif time_to_close < 10:
                                reeval_interval = LIVE_REEVAL_INTERVAL_LAST_10S
                            elif time_to_close < 30:
                                reeval_interval = LIVE_REEVAL_INTERVAL_LAST_30S
                            else:
                                reeval_interval = LIVE_REEVAL_INTERVAL_MID
                        else:
                            reeval_interval = LIVE_REEVAL_INTERVAL_MID
                        live_only = 0 < time_to_close < 30
                        if len(stream.ticks) >= 4 and reeval_interval > 2:
                            try:
                                recent = list(stream.ticks)[-4:]
                                recent_range = max(recent) - min(recent)
                                _atr_val = (_atr(stream.candles[-20:])
                                            if len(stream.candles) >= 20
                                            else 0.0001)
                                if _atr_val > 0 and recent_range > _atr_val * 0.5:
                                    reeval_interval = max(2, reeval_interval // 2)
                            except Exception as _e:
                                print(f"[silent-except] feed.py:3732 {type(_e).__name__}: {_e}")
                                pass
                        if len(stream.ticks) - stream._live_reeval_ticks >= reeval_interval:
                            try:
                                fresh, _ = await self._analyze_core(
                                    stream.asset, stream.period,
                                    stream.base_candles, stream.base_ticks,
                                    running_ticks=list(stream.ticks)[-100:],
                                    stream=stream,
                                    live_only=live_only)
                                stream._live_reeval_ticks = len(stream.ticks)
                                if fresh and stream.prediction:
                                    locked_dir = stream.prediction.get("signal")
                                    fresh_dir = fresh.get("signal")
                                    if locked_dir in ("CALL", "PUT"):
                                        if fresh_dir == locked_dir:
                                            _prev_strength = stream.prediction.get("strength")
                                            stream.prediction = {
                                                **stream.prediction,
                                                "score": fresh.get("score",
                                                    stream.prediction.get("score")),
                                                "confidence": fresh.get("confidence",
                                                    stream.prediction.get("confidence")),
                                                "strength": fresh.get("strength",
                                                    stream.prediction.get("strength")),
                                                "agree": fresh.get("agree",
                                                    stream.prediction.get("agree")),
                                                "total": fresh.get("total",
                                                    stream.prediction.get("total")),
                                                "reasons": (
                                                    list(stream.prediction.get("reasons", []))
                                                    + [r for r in fresh.get("reasons", [])
                                                       if "LIVE re-eval" in str(r)
                                                       or "reeval" in str(r).lower()][:2]
                                                ),
                                                "regime": fresh.get("regime",
                                                    stream.prediction.get("regime")),
                                                "micro": fresh.get("micro",
                                                    stream.prediction.get("micro")),
                                            }
                                            if (fresh.get("strength") == "STRONG"
                                                    and _prev_strength != "STRONG"):
                                                pred_changed = True
                                        else:
                                            _note = (
                                                f"LIVE re-eval CONTEST: fresh signal "
                                                f"{fresh_dir} differs from locked "
                                                f"{locked_dir} — original kept.")
                                            stream.prediction.setdefault(
                                                "reasons", []).append(_note)
                                    elif locked_dir == "NEUTRAL" and fresh_dir in ("CALL", "PUT"):
                                        if getattr(stream, '_option_b_fired', False):
                                            pass
                                        else:
                                            prev_locked = getattr(stream, '_locked_direction', None)
                                            if prev_locked and fresh_dir != prev_locked:
                                                pass
                                            elif prev_locked and fresh_dir == prev_locked:
                                                _merged_reasons = (
                                                    list(stream.prediction.get("reasons", []))
                                                    + list(fresh.get("reasons", []))
                                                    + [f"LIVE re-eval upgraded NEUTRAL→{fresh_dir}"]
                                                )
                                                stream.prediction = {
                                                    **stream.prediction,
                                                    **fresh,
                                                    "reasons": _merged_reasons,
                                                }
                                                pred_changed = True
                                            else:
                                                _merged_reasons = (
                                                    list(stream.prediction.get("reasons", []))
                                                    + list(fresh.get("reasons", []))
                                                    + [f"LIVE re-eval upgraded NEUTRAL→{fresh_dir}"]
                                                )
                                                stream.prediction = {
                                                    **stream.prediction,
                                                    **fresh,
                                                    "reasons": _merged_reasons,
                                                }
                                                stream._locked_direction = fresh_dir
                                                pred_changed = True
                            except Exception as exc:
                                print(f"[feed] LIVE re-eval error "
                                      f"({stream.asset}@{stream.period}s): {exc}")
                    if (ENABLE_STRENGTH_GATE and stream.prediction
                            and stream.prediction.get("signal") in ("CALL", "PUT")):
                        _time_to_close = -1
                        if stream.candle_open_time > 0:
                            _time_to_close = (stream.candle_open_time
                                             + stream.period) - time.time()
                        if not DISABLE_LIVE_REEVAL and 0 < _time_to_close < STRENGTH_GATE_LAST_SECS:
                            gated = self._apply_strength_gate(stream, stream.prediction)
                            if gated is not stream.prediction:
                                if gated.get("strength") == "WEAK":
                                    orig_signal = gated.get("signal", "NEUTRAL")
                                    orig_conf = gated.get("confidence", 0)
                                    if not getattr(stream, '_locked_direction', None):
                                        stream._locked_direction = orig_signal
                                    stream._option_b_fired = True
                                    gated["signal"] = "NEUTRAL"
                                    gated["strength"] = "NEUTRAL"
                                    gated["confidence"] = 0
                                    gated.setdefault("reasons", []).append(
                                        f"LIVE WEAK→NEUTRAL (Option B): running ticks "
                                        f"opposed original {orig_signal} (conf was "
                                        f"{orig_conf}) — skip is +EV. "
                                        f"(gated in last 30s of candle)")
                                    pred_changed = True
                                stream.prediction = gated
                    if stream.candle_open_price > 0:
                        cur_high = running["high"]
                        cur_low  = running["low"]
                        cur_close = running["close"]
                        tick_n   = len(stream.ticks)
                        if (stream._micro_cache is None
                                or (tick_n - stream._micro_cache_at_tick) >= MICRO_RECALC_EVERY
                                or cur_high != stream._micro_cache_high
                                or cur_low  != stream._micro_cache_low
                                or cur_close != stream._micro_cache_close):
                            recent_ticks = list(stream.ticks)[-200:]
                            stream._micro_cache = self._analyze_microstructure(
                                recent_ticks, stream.candle_open_price)
                            stream._micro_cache_at_tick = tick_n
                            stream._micro_cache_high    = cur_high
                            stream._micro_cache_low     = cur_low
                            stream._micro_cache_close   = cur_close
                        micro_snap = stream._micro_cache
                        now_ts = time.time()
                        gate_opened_this_tick = False
                        if stream.signal_delay_until > 0 and now_ts < stream.signal_delay_until:
                            delay_left = stream.signal_delay_until - now_ts
                            if reanchored or pred_changed:
                                pass
                        else:
                            if stream.signal_delay_until > 0:
                                stream.signal_delay_until = 0.0
                                pred_changed = True
                                gate_opened_this_tick = True
                        if (SKIP_REDUNDANT_BROADCAST
                                and not reanchored
                                and not pred_changed
                                and not gate_opened_this_tick
                                and cur_high  == stream._last_bcast_high
                                and cur_low   == stream._last_bcast_low
                                and cur_close == stream._last_bcast_close):
                            # No change at all — skip
                            continue
                        msg = {
                            "type":          "tick",
                            "asset":         stream.asset,
                            "period":        stream.period,
                            "candle":        running,
                            "running_conf":  self._running_confirmation(stream),
                            "micro":         micro_snap,
                        }
                        if not (stream.signal_delay_until > 0 and time.time() < stream.signal_delay_until):
                            if stream.signal_delay_until > 0:
                                stream.signal_delay_until = 0.0
                            if stream.prediction:
                                msg["prediction"] = stream.prediction
                        stream._last_bcast_high  = cur_high
                        stream._last_bcast_low   = cur_low
                        stream._last_bcast_close = cur_close
                        await self._broadcast(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                import traceback
                print(f"[feed] stream {stream.asset}@{stream.period}s "
                      f"loop error: {exc}")
                traceback.print_exc()
                self._record_stream_error()
                await asyncio.sleep(2)
                continue
            if stream.tick_callback is None:
                await self._smart_sleep(stream)

    async def _run_stream(self, stream: _AssetStream) -> None:
        """Owns one _AssetStream for its whole life: start, run, clean up."""
        key = (stream.asset, stream.period)
        try:
            while not self._connected_event.is_set():
                await self._connected_event.wait()
            async with self._new_stream_gate:
                await self._start_stream(stream)
                await asyncio.sleep(self._stagger_gap)
            await self._stream_loop(stream)
        except asyncio.CancelledError as _e:
            print(f"[silent-except] feed.py:4171 {type(_e).__name__}: {_e}")
            pass
        except Exception as exc:
            import traceback
            print(f"[feed] stream {key} failed to start: {exc}")
            traceback.print_exc()
            self._record_stream_error()
        finally:
            try:
                if (self._client and stream.tick_callback is not None
                        and hasattr(self._client, 'unregister_tick_callback')):
                    self._client.unregister_tick_callback(
                        stream.asset, stream.tick_callback)
                    stream.tick_callback = None
            except Exception as _e:
                print(f"[silent-except] feed.py:4189 {type(_e).__name__}: {_e}")
                pass
            try:
                sub_client_id = getattr(stream, '_sub_client_id', None)
                current_client_id = id(self._client) if self._client else None
                sub_matches_current = (sub_client_id is None) or (sub_client_id == current_client_id)
                if (self._client and stream.sub_started
                        and not getattr(stream, '_evicting', False)
                        and sub_matches_current):
                    await self._client.stop_candles_stream(stream.asset)
            except Exception as _e:
                print(f"[silent-except] feed.py:4210 {type(_e).__name__}: {_e}")
                pass
            if stream.always_on:
                stream._needs_restart = True
                print(f"[feed] always_on stream {key} crashed — preserved for watchdog")
            elif self._streams.get(key) is stream:
                self._streams.pop(key, None)
            print(f"[feed] stream {key} stopped")

    async def _rearm_stream(self, stream: _AssetStream) -> None:
        """After our own full client rebuild only — native reconnects"""
        async with self._new_stream_gate:
            try:
                if self._client:
                    await self._client.start_candles_stream(stream.asset, stream.period)
                    stream.sub_started = True
                    stream._sub_client_id = id(self._client)
                    stream.last_real_tick_wall = time.time()
                    stream.tick_callback = None
                    try:
                        while not stream.tick_queue.empty():
                            stream.tick_queue.get_nowait()
                    except Exception as _e:
                        print(f"[silent-except] feed.py:4264 {type(_e).__name__}: {_e}")
                        pass
                    if hasattr(self._client, 'register_tick_callback'):
                        _loop = asyncio.get_running_loop()
                        def _on_tick(tick_dict, _stream=stream, _loop=_loop):
                            try:
                                _loop.call_soon_threadsafe(
                                    _stream.tick_queue.put_nowait, tick_dict)
                            except Exception as _e:
                                print(f"[silent-except] feed.py:4272 {type(_e).__name__}: {_e}")
                                pass
                        self._client.register_tick_callback(stream.asset, _on_tick)
                        stream.tick_callback = _on_tick
            except Exception:
                self._record_stream_error()
            await asyncio.sleep(self._stagger_gap)

    async def _rebuild_client(self) -> None:
        for s in list(self._streams.values()):
            await self._broadcast({"type": "stale", "asset": s.asset, "period": s.period})
        try:
            if self._client:
                await self._client.close()
        except Exception as _e:
            print(f"[silent-except] feed.py:4291 {type(_e).__name__}: {_e}")
            pass
        self._client, self._connected = None, False
        self._connected_event.clear()
        self._record_stream_error()

    def _reconcile_always_on(self) -> None:
        """Keep eligible forex pairs running as ALWAYS-ON 1m streams."""
        eligible_all = set()
        for p in self._pairs_list:
            is_curated_otc = p["asset"].endswith("_otc")
            is_live_real = (not is_curated_otc) and p["status"] == "live" and not p.get("locked")
            if not (is_curated_otc or is_live_real):
                continue
            if is_curated_otc:
                eligible_all.add((p["asset"], 60))
            elif not p.get("locked"):
                # Real pair — eligible only if not locked.
                eligible_all.add((p["asset"], 60))
        otc_assets = {p["asset"] for p in self._pairs_list
                      if p["asset"].endswith("_otc")}
        _MAX = MAX_ALWAYS_ON_STREAMS  # module-level constant
        _prioritized = []
        for key in eligible_all:
            asset = key[0]
            if asset in otc_assets:
                priority = 0  # OTC — always tradeable, highest priority
            else:
                priority = 1  # real pair
            pair_info = next((p for p in self._pairs_list if p["asset"] == asset), {})
            payout = pair_info.get("payout") or 0
            _prioritized.append((key, priority, -payout))
        _prioritized.sort(key=lambda x: (x[1], x[2]))
        eligible = {item[0] for item in _prioritized[:_MAX]}
        for key, s in list(self._streams.items()):
            if s.always_on and key not in eligible:
                s.always_on = False
        for key in eligible:
            s = self._streams.get(key)
            if s is None:
                asset, period = key
                s = _AssetStream(asset=asset, period=period, always_on=True)
                self._streams[key] = s
                s.task = asyncio.create_task(self._run_stream(s))
            else:
                s.always_on = True
                s.idle_since = None

    async def _watchdog_always_on(self) -> None:
        """Restart dead always_on streams. CRITICAL for Railway deployment."""
        eligible_assets = set()
        for p in self._pairs_list:
            is_curated_otc = p["asset"].endswith("_otc")
            is_live_real = (not is_curated_otc) and p["status"] == "live" and not p.get("locked")
            if not (is_curated_otc or is_live_real):
                continue
            if is_curated_otc:
                eligible_assets.add(p["asset"])
            elif not p.get("locked"):
                # Real pair — eligible only if not locked.
                eligible_assets.add(p["asset"])
        for asset in eligible_assets:
            key = (asset, 60)  # always_on is always 1m
            stream = self._streams.get(key)
            if stream is None:
                try:
                    s = _AssetStream(asset=asset, period=60, always_on=True)
                    self._streams[key] = s
                    s.task = asyncio.create_task(self._run_stream(s))
                    print(f"[feed] watchdog: created always_on stream for {asset}")
                except Exception as exc:
                    print(f"[feed] watchdog: FAILED to create {asset}: {exc}")
                continue
            # Stream exists — check if its task is alive.
            task = stream.task
            if task is None or task.done():
                if task is not None and not task.cancelled():
                    try:
                        exc = task.exception()
                        if exc:
                            print(f"[feed] watchdog: stream {asset} died with "
                                  f"{type(exc).__name__}: {exc}. Restarting.")
                        else:
                            print(f"[feed] watchdog: stream {asset} task completed "
                                  f"unexpectedly. Restarting.")
                    except asyncio.InvalidStateError as _e:
                        print(f"[silent-except] feed.py:4421 {type(_e).__name__}: {_e}")
                        pass
                else:
                    print(f"[feed] watchdog: stream {asset} task was cancelled. Restarting.")
                old_candles = stream.candles
                old_ticks = list(stream.ticks) if stream.ticks else []
                old_pred = stream.prediction
                old_open_time = stream.candle_open_time
                old_open_price = stream.candle_open_price
                old_open_is_real = stream.candle_open_is_real
                new_stream = _AssetStream(
                    asset=asset, period=60, always_on=True,
                    candles=old_candles,
                    candle_open_time=old_open_time,
                    candle_open_price=old_open_price,
                    candle_open_is_real=old_open_is_real,
                )
                new_stream.ticks.extend(old_ticks)
                new_stream.prediction = old_pred
                new_stream.idle_since = None
                new_stream._consecutive_losses = getattr(stream, '_consecutive_losses', 0)
                new_stream._loss_cooldown_until = getattr(stream, '_loss_cooldown_until', 0)
                new_stream.zone_streak = dict(getattr(stream, 'zone_streak',
                                                       {"regime": None, "zone": None, "losses": 0}))
                new_stream.cached_accuracy = getattr(stream, 'cached_accuracy', (None, 0))
                new_stream.payout = getattr(stream, 'payout', None)
                new_stream._last_payout_refresh = getattr(stream, '_last_payout_refresh', 0.0)
                new_stream.interested_cids = set(stream.interested_cids)
                stream._evicting = True
                new_stream._evicting = False
                # Replace in registry
                self._streams[key] = new_stream
                new_stream.task = asyncio.create_task(self._run_stream(new_stream))
        for key, s in list(self._streams.items()):
            if s.always_on and s.asset not in eligible_assets:
                s.always_on = False
                print(f"[feed] watchdog: demoted {s.asset} (no longer eligible)")
        _per_stream_stale = PER_STREAM_STALE_SECS
        now = time.time()
        _checked = 0
        _stale = 0
        for key, s in list(self._streams.items()):
            if getattr(s, '_evicting', False) or not s.sub_started:
                continue
            if not s.last_real_tick_wall:
                continue
            _checked += 1
            age = now - s.last_real_tick_wall
            if age > _per_stream_stale:
                _stale += 1
                print(f"[feed] per-stream stale: {s.asset}@{s.period}s "
                      f"no tick for {age:.0f}s — re-arming subscription")
                try:
                    asyncio.create_task(self._rearm_stream(s))
                    s.last_real_tick_wall = now
                except Exception as exc:
                    print(f"[feed] per-stream re-arm failed for {s.asset}: {exc}")
        if _checked >= 3 and _stale / _checked >= 0.8:
            _alerts.all_streams_stale(_stale, _checked)

    async def _sweep_idle_streams(self) -> None:
        """Evict streams with no interested viewers for > IDLE_TIMEOUT."""
        now = time.time()
        for key, s in list(self._streams.items()):
            if s.always_on:
                continue
            if getattr(s, '_evicting', False):
                continue   # already being torn down
            if s.interested_cids:
                s.idle_since = None
                continue
            if s.idle_since is None:
                s.idle_since = now
            elif now - s.idle_since > IDLE_TIMEOUT:
                print(f"[feed] evicting idle stream {key} "
                      f"(no viewers for {IDLE_TIMEOUT}s)")
                s._evicting = True
                if s.task:
                    s.task.cancel()
                    try:
                        await asyncio.wait_for(s.task, timeout=1.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError) as _e:
                        print(f"[silent-except] feed.py:4552 {type(_e).__name__}: {_e}")
                        pass
                if key in self._streams and self._streams[key] is s:
                    self._streams.pop(key, None)

    def _record_stream_error(self, error_msg: str = None) -> None:
        """Rolling error window -> temporary cooldown on starting NEW streams."""
        now = time.time()
        self._recent_errors.append(now)
        self._recent_errors[:] = [t for t in self._recent_errors if t > now - ERROR_WINDOW]
        if error_msg:
            self._last_error = error_msg[:500]
            self._last_error_time = now
        if len(self._recent_errors) >= ERROR_THRESHOLD and now >= self._cooldown_until:
            self._cooldown_until  = now + ERROR_COOLDOWN
            self._cooldown_reason = "connection errors"
            print(f"[feed] error spike ({len(self._recent_errors)}/{ERROR_WINDOW}s) — "
                  f"cooling down new streams for {ERROR_COOLDOWN}s")

    async def _auto_login_startup(self) -> None:
        """Startup: check for existing token in session.json (fast path)."""
        try:
            from quotex_ws import QuotexWSClient
            sess_data = QuotexWSClient.load_session_json()
            if sess_data and sess_data.get("token"):
                token = sess_data["token"]
                print(f"[feed] startup: found saved token in session.json "
                      f"({token[:8]}...) — will try it first")
                os.environ["QX_TOKEN"] = token
                return
        except Exception as _e:
            print(f"[silent-except] feed.py:4600 {type(_e).__name__}: {_e}")
            pass
        email = os.environ.get("QX_EMAIL", "").strip()
        if email:
            print(f"[feed] startup: no saved token — will login with "
                  f"email/password ({email[:3]}***@{email.split('@')[-1]})")
            print("[feed]   vendored pyquotex + Firefox TLS will handle login")
        else:
            print("[feed] startup: no token AND no QX_EMAIL/QX_PASSWORD set")
            print("[feed]   Set QX_EMAIL + QX_PASSWORD in Railway Variables")

    async def _auto_relogin(self) -> bool:
        """Re-login after connection failure."""
        if os.environ.get("QX_TOKEN"):
            print("[feed] auto-relogin: keeping QX_TOKEN (manual-token mode) "
                  "— backoff will retry; push a fresh token via /api/set-token "
                  "if the current one is dead.")
        else:
            print("[feed] auto-relogin: no QX_TOKEN set — waiting for one via "
                  "Railway Variables or POST /api/set-token {\"token\":\"...\"}.")
        return False

    async def run(self, broadcast) -> None:
        _orig_broadcast = broadcast
        async def _safe_broadcast(msg):
            if _orig_broadcast is None:
                return
            try:
                await _orig_broadcast(msg)
            except Exception as _e:
                print(f"[feed] broadcast error (non-fatal, type={msg.get('type','?')}): {_e}")
        self._broadcast = _safe_broadcast
        _db.init()          # create DB tables if not exist
        _db.cleanup()       # prune rows older than 7 days
        try:
            self._manager_task = asyncio.current_task()
        except Exception:
            self._manager_task = None
        self._abandoned = False
        self._sim_delegate = None
        self._connected_event.clear()
        self._reconnect_task = asyncio.create_task(self._aggressive_reconnect())
        env_token_check = os.environ.get("QX_TOKEN", "").strip()
        if env_token_check:
            print(f"[feed] startup: QX_TOKEN set (…{env_token_check[-4:]}) "
                  f"— will use it to connect.")
        else:
            try:
                from quotex_ws import QuotexWSClient
                sess_data = QuotexWSClient.load_session_json()
                if sess_data and sess_data.get("token"):
                    saved = sess_data["token"]
                    print(f"[feed] startup: found saved token in session.json "
                          f"(…{saved[-4:]}) — will use it.")
                    os.environ["QX_TOKEN"] = saved
                else:
                    print("[feed] startup: no QX_TOKEN set and session.json "
                          "empty — push a token via:")
                    print("[feed]   POST /api/set-token {\"token\":\"...\"}")
                    print("[feed]   or set QX_TOKEN in Railway Variables.")
            except Exception as _e:
                print(f"[feed] startup: no QX_TOKEN, session.json check failed: {_e}")
                print("[feed]   Push a token via POST /api/set-token "
                      "{\"token\":\"...\"}")
        _last_watchdog_run = 0.0
        while True:
            if self._abandoned:
                print("[feed] run() exiting — sim feed has taken over")
                return
            try:
                if not self._connected:
                    print("[feed] connecting...")
                    self._connected = await self._connect()
                    if not self._connected:
                        if self._reconnect_attempts % 3 == 2:  # every 3rd fail
                            print("[feed] ── auto-relogin attempt ────────────────")
                            relogin_ok = await self._auto_relogin()
                            if relogin_ok:
                                self._reconnect_attempts = 0
                                continue
                        self._reconnect_attempts += 1
                        delay = min(60 * (2 ** min(self._reconnect_attempts - 1, 2)), 120)
                        print(f"[feed] reconnect attempt {self._reconnect_attempts} "
                              f"failed — retrying in {delay}s "
                              f"(gentle backoff — Quotex blocks aggressive retries)")
                        print(f"[feed]   to push a fresh token NOW without waiting, "
                              f"call: POST /api/set-token {{\"token\":\"...\"}}")
                        self._record_stream_error()
                        await asyncio.sleep(delay)
                        continue
                    self._reconnect_attempts = 0          # reset on success
                    print("[feed] connected OK")
                    self._connected_event.set()
                    await self._load_pairs(broadcast)
                    for stream in list(self._streams.values()):
                        stream.sub_started = False
                        asyncio.create_task(self._rearm_stream(stream))
                    self._reconcile_always_on()
                    try:
                        asyncio.create_task(self._watchdog_always_on())
                    except Exception as _wd_err:
                        print(f"[feed] immediate watchdog post-connect failed: {_wd_err}")
                if self._streams:
                    newest = max((s.last_real_tick_wall
                                 for s in self._streams.values()), default=0.0)
                    if newest > 0 and time.time() - newest > GLOBAL_STALE_SECS:
                        print(f"[feed] GLOBAL STALE: every active stream silent "
                              f"for {time.time() - newest:.0f}s — rebuilding client")
                        await self._rebuild_client()
                await self._sweep_idle_streams()
                if time.time() - _last_watchdog_run > WATCHDOG_INTERVAL:
                    _last_watchdog_run = time.time()
                    try:
                        await self._watchdog_always_on()
                    except Exception as exc:
                        print(f"[feed] watchdog error: {exc}")
                if time.time() - self._last_pairs_refresh > 300:
                    await self._load_pairs(broadcast)
                    self._reconcile_always_on()
                if time.time() - self._last_db_cleanup > 6 * 3600:
                    self._last_db_cleanup = time.time()
                    try:
                        await asyncio.to_thread(_db.cleanup)
                    except Exception as exc:
                        print(f"[feed] periodic db.cleanup() failed: {exc}")
            except Exception as exc:
                import traceback
                print(f"[feed] manager loop error: {exc}")
                traceback.print_exc()
            await asyncio.sleep(HOUSEKEEP_SECS)
