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
from engines.base.types import ModuleResult, MarketContext


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
    # FIX (Bug 13, deep audit 2026-07-19): cap the lookback to
    # min(consec, len(candles)) so `range(-consec, 0)` never indexes past
    # the start of the list. Previously, if `consec > len(candles)` (rare
    # but possible at cold-start when streak is computed from a long
    # virtual history but the candle list is short), `candles[-consec]`
    # would raise IndexError.
    if consec >= 3 and len(candles) >= 4:
        lookback = min(consec, len(candles))
        bodies = [abs(candles[i]["close"] - candles[i]["open"])
                  for i in range(-lookback, 0)]
        rising_bodies = all(bodies[i] >= bodies[i-1] * 0.85
                            for i in range(1, len(bodies)) if bodies[i-1] > 0)
        if rising_bodies and len(bodies) >= 3:
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
    #
    # FIX (Real issue 3, 2026-07-19): the previous version used only the
    # last candle's close vs the lookback high/low + an ATR buffer. That
    # misfires on:
    #   (a) News wicks — a single-candle spike that closes inside the
    #       range but had a high/low poke outside. We now require the
    #       candle BODY (not just high/low) to extend beyond the level.
    #   (b) Stop-hunt fakeouts — a candle breaks the level, the next one
    #       reverses. We now require multi-candle confirmation: BOTH the
    #       prior candle AND the current candle must close beyond the
    #       level. A single-candle poke gets a weaker "unconfirmed" vote.
    #   (c) Recent same-direction failure — if a breakout within the last
    #       6 candles was immediately reversed, suppress the new vote
    #       (fakeout-pattern anti-trigger).
    #
    # FIX (anti-fakeout no-op, 2026-07-19, AUDIT-ENGINES #19): the
    # anti-fakeout loop scanned candles[-7:-1] and compared each candle's
    # close against `recent_high` / `recent_low`. But `recent_high` was
    # computed from `candles[-12:-2]` — which OVERLAPS the scan range
    # ([-7,-2] is inside [-12,-2]). So a candle that set the high would
    # trivially have `c["close"] > recent_high` only if it closed ABOVE
    # its OWN high — impossible. The condition `c["close"] > recent_high`
    # was structurally always False (a candle's close is at most its
    # high, and recent_high is the MAX high over a window that includes
    # the candle itself). The anti-fakeout was therefore a no-op.
    # Fix: compute the level from a window that EXCLUDES the scan range.
    # The scan checks candles[-7:-1] (6 candles). The level must come
    # from candles BEFORE that, i.e. candles[-13:-7]. This way the
    # scan can actually find failed breakouts against a real prior level.
    if len(candles) >= 14 and not regime.get("is_volatile", False):
        # Level window: candles[-12:-2] (10 candles, EXCLUDES current + prior).
        lookback = candles[-12:-2]
        recent_high = max(x["high"] for x in lookback)
        recent_low = min(x["low"] for x in lookback)
        buffer = atr * 0.15 if atr > 0 else 0
        prior_close = candles[-2]["close"]

        # Anti-fakeout: scan the last 6 closed candles (excluding current).
        # FIX (AUDIT-ENGINES #19): use a SEPARATE pre-level window that
        # does NOT overlap the scan range, so failed breakouts against a
        # real prior level can actually be detected.
        # Pre-level window: candles[-13:-7] (6 candles before the scan range).
        # Requires len(candles) >= 14 (one extra for the offset).
        pre_level_window = candles[-13:-7]
        pre_level_high = max(x["high"] for x in pre_level_window)
        pre_level_low = min(x["low"] for x in pre_level_window)
        # If pre_level_high < recent_high, the level ROSE between the two
        # windows — use the older (pre-level) high as the "broken level".
        # If pre_level_high > recent_high, the level FELL — use recent_high.
        # The actual level being tested for failure is the OLDER one
        # (pre_level), because the scan range candles were attempting to
        # break THAT level, not the newer one.
        failed_level_high = pre_level_high
        failed_level_low = pre_level_low

        recent_failed_bull = False
        recent_failed_bear = False
        # FIX (Bug 18, deep audit 2026-07-19): the previous loop
        # `range(-7, -1)` only checked 6 candles, missing older failed
        # breakouts within the lookback window. Now we scan
        # `range(-min(11, len(candles)-1), -1)` so we check up to 10 prior
        # candles (matching the 10-candle lookback used for recent_high).
        scan_start = -min(11, len(candles) - 1)
        for i in range(scan_start, -1):
            if abs(i) > len(candles) - 1:
                continue
            c = candles[i]
            nc = candles[i + 1] if (i + 1) < 0 else None
            if nc is None:
                continue
            # Bullish attempt: candle i closed above the failed level,
            # next candle closed back below — failed breakout.
            if c["close"] > failed_level_high and nc["close"] < failed_level_high:
                recent_failed_bull = True
            # Bearish attempt: candle i closed below the failed level,
            # next candle closed back above — failed breakdown.
            if c["close"] < failed_level_low and nc["close"] > failed_level_low:
                recent_failed_bear = True

        # Bullish breakout: current close above recent_high + buffer AND body extends
        # beyond the level (filters out upper-wick-only pokes).
        if close > recent_high + buffer and (close - max(o, recent_high)) > buffer:
            # Multi-candle confirmation: prior candle also closed above the level.
            confirmed = prior_close > recent_high
            if recent_failed_bull:
                # Recent fakeout → skip (anti-trigger)
                pass
            elif confirmed:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="CALL", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_BREAKOUT",
                    reasons=[f"Confirmed breakout above 10-candle high ({recent_high:.5f}, body-confirmed, prior close {prior_close:.5f}) → CALL continuation"]))
            else:
                # Single-candle breakout, no confirmation yet — weaker vote.
                results.append(ModuleResult(
                    module_name="trend_follow", direction="CALL", score=2, confidence=55,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_BREAKOUT",
                    reasons=[f"Unconfirmed breakout above 10-candle high ({recent_high:.5f}, prior close {prior_close:.5f}) → CALL (weak, no multi-candle confirm)"]))
        # Bearish breakout (mirror)
        elif close < recent_low - buffer and (min(o, recent_low) - close) > buffer:
            confirmed = prior_close < recent_low
            if recent_failed_bear:
                pass
            elif confirmed:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="PUT", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_BREAKOUT",
                    reasons=[f"Confirmed breakdown below 10-candle low ({recent_low:.5f}, body-confirmed, prior close {prior_close:.5f}) → PUT continuation"]))
            else:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="PUT", score=2, confidence=55,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_BREAKOUT",
                    reasons=[f"Unconfirmed breakdown below 10-candle low ({recent_low:.5f}, prior close {prior_close:.5f}) → PUT (weak, no multi-candle confirm)"]))

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
    # FIX (AUDIT-ENGINES #20, 2026-07-19): the previous version used
    # `regime_dir = "TREND_UP" if regime.get("regime") == "TREND_UP" else "TREND_DOWN"`
    # — meaning RANGE and VOLATILE regimes defaulted to TREND_DOWN, firing
    # a PUT momentum boost in non-trending markets. Now we only fire this
    # signal when the regime is EXPLICITLY TREND_UP or TREND_DOWN (the
    # enclosing `if` already requires is_trending=True, but we now check
    # the regime name explicitly to avoid the else-default bug).
    vol_pct = ctx.vol_pct
    regime_name = regime.get("regime", "RANGE")
    if vol_pct > 1.1 and regime.get("is_trending", False) and regime_name in ("TREND_UP", "TREND_DOWN"):
        if regime_name == "TREND_UP":
            results.append(ModuleResult(
                module_name="trend_follow", direction="CALL", score=2, confidence=57,
                signal_type="CONTINUATION", reliability="TREND", group="TREND_VOL",
                reasons=[f"ATR expansion ({vol_pct:.1f}x) in uptrend → CALL momentum boost"]))
        else:  # TREND_DOWN
            results.append(ModuleResult(
                module_name="trend_follow", direction="PUT", score=2, confidence=57,
                signal_type="CONTINUATION", reliability="TREND", group="TREND_VOL",
                reasons=[f"ATR expansion ({vol_pct:.1f}x) in downtrend → PUT momentum boost"]))

    return results
