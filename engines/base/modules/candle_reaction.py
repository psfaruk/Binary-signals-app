"""
Module 1: Candle Reaction Engine

Single-candle price action signals from the last closed candle.
Signals are grouped into BODY (correlated) and WICK (independent) groups.

Signals:
  1. Consecutive streak reversal (3+/4+/5+ same-direction)
  2. Big body → reversal (>1.5× median body, Z-score boosted)
  3. Wick rejection (upper/lower wick >40%)
  4. Close position in range (top/bottom 20%, percentile boosted)
  5. Body shrinking → exhaustion (<50% of previous body)

Volatility-scaled thresholds adapt to market noise level.
"""
from engines.base.types import ModuleResult, MarketContext


def analyze(candles, ctx: MarketContext) -> list:
    """Run all single-candle signals.

    Returns list of ModuleResult objects. Body-derived signals share
    group="BODY" (will be collapsed by blender). Wick signals use
    group="WICK" (independent).
    """
    results = []
    if not candles or len(candles) < 3:
        return results

    last = candles[-1]
    o, h, l, c = last["open"], last["high"], last["low"], last["close"]
    body = c - o
    rng = h - l
    body_pct = abs(body) / rng * 100 if rng > 0 else 0

    stats = ctx.stats
    vol_pct = ctx.vol_pct

    # ── Volatility-scaled thresholds ─────────────────────────────────────
    if vol_pct > 1.3:
        streak_thresh_5, streak_thresh_4, streak_thresh_3 = 6, 5, 4
        body_mult = 2.0
    elif vol_pct < 0.7:
        streak_thresh_5, streak_thresh_4, streak_thresh_3 = 4, 3, 2
        body_mult = 1.3
    else:
        streak_thresh_5, streak_thresh_4, streak_thresh_3 = 5, 4, 3
        body_mult = 1.5

    # ── SIGNAL 1: Consecutive streak reversal (BODY group) ───────────────
    consec = stats["current_streak"]
    streak_dir = stats["streak_direction"]
    if consec >= streak_thresh_5:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=4, confidence=75,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ UP streak → PUT reversal (75% win rate, rarity={stats['streak_rarity']:.0%})"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=4, confidence=75,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ DOWN streak → CALL reversal (75% win rate, rarity={stats['streak_rarity']:.0%})"]))
    elif consec >= streak_thresh_4:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=3, confidence=60,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ UP streak → PUT reversal (60% win rate)"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=3, confidence=60,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ DOWN streak → CALL reversal (60% win rate)"]))
    elif consec >= streak_thresh_3:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=2, confidence=55,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ UP streak → PUT reversal (62% win rate)"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=2, confidence=55,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ DOWN streak → CALL reversal (62% win rate)"]))

    # ── SIGNAL 2: Big body → reversal (BODY group, vol-scaled) ───────────
    if len(candles) >= 10:
        recent_bodies = [abs(candles[i]["close"] - candles[i]["open"])
                         for i in range(-min(20, len(candles)), 0)]
        median_body = sorted(recent_bodies)[len(recent_bodies) // 2]
        if median_body > 0 and abs(body) > median_body * body_mult:
            # FIX (Bug #19, 2026-07-17): Z-score boost threshold is now
            # volatility-scaled (was static Z > 2.0). Matches the same
            # scaling used in otc_pattern.SIGNAL 3 for consistency.
            if vol_pct >= 1.3:
                z_boost_threshold = 2.0
            elif vol_pct <= 0.7:
                z_boost_threshold = 2.8
            else:
                z_boost_threshold = 2.3
            z_boost = 1 if stats["z_body"] > z_boost_threshold else 0
            score = 3 + z_boost
            if body > 0:
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="PUT", score=score, confidence=64,
                    signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                    reasons=[f"Big UP body ({body_pct:.0f}%, Z={stats['z_body']:.1f}, {body_mult}x median) → PUT reversal"]))
            else:
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="CALL", score=score, confidence=63,
                    signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                    reasons=[f"Big DOWN body ({body_pct:.0f}%, Z={stats['z_body']:.1f}, {body_mult}x median) → CALL reversal"]))

    # ── SIGNAL 3: Wick rejection (WICK group — independent) ──────────────
    if rng > 0:
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        uw_pct = upper_wick / rng * 100
        lw_pct = lower_wick / rng * 100
        if uw_pct > 40 and body_pct < 35:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=3, confidence=59,
                signal_type="REVERSAL", reliability="CANDLE", group="WICK",
                reasons=[f"Upper wick rejection ({uw_pct:.0f}%) → PUT (59% win rate)"]))
        elif lw_pct > 40 and body_pct < 35:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=3, confidence=56,
                signal_type="REVERSAL", reliability="CANDLE", group="WICK",
                reasons=[f"Lower wick rejection ({lw_pct:.0f}%) → CALL (56% win rate)"]))

    # ── SIGNAL 4: Close position in range (BODY group) ───────────────────
    if rng > 0:
        close_pos = max(0, min(100, (c - l) / rng * 100))
        if close_pos >= 80:
            percentile_boost = 1 if stats["close_percentile"] >= 90 else 0
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=2 + percentile_boost, confidence=62,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"Close at range top ({close_pos:.0f}%, pctile={stats['close_percentile']:.0f}) → PUT"]))
        elif close_pos <= 20:
            percentile_boost = 1 if stats["close_percentile"] <= 10 else 0
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=2 + percentile_boost, confidence=60,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"Close at range bottom ({close_pos:.0f}%, pctile={stats['close_percentile']:.0f}) → CALL"]))

    # ── SIGNAL 5: Body shrinking → exhaustion (BODY group) ───────────────
    if len(candles) >= 2:
        prev_body = abs(candles[-2]["close"] - candles[-2]["open"])
        if prev_body > 0 and abs(body) < prev_body * 0.5 and abs(body) > 0:
            if body > 0:
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="PUT", score=1, confidence=54,
                    signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                    reasons=["Shrinking bull body → PUT exhaustion (54% win rate)"]))
            else:
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="CALL", score=1, confidence=54,
                    signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                    reasons=["Shrinking bear body → CALL exhaustion (54% win rate)"]))

    # ═══════════════════════════════════════════════════════════════════════
    #  CONTINUATION SIGNALS (NEW, 2026-07-18)
    #  Previously this module was 100% REVERSAL (5/5 signals) — making it
    #  structurally incapable of voting for a trend. Since candle_reaction
    #  fires on EVERY candle close (it's the most active module), this
    #  was the single biggest source of structural reversal bias.
    #
    #  Two CONTINUATION signals added:
    #    6. Rising/falling closes momentum — 3+ candles with monotonically
    #       rising (or falling) closes, each with a non-trivial body.
    #       This is the definition of trend momentum.
    #    7. Trend-aligned wick rejection — a wick rejection in the direction
    #       of a confirmed trend is a continuation signal (e.g. lower wick
    #       during an uptrend = buyers stepped in = continuation).
    #  These are gated on trend regime so they don't fire in RANGE markets
    #  where reversal interpretation is correct.
    # ═══════════════════════════════════════════════════════════════════════

    regime = ctx.regime
    is_trending = regime.get("is_trending", False)
    trend_regime = regime.get("regime", "RANGE")
    trend_strength = regime.get("trend_strength", 0.0)

    # ── CONTINUATION SIGNAL 6: Rising/falling closes momentum ────────────
    # 3+ consecutive candles with monotonically rising closes + each body
    # is non-trivial (>= 30% of range) = clean trend momentum.
    # This is the structural opposite of Signal 1 (streak reversal):
    # Signal 1 bets the streak ENDS, Signal 6 bets the streak CONTINUES.
    # The difference: Signal 6 requires monotonic CLOSES (not just bodies)
    # AND requires a confirmed trend regime — so it only fires when the
    # trend is real, not a 3-candle blip in a range.
    if is_trending and trend_strength > 0.4 and len(candles) >= 3:
        c1_close = candles[-3]["close"]
        c2_close = candles[-2]["close"]
        c3_close = candles[-1]["close"]
        # Monotonic rising closes
        if c1_close < c2_close < c3_close:
            # Each body must be non-trivial (not dojis)
            b1 = abs(candles[-3]["close"] - candles[-3]["open"])
            b2 = abs(candles[-2]["close"] - candles[-2]["open"])
            b3 = abs(body)
            r1 = candles[-3]["high"] - candles[-3]["low"]
            r2 = candles[-2]["high"] - candles[-2]["low"]
            if (r1 > 0 and r2 > 0 and rng > 0
                    and b1/r1 >= 0.30 and b2/r2 >= 0.30 and body_pct >= 30
                    and trend_regime == "TREND_UP"):
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="CALL", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="CANDLE", group="BODY_CONT",
                    reasons=[f"Rising closes momentum (3 UP, str={trend_strength:.2f}) → CALL continuation (62% win rate)"]))
        # Monotonic falling closes
        elif c1_close > c2_close > c3_close:
            b1 = abs(candles[-3]["close"] - candles[-3]["open"])
            b2 = abs(candles[-2]["close"] - candles[-2]["open"])
            b3 = abs(body)
            r1 = candles[-3]["high"] - candles[-3]["low"]
            r2 = candles[-2]["high"] - candles[-2]["low"]
            if (r1 > 0 and r2 > 0 and rng > 0
                    and b1/r1 >= 0.30 and b2/r2 >= 0.30 and body_pct >= 30
                    and trend_regime == "TREND_DOWN"):
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="PUT", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="CANDLE", group="BODY_CONT",
                    reasons=[f"Falling closes momentum (3 DOWN, str={trend_strength:.2f}) → PUT continuation (62% win rate)"]))

    # ── CONTINUATION SIGNAL 7: Trend-aligned wick rejection ──────────────
    # Signal 3 (WICK) always treats wick rejection as REVERSAL. But a
    # lower-wick rejection during an UPTREND is actually a CONTINUATION
    # signal — it means buyers stepped in at the low and pushed price
    # back up, confirming the trend. Similarly for upper-wick in downtrend.
    # This signal fires INSTEAD OF Signal 3 when the wick aligns with
    # a confirmed trend. We use group="WICK_CONT" (not "WICK") so the
    # blender doesn't double-count with Signal 3.
    if is_trending and trend_strength > 0.4 and rng > 0:
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        uw_pct = upper_wick / rng * 100
        lw_pct = lower_wick / rng * 100
        # Lower wick rejection in uptrend = buyers defended the low → CALL continuation
        if lw_pct > 40 and body_pct < 35 and trend_regime == "TREND_UP" and body > 0:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="CANDLE", group="WICK_CONT",
                reasons=[f"Trend-aligned lower wick ({lw_pct:.0f}%, uptrend str={trend_strength:.2f}) → CALL continuation (58% win rate)"]))
        # Upper wick rejection in downtrend = sellers defended the high → PUT continuation
        elif uw_pct > 40 and body_pct < 35 and trend_regime == "TREND_DOWN" and body < 0:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="CANDLE", group="WICK_CONT",
                reasons=[f"Trend-aligned upper wick ({uw_pct:.0f}%, downtrend str={trend_strength:.2f}) → PUT continuation (58% win rate)"]))

    return results
