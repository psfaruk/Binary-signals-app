"""
Module 6 (REAL engine): Trend-Follow Engine

Replaces the OTC engine's `otc_pattern` module. In REAL markets (live
exchange prices, real order flow), trends are MORE persistent than in
OTC — institutional money drives multi-candle continuations, and
mean-reversion is weaker. This module is tuned for that:

  1. Momentum continuation (3+ same-direction with rising bodies → continue)
  2. EMA alignment confirmation (EMA9 > EMA21 + price above EMA9 → uptrend)
  3. Breakout confirmation (close above recent swing high → continuation)
  4. Higher-high / higher-low structure (HH/HL → uptrend, LH/LL → downtrend)
  5. ATR expansion (volatility expanding in trend direction → momentum)

Reliability: TREND ×1.3 (real-market trends are structurally meaningful)

FIX (2026-07-17): the previous version of this file was a verbatim copy
of engines/otc/modules/otc_pattern.py — it was running OTC mean-reversion
logic on real-market pairs, defeating the entire purpose of having two
engines. This file is now a proper trend-following module.
"""
from engines.real.types import ModuleResult, MarketContext


def analyze(candles, ctx: MarketContext) -> list:
    """Run trend-following pattern detection for real-market pairs.

    Returns list of ModuleResult objects.
    """
    results = []
    if len(candles) < 10:
        return results

    stats = ctx.stats
    regime = ctx.regime
    ema9 = ctx.ema9
    ema21 = ctx.ema21
    atr = ctx.atr
    last = candles[-1]
    close = last["close"]
    o, h, l, c = last["open"], last["high"], last["low"], last["close"]

    # ── SIGNAL 1: Momentum continuation (3+ same-direction with rising bodies) ─
    # In real markets, a 3-candle streak with INCREASING body sizes is a
    # momentum signal — institutional buyers/sellers are stepping in harder.
    # Opposite of the OTC engine which treats 3+ streaks as reversal.
    consec = stats["current_streak"]
    streak_dir = stats["streak_direction"]
    if consec >= 3 and len(candles) >= 4:
        # Check if bodies are RISING (increasing commitment)
        bodies = [abs(candles[i]["close"] - candles[i]["open"])
                  for i in range(-consec, 0)]
        rising_bodies = all(bodies[i] >= bodies[i-1] * 0.85
                            for i in range(1, len(bodies)) if bodies[i-1] > 0)
        if rising_bodies:
            # Continuation: 3+ rising-body candles → next candle continues
            if streak_dir == 1:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="CALL", score=3, confidence=63,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_MOMENTUM",
                    reasons=[f"3+ rising-body UP candles → CALL continuation (momentum building)"]))
            elif streak_dir == -1:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="PUT", score=3, confidence=63,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_MOMENTUM",
                    reasons=[f"3+ rising-body DOWN candles → PUT continuation (momentum building)"]))

    # ── SIGNAL 2: EMA alignment confirmation ─────────────────────────────
    # EMA9 > EMA21 + price above EMA9 → uptrend continuation
    # EMA9 < EMA21 + price below EMA9 → downtrend continuation
    # Only fires when EMA separation is meaningful (>0.02% of price).
    if ema9 > 0 and ema21 > 0:
        sep_pct = abs(ema9 - ema21) / max(ema21, 0.0001) * 100
        if sep_pct > 0.02:
            if ema9 > ema21 and close > ema9:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="CALL", score=2, confidence=58,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_EMA",
                    reasons=[f"EMA9 > EMA21 (sep={sep_pct:.3f}%) + close above EMA9 → CALL continuation"]))
            elif ema9 < ema21 and close < ema9:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="PUT", score=2, confidence=58,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_EMA",
                    reasons=[f"EMA9 < EMA21 (sep={sep_pct:.3f}%) + close below EMA9 → PUT continuation"]))

    # ── SIGNAL 3: Breakout confirmation ──────────────────────────────────
    # Close above the highest high of the last 10 candles → bullish breakout
    # Close below the lowest low of the last 10 candles → bearish breakout
    # (Only fires when ATR is not elevated — breakouts in high-vol regimes
    #  are unreliable, often fakeouts.)
    if len(candles) >= 10 and not regime.get("is_volatile", False):
        lookback = candles[-11:-1]
        recent_high = max(x["high"] for x in lookback)
        recent_low = min(x["low"] for x in lookback)
        # Only count as breakout if close is meaningfully beyond the level
        # (not just a 1-pip poke).
        buffer = atr * 0.15 if atr > 0 else 0
        if close > recent_high + buffer:
            results.append(ModuleResult(
                module_name="trend_follow", direction="CALL", score=3, confidence=62,
                signal_type="CONTINUATION", reliability="TREND", group="TREND_BREAKOUT",
                reasons=[f"Breakout above 10-candle high ({recent_high:.5f}) → CALL continuation"]))
        elif close < recent_low - buffer:
            results.append(ModuleResult(
                module_name="trend_follow", direction="PUT", score=3, confidence=62,
                signal_type="CONTINUATION", reliability="TREND", group="TREND_BREAKOUT",
                reasons=[f"Breakdown below 10-candle low ({recent_low:.5f}) → PUT continuation"]))

    # ── SIGNAL 4: Higher-high / higher-low structure ─────────────────────
    # Classic Dow Theory uptrend: each swing high higher than previous,
    # each swing low higher than previous. Only check on the last ~10
    # candles so the structure is recent.
    if len(candles) >= 10:
        # Find last 2 swing highs and last 2 swing lows in the last 10 candles
        window = candles[-10:]
        swing_highs = []
        swing_lows = []
        for i in range(2, len(window) - 2):
            cx = window[i]
            if (cx["high"] >= window[i-1]["high"] and cx["high"] >= window[i-2]["high"]
                    and cx["high"] >= window[i+1]["high"] and cx["high"] >= window[i+2]["high"]):
                swing_highs.append(cx["high"])
            if (cx["low"] <= window[i-1]["low"] and cx["low"] <= window[i-2]["low"]
                    and cx["low"] <= window[i+1]["low"] and cx["low"] <= window[i+2]["low"]):
                swing_lows.append(cx["low"])
        # HH + HL → uptrend; LH + LL → downtrend
        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            if swing_highs[-1] > swing_highs[-2] and swing_lows[-1] > swing_lows[-2]:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="CALL", score=2, confidence=60,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_STRUCTURE",
                    reasons=["HH + HL structure → CALL continuation (Dow uptrend)"]))
            elif swing_highs[-1] < swing_highs[-2] and swing_lows[-1] < swing_lows[-2]:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="PUT", score=2, confidence=60,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_STRUCTURE",
                    reasons=["LH + LL structure → PUT continuation (Dow downtrend)"]))

    # ── SIGNAL 5: ATR expansion in trend direction ───────────────────────
    # When volatility is expanding (vol_pct > 1.0) AND regime is trending,
    # the trend has fuel — boost continuation signals.
    vol_pct = ctx.vol_pct
    if vol_pct > 1.1 and regime.get("is_trending", False):
        regime_dir = "TREND_UP" if regime.get("regime") == "TREND_UP" else "TREND_DOWN"
        if regime_dir == "TREND_UP":
            results.append(ModuleResult(
                module_name="trend_follow", direction="CALL", score=2, confidence=57,
                signal_type="CONTINUATION", reliability="TREND", group="TREND_VOL",
                reasons=[f"ATR expansion ({vol_pct:.1f}x) in uptrend → CALL momentum boost"]))
        else:
            results.append(ModuleResult(
                module_name="trend_follow", direction="PUT", score=2, confidence=57,
                signal_type="CONTINUATION", reliability="TREND", group="TREND_VOL",
                reasons=[f"ATR expansion ({vol_pct:.1f}x) in downtrend → PUT momentum boost"]))

    return results
