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
from engines.real.types import ModuleResult, MarketContext


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

    return results
