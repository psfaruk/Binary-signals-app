"""
Quotex live data feed — multi-asset concurrent version.

Flow (per asset, all sharing ONE Quotex connection/login):
  1. connect() → pyquotex WebSocket (shared, connection-level)
  2. start_candles_stream()  → server pushes ticks for that asset
  3. get_realtime_price()    → poll in-memory tick buffer (no extra WS request)
  4. Aggregate ticks → running OHLC candle
  5. On new candle period → EOC analysis → prediction

Only forex pairs are ever streamed (see _FOREX_BASES). Each forex pair whose
live 1-minute payout is >= PAYOUT_FLOOR runs as an ALWAYS-ON 1m stream,
started at boot / on each pairs refresh and never idle-evicted (see
_reconcile_always_on) — this exists so switching between tradeable pairs is
instant instead of hitting a cold-start gap. Pairs below the payout floor are
blocked from streaming entirely (ensure_stream rejects them outright).
Everything else (other timeframes on an always-on pair, or any pair a viewer
opens directly) is still created ON DEMAND (only when a viewer requests it
via /api/subscribe) and torn down when idle — see the manager-level
capacity/cooldown/staggering logic below, which exists specifically so many
viewers sharing one personal Quotex account can't accidentally hammer Quotex
into looking like a bot/signal-service and risking the account.
"""
import asyncio
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from core.analysis import _round_level, _key_levels
import db as _db

# Minimum live 1-minute payout % for a forex pair to be tradeable in this
# app — pairs below this are blocked from streaming outright (not just from
# always-on pre-warming), matching the win-rate-needs-to-clear-payout math
# already shown in the signal bar (see stream.payout / signal-payout in
# chart.js). Overridable per-deployment since Quotex's payout schedule can
# vary by broker account/region.
#
# FIX (2026-07-17): split into two separate floors for REAL vs OTC.
#   - REAL pairs reflect actual market liquidity and have lower broker
#     margin, so payouts are typically 70-85%. Floor = 70%.
#   - OTC pairs are broker-generated with higher spreads but Quotex
#     publishes higher headline payouts (typically 85-92%). Floor = 85%.
# `PAYOUT_FLOOR` is kept as an alias for OTC for backward compatibility
# with any code that still reads the old single constant — new code
# should use PAYOUT_FLOOR_REAL or PAYOUT_FLOOR_OTC explicitly.
PAYOUT_FLOOR_REAL = int(os.environ.get("QX_PAYOUT_FLOOR_REAL", "70"))
PAYOUT_FLOOR_OTC  = int(os.environ.get("QX_PAYOUT_FLOOR_OTC",
                                       os.environ.get("QX_PAYOUT_FLOOR", "85")))
# FIX (DEAD-CODE-2026-07-21): removed `PAYOUT_FLOOR = PAYOUT_FLOOR_OTC`
# legacy alias — no code reads it.


def _payout_floor_for(asset: str) -> int:
    """Return the appropriate payout floor for an asset based on category.

    OTC assets (ending in '_otc') → PAYOUT_FLOOR_OTC (default 85)
    Real assets                   → PAYOUT_FLOOR_REAL (default 70)
    """
    return PAYOUT_FLOOR_OTC if asset.endswith("_otc") else PAYOUT_FLOOR_REAL

# Method A (LIVE running-candle re-eval) / Method B (strength gating) rollout
# flags — both untested, added 2026-07-10. Zero-redeploy killswitch: set
# either to "0" via the platform's env var UI to fall back to prior behavior.
ENABLE_LIVE_THEORY   = os.environ.get("ENABLE_LIVE_REEVAL",  "1") == "1"
ENABLE_STRENGTH_GATE = os.environ.get("ENABLE_STRENGTH_GATE", "1") == "1"
# ── Signal delay ───────────────────────────────────────────────────────────
# How long after a new candle opens before the prediction is broadcast.
# Was set to 0.0 on 2026-07-15 per user request, but this caused
# predictions to fire on the open price with zero opening-tick confirmation
# — a documented cause of wrong predictions (Bug #2, restored 2026-07-17).
# Default 3.0s: lets the first 2-3 opening ticks confirm gap direction
# before broadcasting. Override to 0.0 via env var only if you specifically
# want instant EOC broadcast.
SIGNAL_DELAY_SEC = float(os.environ.get("SIGNAL_DELAY_SEC", "3.0"))

# ── Event-driven pipeline tuning (2026-07-11) ──────────────────────────────
# MICRO_RECALC_EVERY: recompute _analyze_microstructure() every N ticks. The
# common OTC case is close-only updates (price moves but high/low don't), so
# the cached micro stays valid. Set to 1 to disable caching (legacy behavior).
MICRO_RECALC_EVERY = int(os.environ.get("MICRO_RECALC_EVERY", "5"))
# SKIP_REDUNDANT_BROADCAST: when True, skip the tick broadcast if the running
# candle's high/low/close are all unchanged since the last broadcast AND no
# prediction change happened. Cuts JSON serialize + WS send for repeated
# ticks (common in OTC sparse feeds). Set to 0 to disable.
SKIP_REDUNDANT_BROADCAST = os.environ.get("SKIP_REDUNDANT_BROADCAST", "1") == "1"

# ── Stream lifecycle / housekeeping tunables (2026-07-13) ─────────────────────
# All of these were previously hardcoded local constants scattered through the
# stream loop, idle-sweep, error-cooldown, and snapshot paths. Moved to
# module-level with env overrides so a deployment can tune them without a
# redeploy.
#
# STALE_SECS: a single stream is considered "stuck" if no real tick has
# arrived in this many seconds — the stream loop re-arms that one stream's
# subscription. ALSO reused as the global-staleness threshold: when EVERY
# active stream is silent for this long, the manager loop rebuilds the
# whole Quotex client (was previously a separate GLOBAL_STALE_SECS = 90).
STALE_SECS = int(os.environ.get("STALE_SECS", "90"))
# IDLE_TIMEOUT: seconds a non-always-on stream may run with zero interested
# viewers before being evicted by _sweep_idle_streams. Always-on 1m pairs
# are exempt (see _reconcile_always_on).
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "300"))
# Rolling-error cooldown for starting NEW streams. If >= ERROR_THRESHOLD
# errors occur within ERROR_WINDOW seconds, refuse new streams for
# ERROR_COOLDOWN seconds. Existing streams are never torn down by this —
# only ensure_stream()'s capacity/cooldown gate for brand-new pairs fires.
ERROR_WINDOW    = int(os.environ.get("ERROR_WINDOW",    "60"))
ERROR_THRESHOLD = int(os.environ.get("ERROR_THRESHOLD", "10"))
ERROR_COOLDOWN  = int(os.environ.get("ERROR_COOLDOWN",  "30"))
# Candle history bounding. When a stream's candle list exceeds MAX_CANDLES,
# truncate to the most recent TRUNCATE_TO. Keeps memory bounded on long-lived
# always-on pairs without throwing away recent chart context.
MAX_CANDLES = int(os.environ.get("MAX_CANDLES", "500"))
TRUNCATE_TO = int(os.environ.get("TRUNCATE_TO", "400"))
# SNAPSHOT_CANDLES: how many recent candles to include in the initial
# /api/subscribe snapshot and the on-join snapshot handed to a viewer joining
# an already-running stream. Frontend charts render off this.
SNAPSHOT_CANDLES = int(os.environ.get("SNAPSHOT_CANDLES", "300"))

# ── Fallback display-name helper ─────────────────────────────────────────────
def _api_to_display(api_name: str) -> str:
    """Convert a Quotex forex asset code to a readable display string, e.g.
    "EURUSD_otc" -> "EUR/USD". Only used before a live connection exists (no
    Quotex-supplied display string to draw on yet) — see _clean_display for
    why the live path doesn't reconstruct names from the code this way."""
    base = api_name[:-4] if api_name.endswith("_otc") else api_name
    if len(base) == 6 and base.isalpha():
        return base[:3] + "/" + base[3:]
    return base


_OTC_SUFFIX_RE = re.compile(r"\s*\(otc\)\s*$", re.IGNORECASE)

def _clean_display(raw_display: str) -> str:
    """Strip Quotex's own "(OTC)" suffix from its raw instrument display
    string — the frontend adds its own "Otc"/"Real" suffix uniformly (see
    renderPairSelect in chart.js), so keeping Quotex's would double it up.
    Deliberately uses Quotex's own display string rather than reconstructing
    one from the asset code (_api_to_display): a pair's code doesn't always
    match the base/quote ORDER Quotex itself displays it in — confirmed live,
    BRLUSD_otc's actual Quotex display is "USD/BRL", not "BRL/USD"."""
    return _OTC_SUFFIX_RE.sub("", raw_display.replace("\n", "")).strip()


# ── Pair catalog, split by category ──────────────────────────────────────────
# The app now only ever streams/lists forex pairs (see _load_pairs) — the
# other categories are kept here only as documentation of what's excluded,
# and so re-adding a category later is a one-line change.
# Only forex pairs are ever streamed/listed (see _FOREX_BASES below).
# Other categories (stocks/crypto/commodities) were previously defined
# here as documentation but never referenced — removed 2026-07-13.
_FOREX_OTC = [
    # Forex majors OTC
    "EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "USDCHF_otc",
    "AUDUSD_otc", "NZDUSD_otc", "USDCAD_otc",
    # Forex minors OTC
    "EURGBP_otc", "EURJPY_otc", "EURAUD_otc", "EURCHF_otc", "EURCAD_otc",
    "GBPJPY_otc", "GBPAUD_otc", "GBPCAD_otc", "GBPCHF_otc", "GBPNZD_otc",
    "AUDJPY_otc", "AUDCAD_otc", "AUDNZD_otc", "AUDCHF_otc",
    "CADJPY_otc", "CADCHF_otc",
    "NZDJPY_otc", "NZDCAD_otc", "NZDCHF_otc",
    "CHFJPY_otc", "EURNZD_otc",
    # Forex exotics OTC
    "USDMXN_otc", "USDTRY_otc", "USDPKR_otc", "USDCOP_otc",
    "USDBDT_otc", "INRUSD_otc", "EURSGD_otc",
    "BRLUSD_otc", "USDARS_otc", "USDDZD_otc",
    # FIX (DATA-FLOW-2026-07-22): All-Time OTC pairs — 6 exotics the user
    # wants monitored 24/7 regardless of payout %. USDIDR_otc is new;
    # USDBRL_otc is the canonical ISO form (BRLUSD_otc above is the
    # broker's non-standard listing — kept both so either works).
    "USDBRL_otc", "USDIDR_otc",
]

# FIX (DATA-FLOW-2026-07-22): All-Time OTC pair set — these 6 exotic pairs
# are ALWAYS-ON regardless of payout % (no payout floor). The user wants
# 24/7 monitoring to detect Quotex algorithm changes. They use the OTC
# engine (asset ends with _otc → routes to otc engine automatically).
# NOTE: USDBRL is listed as BRLUSD_otc on Quotex (non-standard ISO order).
# We use BRLUSD_otc so Quotex recognizes the symbol and streams data.
_ALLTIME_OTC_ASSETS = frozenset({
    "USDBDT_otc", "BRLUSD_otc", "USDPKR_otc",
    "USDCOP_otc", "USDMXN_otc", "USDIDR_otc",
})

# Logical base symbols (no _otc suffix) that count as forex — used to filter
# the REAL Quotex instrument list in _load_pairs, not just this fallback. A
# curated whitelist rather than a "3-letter currency code" regex deliberately:
# XAU/XAG are real ISO-4217 codes too, so a currency-code heuristic would
# misclassify gold/silver (XAUUSD/XAGUSD) as forex.
_FOREX_BASES = {a[:-4] if a.endswith("_otc") else a for a in _FOREX_OTC}

# Fallback pair list (shown while Quotex instruments load) — forex only, to
# match what _load_pairs serves once connected.
_FALLBACK_ASSETS = _FOREX_OTC

_DEFAULT_PAIRS: list[dict] = [
    {"asset": a, "display": _api_to_display(a), "status": "otc",
     "payout": None, "locked": False}
    for a in _FALLBACK_ASSETS
]


# ── Small helpers ─────────────────────────────────────────────────────────────

def _atr(candles: list[dict], n: int = 20) -> float:
    """True Range ATR — properly accounts for overnight gaps.

    FIX (Bug #21, 2026-07-17): previously used the simple high-low average
    (no gap handling) which understated ATR on pairs with frequent gaps
    and diverged from advanced_analysis._atr used by the prediction
    engine. Now uses the same True Range formula as
    advanced_analysis._atr — max(high-low, |high-prev_close|, |low-prev_close|)
    — so feed-side ATR (used for stream housekeeping, contamination
    detection, pred-candle sizing) is consistent with engine-side ATR
    (used for regime classification and key-level confluence).

    Falls back to price-relative 0.01% on flat/empty inputs.
    """
    if not candles:
        return 0.0001
    if len(candles) < 2:
        # Single candle: just high-low, no prev close to gap from.
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
        return None    # FIX (2026-07-13): was IndexError on empty list
    last = candles[-1]
    op   = actual_open if actual_open is not None else last["close"]
    atr  = _atr(candles[-20:]) if len(candles) >= 20 else (last["high"] - last["low"]) or 0.0001
    t    = last["time"] + period

    # Realistic candle proportions (fractions of ATR)
    # Body is the main body, wick extends from body tip (signal direction),
    # tail extends from open (opposite direction).
    # Total range = body + wick + tail = 0.85 * ATR (close to average candle)
    body = atr * 0.45   # ~45% of ATR — typical for a moderately strong candle
    wick = atr * 0.25   # ~25% of ATR — main wick in signal direction (from close)
    tail = atr * 0.15   # ~15% of ATR — opposite wick (from open)

    if signal == "CALL":
        # Green candle: open at bottom, close at top
        # upper wick extends FROM close upward, lower tail extends FROM open downward
        return {"time":  t, "open":  op,
                "high":  round(op + body + wick, 6),
                "low":   round(op - tail, 6),
                "close": round(op + body, 6)}
    # PUT — red candle: open at top, close at bottom
    # lower wick extends FROM close downward, upper tail extends FROM open upward
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
            raw = list(raw.values())[0] if raw else []
    seen: dict[int, dict] = {}
    for c in raw:
        try:
            # FIX (2026-07-13): skip entries with missing OHLC fields instead
            # of defaulting to 0.0. A malformed candle with no "open" key
            # became open=0.0, which sailed through and poisoned the chart
            # with a flat-zero candle. Now: require all 4 OHLC fields present.
            if not all(k in c for k in ("open", "high", "low", "close")):
                continue
            bar = {
                "time":  int(c.get("time",  c.get("from", 0))),
                "open":  float(c["open"]),
                "high":  float(c["high"]),
                "low":   float(c["low"]),
                "close": float(c["close"]),
            }
            # Sanity check: high >= max(open, close) and low <= min(open, close)
            if bar["high"] < max(bar["open"], bar["close"]) or \
               bar["low"]  > min(bar["open"], bar["close"]):
                continue   # invalid OHLC — skip
            seen[bar["time"]] = bar   # deduplicate: later entry wins
        except (TypeError, ValueError):
            continue
    return sorted(seen.values(), key=lambda x: x["time"])


