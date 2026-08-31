"""
Module: MULTI-TIMEFRAME CONFIRMATION (DEEP-FIX-2026-08-07)

Checks whether the 1-minute candle signal is aligned with higher-timeframe
trends (5-min and 15-min). A signal aligned with BOTH higher timeframes
is significantly more likely to win than one fighting both.

Research basis:
  - 1-min signals aligned with 5-min trend: +2-4% win rate boost
  - 1-min signals aligned with 15-min trend: +3-6% win rate boost
  - Counter-trend on BOTH: -5-8% win rate penalty
  - This module was missing — HTF alignment was a crude multiplier in
    blender.py (×0.7 or ×1.1), not a proper voting module with its own
    evidence weight.

Output:
  - Aligned with both HTFs → CONFIRM (boost signal)
  - Counter-trend on 5m only → WEAKEN
  - Counter-trend on both → VETO-worthy (strong counter-signal)
  - Sideways HTF → PASS (no opinion)
"""
from engines.base.types import ModuleResult, MarketContext


def _ema(values, period):
    """Exponential moving average."""
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / min(period, len(values))
    if len(values) <= period:
        return seed
    result = seed
    for v in values[period:]:
        result = v * k + result * (1 - k)
    return result


def _build_n_min_closes(candles, n_candles):
    """Aggregate 1-min candles into n-min closes by timestamp-boundary alignment.

    FIX (HTF-ALIGN-FIX-2026-08-31): the old version chunked candles[i:i+n]
    from the START of the rolling buffer, which is almost never on an n-minute
    wall boundary. That produced "5m/15m candles" spanning wall boundaries and
    injected artificial momentum/reversal noise into the HTF EMA trend — the
    exact Bug A that feed._aggregate_5m_closes (2026-07-19) already fixed for
    the 5m HTF trend, but this module was never ported.

    Now: floor each 1m candle's `time` to its n-minute bucket, group candles
    in the same bucket, emit the close of the LAST candle in each bucket.
    Partial (trailing) buckets are kept — their close still reflects the most
    recent price, and EMA responds to the newest value.

    Args:
        candles: list of candle dicts with at least "time" and "close"
                 (`time` seconds or ms — auto-detected by magnitude).
        n_candles: bucket size in 1-min candles (5 → 5-minute buckets).

    Returns:
        List of n-min close prices, oldest → newest.
    """
    if len(candles) < n_candles:
        return []
    # Auto-detect seconds vs milliseconds (same heuristic as feed.py).
    t0 = candles[0].get("time", 0)
    tN = candles[-1].get("time", 0)
    ms_mode = (t0 > 10_000_000_000 or tN > 10_000_000_000)
    bucket_seconds = n_candles * 60
    closes = []
    current_bucket = None
    prev_close = 0.0
    for c in candles:
        t = c.get("time", 0)
        if ms_mode:
            t = t / 1000
        bucket = (int(t) // bucket_seconds) * bucket_seconds
        if current_bucket is None or bucket != current_bucket:
            if current_bucket is not None:
                closes.append(prev_close)
            current_bucket = bucket
        prev_close = c["close"]
    if current_bucket is not None:
        closes.append(prev_close)
    return closes


def analyze(candles, ctx: MarketContext) -> list:
    """Check HTF alignment and produce confirm/weaken votes."""
    if len(candles) < 15:
        return []

    results = []
    closes_1m = ctx.closes if ctx.closes else [c["close"] for c in candles]

    # ── 5-minute trend ──────────────────────────────────────────────────
    closes_5m = _build_n_min_closes(candles, 5)
    if len(closes_5m) >= 4:
        ema5_short = _ema(closes_5m, 3)   # 15-min EMA on 5-min candles
        ema5_long = _ema(closes_5m, 6)    # 30-min EMA on 5-min candles
        trend_5m = "UP" if ema5_short > ema5_long else ("DOWN" if ema5_short < ema5_long else "SIDEWAYS")
    else:
        # Fallback: use 5 direct 1-min closes vs 20
        if len(closes_1m) >= 20:
            ema5 = _ema(closes_1m, 5)
            ema20 = _ema(closes_1m, 20)
            trend_5m = "UP" if ema5 > ema20 else ("DOWN" if ema5 < ema20 else "SIDEWAYS")
        else:
            trend_5m = "SIDEWAYS"

    # ── 15-minute trend ─────────────────────────────────────────────────
    closes_15m = _build_n_min_closes(candles, 15)
    if len(closes_15m) >= 3:
        ema15_short = _ema(closes_15m, 2)
        ema15_long = _ema(closes_15m, 4)
        trend_15m = "UP" if ema15_short > ema15_long else ("DOWN" if ema15_short < ema15_long else "SIDEWAYS")
    else:
        # Fallback: 15 vs 50 simple MA
        if len(closes_1m) >= 50:
            ma15 = sum(closes_1m[-15:]) / 15
            ma50 = sum(closes_1m[-50:]) / 50
            trend_15m = "UP" if ma15 > ma50 else ("DOWN" if ma15 < ma50 else "SIDEWAYS")
        elif len(closes_1m) >= 20:
            ma15 = sum(closes_1m[-15:]) / 15
            ma20 = sum(closes_1m[-20:]) / 20
            trend_15m = "UP" if ma15 > ma20 else ("DOWN" if ma15 < ma20 else "SIDEWAYS")
        else:
            trend_15m = "SIDEWAYS"

    # Get current signal from last candle
    last = candles[-1]
    signal = "CALL" if last["close"] >= last["open"] else "PUT"

    # ── Determine alignment ──────────────────────────────────────────────
    align_5m = (trend_5m == "UP" and signal == "CALL") or \
               (trend_5m == "DOWN" and signal == "PUT")
    counter_5m = (trend_5m == "UP" and signal == "PUT") or \
                 (trend_5m == "DOWN" and signal == "CALL")
    align_15m = (trend_15m == "UP" and signal == "CALL") or \
                (trend_15m == "DOWN" and signal == "PUT")
    counter_15m = (trend_15m == "UP" and signal == "PUT") or \
                  (trend_15m == "DOWN" and signal == "CALL")

    # ── Vote ─────────────────────────────────────────────────────────────
    if align_5m and align_15m:
        # Strongest: both HTFs confirm
        results.append(ModuleResult(
            module_name="multi_tf",
            direction=signal,
            score=3,
            confidence=65,
            signal_type="CONTINUATION",
            reliability="CANDLE",
            group="MULTI_TF",
            reasons=[f"HTF CONFIRM: 5m={trend_5m}, 15m={trend_15m} "
                     f"both aligned with {signal} → strong confirmation"],
        ))
    elif align_5m and trend_15m == "SIDEWAYS":
        # Moderate: 5m confirms, 15m neutral
        results.append(ModuleResult(
            module_name="multi_tf",
            direction=signal,
            score=2,
            confidence=58,
            signal_type="CONTINUATION",
            reliability="CANDLE",
            group="MULTI_TF",
            reasons=[f"HTF CONFIRM: 5m={trend_5m} aligned, "
                     f"15m={trend_15m} neutral → moderate confirmation"],
        ))
    elif counter_5m and counter_15m:
        # Strong counter-signal: both HTFs oppose → VETO-worthy
        results.append(ModuleResult(
            module_name="multi_tf",
            direction="PUT" if signal == "CALL" else "CALL",
            score=3,
            confidence=62,
            signal_type="REVERSAL",
            reliability="CANDLE",
            group="MULTI_TF",
            reasons=[f"HTF COUNTER: 5m={trend_5m}, 15m={trend_15m} "
                     f"both opposing {signal} → strong counter-signal"],
        ))
    elif counter_5m or counter_15m:
        # Mild counter: one HTF opposes
        opposing = "5m" if counter_5m else "15m"
        results.append(ModuleResult(
            module_name="multi_tf",
            direction="PUT" if signal == "CALL" else "CALL",
            score=1,
            confidence=52,
            signal_type="REVERSAL",
            reliability="CANDLE",
            group="MULTI_TF",
            reasons=[f"HTF WEAKEN: {opposing}={trend_5m if counter_5m else trend_15m} "
                     f"opposes {signal} → mild counter-signal"],
        ))
    # else: sideways on both → no vote (PASS)

    return results