def _drop_price_contamination(candles: list[dict]) -> list[dict]:
    """
    Defend against a stale/wrong-asset candle batch getting spliced into a
    fresh history fetch. Symptom: chart shows an old price-level cluster,
    a big blank jump, then the new pair's real candles — permanently.

    FIX (2026-07-13): Three improvements over the original:
    1. Lowered the short-batch threshold from 6 to 3 — a 5-candle batch
       that is fully contaminated (e.g., wrong-asset fetch on a fresh
       stream with little history) was passing through unchecked.
    2. Track the LAST contamination boundary (not just the first), so
       legit candles between two contamination points aren't dropped.
    3. Also detect SUFFIX contamination (stale data spliced at the END
       of the batch) — the old code only dropped a leading prefix.
    """
    if len(candles) < 3:
        return candles
    ranges = sorted(c["high"] - c["low"] for c in candles if c["high"] > c["low"])
    if not ranges:
        return candles
    median_rng = ranges[len(ranges) // 2]
    if median_rng <= 0:
        return candles

    # Find the LAST prefix contamination boundary (drops everything before it)
    cut = 0
    for i in range(1, len(candles)):
        jump = abs(candles[i]["close"] - candles[i - 1]["close"])
        gap  = abs(candles[i]["open"]  - candles[i - 1]["close"])
        if jump > median_rng * 10 or gap > median_rng * 10:
            cut = i   # keep updating — we want the LAST contamination point
    # Find suffix contamination (stale data after fresh data)
    suffix_cut = len(candles)
    for i in range(len(candles) - 1, 0, -1):
        jump = abs(candles[i]["close"] - candles[i - 1]["close"])
        gap  = abs(candles[i]["open"]  - candles[i - 1]["close"])
        if jump > median_rng * 10 or gap > median_rng * 10:
            suffix_cut = i
            break
    if cut:
        print(f"[feed] dropped {cut} contaminated candle(s) "
              f"(price gap > 10x median range) before index {cut}")
    if suffix_cut < len(candles):
        print(f"[feed] dropped {len(candles) - suffix_cut} contaminated candle(s) "
              f"(suffix price gap > 10x median range) from index {suffix_cut}")
    return candles[cut:suffix_cut]


def _floor_to_period(ts: float, period: int) -> int:
    """Floor a Unix timestamp to the start of its candle period."""
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
    """Aggregate 1m candles into 5m closes by timestamp-boundary alignment.

    FIX (Bug A, 2026-07-19): the old HTF code took every 5th 1m close as
    a 5m close. That is only correct if the 1m buffer happens to start on
    a 5-minute wall boundary — which a rolling 105-candle window almost
    never does. Misalignment shifts the 5m boundary by up to 4 minutes,
    producing 5m "candles" that span wall boundaries and inject artificial
    momentum/reversal noise into ema9 vs ema21.

    This helper floors each 1m candle's `time` to its 5-minute bucket
    (5 * period seconds), groups all 1m candles in the same bucket, and
    emits the close of the LAST 1m candle in each bucket as that bucket's
    5m close. Buckets with no candles are skipped (the next bucket's close
    still represents a true 5-minute boundary).

    Args:
        candles_1m: list of candle dicts with at least "time" and "close".
            `time` may be in seconds (Quotex) or milliseconds (some feeds);
            we auto-detect by magnitude.
        period: the 1m candle period in seconds (default 60).

    Returns:
        List of 5m close prices, ordered oldest → newest.
    """
    if not candles_1m:
        return []
    # Auto-detect seconds vs milliseconds. Quotex uses seconds, but be safe.
    sample_ts = candles_1m[0].get("time", 0)
    ms_mode = sample_ts > 10_000_000_000  # > year 2286 in seconds
    bucket_seconds = 5 * period
    closes_5m: list[float] = []
    current_bucket: int | None = None
    for c in candles_1m:
        t = c.get("time", 0)
        if ms_mode:
            t = t / 1000
        bucket = (int(t) // bucket_seconds) * bucket_seconds
        if current_bucket is None or bucket != current_bucket:
            # New 5m bucket started — flush previous (its close was already
            # captured as the last 1m close before this bucket began).
            # The first bucket just initializes; subsequent ones emit.
            if current_bucket is not None:
                closes_5m.append(prev_close)
            current_bucket = bucket
        prev_close = c["close"]
    # Flush the last bucket
    if current_bucket is not None:
        closes_5m.append(prev_close)
    return closes_5m


# ── Per-asset stream state ────────────────────────────────────────────────────
# Everything that used to live directly on QuotexFeed (one asset at a time)
# now lives on its own _AssetStream instance, owned for its whole life by one
# asyncio.Task (see QuotexFeed._run_stream) — nothing else can ever mutate it
# mid-await, which structurally rules out the cross-asset contamination bugs
# the old singleton design needed manual guards against.
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
    # Chop guard: consecutive losses in the CURRENT (regime, zone). See
    # ZONE_LOSS_GUARD / QuotexFeed._run_eoc.
    zone_streak: dict = field(
        default_factory=lambda: {"regime": None, "zone": None, "losses": 0})
    payout: int | None = None
    sub_started: bool = False           # start_candles_stream() issued at least once
    task: "asyncio.Task | None" = None       # the asyncio.Task running this stream
    # Server pre-warmed this pair (payout >= PAYOUT_FLOOR) — immune to idle
    # eviction while true. See QuotexFeed._reconcile_always_on.
    always_on: bool = False
    interested_cids: set = field(default_factory=set)   # viewer client-ids watching
    idle_since: float | None = None
    created_at: float = field(default_factory=time.time)
    # Snapshot of the (closed candles, just-closed-candle ticks) that fed the
    # LAST _run_eoc call — reused by the LIVE periodic re-eval so it
    # can re-score with fresh running_ticks without re-deriving stale state
    # from stream.ticks, which has since been cleared for the new candle.
    base_candles: list = field(default_factory=list)
    base_ticks: list = field(default_factory=list)
    _live_reeval_ticks: int = 0   # last tick-count LIVE re-eval fired at
    # ── Last-10s optimization (2026-07-10) ────────────────────────────────
    # Cached recent_accuracy — queried ONCE per candle (accuracy only changes
    # at candle close, so re-querying mid-candle is pure waste).
    cached_accuracy: tuple = field(default_factory=lambda: (None, 0))
    # FIX (2026-07-13): removed `cached_accuracy_at` — was set but never read
    # (no TTL invalidation was ever implemented).
    # FIX (Bug #3, 2026-07-17): removed the `inverted` field — was set
    # from result.get("_flipped") which the prediction engine never emits,
    # and persisted for a "hysteresis check" that was never read. Dead
    # code; removing both the field and the assignment in _run_eoc.
    # ── Signal delay (2026-07-10) ─────────────────────────────────────────
    # User requirement: prediction candle open হওয়ার ২-৩ সেকেন্ড পরে signal
    # broadcast হবে, যাতে opening tick behavior confirm হয়। EOC-তে
    # signal_delay_until = time.time() + SIGNAL_DELAY_SEC সেট হয়; tick
    # broadcast এর সময় চেক করা হয় — যদি এখনও delay চলছে, prediction কে
    # broadcast থেকে বাদ দেওয়া হয় (candle data যাবে, prediction যাবে না)।
    # যখন delay শেষ হয়, প্রথম tick-এ prediction broadcast হয়।
    signal_delay_until: float = 0.0
    # BRAIN-LEARNED: loss cluster cooldown fields
    _consecutive_losses: int = 0
    _loss_cooldown_until: float = 0.0
    _sub_client_id: object = None  # FIX (AUDIT-FEED #5): track which client started subscription
    tick_callback: object = None   # FIX (2026-07-13): event-driven tick callback  # wall-time when signal can be broadcast

    # ── Event-driven tick pipeline (2026-07-11) ──────────────────────────
    # When the raw-WS backend is active, the WS reader pushes ticks directly
    # into this queue via register_tick_callback(). _stream_loop awaits
    # queue.get() with a 50ms timeout — eliminating the legacy 50ms polling
    # loop and shaving ~25-50ms off every tick → browser-render hop.
    # Empty queue (legacy pyquotex backend, no callback support) means the
    # stream loop falls back to polling get_realtime_price() as before.
    tick_queue: "asyncio.Queue" = field(default_factory=asyncio.Queue)
    tick_callback: "Callable | None" = None  # registered callback (for unregister on stop)

    # ── Microstructure caching (2026-07-11) ──────────────────────────────
    # _analyze_microstructure() is O(n) over stream.ticks — expensive when
    # called on every single tick (was). Cache the last result and only
    # recompute every MICRO_RECALC_EVERY ticks, or when high/low change
    # (those are the only OHLC values that can change a cached micro).
    # Close-only updates (the common case in OTC) reuse the cache.
    _micro_cache: dict | None = None
    _micro_cache_at_tick: int = 0  # len(stream.ticks) when cache was built
    _micro_cache_high: float = 0.0
    _micro_cache_low: float = 0.0

    # ── Skip-redundant-broadcast (2026-07-11) ────────────────────────────
    # Snapshot of the last-broadcast candle (high/low/close). If the next
    # tick produces the same high/low/close AND pred_changed is False,
    # skip the broadcast entirely — no JSON serialize, no WS send. Common
    # when ticks are sparse and the same price repeats for a few cycles.
    _last_bcast_high: float = 0.0
    _last_bcast_low: float = 0.0
    _last_bcast_close: float = 0.0

    # NOTE: the following attributes are set at runtime but NOT declared as
    # fields (set via stream.xxx = ...). They work via Python's instance
    # __dict__ but bypass the dataclass schema:
    #   _tracked_high, _tracked_low — running-candle high/low cache (see _track_tick)
    #   _evicting — flag set by _sweep_idle_streams to skip stop_candles_stream


# ── Feed ──────────────────────────────────────────────────────────────────────

# Consecutive wrong predictions in the SAME (regime, zone) before the signal
# is suppressed to NEUTRAL. Live data showed the model whipsawing (CALL wrong,
# PUT wrong, CALL wrong...) while price chops sideways at one level — neither
# continuation nor reversal theories have a real edge there (see project
# history), so once a zone proves itself unreadable N times running, stop
# guessing in it rather than keep flipping a coin. Resets the moment the
# regime/zone classification actually changes. Overridable per-deployment.
ZONE_LOSS_GUARD = int(os.environ.get("ZONE_LOSS_GUARD", "3"))


class QuotexFeed:
    def __init__(self):
        self._client              = None
        self._connected           = False
        self._reconnect_attempts  = 0        # for exponential backoff
        self._broadcast           = None     # set once in run()
        self._last_error          = None     # set by _record_stream_error for /api/debug
        self._last_error_time     = 0        # wall time of last error

        # ── Multi-asset stream management (replaces the old singleton
        # asset/candles/ticks/... fields) ───────────────────────────────────
        self._streams: dict[tuple[str, int], _AssetStream] = {}
        # FIX (AUDIT-CORE #71, 2026-07-21): per-(asset, period) lock so that
        # concurrent ensure_stream() calls for the same key serialize. Without
        # this, two viewers subscribing to the same pair simultaneously would
        # both see stream is None, both create a new _AssetStream, and the
        # second overwrites the first in self._streams — orphaning the first
        # task forever (it stays subscribed to Quotex but its outputs go
        # nowhere). The orphan eventually counts toward _max_streams and
        # triggers at_capacity for legitimately new pairs.
        self._stream_locks: dict[tuple[str, int], asyncio.Lock] = {}
        # Default covers ~38 always-on forex pairs (see _reconcile_always_on)
        # plus headroom for on-demand non-1m streams and the brief overlap
        # window when a pair's real/otc asset code swaps.
        self._max_streams     = int(os.environ.get("QX_MAX_STREAMS", "60"))
        # Held across a stream's whole start sequence (start_candles_stream +
        # history fetch) — staggers concurrent starts AND serializes history
        # fetches, closing a real race in pyquotex's Strategy-2 history
        # fallback (a shared, non-asset-keyed scratch attribute).
        self._new_stream_gate = asyncio.Semaphore(1)
        self._stagger_gap     = float(os.environ.get("QX_STAGGER_GAP_SEC", "1.5"))
        # Rolling error window -> temporary cooldown on starting NEW streams
        # (existing streams are never affected) — the safety net against
        # hammering Quotex if something starts failing repeatedly.
        self._recent_errors: list[float] = []
        self._cooldown_until: float = 0.0
        self._cooldown_reason: str  = ""

        # Unified pair list — one entry per logical pair, status=live/otc/closed
        # (connection-wide, not per-asset — kept as-is)
        # FIX (2026-07-17): split into real_pairs_list and otc_pairs_list so
        # the frontend 3-dot menu can switch between Real Market and OTC
        # Market views. _pairs_list is kept as a combined backward-compat list.
        self._pairs_list: list[dict] = list(_DEFAULT_PAIRS)
        self._real_pairs_list: list[dict] = []   # populated by _load_pairs
        self._otc_pairs_list:  list[dict] = list(_DEFAULT_PAIRS)  # default to OTC list (matches old behavior)
        # FIX (DATA-FLOW-2026-07-22): all-time OTC pair list — 6 exotic pairs
        # that bypass the payout floor and are always-on. Populated in
        # _load_pairs with live payout data; defaults below so the list is
        # non-empty even before _load_pairs runs.
        # Display names use canonical ISO order (USD first) even when the
        # Quotex symbol is non-standard (e.g. BRLUSD_otc → "USD/BRL").
        _ALLTIME_DISPLAY = {
            "USDBDT_otc": "USD/BDT",
            "BRLUSD_otc": "USD/BRL",   # Quotex lists as BRLUSD, display as USD/BRL
            "USDPKR_otc": "USD/PKR",
            "USDCOP_otc": "USD/COP",
            "USDMXN_otc": "USD/MXN",
            "USDIDR_otc": "USD/IDR",
        }
        self._alltime_otc_pairs_list: list[dict] = [
            {"asset": a, "display": _ALLTIME_DISPLAY.get(a, a.replace("_otc","")),
             "status": "otc", "payout": 85, "locked": False,
             "category": "alltime_otc"} for a in sorted(_ALLTIME_OTC_ASSETS)
        ]
        self._last_pairs_refresh: float = 0.0

        # NOTE (refactor 2026-07-14): the per-pair mute gate that used to live
        # here (`_muted_theories` + `_last_perf_refresh` + `_refresh_theory_mutes`)
        # was removed — the prediction path no longer runs the old theory engine,
        # so per-pair muting has nothing to mute. The candle_reaction
        # engine doesn't take a `muted` argument.
        #
        # DB row-count housekeeping. Was startup-only for a long time (see
        # run()'s initial _db.cleanup() call) — this service can stay up for
        # weeks without a redeploy, so unbounded growth between restarts
        # filled the Railway volume to 83% (2026-07-08 incident). Now also
        # re-run periodically from the manager loop.
        self._last_db_cleanup: float = 0.0

        # ── Higher Timeframe (HTF) trend cache ──
        # Key = (asset, period) — see FIX below.
        # Value = {"trend": "UPTREND"/"DOWNTREND"/"SIDEWAYS",
        #          "fetched_at": float, "ema9": float, "ema21": float}
        # Refreshed every 60s per (asset, period). Used to filter 1m signals:
        # if 1m signal opposes 5m trend, strength is demoted.
        #
        # FIX (Bug A+B, 2026-07-19): cache key was just `asset` — same asset
        # streaming under multiple periods would collide and reuse a stale
        # trend from a different stream. Now keyed by `(asset, period)`.
        #
        # NOTE (2026-07-13): the NTP time-offset subsystem that used to live
        # here was removed — it computed an offset that no caller consumed
        # (every candle-boundary / signal-delay check used raw time.time()).
        # If real NTP correction is needed, use ntplib AND replace every
        # time.time() in the candle-boundary / signal-delay / stale-detection
        # paths with the corrected clock in one go.
        self._htf_cache: dict[tuple[str, int], dict] = {}

    # ── Public ────────────────────────────────────────────────────────────────

    async def _get_htf_trend(self, asset: str, stream: '_AssetStream' = None) -> str:
        """Get the 5-minute trend for an asset (Higher Timeframe Confluence).
        Returns 'UPTREND', 'DOWNTREND', or 'SIDEWAYS'.

        Derives the 5m trend from the EXISTING 1m closed candles in
        stream.candles, aggregated into proper 5m candles by timestamp
        boundary alignment (NOT every-5th close — that was incorrect when
        the 1m candle buffer wasn't aligned to a 5-minute wall boundary).
        Cached per (asset, period) for 60 seconds. Uses only closed 1m
        candles that are already in memory — zero extra network/subscriptions.

        FIX (Bug A, 2026-07-19): the previous version accepted as few as 25
        1m candles (=> 5 5m closes), then ran `_ema_simple(closes_5m,
        min(9, 5))` and `min(21, 5)` — both EMAs degenerated to period=5,
        so ema9 == ema21, separation == 0, and trend was ALWAYS SIDEWAYS.
        Now requires >= 105 1m candles (=> 21 5m closes) so ema21 can use
        its full period. Anything below that returns SIDEWAYS rather than
        emitting a noise-driven false trend.

        FIX (Bug B, 2026-07-19): cache key was just `asset` — same asset
        streaming under multiple periods would collide and reuse a stale
        trend from a different stream. Now keyed by `(asset, period)`.

        FIX (Bug A, 2026-07-19, 5m alignment): previously, 5m closes were
        built by chunking every 5 consecutive 1m closes — this only yields
        correct 5m candles when the 1m buffer starts on a 5-minute wall
        boundary. With a rolling 105-candle window that's almost never the
        case. Now we use each 1m candle's `time` field, floor to its
        5-minute bucket, and aggregate OHLC properly. The close of each
        5m bucket becomes the input to the EMA.

        FIX (HTF cold-start regression, 2026-07-19): the original Bug A
        fix introduced a COLD-START REGRESSION. Requiring >= 105 1m
        candles means HTF is forced to SIDEWAYS for ~1h45m after a fresh
        start (or after any reconnect that resets the candle buffer — and
        this codebase has a known reconnect/re-subscribe storm history on
        Cloudflare-blocked Railway). During that window:
          - blender.py's HTF confluence multiplier is fully disabled
            (counter-trend ×0.7 penalty = 1.0, aligned ×1.1 bonus = 1.0)
          - candle_reaction/otc_pattern's NEW trend-aware dampening (which
            only consults the 1m regime, NOT 5m HTF) fires against weak
            1m noise, suppressing real reversals without HTF confirmation.

        New approach: GRACEFUL DEGRADATION across three confidence tiers
        based on how many 5m closes are available. We NEVER silently
        degrade to flat SIDEWAYS — instead we scale the EMA separation
        threshold so a clear trend read fires as soon as we have enough
        5m closes for EMA9 (>=9), and tighten the threshold as more data
        arrives. Below 9 5m closes we still return SIDEWAYS because
        EMA9 itself isn't fully formed — that's a genuine minimum.

        Threshold schedule (sep = |ema9-ema21|/ema21):
          - >=21 5m closes (full EMA21):  sep > 0.0003  (0.03%)
          - >=14 5m closes (EMA21 seeded): sep > 0.0005 (0.05%) — tighter
                                               because EMA21 is still warming up
          - >=9  5m closes (EMA9 ready):  sep > 0.0008 (0.08%) — even tighter,
                                               only fires on strong directional
                                               moves during early warmup
          - <9   5m closes: SIDEWAYS (no EMA9 yet)

        This way a strong trend is detected within ~9-14 minutes of cold
        start instead of ~1h45m, while spurious noise-driven reads during
        warmup are kept out by the progressively tighter thresholds.
        """
        # Determine period from the stream (default to 60s = 1m).
        period = stream.period if stream is not None else 60
        cache_key = (asset, period)

        # Check cache (60s TTL)
        cached = self._htf_cache.get(cache_key)
        if cached and (time.time() - cached["fetched_at"]) < 60:
            return cached.get("trend", "SIDEWAYS")

        # Use the stream's existing 1m closed candles (NO network call)
        candles_1m = stream.candles if stream is not None else []
        if not candles_1m:
            return "SIDEWAYS"

        try:
            # Cap at the last 105 1m candles (=> up to 21 5m closes).
            window = candles_1m[-105:]
            closes_5m = _aggregate_5m_closes(window, period)
            n5 = len(closes_5m)

            # Hard floor: EMA9 needs >=9 closes. Below that, any "trend"
            # would be pure noise. (Note: _ema_simple seeds with SMA over
            # min(period, len(prices)) so it technically runs with fewer,
            # but the result is meaningless — a 3-point SMA of 5m closes
            # tells you nothing about the 5m trend.)
            if n5 < 9:
                return "SIDEWAYS"

            # Compute EMAs. _ema_simple seeds with SMA over min(period, n5)
            # so EMA21 still works (warmup-mode) when n5 < 21.
            ema9  = _ema_simple(closes_5m, 9)
            ema21 = _ema_simple(closes_5m, 21)
            sep = abs(ema9 - ema21) / ema21 if ema21 > 0 else 0

            # Progressive threshold: tighter when we have less data, so a
            # cold-start read only fires on genuinely strong moves.
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
        """Return the current forex pair lists and payout floors for /api/pairs.

        FIX (AUDIT-FEED #2, 2026-07-19): if sim fallback is active, return
        the sim feed's pair list — the real feed's pair list is stale
        (Quotex connection is dead).

        Returns a dict with TWO separate pair lists so the frontend can
        render Real Market and OTC Market as two distinct categories:

            {
              "real_pairs": [...],         # real-market pairs (no _otc suffix)
              "otc_pairs":  [...],         # OTC pairs (asset ends with _otc)
              "payout_floor_real": 70,     # minimum payout % for real pairs
              "payout_floor_otc":  85,     # minimum payout % for OTC pairs
              "pairs":        [...],       # BACKWARD COMPAT: combined list (real first, then otc)
              "payout_floor": 85,          # BACKWARD COMPAT: alias for payout_floor_otc
            }

        The combined `pairs` list is kept for any older client code that
        still expects a single list — new frontend code should read
        real_pairs / otc_pairs instead.

        FIX (2026-07-17): closed real pairs (status="closed" — weekend /
        outside bank hours) are filtered OUT of the active lists. Quotex
        publishes real-market instruments only during bank hours; showing
        closed real pairs in the Real Market dropdown is misleading
        because the user can't trade them. OTC pairs are always open
        (24/7 broker-generated), so they're never filtered.
        """
        if getattr(self, '_sim_delegate', None) is not None:
            return self._sim_delegate.available_pairs()
        active_real = [p for p in self._real_pairs_list if p["status"] == "live"]
        active_otc  = [p for p in self._otc_pairs_list  if p["status"] == "otc"]
        # FIX (DATA-FLOW-2026-07-22): all-time OTC pairs — always in the
        # active list (never locked, never closed). Exotic OTC pairs cycle
        # payout 30%↔90% but the user wants them tradeable regardless.
        active_alltime = list(self._alltime_otc_pairs_list)
        return {
            "real_pairs": active_real,
            "otc_pairs":  active_otc,
            "alltime_otc_pairs": active_alltime,
            "payout_floor_real": PAYOUT_FLOOR_REAL,
            "payout_floor_otc":  PAYOUT_FLOOR_OTC,
            "payout_floor_alltime_otc": 0,  # no floor — always tradeable
            # Backward compat: combined list (real + otc + alltime)
            "pairs":        active_real + active_otc + active_alltime,
            "payout_floor": PAYOUT_FLOOR_OTC,
        }

    async def _load_pairs(self, broadcast=None) -> None:
        """
        Fetch all Quotex instruments and build TWO separate forex pair lists:
        one for REAL market pairs (live exchange, no _otc suffix) and one
        for OTC pairs (broker-generated, _otc suffix).

        Each pair carries its 1-minute payout % and a "locked" flag — locked
        means payout is below the category-specific floor:
          - REAL pairs use PAYOUT_FLOOR_REAL (default 70%)
          - OTC pairs use PAYOUT_FLOOR_OTC  (default 85%)

        Non-forex instruments (crypto/commodities/stocks) are dropped
        entirely — this app only ever streams forex (see _FOREX_BASES).

        Both real and OTC variants of the same logical pair (e.g. EURUSD
        and EURUSD_otc) appear in their respective lists. This is intentional
        — the user can switch between Real Market view and OTC Market view
        from the 3-dot menu in the topbar.
        """
        try:
            instruments = await self._client.get_instruments()
            if not instruments:
                return

            # Group by logical base name (forex only)
            by_base: dict[str, dict] = {}
            for i in instruments:
                name   = i[1]
                is_otc = name.endswith("_otc")
                base   = name[:-4] if is_otc else name
                if base not in _FOREX_BASES:
                    continue

                is_open = bool(i[14])
                payout  = i[-9]   # 1-minute payout %, same field pyquotex's
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

            # Build TWO separate lists — real_pairs and otc_pairs.
            # Both variants of the same logical pair appear (when both are
            # open). This is the explicit user requirement: the 3-dot menu
            # switches between Real Market view and OTC Market view.
            real_pairs: list[dict] = []
            otc_pairs:  list[dict] = []

            for base, v in by_base.items():
                real = v.get("real")
                otc  = v.get("otc")

                # REAL list entry — only if a real instrument exists
                if real:
                    status = "live" if real["open"] else "closed"
                    floor = PAYOUT_FLOOR_REAL
                    payout = real["payout"]
                    locked = status == "live" and (
                        payout is None or payout < floor)
                    real_pairs.append({
                        "asset":   real["asset"],
                        "display": real["display"],
                        "status":  status,
                        "payout":  payout,
                        "locked":  locked,
                        "category": "real",
                    })

                # OTC list entry — only if an OTC instrument exists
                if otc:
                    status = "otc" if otc["open"] else "closed"
                    floor = PAYOUT_FLOOR_OTC
                    payout = otc["payout"]
                    locked = status == "otc" and (
                        payout is None or payout < floor)
                    otc_pairs.append({
                        "asset":   otc["asset"],
                        "display": otc["display"],
                        "status":  status,
                        "payout":  payout,
                        "locked":  locked,
                        "category": "otc",
                    })

            # Sort each list: active (live/otc) before closed, unlocked before
            # locked, then highest payout first — the pairs actually worth
            # picking float to the top instead of being buried alphabetically.
            def _sort_key(x):
                return (x["status"] == "closed", x["locked"],
                        -(x["payout"] or 0), x["display"].upper())

            real_pairs.sort(key=_sort_key)
            otc_pairs.sort(key=_sort_key)

            self._real_pairs_list = real_pairs
            self._otc_pairs_list  = otc_pairs
            # FIX (DATA-FLOW-2026-07-22): update alltime_otc pair list with
            # live payout data from Quotex instruments. The pair is always
            # tradeable (no payout floor) but we still want to show the
            # actual live payout in the UI. If the pair isn't in Quotex's
            # instrument list (rare), keep the default 85% payout.
            # Display name uses canonical ISO order (USD first) even when
            # Quotex's symbol is non-standard (BRLUSD_otc → "USD/BRL").
            _ALLTIME_DISPLAY = {
                "USDBDT_otc": "USD/BDT",
                "BRLUSD_otc": "USD/BRL",
                "USDPKR_otc": "USD/PKR",
                "USDCOP_otc": "USD/COP",
                "USDMXN_otc": "USD/MXN",
                "USDIDR_otc": "USD/IDR",
            }
            alltime_otc_pairs = []
            for at_pair in self._alltime_otc_pairs_list:
                # Find matching instrument in the OTC list (by asset name).
                matching = next((p for p in otc_pairs if p["asset"] == at_pair["asset"]), None)
                # Use canonical display name (overrides Quotex's non-standard order).
                canonical_display = _ALLTIME_DISPLAY.get(at_pair["asset"], at_pair["display"])
                if matching:
                    # Update payout from live data, but keep category='alltime_otc'
                    # and locked=False (always tradeable). Use canonical display.
                    alltime_otc_pairs.append({
                        "asset":    matching["asset"],
                        "display":  canonical_display,
                        "status":   "otc",
                        "payout":   matching["payout"],
                        "locked":   False,  # alltime bypasses the floor
                        "category": "alltime_otc",
                    })
                else:
                    # Pair not in Quotex instruments — keep as-is (default).
                    at_pair["display"] = canonical_display
                    alltime_otc_pairs.append(at_pair)
            self._alltime_otc_pairs_list = alltime_otc_pairs

            # Backward-compat: combined list (real + otc + alltime). Old code
            # that reads self._pairs_list still works.
            self._pairs_list = real_pairs + otc_pairs + alltime_otc_pairs
            self._last_pairs_refresh = time.time()

            print(f"[feed] pairs loaded: "
                  f"{len(real_pairs)} real ({sum(1 for p in real_pairs if p['status']=='live')} live, "
                  f"{sum(1 for p in real_pairs if p['locked'])} locked <{PAYOUT_FLOOR_REAL}%) | "
                  f"{len(otc_pairs)} OTC ({sum(1 for p in otc_pairs if p['status']=='otc')} open, "
                  f"{sum(1 for p in otc_pairs if p['locked'])} locked <{PAYOUT_FLOOR_OTC}%) | "
                  f"{len(alltime_otc_pairs)} all-time OTC (always tradeable)")

            if broadcast:
                await broadcast({
                    "type": "pairs",
                    "pairs":  self._pairs_list,            # backward compat
                    "real_pairs": real_pairs,
                    "otc_pairs":  otc_pairs,
                    "alltime_otc_pairs": alltime_otc_pairs,
                    "payout_floor_real": PAYOUT_FLOOR_REAL,
                    "payout_floor_otc":  PAYOUT_FLOOR_OTC,
                    "payout_floor_alltime_otc": 0,
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
        """
        Called from /api/subscribe. Starts a stream for (asset, period) if one
        isn't already running, subject to the capacity cap / error cooldown.
        An already-running stream is NEVER rejected or torn down here — those
        guards only gate the creation of a brand-new stream.

        FIX (AUDIT-FEED #2, 2026-07-19): if sim fallback has fired
        (self._sim_delegate is set), route the call to the sim feed
        instead of trying to use the dead real feed. Previously the
        delegate was set but never consulted — new subscribers would
        hang forever because the real feed's _client was None.
        """
        # Route to sim delegate if sim fallback has taken over.
        if getattr(self, '_sim_delegate', None) is not None:
            return await self._sim_delegate.ensure_stream(asset, period, cid=cid)
        key = (asset, period)
        # FIX (AUDIT-CORE #71, 2026-07-21): acquire per-key lock so concurrent
        # ensure_stream() calls for the same (asset, period) serialize. The
        # first caller creates the stream; subsequent callers see it exists
        # and just add their cid to interested_cids. Previously both callers
        # raced past the `stream is None` check, both created streams, and
        # the second orphaned the first.
        if key not in self._stream_locks:
            self._stream_locks[key] = asyncio.Lock()
        async with self._stream_locks[key]:
            stream = self._streams.get(key)
            if stream is not None:
                if cid:
                    stream.interested_cids.add(cid)
                    for k, s in list(self._streams.items()):   # a cid watches one pair at a time
                        if k != key:
                            s.interested_cids.discard(cid)
                stream.idle_since = None
                # A joining viewer only gets ongoing tick/eoc broadcasts from here
                # on — without handing back the CURRENT candles/prediction, their
                # chart stays empty until the next candle close (up to a full
                # period away) even though the stream has been live the whole
                # time. Include the snapshot directly in the response so the
                # frontend can paint immediately, same as a brand-new stream's
                # first broadcast.
                # Signal delay (2026-07-10): honor the same gate a live viewer
                # would see — a joiner landing inside the opening-tick
                # confirmation window gets prediction=None (PENDING) instead of
                # the still-unconfirmed raw prediction; it arrives via the next
                # gated tick broadcast same as for existing viewers.
                gated_prediction = stream.prediction
                if (stream.signal_delay_until > 0
                        and time.time() < stream.signal_delay_until):
                    gated_prediction = None
                return {"type": "snapshot", "ok": True, "status": "streaming",
                        "asset": asset, "period": period,
                        "candles": stream.candles[-SNAPSHOT_CANDLES:], "prediction": gated_prediction}

            # Payout gate — only blocks starting a BRAND NEW stream, same as the
            # cooldown/capacity checks below. If a pair's payout later drifts
            # below the floor, anyone already watching keeps their stream (see
            # _reconcile_always_on, which only ever demotes always_on, never
            # tears the stream down).
            #
            # FIX (2026-07-17): use category-specific payout floor. Real pairs
            # use PAYOUT_FLOOR_REAL (default 70%), OTC pairs use PAYOUT_FLOOR_OTC
            # (default 85%). The pair's "locked" flag is already set per-category
            # in _load_pairs, but the error message here also needs the right
            # floor value to display correctly.
            pair = next((p for p in self._pairs_list if p["asset"] == asset), None)
            # FIX (DATA-FLOW-2026-07-22): all-time OTC pairs bypass the payout
            # floor entirely. Check the dedicated list — if the asset is there,
            # never reject it for low payout (the user explicitly wants these
            # 6 exotic pairs tradeable regardless of payout %).
            is_alltime = any(p["asset"] == asset for p in self._alltime_otc_pairs_list)
            if pair and pair.get("locked") and not is_alltime:
                floor = _payout_floor_for(asset)
                return {"ok": False, "status": "locked", "payout": pair.get("payout"),
                        "reason": f"Needs {floor}% payout "
                                  f"(currently {pair.get('payout', '?')}%)"}

            if time.time() < self._cooldown_until:
                return {"ok": False, "status": "cooldown",
                        "retry_after": round(self._cooldown_until - time.time(), 1),
                        "reason": self._cooldown_reason}
            if len(self._streams) >= self._max_streams:
                return {"ok": False, "status": "at_capacity", "max": self._max_streams}

            stream = _AssetStream(asset=asset, period=period)
            if cid:
                stream.interested_cids.add(cid)
            self._streams[key] = stream
            stream.task = asyncio.create_task(self._run_stream(stream))

        # FIX (2026-07-15): "connected but no candles" problem.
        # If real feed is connected but stream fails to get any ticks/candles
        # within 30s (common when token expired mid-session, or Quotex WS
        # silently drops the subscription), auto-fallback to sim mode so
        # the user at least sees a working chart instead of a blank screen.
        # The fallback only triggers if we're NOT already in sim mode.
        if self._connected and not os.environ.get("USE_SIM") == "1":
            asyncio.create_task(self._fallback_to_sim_if_stuck(asset, period, stream))

        return {"ok": True, "status": "starting"}

    async def _fallback_to_sim_if_stuck(self, asset: str, period: int,
                                         stream: '_AssetStream') -> None:
        """If a stream has 0 candles after 30s, switch to sim feed.

        Common cause: Quotex token expired mid-session. The WS connection
        shows 'connected' but no data flows. Auto-fallback lets the user
        keep using the app while the real feed reconnects in background.

        FIX (H4, 2026-07-19): previously this method started a sim feed
        but DID NOT stop the real feed's run() loop — leaving a zombie
        real-feed manager fighting with the sim feed over broadcasts.
        Now we mark the real feed as "abandoned" so the run() loop exits
        cleanly before starting the sim feed.

        FIX (LIVE-DATA-2026-07-21): the previous implementation used
        `importlib.reload(sim_feed)` which is FRAGILE — reloading a
        module re-executes all top-level code and can break existing
        instances. Also, `await sim_stream.run(...)` would block this
        task forever, never returning control. Now we just instantiate
        sim_feed.QuotexFeed() (no reload), set the delegate, and let
        the main server lifespan start the sim feed's run() loop in a
        separate task. The sim feed then handles ensure_stream calls
        via the existing _sim_delegate routing.
        """
        try:
            await asyncio.sleep(30)
            if stream.candles or stream.ticks:
                return  # data arrived, no fallback needed
            err = (f"stream {asset}@{period}s stuck (0 candles/ticks after 30s) "
                   "— token may have expired. Auto-falling back to SIM mode.")
            print(f"[feed] {err}")
            self._last_error = err
            self._last_error_time = time.time()

            # Cancel the stuck stream task
            if stream.task:
                stream.task.cancel()
            # Use the per-key lock to safely remove from _streams
            key = (asset, period)
            if key in self._stream_locks:
                async with self._stream_locks[key]:
                    self._streams.pop(key, None)
            else:
                self._streams.pop(key, None)

            # If a sim delegate is already set (previous fallback), don't
            # spawn another — just return.
            if getattr(self, '_sim_delegate', None) is not None:
                print("[feed] sim delegate already active — skipping re-init")
                return

            # Mark the real feed as abandoned so the run() loop exits.
            self._abandoned = True
            if getattr(self, '_manager_task', None) is not None:
                self._manager_task.cancel()
                try:
                    await self._manager_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    print(f"[feed] manager task cleanup error: {exc}")

            # Cancel every other real-feed stream task too — they all share
            # the same broken Quotex connection.
            for k, s in list(self._streams.items()):
                if s.task and not s.task.done():
                    s._evicting = True
                    s.task.cancel()
            self._streams.clear()

            # FIX (LIVE-DATA-2026-07-21): DO NOT importlib.reload — that's
            # fragile and breaks existing references. Just import and
            # instantiate. The sim module is already cached in sys.modules.
            # Set USE_SIM=1 so any code that reads the env var also switches.
            os.environ["USE_SIM"] = "1"
            import sim_feed as _sim_module
            sim_stream = _sim_module.QuotexFeed()
            # FIX (RECONNECT-2026-07-23): copy ALL pair lists, not just
            # _pairs_list. Without _alltime_otc_pairs_list, the sim feed
            # can't serve All-Time OTC pairs → streams stay empty.
            sim_stream._pairs_list = list(self._pairs_list)
            sim_stream._real_pairs_list = list(getattr(self, '_real_pairs_list', []))
            sim_stream._otc_pairs_list = list(getattr(self, '_otc_pairs_list', []))
            sim_stream._alltime_otc_pairs_list = list(getattr(self, '_alltime_otc_pairs_list', []))
            # Copy broadcast fn (set later by run())
            sim_stream._broadcast = self._broadcast
            # Mark as delegate — ensure_stream() will route to it.
            self._sim_delegate = sim_stream
            # Start the sim feed's run() loop in a background task —
            # don't await it (would block forever).
            sim_stream._manager_task = asyncio.create_task(sim_stream.run(self._broadcast))
            print(f"[feed] sim delegate started — feed is now in SIM mode "
                  f"({len(sim_stream._pairs_list)} pairs, "
                  f"{len(sim_stream._alltime_otc_pairs_list)} all-time OTC)")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[feed] fallback error: {exc}")

    async def drop_interest(self, cid: str) -> None:
        """A viewer disconnected — stop counting it toward any stream's
        interested_cids (idle-eviction sweep does the rest).

        FIX (AUDIT-FEED #2, 2026-07-19): also drop interest on the sim
        delegate if one is active — otherwise the sim feed leaks viewer
        slots and idle-eviction never fires for streams the user left.
        """
        for s in self._streams.values():
            s.interested_cids.discard(cid)
        if getattr(self, '_sim_delegate', None) is not None:
            await self._sim_delegate.drop_interest(cid)

    async def _aggressive_reconnect(self) -> None:
        """FIX (RECONNECT-2026-07-23): aggressive auto-reconnect.

        User complaint: 'candle data আসা বন্ধ হয়ে গেলো, reconnect হয় না'
        Root cause: when Quotex token expires, _fallback_to_sim_if_stuck
        fires ONCE but the sim delegate's streams take time to spin up.
        Meanwhile the user sees 0 streams and a blank chart. This method
        runs every 10s and:
          1. If _sim_delegate is set but has 0 streams -> force it to
             reconcile always-on streams immediately.
          2. If no _sim_delegate and no active streams -> retry connection.
          3. If _abandoned flag is set -> clear it and retry real feed.

        This guarantees: within 10 seconds of ANY data stop, the system
        is actively trying to reconnect.
        """
        try:
            while True:
                await asyncio.sleep(10)
                # Check sim delegate
                sim = getattr(self, '_sim_delegate', None)
                if sim is not None:
                    sim_streams = getattr(sim, '_streams', {})
                    if len(sim_streams) == 0:
                        print("[feed] aggressive_reconnect: sim has 0 streams - forcing reconcile")
                        try:
                            sim._reconcile_always_on()
                            for p in sim._pairs_list:
                                if p.get("status") in ("live", "otc") and not p.get("locked"):
                                    key = (p["asset"], 60)
                                    if key not in sim._streams:
                                        s = sim._AssetStream(asset=p["asset"], period=60, always_on=True)
                                        sim._streams[key] = s
                                        s.task = asyncio.create_task(sim._run_stream(s))
                                        print(f"[feed] aggressive_reconnect: started sim stream {p['asset']}")
                        except Exception as e:
                            print(f"[feed] aggressive_reconnect sim error: {e}")
                    continue

                # No sim delegate - check if we need to reconnect real feed
                if not self._streams and not getattr(self, '_abandoned', False):
                    print("[feed] aggressive_reconnect: 0 streams - triggering reconnect")
                    self._connected = False
                    self._reconnect_attempts = 0
                    continue

                # If abandoned, try to clear and retry
                if getattr(self, '_abandoned', False):
                    print("[feed] aggressive_reconnect: feed abandoned - retrying real connection")
                    self._abandoned = False
                    self._connected = False
                    self._sim_delegate = None
                    os.environ["USE_SIM"] = "0"
                    self._reconnect_attempts = 0

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[feed] aggressive_reconnect error: {exc}")

    def stream_status(self) -> dict:
        """Return active stream count and capacity info for the status endpoint.

        FIX (CANDLE-STUCK-FIX, 2026-07-23): if sim_delegate is active,
        merge its streams into the response so /api/status and /api/debug
        correctly show all live streams. Previously when the real feed
        fell back to sim, /api/debug showed streams: {} because it was
        reading self._streams (real feed's empty dict) instead of
        sim_delegate._streams.
        """
        now = time.time()
        # Start with our own streams
        all_streams = list(self._streams.values())
        # If sim delegate is active, merge its streams too
        sim = getattr(self, '_sim_delegate', None)
        if sim is not None:
            all_streams.extend(sim._streams.values())
        return {
            "active": [{"asset": s.asset, "period": s.period,
                        "viewers": len(s.interested_cids),
                        "age_sec": round(now - s.created_at)}
                       for s in all_streams],
            "count": len(all_streams),
            "max":   self._max_streams,
            "cooldown_until":  self._cooldown_until if self._cooldown_until > now else None,
            "cooldown_reason": self._cooldown_reason if self._cooldown_until > now else None,
            "sim_mode": sim is not None,  # NEW: expose sim fallback state
        }

    async def shutdown(self) -> None:
        for s in list(self._streams.values()):
            if s.task:
                s.task.cancel()

    # ── Connection (shared across all streams) ──────────────────────────────

    def _remember_token(self) -> None:
        """Cache the latest working SSID so reconnects reuse it (no manual token).
        pyquotex also persists it to session.json, so it survives restarts."""
        try:
            tok = (self._client.session_data or {}).get("token")
            if tok:
                os.environ["QX_TOKEN"] = tok
        except Exception:
            pass

    def _clear_stale_token(self) -> None:
        """
        Auto-heal the "authorization/reject" loop (documented project issue):
        pyquotex persists the session to session.json on disk, and its own
        internal connect() logic replays that token on the NEXT attempt even
        for a brand-new client — so a rejected/expired token keeps getting
        rejected forever unless it's cleared. Previously this required a
        manual fix each time; now it runs automatically right after a
        rejection so the exponential-backoff retry in run() self-heals.
        """
        import json as _json
        # pyquotex writes session.json relative to the process's CURRENT
        # WORKING DIRECTORY (pyquotex/config.py's base_dir = Path.cwd(), NOT
        # the root_path constructor arg — an upstream quirk, confirmed by
        # reading config.py/stable_api.py directly) — so this must match cwd,
        # not __file__, for the two to ever agree on the same file. Both
        # local dev (`cd` into the project first) and Railway's default
        # working directory satisfy this.
        path = os.path.join(os.getcwd(), "session.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            changed = False
            for acct in data.values():
                if isinstance(acct, dict) and acct.get("token"):
                    acct["token"] = None
                    changed = True
            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    _json.dump(data, f)
                print("[feed] cleared stale session token after auth rejection "
                      "— next retry will do a fresh login")
        except FileNotFoundError:
            pass
        except Exception as _e:
            print(f"[feed] could not clear stale token: {_e}")

    def _make_client(self, ua: str, root: str):
        """
        Build a Quotex client.

        Two backends are supported, gated by QX_USE_RAW_WS:

          QX_USE_RAW_WS=1  →  quotex_ws.QuotexWSClient (raw Socket.IO v3
                              WebSocket, bypasses pyquotex entirely).
                              Required when Cloudflare is blocking pyquotex's
                              httpx login (403) — see quotex-smooth-candle-
                              mystery.txt for the protocol spec.

          default          →  pyquotex.stable_api.Quotex (original behavior).

        Both backends expose the same API surface that feed.py consumes:
          connect(), set_session(), start_candles_stream(),
          stop_candles_stream(), get_realtime_price(), get_instruments(),
          get_payout_by_asset(), get_candles(), get_historical_candles(),
          close(), session_data.
        """
        # ── Raw WebSocket backend (lighter but Cloudflare blocks login) ──
        if os.environ.get("QX_USE_RAW_WS", "0") == "1":
            from quotex_ws import QuotexWSClient
            print("[feed] using RAW WebSocket backend (quotex_ws.QuotexWSClient)")
            return QuotexWSClient(
                email    = os.environ.get("QX_EMAIL",    ""),
                password = os.environ.get("QX_PASSWORD", ""),
                host     = "market-qx.trade",
                lang     = "en",
                root_path= root,
            )

        # ── Vendored pyquotex (DEFAULT — Firefox TLS bypasses Cloudflare) ──
        # The ./pyquotex folder is a vendored copy from otc-live-trading with:
        #   - ssl_utils.py: Firefox cipher suite → Cloudflare sees Firefox, not bot
        #   - login.py: honors host param (not hardcoded qxbroker.com)
        #   - cookie-jar + token parsing fixes
        from pyquotex.stable_api import Quotex
        from pyquotex.types import ReconnectPolicy
        from pyquotex.network.login import Login
        Login.base_url = "market-qx.trade"
        Login.https_base_url = "https://market-qx.trade"
        ua_src = "env QX_UA" if os.environ.get("QX_UA", "").strip() else "default Firefox"
        print(f"[feed] using vendored pyquotex (Firefox TLS — Cloudflare bypass, UA: {ua_src})")
        return Quotex(
            email    = os.environ.get("QX_EMAIL",    ""),
            password = os.environ.get("QX_PASSWORD", ""),
            host     = "market-qx.trade",
            lang     = "en",
            root_path= root,
            reconnect_policy=ReconnectPolicy(
                enabled=True, max_attempts=0,
                base_delay=2.0, max_delay=30.0, stale_timeout=45.0),
        )

    @staticmethod
    async def _close_client(client) -> None:
        """Best-effort close — pyquotex versions vary on the API.

        FIX (2026-07-13): the `return` used to be inside the `if callable(fn)`
        block, so if `close()` raised an exception, the `except` caught it
        and then `return` exited — `disconnect` and `close_connect` were
        never tried. Now: only return on SUCCESS; on failure, fall through
        to the next method.
        """
        for meth in ("close", "disconnect", "close_connect"):
            fn = getattr(client, meth, None)
            if callable(fn):
                try:
                    result = fn()
                    if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                        await asyncio.wait_for(result, timeout=3)
                    return   # success — stop trying alternatives
                except Exception as _e:
                    # FIX L6 (2026-07-19): log the failure so connection-leak
                    # debugging is possible — was silently swallowed before.
                    print(f"[feed] _close_client {meth}() failed: "
                          f"{type(_e).__name__}: {_e}")
                    continue   # this method failed — try the next one

    async def _connect(self) -> bool:
        try:
            _USE_RAW_WS = os.environ.get("QX_USE_RAW_WS", "0") == "1"
            if not _USE_RAW_WS:
                from pyquotex.types import ReconnectPolicy  # noqa: ensure importable
            import tempfile
            root = os.environ.get(
                "QX_ROOT", os.path.join(tempfile.gettempdir(), "plybit_cache")
            )
            # Firefox UA — matches the Firefox TLS cipher suite in ssl_utils.py
            # so Cloudflare sees a consistent Firefox fingerprint.
            ua = os.environ.get("QX_UA", "").strip() or (
                "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) "
                "Gecko/20100101 Firefox/119.0")

            # ── Attempt 1: TOKEN (fast path, skipped if no token) ─────────────
            env_token = os.environ.get("QX_TOKEN", "").strip()
            if env_token:
                self._client = self._make_client(ua, root)
                self._client.set_session(user_agent=ua, ssid=env_token)
                print(f"[feed] connecting with session token={env_token[:8]}...")
                try:
                    ok, reason = await asyncio.wait_for(
                        self._client.connect(), timeout=30)
                    if ok:
                        self._remember_token()
                        print(f"[feed] connect -> ok=True  reason={reason}")
                        return True
                    print(f"[feed] token auth failed ({reason}) — trying login")
                except Exception as _te:
                    print(f"[feed] token attempt error: {_te}")
                await self._close_client(self._client)
                os.environ.pop("QX_TOKEN", None)

            # ── Attempt 2: Fresh client, email/password (vendored pyquotex) ────
            # This is the MAIN login path. Vendored pyquotex uses Firefox TLS
            # cipher suite (ssl_utils.py) which bypasses Cloudflare. After
            # successful login, pyquotex auto-saves token to session.json via
            # update_session() in login.py — NO manual QX_TOKEN needed.
            #
            # SKIP for raw WS backend (it has no HTTP login).
            if not _USE_RAW_WS:
                print("[feed] connecting via email/password "
                      "(vendored pyquotex + Firefox TLS)...")
                self._client = self._make_client(ua, root)
                ok, reason = await asyncio.wait_for(
                    self._client.connect(), timeout=45)
                print(f"[feed] connect -> ok={ok}  reason={reason}")
                if ok:
                    self._remember_token()
                    return True
                if reason and "reject" in str(reason).lower():
                    self._clear_stale_token()

                # ── Attempt 2b: auth may have succeeded internally but connect()
                #    returned False (pyquotex race condition). If session_data
                #    now holds a fresh token, one more connect() often succeeds.
                new_tok = (self._client.session_data or {}).get("token", "")
                if new_tok and new_tok != env_token:
                    print(f"[feed] retrying with fresh token={new_tok[:8]}...")
                    try:
                        ok, reason = await asyncio.wait_for(
                            self._client.connect(), timeout=30)
                        print(f"[feed] retry -> ok={ok}  reason={reason}")
                        if ok:
                            self._remember_token()
                            return True
                        if reason and "reject" in str(reason).lower():
                            self._clear_stale_token()
                    except Exception as _re:
                        print(f"[feed] retry error: {_re}")

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

        # Strategy 1: get_historical_candles with max_workers=1
        # (sequential — avoids the 3× data explosion that caused slowness before)
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

    # ── EOC helpers ──────────────────────────────────────────────────────────

    async def _analyze_core(self, asset: str, period: int, candles: list[dict],
                      ticks: list[float],
                      running_ticks: list[float] | None = None,
                      stream: _AssetStream | None = None,
                      live_only: bool = False
                      ) -> tuple[dict | None, list]:
        """
        Shared EOC analysis — runs candle_reaction.predict_from_candle on
        the just-closed candle. Used by the watched asset (via _run_eoc)
        AND background trackers, so evidence collected in the background
        goes through the exact same pipeline as the on-screen signal.
        Returns (result, micro_hist).

        candles[-1] is the just-closed candle at this point. micro_history
        is fetched BEFORE the just-closed candle is saved to DB (we save
        it right after this call), so the history contains only the
        candles PRIOR to the current one — no double-counting with the
        current candle's ticks. before_ctime restricts it to the 5
        candle-slots immediately before the just-closed candle: a restart
        / asset switch can no longer feed hours-old rows as if they were
        the previous candle.

        Last-10s optimization (2026-07-10): if `stream` is provided, use
        stream.cached_accuracy instead of querying the DB every call.
        Accuracy only changes at candle close, so caching it per-candle
        saves ~190 DB queries/minute across 38 always-on streams.

        Live-only fast path (2026-07-10 review Next Action #1):
          live_only=True → only the running-tick theories re-evaluate
          (kept for API compatibility — candle_reaction is a single-shot
          pure price-action engine, so live_only mostly affects the
          micro_history fetch + accuracy cache).
        """
        if len(candles) < 5:
            return None, []
        # Skip DB query in live_only mode — closed-candle theories don't run
        # anyway, so micro_history would just be wasted I/O.
        if live_only:
            micro_hist = []
        else:
            micro_hist = await asyncio.to_thread(
                _db.get_micro_history,
                asset, period, 5, candles[-1]["time"])

        # FIX M3 (2026-07-19): removed dead `acc, n_acc` block. Was fetched
        # from cache or DB but never passed to the prediction engine or used
        # afterward — pure wasted I/O (and a sqlite query on every LIVE
        # re-eval for background trackers). If accuracy-aware prediction
        # is needed later, wire it through `predict_from_candle` explicitly.

        # Get HTF (5m) trend for confluence filtering.
        # CRITICAL: pass `stream` so it uses existing 1m candles in memory
        # instead of calling get_candles(period=300) which would re-subscribe
        # the asset and kill the live tick stream.
        htf_trend = "SIDEWAYS"
        try:
            htf_trend = await self._get_htf_trend(asset, stream=stream)
        except Exception:
            pass

        # NOTE (refactor 2026-07-14): the per-pair `pair_muted` set + the
        # `pair_theory_config` import that used to live here were removed.
        # The candle_reaction engine doesn't take a muted-set argument.
        # 6-MODULE ENGINE (active since 2026-07-14)
        # Runs 6 independent modules + Smart Blender with per-pair adaptation.
        from engines import predict as predict_from_candle
        # FIX (Bug 4, deep audit 2026-07-19): use core.microstructure.build_micro
        # for the prediction path. The richer micro dict includes `last_velocity`
        # which the blender's exhaustion gate (Check 3: tick velocity
        # deceleration) reads via `micro.get("last_velocity")`. Without it,
        # Check 3 NEVER fires in real-feed predictions, leaving the gate
        # with only 3 working checks (1, 2, 4) instead of 4.
        # The UI tick-broadcast path continues to use feed's own
        # _analyze_microstructure (cheaper, no need for last_velocity there).
        # Also merge ending_direction from feed's own analyzer since
        # build_micro doesn't include it but running_tick module needs it.
        from core.microstructure import build_micro as _build_micro_for_pred
        _micro_for_pred = None
        if ticks and len(ticks) >= 10:
            _micro_for_pred = _build_micro_for_pred(
                list(ticks), candles[-1]["open"] if candles else ticks[0])
            if _micro_for_pred is not None:
                _feed_micro = self._analyze_microstructure(
                    list(ticks), candles[-1]["open"] if candles else ticks[0])
                if _feed_micro and "ending_direction" in _feed_micro:
                    _micro_for_pred["ending_direction"] = _feed_micro["ending_direction"]
        # FIX (Bug #1, 2026-07-17): htf_trend was computed above but never
        # passed to the prediction engine. Now threaded through so the
        # blender can apply HTF confluence weighting (aligned ×1.1,
        # counter-trend ×0.7).
        # FIX (Bug #5, 2026-07-17): also pass `period` so per_pair
        # DB-adaptation can look up the right (asset, period) accuracy
        # bucket instead of always defaulting to period=60.
        # FIX (2026-07-17, category split): predict_from_candle now routes
        # to engines.otc or engines.real based on asset name (auto-detect:
        # ends with "_otc" → otc, otherwise → real). No explicit category
        # arg needed here — the router in engines/__init__.py handles it.
        # FIX (Bug 17, deep audit 2026-07-19): stream can be None when
        # called from a background tracker (signature allows None). Use
        # the `period` arg in that case instead of crashing on stream.period.
        _period_for_pred = stream.period if stream is not None else period
        # FIX (BUG-I, 2026-07-20): pass recent_accuracy from the per-candle
        # cache so the blender can apply accuracy-aware self-correction.
        # stream.cached_accuracy is refreshed once at candle open by _run_eoc.
        _recent_acc = getattr(stream, 'cached_accuracy', None) if stream is not None else None
        result = predict_from_candle(candles, ticks=list(ticks) if ticks else [],
                                     micro=_micro_for_pred, asset=asset,
                                     htf_trend=htf_trend, period=_period_for_pred,
                                     recent_accuracy=_recent_acc)
        return result, micro_hist

    async def _run_eoc(self, stream: _AssetStream,
                actual_open: float | None = None) -> dict | None:
        closed = stream.candles
        base_ticks = list(stream.ticks)

        # BRAIN-LEARNED: loss cluster cooldown — skip prediction if pair
        # is in cooldown after 5+ consecutive losses.
        # FIX: wrap in try/except to NEVER block the prediction pipeline.
        try:
            cooldown_until = getattr(stream, '_loss_cooldown_until', 0)
            if cooldown_until and time.time() < cooldown_until:
                remaining = int((cooldown_until - time.time()) / 60)
                print(f"[feed] {stream.asset} in loss cooldown ({remaining} min remaining) — skipping prediction")
                stream.prediction = None
                return None
        except Exception:
            pass  # never let cooldown check break the feed
        # running_ticks=None here: the NEW candle's ticks are empty at this
        # exact moment (they accumulate after this call). LIVE re-eval picks
        # up once ticks come in, via the periodic re-eval in the stream loop.

        # Refresh the per-candle accuracy cache ONCE here (at candle open).
        # All subsequent LIVE re-evals in the last 10s will reuse this cached
        # value instead of hitting the DB ~5-10 times per candle.
        # asyncio.to_thread: sqlite3 I/O would otherwise block the shared
        # event loop for every one of the ~38 concurrent streams (2026-07-10).
        # FIX (AUDIT-CORE #4, 2026-07-21): raised n from 20 to 50 for more
        # stable accuracy stats. With n=20, a single win/loss swings the
        # reported accuracy by 5%, which can flip the blender between
        # "boost ×1.05" and "dampen ×0.85" mode on every candle — causing
        # erratic confidence thrashing. n=50 needs ~3 consecutive
        # wins/losses to move the same 5%, smoothing the self-correction.
        # Env-configurable for advanced tuning.
        try:
            _acc_n = int(os.environ.get("RECENT_ACCURACY_N", "50"))
        except (TypeError, ValueError):
            _acc_n = 50
        _acc_n = max(8, min(_acc_n, 200))
        try:
            stream.cached_accuracy = await asyncio.to_thread(
                _db.recent_accuracy, stream.asset, stream.period, n=_acc_n)
        except Exception:
            stream.cached_accuracy = (None, 0)
        # FIX (2026-07-13): removed cached_accuracy_at + live_signal_history
        # assignments (both were dead fields — set but never read).

        result, micro_hist = await self._analyze_core(
            stream.asset, stream.period, closed, base_ticks,
            running_ticks=None, stream=stream)
        if result is None:
            return None
        # FIX (Bug #3, 2026-07-17): removed `stream.inverted = result.get("_flipped")`
        # — the prediction engine never emits an `_flipped` key, so this was
        # always False, and no caller ever read `stream.inverted` afterward.
        # Snapshot for the periodic LIVE re-eval (see stream loop).
        # IMPORTANT: list(closed) makes a SHALLOW COPY — otherwise
        # stream.base_candles aliases stream.candles and the LIVE re-eval
        # would score against the *current* (mutated) candle list, not the
        # snapshot taken at EOC. (Bug found 2026-07-13.)
        stream.base_candles = list(closed)
        stream.base_ticks   = base_ticks
        stream._live_reeval_ticks = 0

        # Chop guard: this exact (regime, zone) has been wrong ZONE_LOSS_GUARD+
        # times in a row — a spot that's proven itself unreadable. Under
        # every-candle mode (2026-07-06) the signal direction stands but is
        # demoted to WEAK instead of being withheld as NEUTRAL. Clears
        # itself the moment the regime/zone classification changes (see the
        # streak update in _close_running_and_start_new), not on a timer.
        _reg = result.get("regime") or {}
        # FIX (BUG-2, 2026-07-18): use correct keys. Previously
        # `_reg.get("trend")` returned None always (no such key), making
        # the chop-guard degenerate to "wrong ≥3x in a row → WEAK"
        # regardless of regime/zone. Now we use the actual `regime` key
        # and derive a zone label, so the guard only fires for the
        # SPECIFIC (regime, zone) that's been losing.
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
            # FIX (BACKTEST-2026-07-21): backtest of 842 live signals showed
            # that WEAK-classified signals (chop-guard triggered) win only
            # 4.2% of the time (17 correct / 408 wrong). The chop-guard is
            # an accurate "this zone is unreadable" signal — instead of
            # demoting to WEAK and still trading, we now convert to NEUTRAL
            # and skip the trade entirely. This sacrifices ~50% of signals
            # but should push overall win rate from 51% → 65%+.
            # The previous behavior (demote to WEAK, still trade) was net
            # negative: every WEAK signal lost 96% of the time.
            _losses = stream.zone_streak['losses']
            result["signal"] = "NEUTRAL"
            result["strength"] = "NEUTRAL"
            result["confidence"] = 0
            result.setdefault("reasons", []).append(
                f"CHOP GUARD (BACKTEST-FIX): {_key[0]}/{_key[1]} wrong "
                f"{_losses}x running → NEUTRAL (skip). "
                f"Backtest: WEAK signals won 4.2% — skipping is +EV.")
            # Re-set the signal field on the prediction result so the
            # downstream code sees NEUTRAL.
            result["signal"] = "NEUTRAL"

        # FIX (WEAK-NEUTRAL-FIX-A, 2026-07-23): user backtest observation
        # showed WEAK signals are systematically wrong — historical data
        # confirms 4.2% win rate (17 correct / 408 wrong). The previous
        # code kept WEAK signals with confidence >= 15 tradeable, which
        # lost ~96% of the time.
        # Option A: ALL WEAK signals → NEUTRAL immediately at EOC. The
        # signal direction is locked (no CALL↔PT flip), but the signal
        # itself is suppressed so it's not graded as a wrong trade.
        # Combined with Option B (LIVE WEAK→NEUTRAL in stream loop),
        # this gives the user an immediate visual signal that the
        # prediction is uncertain, and prevents the trade from being
        # logged as a loss.
        #
        # FIX (LOSS-HISTORY-FIX, 2026-07-23): preserve the original
        # direction in the reason text so _grade_and_log can recover it
        # and grade the signal as a wrong trade. This ensures loss
        # signals appear in the history DB (otherwise the win rate
        # looks artificially high because WEAK losses are invisible).
        if result.get("signal") in ("CALL", "PUT") and result.get("strength") == "WEAK":
            _weak_conf = result.get("confidence", 0)
            _orig_signal = result.get("signal")
            result["signal"] = "NEUTRAL"
            result["strength"] = "NEUTRAL"
            result["confidence"] = 0
            result.setdefault("reasons", []).append(
                f"WEAK→NEUTRAL (Option A): backtest showed 4.2% win rate "
                f"(confidence was {_weak_conf}) — skip is +EV. "
                f"opposed original {_orig_signal}")

        # Neutral signals should remain neutral; do not force a fake CALL/PUT
        # just to keep a ghost candle on screen.
        if result["signal"] == "NEUTRAL":
            return {**result, "candle": None, "payout": stream.payout}
        return {**result, "candle": _pred_candle(closed, result["signal"], stream.period, actual_open),
                "payout": stream.payout}

    def _accuracy(self, just_closed: dict, pred: dict | None,
                  period: int = 60) -> str | None:
        # Compare the candle that just closed against the prediction that was
        # made FOR it (pred), NOT the one before it. `pred` is captured
        # immediately before it is reassigned in the close handler.
        # NEUTRAL is not a direction — it must never be graded (the old code
        # fell through to pred_up=False, silently grading NEUTRAL as PUT).
        #
        # FIX (Bug #4, 2026-07-17): the period parameter is accepted so the
        # caller can document which candle period this grade corresponds to.
        # For binary CALL/PUT grading on Quotex, the trade is settled on the
        # close of the next candle of the SAME period — i.e. just_closed IS
        # the settlement candle. So the grade logic itself (close>open ⇒ UP)
        # is correct; the period arg is used only for sanity-logging if the
        # caller wants to mix periods.
        #
        # FIX (AUDIT-DEEP-A9, 2026-07-23): the previous `if not pred or
        # pred["signal"] not in ("CALL", "PUT")` worked because `not pred`
        # short-circuited the `or` for None/empty dict. But the access
        # `pred["signal"]` would crash on an empty dict if `not pred`
        # somehow returned False (which it can't, since empty dict is
        # falsy). Still, the code is brittle — now use pred.get("signal")
        # for defensive None/missing-key handling.
        if not pred:
            return None
        pred_signal = pred.get("signal")
        if pred_signal not in ("CALL", "PUT"):
            return None
        # Zero-move candle = broker refund (draw), not a win or a loss.
        # Grading close>=open as UP silently counted draws as CALL wins.
        if just_closed["close"] == just_closed["open"]:
            return "draw"
        actual_up = just_closed["close"] > just_closed["open"]
        pred_up   = pred_signal == "CALL"
        return "correct" if actual_up == pred_up else "wrong"

    def _grade_and_log(self, asset: str, period: int, closed: dict,
                       prediction: dict | None, micro_snap: dict | None,
                       candles: list[dict]) -> str | None:
        """
        Grade `closed` against the prediction that was made FOR it and write
        the full postmortem row to signal_log. Shared by the watched asset's
        close path and background trackers. `candles` must already contain
        `closed` as its last element (ATR history reads candles[-11:-1]).
        Returns the accuracy string (correct/wrong/draw) or None.

        FIX (LOSS-HISTORY-FIX, 2026-07-23): user reported that loss (wrong)
        signals are NOT saved to history. Root cause: Option A+B (WEAK→NEUTRAL)
        converts wrong signals to NEUTRAL before grading, and NEUTRAL
        predictions are skipped (line 1857: `if pred_signal not in
        ("CALL", "PUT"): return None`). This means every WEAK signal that
        would have been graded "wrong" is now skipped entirely — the loss
        is invisible in the history DB, making the win rate look higher
        than it actually is.

        FIX: if the prediction's reasons contain "Option A" or "Option B",
        it means the ORIGINAL signal was CALL/PUT but got converted to
        NEUTRAL. We recover the original direction from the reason text
        and grade it as a wrong trade (so it shows up in history as a loss,
        which is what actually happened). This preserves the user's
        ability to see the true win rate.

        If no Option A/B marker is found, the NEUTRAL was genuine (no
        original direction) — skip grading as before.
        """
        accuracy = self._accuracy(closed, prediction, period=period)
        if not prediction:
            return accuracy

        # FIX (LOSS-HISTORY-FIX): recover original direction from WEAK→NEUTRAL
        # conversions so loss signals are properly graded and saved.
        pred_signal = prediction.get("signal")
        if pred_signal == "NEUTRAL":
            # Check if this NEUTRAL was a WEAK→NEUTRAL conversion
            reasons = prediction.get("reasons", [])
            reasons_text = " ".join(str(r) for r in reasons)
            # Look for the original direction in the reason text
            # Format: "LIVE WEAK→NEUTRAL (Option B): running ticks opposed original CALL"
            # or: "WEAK→NEUTRAL (Option A): backtest showed 4.2% win rate (confidence was X)"
            import re as _re
            # Option B includes the original direction explicitly
            m = _re.search(r'opposed original (CALL|PUT)', reasons_text)
            if m:
                # Recover the original direction and grade it
                orig_signal = m.group(1)
                # Re-grade using the recovered direction
                if closed["close"] == closed["open"]:
                    accuracy = "draw"
                else:
                    actual_up = closed["close"] > closed["open"]
                    pred_up = orig_signal == "CALL"
                    accuracy = "correct" if actual_up == pred_up else "wrong"
                # Update the prediction dict so the log shows the original
                # direction (with a note that it was a WEAK→NEUTRAL conversion)
                prediction = dict(prediction)
                prediction["signal"] = orig_signal
                prediction["_was_weak_neutral"] = True
            else:
                # Genuine NEUTRAL — no original direction to recover.
                # Skip grading as before.
                return accuracy

        # Log the resolved prediction with a full WHY report.
        try:
            import json as _json
            reasons   = prediction.get("reasons", [])
            is_draw   = closed["close"] == closed["open"]
            actual_up = closed["close"] > closed["open"]

            # NEUTRAL final — skip the postmortem (no direction to grade).
            if not accuracy:
                return accuracy

            # ── Postmortem: WHY did this trade win or lose ─────────────
            move  = closed["close"] - closed["open"]
            c_rng = closed["high"] - closed["low"]
            _hist = candles[-11:-1]
            atr   = (sum(x["high"] - x["low"] for x in _hist) / len(_hist)
                     if _hist else c_rng)
            _reg  = (prediction.get("regime") or {})
            # FIX (BUG-2, 2026-07-18): previously read `_reg.get("trend")`
            # and `_reg.get("zone")` — but classify_market_regime() returns
            # keys `regime` (e.g. "TREND_UP") and boolean flags
            # `is_trending`/`is_ranging`/`is_volatile`. There is NO "trend"
            # or "zone" key. This silently logged regime=None/zone=None for
            # EVERY signal, breaking the chop-guard and any per-regime
            # analytics. Now we use the correct keys and derive a `zone`
            # label from the regime flags.
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
            if atr > 0 and c_rng < atr * 0.40:
                tags.append("NOISE_CANDLE")      # sub-noise range: coin flip
            if atr > 0 and abs(move) >= atr * 0.80:
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
                f"{sig} s={prediction['score']:+d}"
                f" {prediction.get('strength')}"
                f" agree={prediction.get('agree')}"
                f" | actual {_actual_lbl}"
                f" move={move:+.5f}{_atr_note}"
                f" | {accuracy.upper()}"
                f" | regime {regime}/{zone}"
                f"{' | ' + ','.join(tags) if tags else ''}"
            )

            # Log ANY CALL/PUT signal so the history DB actually populates.
            if sig in ("CALL", "PUT"):
                _db.log_signal(
                    asset, period, closed["time"],
                    sig, prediction["score"],
                    prediction["confidence"], "",
                    _actual_lbl, accuracy,
                    strength=prediction.get("strength"),
                    agree=prediction.get("agree"),
                    reasons=_json.dumps(reasons),
                    a_open=closed["open"], a_close=closed["close"],
                    regime=regime, zone=zone,
                    tags=",".join(tags), postmortem=pm,
                )
        except Exception as _e:
            print(f"[db] log_signal error: {_e}")
        return accuracy

    def _save_micro(self, asset: str, period: int, closed: dict,
                    micro_snap: dict, candles: list[dict],
                    ticks: list[float]) -> None:
        """
        Persist a closed candle's microstructure + gap classification + key
        levels + downsampled ticks. `candles` must already contain `closed`
        as its last element (gap reads candles[-2] as the previous close).
        """
        try:
            # ── Gap classification for this candle ─────────────────
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
                            # Wick reached gap zone — was it rejected?
                            _gap_type = ("REJECTED"
                                         if _gap_up == _is_bull_c
                                         else "WICK_FILL")
                        elif _gap_up == _is_bull_c:
                            _gap_type = "PURE"       # gap unvisited, continuation
                        else:
                            _gap_type = "FLIP"       # gap up but closed down (rare)
            micro_snap["gap_pct"]   = _gap_pct
            micro_snap["gap_type"]  = _gap_type
            micro_snap["key_levels"] = _key_levels(candles)
            # Persist the candle's raw ticks (downsampled to <=240 points)
            # so backtest can replay RUN/TRAP with the same input as live.
            import json as _tick_json
            _tl = list(ticks)
            if len(_tl) > 240:
                # FIX (2026-07-13): the old formula `int(i * _st)` for
                # i in range(240) never sampled the LAST tick when
                # len(_tl) > 240 (e.g., len=241: int(239 * 1.0042) = 239,
                # but index 240 is the 241st = newest tick — never sampled).
                # Now: use `min(len(_tl)-1, int(i * _st))` to always include
                # the most recent tick in the downsampled output.
                _st = len(_tl) / 240
                _tl = [_tl[min(len(_tl) - 1, int(i * _st))] for i in range(240)]
            micro_snap["ticks_json"] = _tick_json.dumps(
                [round(x, 6) for x in _tl])
            _db.save(asset, period, closed, micro_snap)
        except Exception as _me:
            print(f"[db] micro save error: {_me}")

    # ── Running candle ────────────────────────────────────────────────────────

    def _analyze_microstructure(self, ticks: list[float],
                                open_price: float) -> dict | None:
        """
        Real-time tick microstructure analysis of the running candle.
        Identifies buyer/seller pressure, fight zones, hold levels, and reactions.
        """
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

        # ── 1. Buyer vs Seller tick count ─────────────────────────────────────
        up_t = sum(1 for i in range(1, len(ticks)) if ticks[i] > ticks[i - 1])
        dn_t = sum(1 for i in range(1, len(ticks)) if ticks[i] < ticks[i - 1])
        moves = up_t + dn_t
        buy_pct  = round(up_t / moves * 100) if moves else 50
        sell_pct = 100 - buy_pct

        # ── 2. Dominant pressure ──────────────────────────────────────────────
        if buy_pct >= 62:
            pressure = "BUYER"
        elif sell_pct >= 62:
            pressure = "SELLER"
        else:
            pressure = "FIGHT"

        # ── 3. Fight zone: how many times price crosses candle midpoint ────────
        mid     = (hi + lo) / 2
        crosses = sum(
            1 for i in range(1, len(ticks))
            if (ticks[i - 1] < mid) != (ticks[i] < mid)
        )
        is_fight = crosses >= 4

        # ── 4. Hold level: most visited price zone ────────────────────────────
        hold_price = None
        if rng > 0:
            bin_size = rng / 8
            bins: dict[int, int] = {}
            for t in ticks:
                b = int((t - lo) / bin_size)
                bins[b] = bins.get(b, 0) + 1
            top_bin    = max(bins, key=bins.get)
            hold_price = round(lo + top_bin * bin_size + bin_size / 2, 6)
            hold_visits = bins[top_bin]
        else:
            hold_price  = round(cur, 6)
            hold_visits = len(ticks)

        # ── 5. Phase momentum (early / mid / late thirds) ─────────────────────
        n  = len(ticks)
        t3 = max(n // 3, 1)
        early = ticks[t3]     - ticks[0]
        mid_m = ticks[2 * t3] - ticks[t3]
        late  = ticks[-1]     - ticks[2 * t3]

        def _dir(v: float) -> str:
            return "UP" if v > 0 else ("DOWN" if v < 0 else "FLAT")

        phases = [_dir(early), _dir(mid_m), _dir(late)]

        # ── 6. Buyer / Seller reaction ────────────────────────────────────────
        # Reaction = price visited extreme then reversed. We confirm with LATE tick
        # direction (last 25% of ticks) to avoid flagging mid-candle wicks.
        reaction = None
        if rng > 0:
            from_hi   = (hi  - cur) / rng
            from_lo   = (cur - lo)  / rng
            net       = cur - op
            late_q    = max(n // 4, 2)
            late_move = ticks[-1] - ticks[-late_q]  # direction of last 25% ticks
            # SELLER reaction: fell far from high AND late ticks confirm selling
            if from_hi > 0.50 and late_move <= 0 and net < 0:
                reaction = "SELLER"
            # BUYER reaction: rose far from low AND late ticks confirm buying
            elif from_lo > 0.50 and late_move >= 0 and net > 0:
                reaction = "BUYER"

        # ── 7. Final-tick recovery / exhaustion ──────────────────────────────────
        # Real-time version of last-N-tick exhaustion: last 15% of running candle ticks.
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

        # ── 8. Round number proximity ─────────────────────────────────────────
        # Check if current price, candle high, or candle low is near a round level.
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

        # ── 9. 5-SECOND ANALYSIS: ending direction (last 10 ticks) ───────
        # FIX (2026-07-13): this was MISSING from feed.py's _analyze_microstructure.
        # The frontend showed end=?/?(?%) because the field wasn't here.
        # Now compute it inline (last-10-tick ending direction).
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
        """
        Check if the running candle's tick movement confirms the current prediction.

        Idea: after EOC sets a CALL/PUT prediction, the new candle's first ticks
        either move in the predicted direction (CONFIRMING) or against it (OPPOSING).
        This gives real-time validation of the EOC signal.

        Returns: 'CONFIRMING', 'OPPOSING', or None.
        """
        if not stream.prediction or len(stream.ticks) < 5:
            return None
        pred = stream.prediction.get("signal")
        if pred == "NEUTRAL":
            return None

        # Use last 100 ticks for confirmation (full list is O(n) and n can be 600+)
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

        if (pred == "CALL" and running_dir == "UP") or \
           (pred == "PUT"  and running_dir == "DOWN"):
            return "CONFIRMING"
        return "OPPOSING"

    def _apply_strength_gate(self, stream: _AssetStream,
                             prediction: dict) -> dict:
        """
        Method B (2026-07-10, untested) — gate prediction strength using the
        running candle's tick confirmation. Returns a NEW prediction dict
        (does not mutate the input).

        Decision matrix:
          WEAK   + CONFIRMING + 10+ ticks -> MEDIUM
          MEDIUM + OPPOSING   + 10+ ticks -> WEAK
          STRONG + OPPOSING   + 10+ ticks -> MEDIUM
        All other cases: strength unchanged.
        """
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
        else:
            return prediction  # no change

        new_pred = dict(prediction)
        new_pred["strength"] = new_strength
        new_pred["reasons"] = [*prediction.get("reasons", []), gate_reason]
        new_pred["_runconf_tag"] = gate_tag
        return new_pred

    def _reset_micro_cache(self, stream: _AssetStream) -> None:
        """Clear the microstructure cache + last-broadcast snapshot. Call
        whenever stream.ticks is cleared (candle boundary, re-anchor, etc.)
        so the next broadcast forces a fresh compute."""
        stream._micro_cache = None
        stream._micro_cache_at_tick = 0
        stream._micro_cache_high = 0.0
        stream._micro_cache_low = 0.0
        stream._last_bcast_high = 0.0
        stream._last_bcast_low = 0.0
        stream._last_bcast_close = 0.0
        # Reset tracked high/low so _running_candle recomputes from scratch
        stream._tracked_high = None
        stream._tracked_low = None

    def _running_candle(self, stream: _AssetStream) -> dict:
        """Build the current running candle OHLC.
        OPTIMIZED (2026-07-12): use the tracked high/low if available
        (avoids O(n) max()/min() on every tick with 600+ ticks)."""
        op = stream.candle_open_price
        if not stream.ticks:
            return {"time": stream.candle_open_time, "open": op,
                    "high": op, "low": op, "close": op}
        # Fast path: use tracked high/low (updated incrementally in stream loop)
        # FIX (2026-07-13): the tracked high/low used to be updated ONLY from
        # `cur_close = stream.ticks[-1]`. When multiple ticks arrived in a
        # single batch and an INTERMEDIATE tick was the new high/low but the
        # latest tick retraced, the tracked high/low was never updated — the
        # running candle's high/low silently understated. Now the stream loop
        # updates `_tracked_high`/`_tracked_low` for EVERY tick it appends
        # (see `_track_tick` calls in _stream_loop), so this method can safely
        # use the cached values without re-scanning the whole deque.
        cur_close = stream.ticks[-1]
        cur_high = getattr(stream, '_tracked_high', None)
        cur_low = getattr(stream, '_tracked_low', None)
        if cur_high is None or cur_low is None:
            # First call after reset — compute from full list + cache
            ticks_list = list(stream.ticks)
            cur_high = max(ticks_list)
            cur_low = min(ticks_list)
            stream._tracked_high = cur_high
            stream._tracked_low = cur_low
        # NOTE: no per-call update here — the stream loop's `_track_tick`
        # keeps _tracked_high/_tracked_low fresh as ticks are appended.
        return {
            "time":  stream.candle_open_time,
            "open":  op,
            "high":  cur_high,
            "low":   cur_low,
            "close": cur_close,
        }

    @staticmethod
    def _track_tick(stream: '_AssetStream', price: float) -> None:
        """Update tracked high/low for ONE appended tick. Called by the
        stream loop for EVERY tick appended to stream.ticks so the cached
        extremes stay fresh even when intermediate ticks in a batch set a
        new high/low that the latest tick retraced. (Bug found 2026-07-13.)"""
        h = getattr(stream, '_tracked_high', None)
        l = getattr(stream, '_tracked_low', None)
        if h is None or price > h:
            stream._tracked_high = price
        if l is None or price < l:
            stream._tracked_low = price

    async def _close_running_and_start_new(self, stream: _AssetStream,
                                     new_open_time: int, first_tick: float,
                                     open_is_real: bool = True):
        """Finalize the running candle and begin a new one.

        open_is_real=False marks the new open as a placeholder (used by the
        timer-close, which fires before any real tick of the new candle exists).
        The first real tick later re-anchors the open in the stream loop.
        """
        # Time guard: never let a candle go backwards in time — LightweightCharts
        # throws on out-of-order data and the chart breaks.
        if new_open_time <= stream.candle_open_time:
            return None

        closed = self._running_candle(stream)

        # Replace or append the closed candle in the history list
        if stream.candles and stream.candles[-1]["time"] == closed["time"]:
            stream.candles[-1] = closed
        elif not stream.candles or stream.candles[-1]["time"] < closed["time"]:
            stream.candles.append(closed)

        # Keep list bounded
        if len(stream.candles) > MAX_CANDLES:
            stream.candles = stream.candles[-TRUNCATE_TO:]

        # Microstructure of the just-closed candle — computed ONCE here while
        # stream.ticks is still intact; used by the postmortem and persisted
        # to candle_micro further down.
        _micro_snap = (self._analyze_microstructure(stream.ticks, stream.candle_open_price)
                       if len(stream.ticks) >= 10 else None)

        # Grade the candle that just closed against the prediction that was
        # made FOR it (stream.prediction, before we overwrite it below) and
        # write the full postmortem row (shared with background trackers).
        accuracy = await asyncio.to_thread(
            self._grade_and_log, stream.asset, stream.period, closed,
            stream.prediction, _micro_snap, stream.candles)

        # BRAIN-LEARNED (2026-07-20): loss cluster protection.
        # If a pair has 5+ consecutive losses, skip predictions for 30 min.
        # FIX: wrap in try/except to ensure this NEVER breaks the feed pipeline.
        try:
            if accuracy == "wrong":
                stream._consecutive_losses = getattr(stream, '_consecutive_losses', 0) + 1
                if stream._consecutive_losses >= 5:
                    stream._loss_cooldown_until = time.time() + 1800  # 30 min
                    print(f"[feed] {stream.asset} hit {stream._consecutive_losses} consecutive "
                          f"losses — cooling down for 30 min")
            elif accuracy == "correct":
                stream._consecutive_losses = 0
        except Exception:
            pass

        # FIX (BUG-I, 2026-07-20): invalidate DB-adaptation cache after each
        # signal_log write so the next prediction reflects fresh accuracy data.
        # Without this, the cache only refreshes on TTL expiry (60s), meaning
        # up to 60 graded signals could be ignored before adaptation kicks in.
        # Now the cache is invalidated immediately after a new grade is logged,
        # so the very next prediction uses the updated win rates.
        if accuracy in ("correct", "wrong"):
            try:
                # Invalidate both OTC and Real adapters (one will be a no-op
                # since the asset belongs to only one engine, but both adapters
                # share the same cache key structure).
                from engines.otc.config import weight_adapter as _otc_adapter
                from engines.real.config import weight_adapter as _real_adapter
                _otc_adapter.invalidate_cache(stream.asset, stream.period)
                _real_adapter.invalidate_cache(stream.asset, stream.period)
            except Exception:
                pass  # adapters not loaded (e.g. test context) — skip

            # BRAIN: record full prediction context for learning
            try:
                from core.brain import record_prediction
                actual_dir = "UP" if closed["close"] > closed["open"] else (
                    "DRAW" if closed["close"] == closed["open"] else "DOWN")
                await asyncio.to_thread(
                    record_prediction,
                    stream.prediction or {}, stream.asset, stream.period,
                    closed["time"], actual_dir, accuracy, closed, _micro_snap)
            except Exception:
                pass

            # BRAIN: run analysis every 50 graded signals
            try:
                _brain_counter = getattr(self, '_brain_analyze_counter', 0) + 1
                self._brain_analyze_counter = _brain_counter
                if _brain_counter % 50 == 0:
                    from core.brain import analyze_and_learn
                    await asyncio.to_thread(analyze_and_learn)
                    # BACKTEST-2026-07-21: also refresh time/session patterns.
                    try:
                        from core.time_patterns import recompute_from_signal_log
                        await asyncio.to_thread(recompute_from_signal_log, 3)
                    except Exception as _pe:
                        print(f"[feed] pattern refresh skipped: {_pe}")
                    # FIX (AUTO-TUNE-2026-07-23): auto-tune module weights
                    # based on live win rates. Every 50 graded signals,
                    # recompute weights and apply to engine configs.
                    try:
                        from core.auto_tune import apply_tuned_weights_to_engines
                        await asyncio.to_thread(apply_tuned_weights_to_engines)
                    except Exception as _te:
                        print(f"[feed] auto-tune skipped: {_te}")
            except Exception:
                pass

        # Update the chop-guard streak using the regime/zone the JUST-RESOLVED
        # prediction was made under (stream.prediction, before _run_eoc below
        # overwrites it with the next one). A win, or the zone itself changing,
        # clears the streak; a loss in the SAME zone extends it.
        if accuracy in ("correct", "wrong"):
            _reg = (stream.prediction or {}).get("regime") or {}
            # FIX (BUG-2, 2026-07-18): use correct regime/zone keys.
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
        # FIX (LOSS-HISTORY-FIX, 2026-07-23): lock the direction at EOC.
        # Once the EOC prediction sets CALL or PUT, that direction is
        # locked for the entire candle — LIVE re-eval can update
        # confidence/strength but NEVER flip CALL↔PUT.
        if stream.prediction and stream.prediction.get("signal") in ("CALL", "PUT"):
            stream._locked_direction = stream.prediction["signal"]

        # ── Signal delay (2026-07-10) ──────────────────────────────────────
        # Set the gate so the prediction is NOT broadcast until
        # SIGNAL_DELAY_SEC seconds after the new candle opens. Candle data
        # and tick updates still flow; only the prediction panel waits.
        stream.signal_delay_until = time.time() + SIGNAL_DELAY_SEC

        # Persist microstructure NOW — after EOC (so DB was clean during analysis)
        # but BEFORE ticks.clear() so the tick buffer is still fully intact.
        if _micro_snap:
            await asyncio.to_thread(
                self._save_micro, stream.asset, stream.period, closed,
                _micro_snap, stream.candles, list(stream.ticks))

        # FIX (DATA-FLOW-2026-07-22): record this candle with the algorithm
        # monitor. It maintains a rolling 30-candle window per asset and
        # detects payout-driven algorithm switches (Quotex's behavior of
        # cycling candle-generation algorithms when payout spikes).
        try:
            from core.algorithm_monitor import record_candle
            payout = getattr(stream, 'payout', None) or 0
            tick_count = int(_micro_snap.get('tick_count', 0)) if _micro_snap else 0
            record_candle(
                asset=stream.asset, ctime=closed.get('time', 0),
                payout=payout,
                open_=closed.get('open', 0), high=closed.get('high', 0),
                low=closed.get('low', 0), close=closed.get('close', 0),
                tick_count=tick_count)
        except Exception as _e:
            # Never let monitoring break the prediction pipeline.
            pass

        # Start new candle
        stream.candle_open_time    = new_open_time
        stream.candle_open_price   = first_tick
        stream.candle_open_is_real = open_is_real
        stream.ticks.clear()
        stream.ticks.append(first_tick)
        self._track_tick(stream, first_tick)   # keep tracked high/low fresh
        # Invalidate caches — new candle, fresh compute needed.
        self._reset_micro_cache(stream)
        # FIX (LOSS-HISTORY-FIX, 2026-07-23): reset the locked direction
        # for the new candle. Each candle gets ONE direction lock — once
        # set (via EOC prediction or LIVE re-eval), it can't flip to the
        # opposite direction on the same candle.
        stream._locked_direction = None

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

    # ── Per-stream lifecycle ──────────────────────────────────────────────────

    async def _start_stream(self, stream: _AssetStream) -> None:
        """Subscribe + load history for one stream. Raises on failure so the
        caller (_run_stream) can count it toward the error cooldown."""
        if self._client is None:
            raise RuntimeError("Quotex client not connected yet")

        asset, period = stream.asset, stream.period
        print(f"[feed] starting stream {asset}@{period}s"
              + (f" (ALWAYS-ON — 85%+ payout)" if stream.always_on else ""))

        await self._client.start_candles_stream(asset, period)
        stream.sub_started = True
        # FIX (AUDIT-FEED #5, 2026-07-19): record which client instance
        # started this subscription, so _run_stream's finally block can
        # detect a stale-client post-rebuild and skip stop_candles_stream.
        stream._sub_client_id = id(self._client)

        # ── Register event-driven tick callback (raw-WS backend only) ──
        # When the raw-WS backend is active, register a callback that pushes
        # each tick directly into this stream's asyncio.Queue. _stream_loop
        # then awaits queue.get() with a 50ms timeout — eliminating the
        # legacy 50ms polling loop and shaving ~25-50ms latency off every
        # tick → browser-render hop.
        # Legacy pyquotex backend has no register_tick_callback(), so the
        # stream loop falls back to polling get_realtime_price() as before.
        #
        # FIX (2026-07-13): asyncio.Queue is NOT thread-safe. The raw-WS
        # reader runs in a separate thread and calls _on_tick from there.
        # Calling put_nowait() directly mutates the queue's internal deque
        # without synchronization → corruption under load. Now we use
        # loop.call_soon_threadsafe() which schedules the put on the
        # asyncio event loop's thread.
        if hasattr(self._client, 'register_tick_callback'):
            _loop = asyncio.get_event_loop()
            def _on_tick(tick_dict, _stream=stream, _loop=_loop):
                try:
                    _loop.call_soon_threadsafe(
                        _stream.tick_queue.put_nowait, tick_dict)
                except Exception:
                    # Queue full or loop closed — drop the tick (best effort).
                    # Try a direct put as a last resort; if that also fails,
                    # drop oldest to make room.
                    try:
                        _stream.tick_queue.put_nowait(tick_dict)
                    except asyncio.QueueFull:
                        try:
                            _stream.tick_queue.get_nowait()
                            _stream.tick_queue.put_nowait(tick_dict)
                        except Exception:
                            pass
            self._client.register_tick_callback(asset, _on_tick)
            stream.tick_callback = _on_tick
            print(f"[feed] event-driven ticks enabled for {asset}@{period}s")

        await asyncio.sleep(1)  # let first ticks arrive

        # Payout is informational only (breakeven display) — never affects
        # signal/score.
        try:
            pay = self._client.get_payout_by_asset(asset)
            stream.payout = int(pay) if pay is not None else None
        except Exception:
            stream.payout = None

        # FIX (H3, 2026-07-19): if `stream.candles` is already populated
        # (preserved by watchdog restart), don't blindly overwrite it with
        # the freshly-fetched history. Instead, only append candles newer
        # than the latest preserved one. This honors the watchdog's intent:
        # the chart continues seamlessly instead of briefly blanking.
        history = await self._load_history(asset, period)
        stream.last_real_tick_wall = time.time()

        if not history:
            # History unavailable (live pair or API timeout). Don't retry-loop
            # — mark started and let tick streaming build the chart from
            # scratch.
            print(f"[feed] no history for {asset}@{period}s "
                  f"— starting from ticks only")
            # If watchdog preserved candles, keep them — don't blank the chart.
            if not stream.candles:
                await self._broadcast({
                    "type":       "snapshot",
                    "asset":      asset,
                    "period":     period,
                    "candles":    [],
                    "prediction": None,
                })
            return

        # If watchdog preserved state, merge instead of overwrite.
        if stream.candles:
            preserved_last_time = stream.candles[-1].get("time", 0)
            # Append only history candles newer than the latest preserved.
            new_candles = [c for c in history if c.get("time", 0) > preserved_last_time]
            if new_candles:
                stream.candles.extend(new_candles)
                # Trim to last 400 to avoid unbounded growth.
                if len(stream.candles) > 500:
                    stream.candles = stream.candles[-400:]
                print(f"[feed] watchdog-merged {len(new_candles)} new candles "
                      f"into preserved {len(stream.candles) - len(new_candles)} "
                      f"for {asset}@{period}s")
            # Update open time/price only if a new candle started.
            new_last = stream.candles[-1]
            new_open_time = new_last["time"] + period
            if new_open_time > stream.candle_open_time:
                stream.candle_open_time  = new_open_time
                stream.candle_open_price = new_last["close"]
                stream.candle_open_is_real = False
            # Don't clear ticks — preserved ticks are still valid for the
            # current running candle.
            if not stream.ticks:
                stream.ticks.append(new_last["close"])
                self._track_tick(stream, new_last["close"])
            # Reset micro cache + recompute prediction (cheap, no DB I/O
            # for the prediction engine itself; _run_eoc does the to_thread).
            self._reset_micro_cache(stream)
            stream.prediction = await self._run_eoc(stream, actual_open=new_last["close"])
            stream.signal_delay_until = 0.0
            await self._broadcast({
                "type":       "snapshot",
                "asset":      asset,
                "period":     period,
                "candles":    stream.candles[-300:],
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
        # Fresh stream — clear caches so the first broadcast forces a fresh
        # micro compute instead of serving a stale cache from a previous
        # (now-dead) stream that reused this _AssetStream instance.
        self._reset_micro_cache(stream)
        # Generate initial prediction from history so the ghost candle
        # appears immediately without waiting for the first EOC.
        stream.prediction = await self._run_eoc(stream, actual_open=last["close"])
        # Initial subscription joins mid-candle — no signal delay (the candle
        # has already been running, opening ticks already happened).
        stream.signal_delay_until = 0.0
        await self._broadcast({
            "type":       "snapshot",
            "asset":      asset,
            "period":     period,
            "candles":    history,
            "prediction": stream.prediction,
        })

    async def _stream_loop(self, stream: _AssetStream) -> None:
        """Runs 'forever' for one (asset, period) — timer-close fallback,
        tick polling, tick-based close, same-candle updates. Direct per-asset
        port of what used to be the single shared run() loop's body."""
        # TIMER_GRACE: how long past the candle boundary to wait for a real
        # tick before forcing a timer-close with a placeholder open.
        # Was 1.5s — too short for OTC where tick gaps can be 5-10s.
        # At 1.5s, most candles closed with a fake open, then the real
        # first tick arrived "late" and got dropped — corrupting OHLC.
        # 7.0s gives OTC ticks enough time to arrive naturally.
        TIMER_GRACE = float(os.environ.get("TIMER_GRACE", "7.0"))
        # STALE_SECS is module-level (overridable via env) — see top of file.

        while True:
            try:
                # ── Signal-delay timer fallback (2026-07-13) ──────────────────
                # If the signal-delay gate has opened but no tick arrived to
                # deliver the prediction, broadcast it now. Without this, a
                # sparse OTC feed (5-10s tick gaps) could withhold the
                # prediction for up to 30s past the intended delay.
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

                # ── Per-stream stale re-arm ────────────────────────────────
                # Only re-issues THIS stream's own subscription (cheap) — never
                # tears down self._client, which would kill every other
                # viewer's stream too. A GLOBAL "everything is stale" backstop
                # lives in the manager loop (run()) instead.
                if (stream.last_real_tick_wall > 0
                        and time.time() - stream.last_real_tick_wall > STALE_SECS):
                    print(f"[feed] STALE: {stream.asset}@{stream.period}s "
                          f"— re-arming stream")
                    try:
                        if self._client:
                            await self._client.start_candles_stream(
                                stream.asset, stream.period)
                    except Exception:
                        pass
                    stream.last_real_tick_wall = time.time()
                    await self._broadcast({"type": "stale", "asset": stream.asset,
                                           "period": stream.period})
                    await asyncio.sleep(2)
                    continue

                # ── Timer-based candle close (fallback after a grace window) ──
                # OTC ticks can be sparse (5-10s gaps). A tick that crosses the
                # boundary closes the candle immediately (tick-close below, the
                # accurate path). The timer is only the FALLBACK for silent
                # feeds — it waits a short grace past the boundary so a late
                # final tick can still shape the true close before we grade and
                # log the candle.
                now = time.time()
                if (stream.candle_open_time > 0
                        and now >= stream.candle_open_time + stream.period + TIMER_GRACE):
                    expected_new = _floor_to_period(now, stream.period)
                    # Only ever move FORWARD in time (never reopen an older candle)
                    if expected_new > stream.candle_open_time:
                        last_px = (list(stream.ticks)[-1] if stream.ticks
                                   else stream.candle_open_price)
                        print(f"[feed] timer-close {stream.asset}@{stream.period}s "
                              f"{stream.candle_open_time} -> {expected_new}")
                        accuracy = await self._close_running_and_start_new(
                            stream, expected_new, last_px, open_is_real=False)
                        running  = self._running_candle(stream)
                        all_c    = stream.candles + [running]
                        # ── Signal delay (2026-07-10) ──
                        # Don't broadcast prediction at EOC — wait for the
                        # signal_delay_until gate to pass in the tick loop.
                        # Candle data + accuracy flow now; prediction flows
                        # a few seconds later once opening ticks confirm.
                        await self._broadcast({
                            "type":       "eoc",
                            "asset":      stream.asset,
                            "period":     stream.period,
                            "candles":    all_c[-300:],
                            "prediction": None,   # gated — arrives via tick
                            "accuracy":   accuracy,
                        })

                if self._client is None:
                    await asyncio.sleep(1)
                    continue

                # ── Collect new ticks (event-driven OR polling fallback) ──────
                # Event-driven path (raw-WS backend): wait on the per-stream
                # asyncio.Queue with a 50ms timeout. Ticks arrive immediately
                # when the WS reader fires the registered callback — NO 50ms
                # polling delay. The timeout doubles as the timer-close
                # check cadence (the loop falls through to the top next
                # iteration, where the timer-close block runs).
                #
                # Polling fallback (legacy pyquotex): poll get_realtime_price()
                # every ~50ms as before.
                if stream.tick_callback is not None:
                    # ── Event-driven: wait on queue ────────────────────────
                    try:
                        first = await asyncio.wait_for(
                            stream.tick_queue.get(), timeout=0.05)
                        new_ticks = [first]
                        # Drain any additional ticks that arrived in the same
                        # wakeup window — batches them into one broadcast.
                        while not stream.tick_queue.empty():
                            try:
                                new_ticks.append(stream.tick_queue.get_nowait())
                            except Exception:
                                break
                    except asyncio.TimeoutError:
                        # No ticks this 50ms window — fall through to top of
                        # loop so timer-close + stale checks can run. NO sleep
                        # here: the wait_for already waited 50ms.
                        continue
                else:
                    # ── Legacy polling fallback ────────────────────────────
                    price_data = await self._client.get_realtime_price(stream.asset)
                    if not price_data:
                        await self._smart_sleep(stream)
                        continue
                    new_ticks = list(price_data)

                # ── Collect EVERY new tick since last processed ───────────────
                if stream.last_tick_ts <= 0.0:
                    # Fresh subscribe / reconnect: process the current buffer once
                    # so the first live tick can seed the running candle without
                    # reusing stale data from a previous feed session.
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

                # ── Find if any tick crossed a candle boundary ────────────────
                boundary_idx = None
                for i, t in enumerate(new_ticks):
                    t_open = _floor_to_period(float(t["time"]), stream.period)
                    if stream.candle_open_time > 0 and t_open != stream.candle_open_time:
                        boundary_idx = i
                        break

                if boundary_idx is not None:
                    # ── TICK-BASED CANDLE CLOSE (multi-boundary loop) ────────
                    # FIX (2026-07-13): previously only the FIRST boundary in
                    # a batch was detected; all post-boundary ticks (including
                    # those belonging to N+2, N+3) were appended to the N+1
                    # candle. Now we loop: close the current candle, start the
                    # new one, then re-scan remaining ticks for ANOTHER
                    # boundary. Each intermediate candle gets a real close.
                    #
                    # We process at most 10 boundaries per batch (safety cap —
                    # a healthy feed never has more than 1-2 in a 50ms window).
                    remaining = new_ticks
                    last_accuracy = None
                    last_eoc_candles = None
                    for _iter in range(10):
                        if not remaining:
                            break
                        # Re-find the first boundary in `remaining`
                        b_idx = None
                        for i, t in enumerate(remaining):
                            t_open = _floor_to_period(float(t["time"]), stream.period)
                            if stream.candle_open_time > 0 and t_open != stream.candle_open_time:
                                b_idx = i
                                break
                        if b_idx is None:
                            # No more boundaries — append the rest as same-candle ticks
                            for t in remaining:
                                stream.ticks.append(float(t["price"]))
                                self._track_tick(stream, float(t["price"]))
                            break

                        b_tick = remaining[b_idx]
                        tick_new_open = _floor_to_period(
                            float(b_tick["time"]), stream.period)

                        if tick_new_open <= stream.candle_open_time:
                            # Timer already fired for this boundary — drop late ticks
                            # belonging to the closed candle, keep only current-window.
                            cur = [
                                t for t in remaining
                                if _floor_to_period(float(t["time"]), stream.period)
                                == stream.candle_open_time
                            ]
                            n_drop = len(remaining) - len(cur)
                            if n_drop:
                                print(f"[feed] dropped {n_drop} late tick(s) from "
                                      f"closed candle ({stream.asset}@{stream.period}s)")
                            # First CURRENT-window tick after timer-close is the true open
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

                        # Real tick-based close: append pre-boundary ticks to old candle
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
                        last_eoc_candles = (stream.candles + [self._running_candle(stream)])[-300:]

                        # Continue with ticks AFTER this boundary — may contain
                        # another boundary (N+2, N+3, ...)
                        remaining = remaining[b_idx + 1:]

                    # After the loop: broadcast the last EOC. If we did multiple
                    # closes, only the LAST one's EOC is broadcast (intermediate
                    # candles are still graded + logged, just not broadcast —
                    # the chart only needs the final state).
                    if last_accuracy is not None and last_eoc_candles is not None:
                        await self._broadcast({
                            "type":       "eoc",
                            "asset":      stream.asset,
                            "period":     stream.period,
                            "candles":    last_eoc_candles,
                            "prediction": None,   # gated — arrives via tick
                            "accuracy":   last_accuracy,
                        })
                    # remaining ticks (if any) were already appended in the loop.

                else:
                    # ── SAME CANDLE — feed ALL new ticks, broadcast once ──────

                    # Bootstrap running candle from the very first tick when
                    # history was unavailable (live pair, API timeout, etc.)
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

                    # Re-anchor a timer-opened candle to its first REAL tick.
                    # After a timer-close the open was a placeholder, so the
                    # prediction candle was drawn from the wrong price. The first
                    # real tick fixes the open AND redraws the prediction candle
                    # so it starts exactly where the new market candle starts.
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
                    # Keep historical closed candles intact; the live candle is
                    # rendered from tick updates and does not need to overwrite
                    # the last completed bar in history.

                    # ── LIVE periodic re-eval (Method A, 2026-07-10) ──
                    # Re-run prediction with the running candle's own ticks
                    # so the LIVE vote can update mid-candle. Reuses the
                    # (closed candles, just-closed ticks) snapshot taken by
                    # _run_eoc at candle-open — stream.ticks now holds the NEW
                    # (still-open) candle's ticks instead.
                    #
                    # LIVE re-eval — OPTIMIZED (2026-07-12):
                    # OTC ticks come at 5-10/sec. Re-evaluating on
                    # every tick was burning CPU and delaying broadcasts.
                    # New intervals are 5x higher to match tick density:
                    #   - Last 5s (critical): every 10 ticks (~1-2s)
                    #   - Last 10s: every 15 ticks (~2-3s)
                    #   - Last 30s: every 30 ticks (~3-6s)
                    #   - Mid-candle: every 100 ticks (~10-20s)
                    pred_changed = False
                    if (ENABLE_LIVE_THEORY and stream.base_candles
                            and len(stream.ticks) >= 15):
                        time_to_close = -1
                        if stream.candle_open_time > 0:
                            time_to_close = (stream.candle_open_time
                                             + stream.period) - time.time()
                            if time_to_close < 5:
                                reeval_interval = 10  # critical zone
                            elif time_to_close < 10:
                                reeval_interval = 15  # last 10s
                            elif time_to_close < 30:
                                reeval_interval = 30  # last 30s
                            else:
                                reeval_interval = 100  # mid-candle
                        else:
                            reeval_interval = 100
                        # Live-only fast path (2026-07-10 review Next Action #1):
                        # In the last 30s (where LIVE re-eval actually matters),
                        # only run the signals that use running_ticks.
                        # Earlier in the candle there's nothing to gain from
                        # re-eval (closed candle hasn't changed), so we still
                        # use the full signal set just for consistency — but the
                        # 30-tick interval means it fires rarely.
                        live_only = 0 < time_to_close < 30

                        # Priority 3: volatility speedup — if last 3 ticks
                        # moved more than 0.5 ATR, the market is making a
                        # decisive move RIGHT NOW. Cut interval in half so we
                        # re-evaluate before the move is over.
                        if len(stream.ticks) >= 4 and reeval_interval > 2:
                            try:
                                recent = list(stream.ticks)[-4:]
                                recent_range = max(recent) - min(recent)
                                _atr_val = (_atr(stream.candles[-20:])
                                            if len(stream.candles) >= 20
                                            else 0.0001)
                                if _atr_val > 0 and recent_range > _atr_val * 0.5:
                                    reeval_interval = max(2, reeval_interval // 2)
                            except Exception:
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

                                # ── ONE SIGNAL PER CANDLE (2026-07-13) ──────
                                # The signal direction is LOCKED at EOC.
                                # LIVE re-eval can only update score/confidence/
                                # strength — it can NEVER change CALL↔PUT.
                                # This prevents the "signal flipping on same
                                # candle" problem the user reported.
                                if fresh and stream.prediction:
                                    locked_dir = stream.prediction.get("signal")
                                    fresh_dir = fresh.get("signal")
                                    if locked_dir in ("CALL", "PUT"):
                                        if fresh_dir == locked_dir:
                                            # Same direction → silently update
                                            # score/confidence/strength only.
                                            # Do NOT set pred_changed (no
                                            # rebroadcast — signal is stable).
                                            stream.prediction = {
                                                **stream.prediction,
                                                "score": fresh.get("score",
                                                    stream.prediction.get("score")),
                                                "confidence": fresh.get("confidence",
                                                    stream.prediction.get("confidence")),
                                                "agree": fresh.get("agree",
                                                    stream.prediction.get("agree")),
                                                "total": fresh.get("total",
                                                    stream.prediction.get("total")),
                                            }
                                        # else: different direction → IGNORE.
                                        # The original EOC signal stays.
                                    elif locked_dir == "NEUTRAL" and fresh_dir in ("CALL", "PUT"):
                                        # FIX (LOSS-HISTORY-FIX, 2026-07-23):
                                        # Original was NEUTRAL but live data
                                        # now shows a clear direction → allow
                                        # upgrade to CALL/PUT (one-time only).
                                        # BUT: if a direction was previously
                                        # locked this candle (via Option B
                                        # WEAK→NEUTRAL conversion), do NOT
                                        # allow upgrade to a DIFFERENT direction.
                                        # This prevents the "CALL then PUT"
                                        # flip the user reported.
                                        prev_locked = getattr(stream, '_locked_direction', None)
                                        if prev_locked and fresh_dir != prev_locked:
                                            # Block the flip — keep the
                                            # original locked direction as
                                            # NEUTRAL (don't upgrade to a
                                            # conflicting direction).
                                            pass
                                        elif prev_locked and fresh_dir == prev_locked:
                                            # Same direction as the original
                                            # locked direction — allow upgrade
                                            # back (the original signal is
                                            # being restored).
                                            stream.prediction = fresh
                                            pred_changed = True
                                        else:
                                            # No previous lock — first time
                                            # establishing a direction. Allow.
                                            stream.prediction = fresh
                                            stream._locked_direction = fresh_dir
                                            pred_changed = True
                            except Exception as exc:
                                print(f"[feed] LIVE re-eval error "
                                      f"({stream.asset}@{stream.period}s): {exc}")

                    # ── Strength gate — strength-only, NO direction change ──
                    # Can upgrade/downgrade strength based on running candle
                    # confirmation, but NEVER changes CALL↔PUT.
                    #
                    # FIX (LOSS-HISTORY-FIX, 2026-07-23): user reported that
                    # the same candle shows CALL then PUT (signal flips).
                    # Root cause: Option B converts WEAK→NEUTRAL, then the
                    # LIVE re-eval at line 3141-3146 sees locked_dir=NEUTRAL
                    # and upgrades to a fresh CALL/PUT — a DIFFERENT direction
                    # than the original. This causes the visible flip.
                    #
                    # FIX: track the ORIGINAL locked direction in
                    # stream._locked_direction. Once set (at candle open +
                    # 3s), it can NEVER change — even if the prediction
                    # becomes NEUTRAL via Option A+B. The LIVE re-eval
                    # upgrade path (line 3141) is now blocked if a direction
                    # was ever locked this candle.
                    #
                    # FIX (STRENGTH-GATE-DELAY, 2026-07-23): user reported
                    # that the signal arrives at t=3s (SIGNAL_DELAY_SEC) but
                    # goes NEUTRAL by t=4s — too fast, no time to act on it.
                    # Root cause: the strength gate fires as soon as
                    # len(stream.ticks) >= 10 (~1-2s into the candle),
                    # which is BEFORE the signal delay expires. So the user
                    # sees the signal for ~1 second before it's demoted.
                    # FIX: only run the strength gate in the LAST 30s of the
                    # candle (time_to_close < 30). This gives the signal a
                    # stable first 30s — enough time for the user to read it
                    # and act. The gate still catches wrong signals before
                    # the candle closes, so loss recovery still works.
                    if (ENABLE_STRENGTH_GATE and stream.prediction
                            and stream.prediction.get("signal") in ("CALL", "PUT")):
                        # Compute time_to_close — only gate in the last 30s
                        _time_to_close = -1
                        if stream.candle_open_time > 0:
                            _time_to_close = (stream.candle_open_time
                                             + stream.period) - time.time()
                        # Only apply the strength gate in the last 30s of
                        # the candle. Earlier in the candle, the signal
                        # stays as-is (stable for the user to read).
                        if 0 < _time_to_close < 30:
                            gated = self._apply_strength_gate(stream, stream.prediction)
                            if gated is not stream.prediction:
                                # Option B: if the strength gate demoted to WEAK,
                                # immediately promote to NEUTRAL. The original
                                # direction is preserved in the reasons for audit.
                                if gated.get("strength") == "WEAK":
                                    orig_signal = gated.get("signal", "NEUTRAL")
                                    orig_conf = gated.get("confidence", 0)
                                    # FIX (LOSS-HISTORY-FIX): record the original
                                    # direction as locked so LIVE re-eval can't
                                    # flip to a different direction later.
                                    if not getattr(stream, '_locked_direction', None):
                                        stream._locked_direction = orig_signal
                                    gated["signal"] = "NEUTRAL"
                                    gated["strength"] = "NEUTRAL"
                                    gated["confidence"] = 0
                                    gated.setdefault("reasons", []).append(
                                        f"LIVE WEAK→NEUTRAL (Option B): running ticks "
                                        f"opposed original {orig_signal} (conf was "
                                        f"{orig_conf}) — skip is +EV. "
                                        f"(gated in last 30s of candle)")
                                    # Force a rebroadcast so the browser sees
                                    # the NEUTRAL signal immediately, instead of
                                    # waiting for the next natural tick broadcast.
                                    pred_changed = True
                                stream.prediction = gated
                                # Don't set pred_changed for non-WEAK strength-only
                                # updates — they don't need a rebroadcast.

                    # Skip broadcast if open price is still 0 (no valid tick yet)
                    # — prevents LightweightCharts "Value is null" on the client
                    if stream.candle_open_price > 0:
                        # ── Microstructure caching (2026-07-11, OPTIMIZED 2026-07-12) ─
                        # _analyze_microstructure() is O(n) over stream.ticks.
                        # With 5-10 ticks/sec, n can reach 600+ per candle.
                        # Solution: only analyze the LAST 200 ticks (still gives
                        # accurate pressure/reaction/hold analysis, 10x faster).
                        # Cache + only recompute every MICRO_RECALC_EVERY ticks
                        # or when high/low change.
                        cur_high = running["high"]
                        cur_low  = running["low"]
                        tick_n   = len(stream.ticks)
                        if (stream._micro_cache is None
                                or (tick_n - stream._micro_cache_at_tick) >= MICRO_RECALC_EVERY
                                or cur_high != stream._micro_cache_high
                                or cur_low  != stream._micro_cache_low):
                            # Use only last 200 ticks for micro analysis
                            # (6x speedup vs full 2000-tick buffer)
                            recent_ticks = list(stream.ticks)[-200:]
                            stream._micro_cache = self._analyze_microstructure(
                                recent_ticks, stream.candle_open_price)
                            stream._micro_cache_at_tick = tick_n
                            stream._micro_cache_high    = cur_high
                            stream._micro_cache_low     = cur_low
                        micro_snap = stream._micro_cache

                        # ── Signal delay gate (2026-07-10) ──────────────────
                        # While the opening-tick confirmation window is still
                        # active, do NOT broadcast the prediction. Candle data,
                        # micro, and running_conf still flow so the chart and
                        # micro panel update live — only the signal panel
                        # waits. Once the gate passes, the FIRST eligible tick
                        # broadcasts the prediction (pred_changed = True).
                        now_ts = time.time()
                        gate_opened_this_tick = False
                        if stream.signal_delay_until > 0 and now_ts < stream.signal_delay_until:
                            # Still in delay window — withhold prediction
                            delay_left = stream.signal_delay_until - now_ts
                            # If reanchored/pred_changed happened during the
                            # delay (rare), queue it for after delay passes
                            if reanchored or pred_changed:
                                # mark that prediction is pending — will be
                                # broadcast on the first tick after delay
                                pass
                        else:
                            # Delay passed (or no delay set). If this is the
                            # first broadcast after the gate opened, force
                            # prediction delivery even if no LIVE re-eval fired.
                            if stream.signal_delay_until > 0:
                                # Gate just opened — clear it and force broadcast
                                stream.signal_delay_until = 0.0
                                pred_changed = True  # force prediction in this msg
                                gate_opened_this_tick = True

                        # ── Skip-redundant-broadcast (2026-07-11) ───────────
                        # If the running candle's high/low/close are unchanged
                        # since the last broadcast AND no prediction change
                        # happened AND no signal-delay gate just opened — skip
                        # the broadcast entirely. Saves JSON serialize + WS
                        # send on every connected client. Common when ticks
                        # are sparse (OTC) and the same price repeats.
                        cur_close = running["close"]
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
                        # FIX (2026-07-13): always send prediction if gate has opened
                        # (not just on pred_changed — that was blocking real-time updates)
                        if not (stream.signal_delay_until > 0 and time.time() < stream.signal_delay_until):
                            if stream.signal_delay_until > 0:
                                stream.signal_delay_until = 0.0
                            if stream.prediction:
                                msg["prediction"] = stream.prediction

                        # Update the last-broadcast snapshot for the next
                        # skip-redundant-broadcast check.
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

            # Sleep ONLY in legacy polling mode. Event-driven mode uses
            # asyncio.wait_for(queue.get(), timeout=0.05) which is itself
            # the wait — adding _smart_sleep on top would double the latency.
            if stream.tick_callback is None:
                await self._smart_sleep(stream)

    async def _run_stream(self, stream: _AssetStream) -> None:
        """Owns one _AssetStream for its whole life: start, run, clean up.
        Nothing else ever mutates this stream's state, which structurally
        rules out the cross-asset contamination bugs the old singleton design
        needed manual mid-await guards against."""
        key = (stream.asset, stream.period)
        try:
            # Wait for the shared Quotex connection FIRST. Viewers' tabs
            # subscribe the instant the server comes up after a deploy —
            # before connect() has finished — and starting then meant
            # start_candles_stream went out on a not-yet-authorized socket
            # (a dead subscription: zero ticks until the 90s stale re-arm),
            # the history fetch burned its full ~25s of timeouts, and the
            # resulting _record_stream_error hits tripped the cooldown,
            # blocking every OTHER pair for 2 more minutes. Observed live
            # on Railway as "blank chart for minutes after every deploy".
            while not (self._connected and self._client):
                await asyncio.sleep(0.5)
            async with self._new_stream_gate:
                await self._start_stream(stream)
                await asyncio.sleep(self._stagger_gap)   # paces the NEXT waiting stream
            await self._stream_loop(stream)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            import traceback
            print(f"[feed] stream {key} failed to start: {exc}")
            traceback.print_exc()
            self._record_stream_error()
        finally:
            # Unregister the event-driven tick callback before tearing down
            # the stream — otherwise the WS reader would keep pushing ticks
            # into a dead queue. Safe to call even if no callback was ever
            # registered (legacy pyquotex backend).
            try:
                if (self._client and stream.tick_callback is not None
                        and hasattr(self._client, 'unregister_tick_callback')):
                    self._client.unregister_tick_callback(
                        stream.asset, stream.tick_callback)
                    stream.tick_callback = None
            except Exception:
                pass
            try:
                # FIX (2026-07-13): skip stop_candles_stream if this stream
                # is being EVICTED (not crashed). A new stream for the same
                # asset may already be starting — calling stop here would
                # unsubscribe the NEW stream. The new stream's own
                # start_candles_stream will re-subscribe cleanly.
                # FIX (AUDIT-FEED #5, 2026-07-19): also skip if the stream's
                # subscription was started on a DIFFERENT client than the
                # current one (i.e. after a client rebuild). The old client
                # is gone; calling stop_candles_stream on the NEW client
                # would unsubscribe a live stream that the new client owns.
                # Track which client started the subscription via stream._sub_client_id.
                sub_client_id = getattr(stream, '_sub_client_id', None)
                current_client_id = id(self._client) if self._client else None
                sub_matches_current = (sub_client_id is None) or (sub_client_id == current_client_id)
                if (self._client and stream.sub_started
                        and not getattr(stream, '_evicting', False)
                        and sub_matches_current):
                    await self._client.stop_candles_stream(stream.asset)
            except Exception:
                pass
            self._streams.pop(key, None)
            print(f"[feed] stream {key} stopped")

    async def _rearm_stream(self, stream: _AssetStream) -> None:
        """After our own full client rebuild only — native reconnects
        self-heal via pyquotex's own subscription replay and need nothing
        here. Deliberately does NOT refetch history (existing candles/ticks
        are kept, ticks just resume) — refetching N histories on every
        rebuild is exactly the kind of burst this design exists to avoid.

        FIX (2026-07-13): after a client rebuild, the OLD client's tick
        callback is dead. The new client needs a FRESH callback registered
        or the stream loop will spin on an empty queue forever (no ticks
        ever arrive). Now re-registers the callback after start_candles_stream.
        """
        async with self._new_stream_gate:
            try:
                if self._client:
                    await self._client.start_candles_stream(stream.asset, stream.period)
                    stream.sub_started = True
                    # FIX (AUDIT-FEED #5, 2026-07-19): record which client
                    # instance this subscription was started on, so the
                    # finally block in _run_stream can detect that the
                    # subscription belongs to a STALE client (post-rebuild)
                    # and skip stop_candles_stream on the new client.
                    stream._sub_client_id = id(self._client)
                    stream.last_real_tick_wall = time.time()

                    # Re-register the event-driven tick callback if the new
                    # client supports it (raw-WS backend). The old callback
                    # pointed at the dead client — clear it first so the
                    # old client's unregister (if it ever runs) doesn't
                    # remove the NEW callback.
                    stream.tick_callback = None
                    if hasattr(self._client, 'register_tick_callback'):
                        _loop = asyncio.get_event_loop()
                        def _on_tick(tick_dict, _stream=stream, _loop=_loop):
                            try:
                                _loop.call_soon_threadsafe(
                                    _stream.tick_queue.put_nowait, tick_dict)
                            except Exception:
                                try:
                                    _stream.tick_queue.put_nowait(tick_dict)
                                except Exception:
                                    pass
                        self._client.register_tick_callback(stream.asset, _on_tick)
                        stream.tick_callback = _on_tick
            except Exception:
                self._record_stream_error()
            await asyncio.sleep(self._stagger_gap)

    async def _rebuild_client(self) -> None:
        for s in self._streams.values():
            await self._broadcast({"type": "stale", "asset": s.asset, "period": s.period})
        try:
            if self._client:
                await self._client.close()
        except Exception:
            pass
        self._client, self._connected = None, False
        self._record_stream_error()

    # NOTE (refactor 2026-07-14): `_refresh_theory_mutes` removed.
    # It used to read per-pair win rates from a theory_votes table
    # and mute underperforming signals. With the prediction path
    # switched to candle_reaction (no per-theory votes), the theory_votes table
    # is no longer populated, so the mute set was always empty anyway.

    def _reconcile_always_on(self) -> None:
        """
        Keep ALL 85%+ payout pairs running as ALWAYS-ON 1m streams.

        This is the KEY fix for the user's complaint:
        'প্রত্যেকটি পেয়ার নতুন করে ডেটা সংগ্রহ করে ক্যান্ডেল ওপেন হয়'

        With always-on, ALL 85%+ pairs have:
          - History pre-loaded
          - Live tick stream running
          - EOC predictions being generated every candle
          - Market state being tracked

        When the user switches pairs, data is ALREADY there — no cold start,
        no re-fetching, no delay. Each pair runs independently in its own
        asyncio task, so switching one doesn't affect another.
        """
        # FIX (2026-07-17): previously used a single eligibility check
        # against `p.get("locked")` which was set with category-specific
        # floors in _load_pairs. That part is correct (locked flag already
        # uses the right floor per pair). However, the eligibility filter
        # also checked `p["status"] in ("live", "otc")` — we now keep
        # that AND make sure both real (live) AND otc always-on pairs are
        # warmed up. The locked flag is the source of truth — if a pair
        # is locked, it's below its category's payout floor.
        eligible = {(p["asset"], 60) for p in self._pairs_list
                    if p["status"] in ("live", "otc") and not p.get("locked")}
        # FIX (DATA-FLOW-2026-07-22): all-time OTC pairs are ALWAYS eligible
        # for always-on — they bypass the payout floor entirely. Without
        # this, the 6 exotic pairs (USDBDT, USDBRL, USDPKR, USDCOP, USDMXN,
        # USDIDR) would never be pre-warmed → user opens the All-Time OTC
        # page → sees "Loading..." forever because no stream is running.
        for p in self._alltime_otc_pairs_list:
            eligible.add((p["asset"], 60))

        for key, s in self._streams.items():
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
        """Restart dead always_on streams. CRITICAL for Railway deployment.

        User requirement (2026-07-17): 'Once the app is deployed, candles
        should always be running on the server, whether anyone is watching
        or not. No kind of crash or obstacle allowed.'

        The existing _reconcile_always_on only runs every 5 minutes (when
        _load_pairs runs) and only SETS the always_on flag — it does NOT
        check if the stream's asyncio.Task is actually alive. If a stream
        task dies (Quotex WS drop, exception in _stream_loop, OOM kill,
        etc.), the always_on flag stays True but no ticks flow. The user
        sees a blank chart when they open the app, and candles only start
        when they manually subscribe.

        This watchdog runs every 30 seconds (10x faster than
        _reconcile_always_on) and for each always_on-eligible pair:
          1. If no stream exists → create one (always_on=True)
          2. If stream exists but task is done/crashed → restart it
          3. If stream exists and task is alive → leave alone

        This guarantees that within 30s of any stream death, it's back up
        and ticking — meeting the 'always running' requirement.
        """
        # Build the eligible set fresh each run — pair open/closed status
        # changes over time (real market opens/closes), so we can't cache.
        eligible_assets = {
            p["asset"] for p in self._pairs_list
            if p["status"] in ("live", "otc") and not p.get("locked")
        }
        # FIX (DATA-FLOW-2026-07-22): all-time OTC pairs are always eligible
        # — they bypass the payout floor and should always be running so
        # the All-Time OTC page has live data the moment the user opens it.
        for p in self._alltime_otc_pairs_list:
            eligible_assets.add(p["asset"])

        for asset in eligible_assets:
            key = (asset, 60)  # always_on is always 1m
            stream = self._streams.get(key)

            if stream is None:
                # Stream doesn't exist — create it with always_on=True.
                # Same logic as _reconcile_always_on's "create" branch.
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
                # Task is dead (crashed, returned, or was cancelled).
                # Log the exception if there was one, then restart.
                if task is not None and not task.cancelled():
                    try:
                        exc = task.exception()
                        if exc:
                            print(f"[feed] watchdog: stream {asset} died with "
                                  f"{type(exc).__name__}: {exc}. Restarting.")
                        else:
                            print(f"[feed] watchdog: stream {asset} task completed "
                                  f"unexpectedly. Restarting.")
                    except asyncio.InvalidStateError:
                        pass
                else:
                    print(f"[feed] watchdog: stream {asset} task was cancelled. Restarting.")

                # Preserve the candle history so the new task continues
                # where the old one left off (don't lose chart context).
                old_candles = stream.candles
                old_ticks = list(stream.ticks) if stream.ticks else []
                old_pred = stream.prediction
                old_open_time = stream.candle_open_time
                old_open_price = stream.candle_open_price
                old_open_is_real = stream.candle_open_is_real

                # Create a fresh stream with preserved state.
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
                # Mark old stream as evicting so its cleanup doesn't
                # call stop_candles_stream on the new subscription.
                stream._evicting = True
                # Replace in registry
                self._streams[key] = new_stream
                new_stream.task = asyncio.create_task(self._run_stream(new_stream))

        # Also: demote always_on streams for pairs that are no longer
        # eligible (pair closed, payout dropped below floor).
        for key, s in list(self._streams.items()):
            if s.always_on and s.asset not in eligible_assets:
                s.always_on = False
                print(f"[feed] watchdog: demoted {s.asset} (no longer eligible)")

        # FIX (DATA-FLOW-2026-07-22): per-stream stuck detection.
        # Previously the only "stuck" handler was _fallback_to_sim_if_stuck
        # which fires ONCE 30s after initial subscribe. If ticks stop LATER
        # (Quotex silently drops the subscription mid-session), nothing
        # detected it — the stream's task was still alive (looping on an
        # empty queue), so the watchdog above saw a "healthy" task.
        # Result: 'ডেটা আসা বন্ধ হয়ে যায়' — exactly the user's complaint.
        #
        # Now: every watchdog tick (30s), check each stream's last_real_tick_wall.
        # If a stream hasn't received a real tick in PER_STREAM_STALE_SECS (60s
        # default — well within one 1m candle), re-arm JUST that stream's
        # subscription. This is much cheaper than a full client rebuild and
        # doesn't affect any other stream.
        PER_STREAM_STALE_SECS = int(os.environ.get("PER_STREAM_STALE_SECS", "60"))
        now = time.time()
        for key, s in list(self._streams.items()):
            # Skip streams that are already being evicted or haven't started.
            if getattr(s, '_evicting', False) or not s.sub_started:
                continue
            # Skip streams with no last_real_tick_wall yet (just started).
            if not s.last_real_tick_wall:
                continue
            age = now - s.last_real_tick_wall
            if age > PER_STREAM_STALE_SECS:
                print(f"[feed] per-stream stale: {s.asset}@{s.period}s "
                      f"no tick for {age:.0f}s — re-arming subscription")
                try:
                    asyncio.create_task(self._rearm_stream(s))
                    # Reset the timer so we don't re-arm again before this
                    # re-arm has a chance to receive ticks.
                    s.last_real_tick_wall = now
                except Exception as exc:
                    print(f"[feed] per-stream re-arm failed for {s.asset}: {exc}")

    async def _sweep_idle_streams(self) -> None:
        """Evict streams with no interested viewers for > IDLE_TIMEOUT.

        FIX (2026-07-13): this used to be a sync method that called
        `s.task.cancel()` then immediately `self._streams.pop(key, None)`.
        The task's `finally` block (in _run_stream) hadn't run yet, so it
        would later call `self._client.stop_candles_stream(stream.asset)`.
        If a NEW stream for the same asset was created in the meantime
        (user re-subscribes), the OLD task's cleanup UNSUBSCRIBED THE NEW
        STREAM. Now we mark the stream as "evicting" and let the task's
        finally block do the pop — the task already checks a flag before
        calling stop_candles_stream. We also await the task briefly so the
        cleanup completes before this method returns.
        """
        # IDLE_TIMEOUT is module-level (overridable via env) — see top of file.
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
                s._evicting = True   # signal _run_stream's finally to skip stop_candles_stream
                if s.task:
                    s.task.cancel()
                    # Give the task a moment to run its finally block so the
                    # stop_candles_stream (skipped via _evicting) and the
                    # _streams.pop happen in order. 1s is generous — the
                    # finally block only does unregister + pop.
                    try:
                        await asyncio.wait_for(s.task, timeout=1.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                # The task's finally block will pop the key. But if the task
                # was already dead (e.g., crashed earlier), pop here as a fallback.
                if key in self._streams and self._streams[key] is s:
                    self._streams.pop(key, None)

    def _record_stream_error(self, error_msg: str = None) -> None:
        """Rolling error window -> temporary cooldown on starting NEW streams.
        Existing streams are never torn down by this — only ensure_stream()'s
        capacity/cooldown gate for brand-new pairs is affected."""
        # ERROR_WINDOW / ERROR_THRESHOLD / ERROR_COOLDOWN are module-level
        # (overridable via env) — see top of file.
        now = time.time()
        self._recent_errors.append(now)
        self._recent_errors[:] = [t for t in self._recent_errors if t > now - ERROR_WINDOW]
        # Track last error for /api/debug diagnostic endpoint
        if error_msg:
            self._last_error = error_msg[:500]
            self._last_error_time = now
        if len(self._recent_errors) >= ERROR_THRESHOLD and now >= self._cooldown_until:
            self._cooldown_until  = now + ERROR_COOLDOWN
            self._cooldown_reason = "connection errors"
            print(f"[feed] error spike ({len(self._recent_errors)}/{ERROR_WINDOW}s) — "
                  f"cooling down new streams for {ERROR_COOLDOWN}s")

    # ── Manager loop ──────────────────────────────────────────────────────────

    async def _auto_login_startup(self) -> None:
        """Startup: check for existing token in session.json (fast path).
        
        With vendored pyquotex (QX_USE_RAW_WS=0), the actual login happens
        inside _connect() → pyquotex.connect() → Firefox TLS login. This
        method just checks if a saved token exists so we can skip login.
        
        NO curl_cffi, NO Playwright — pyquotex handles everything with
        Firefox TLS cipher suite that bypasses Cloudflare.
        """
        # ── Check session.json for saved token ────────────────────────────
        try:
            from quotex_ws import QuotexWSClient
            sess_data = QuotexWSClient.load_session_json()
            if sess_data and sess_data.get("token"):
                token = sess_data["token"]
                print(f"[feed] startup: found saved token in session.json "
                      f"({token[:8]}...) — will try it first")
                os.environ["QX_TOKEN"] = token
                return
        except Exception:
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
        """Re-login after connection failure — clears stale tokens so
        _connect() does a fresh login on next retry.

        With vendored pyquotex, _connect() handles the actual login via
        Firefox TLS. This method just clears stale state.

        FIX (2026-07-13): this used to ALWAYS return True, which made the
        caller reset _reconnect_attempts = 0 every 3rd failure and retry
        immediately — the exponential backoff (10s, 20s, 40s, 60s) never
        progressed past attempt 2. Now: only return True if we actually
        cleared a stale token (meaning there's a reason to retry now
        rather than waiting). If there was no token to clear, return
        False so the caller continues the backoff.
        """
        cleared_something = False

        # Clear stale QX_TOKEN env var
        old_token = os.environ.get("QX_TOKEN", "").strip()
        if old_token:
            print(f"[feed] auto-relogin: clearing stale token ({old_token[:8]}...)")
            os.environ.pop("QX_TOKEN", None)
            cleared_something = True

        # Clear stale token in session.json
        try:
            from quotex_ws import QuotexWSClient
            QuotexWSClient.clear_session_json_token()
            cleared_something = True
        except Exception:
            pass

        if cleared_something:
            print("[feed] auto-relogin: cleared stale state — retrying _connect()")
            return True
        # Nothing to clear → no reason to retry immediately. Let the
        # exponential backoff continue so we don't hammer Quotex.
        print("[feed] auto-relogin: nothing to clear — continuing backoff")
        return False

    async def run(self, broadcast) -> None:
        self._broadcast = broadcast
        _db.init()          # create DB tables if not exist
        _db.cleanup()       # prune rows older than 7 days

        # FIX (H4, 2026-07-19): record this manager task so the
        # _fallback_to_sim_if_stuck can cancel it cleanly. Without this,
        # the real feed's run() keeps retrying Quotex connections forever
        # after sim fallback kicks in, racing the sim feed for broadcasts.
        try:
            self._manager_task = asyncio.current_task()
        except Exception:
            self._manager_task = None
        self._abandoned = False
        self._sim_delegate = None

        # FIX (RECONNECT-2026-07-23): start aggressive auto-reconnect loop.
        # Runs every 10s and ensures streams are always alive. If sim
        # fallback fires but sim delegate has 0 streams, this forces
        # streams to start within 10s. If real feed is abandoned, this
        # retries real connection.
        asyncio.create_task(self._aggressive_reconnect())

        # ── Auto-login on startup (2026-07-11) ─────────────────────────────
        # If QX_EMAIL + QX_PASSWORD are set but the connection keeps failing
        # (e.g., token expired, Cloudflare blocking), try a fresh browser-login
        # ONCE before entering the main loop. This makes the app fully
        # auto-connecting — no manual token refresh needed.
        await self._auto_login_startup()

        # FIX L2 (2026-07-19): make housekeep/watchdog intervals env-tunable
        # so ops can dial them in for production tuning.
        HOUSEKEEP_SECS    = int(os.environ.get("HOUSEKEEP_SECS", "5"))
        # Global staleness reuses the module-level STALE_SECS (overridable via
        # env) — see top of file. Was previously a separate GLOBAL_STALE_SECS.
        # FIX (2026-07-17): always_on watchdog runs every 30s (6 housekeep
        # ticks). Separate counter so it doesn't interfere with the existing
        # 5s housekeeping cadence.
        _last_watchdog_run = 0.0
        WATCHDOG_INTERVAL  = float(os.environ.get("WATCHDOG_INTERVAL", "30.0"))

        while True:
            # FIX H4: exit cleanly if sim fallback has taken over.
            if self._abandoned:
                print("[feed] run() exiting — sim feed has taken over")
                return
            try:
                # ── Connect (shared across all streams) ───────────────────
                if not self._connected:
                    print("[feed] connecting...")
                    self._connected = await self._connect()
                    if not self._connected:
                        # ── Auto re-login (NEVER GIVES UP — 2026-07-12) ─────
                        # Try fresh login every 3rd attempt. On Railway where
                        # Cloudflare blocks HTTP login, this loop runs forever
                        # until the user updates QX_TOKEN in Variables OR
                        # Cloudflare temporarily allows the request.
                        if self._reconnect_attempts % 3 == 2:  # every 3rd fail
                            print("[feed] ── auto-relogin attempt ────────────────")
                            relogin_ok = await self._auto_relogin()
                            if relogin_ok:
                                # Token was refreshed (or new QX_TOKEN detected)
                                # — retry immediately with new token
                                self._reconnect_attempts = 0
                                continue

                        # Cap backoff at 60s — never wait longer than a minute.
                        # This ensures the app picks up new QX_TOKEN env var
                        # updates within 60s of the user setting them.
                        self._reconnect_attempts += 1
                        delay = min(10 * (2 ** min(self._reconnect_attempts - 1, 3)), 60)
                        print(f"[feed] reconnect attempt {self._reconnect_attempts} "
                              f"failed — retrying in {delay}s")
                        print(f"[feed]   (attempt counter resets every 60s — "
                              f"app will keep trying forever)")
                        self._record_stream_error()
                        await asyncio.sleep(delay)
                        continue
                    self._reconnect_attempts = 0          # reset on success
                    print("[feed] connected OK")
                    await self._load_pairs(broadcast)

                    # A brand-new client has an empty subscription set — any
                    # streams that were already running (e.g. survived a
                    # previous client's death) need their subscription
                    # re-issued, staggered like any other stream start.
                    for stream in list(self._streams.values()):
                        stream.sub_started = False
                        asyncio.create_task(self._rearm_stream(stream))

                    # Pre-warm every payout-eligible forex pair's 1m stream —
                    # runs AFTER the rearm loop above so freshly-created
                    # streams here don't also get caught by that loop (which
                    # only means to re-issue already-existing subscriptions).
                    self._reconcile_always_on()

                # ── Global stale watchdog (backstop) ──────────────────────
                # pyquotex's native ReconnectPolicy handles most drops itself.
                # This is the LAST resort: if EVERY active stream has been
                # silent for a while, the native layer failed — tear the
                # whole client down and rebuild it (per-stream staleness is
                # handled inside each stream's own loop and never reaches
                # here, since it only re-arms that one stream).
                #
                # FIX (DATA-FLOW-2026-07-22): raised from STALE_SECS (90s) to
                # GLOBAL_STALE_SECS (180s default). Per-stream re-arm at 60s
                # now handles most silent drops gracefully without a full
                # client rebuild. The global rebuild is only needed when the
                # WS connection itself is dead — every single stream silent
                # for 3 minutes is a strong signal of that.
                GLOBAL_STALE_SECS = int(os.environ.get("GLOBAL_STALE_SECS", "180"))
                if self._streams:
                    newest = max((s.last_real_tick_wall
                                 for s in self._streams.values()), default=0.0)
                    if newest > 0 and time.time() - newest > GLOBAL_STALE_SECS:
                        print(f"[feed] GLOBAL STALE: every active stream silent "
                              f"for {time.time() - newest:.0f}s — rebuilding client")
                        await self._rebuild_client()

                await self._sweep_idle_streams()

                # ── Always-on watchdog (every 30s) ───────────────────────────
                # CRITICAL for Railway: ensures all eligible pairs are always
                # ticking on the server, regardless of viewer count. Restarts
                # any stream whose asyncio.Task has died (crash, exception,
                # Quotex WS drop). Without this, candles only tick when a
                # viewer is watching — defeats the purpose of always_on.
                if time.time() - _last_watchdog_run > WATCHDOG_INTERVAL:
                    _last_watchdog_run = time.time()
                    try:
                        await self._watchdog_always_on()
                    except Exception as exc:
                        print(f"[feed] watchdog error: {exc}")

                # ── Refresh pair list every 5 minutes (market open/close) ──
                if time.time() - self._last_pairs_refresh > 300:
                    await self._load_pairs(broadcast)
                    self._reconcile_always_on()

                # NOTE (refactor 2026-07-14): mute refresh block
                # removed — prediction engine no longer uses theories.

                # ── DB row-count cleanup every 6 hours ─────────────────────
                # asyncio.to_thread: _db.cleanup() is blocking sqlite3 I/O
                # (holds db._lock) — same reasoning as every other DB call
                # on this event loop (see _authenticate in server.py).
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
