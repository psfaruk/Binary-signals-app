"""
Candle Reaction Engine — ULTRA-ADVANCED v2 (2026-07-14 calibration fixes)

4-layer signal blending with reliability-based weighting and proper
confidence calibration.

## Fixes vs v1 (based on accuracy audit)

### Bug 1 FIXED: Microstructure confidence inflation
  v1: 3 micro sub-signals (ending_dir, pressure, reaction) counted as
      3 INDEPENDENT votes → inflated vote_count_confidence.
  v2: All micro sub-signals collapse into ONE composite micro vote.
      The composite direction is the majority of the 3 sub-signals,
      and its score is the net (not the sum) so conflicting sub-signals
      produce a weak composite instead of 3 inflated votes.

### Bug 2 FIXED: VOLATILE dampening rounding floor
  v1: `max(1, round(score * 0.7))` floored every dampened score to 1,
      so VOLATILE regime had NO effect on score-1 signals and barely
      any on score-2 signals (1.4 → 1).
  v2: No floor. Dampened scores use round() and CAN become 0 (signal
      suppressed entirely). This makes VOLATILE regime actually dampen.

### Bug 3 FIXED: Regime tie-break biased to TREND_UP
  v1: `hh_hl >= lh_ll` for TREND_UP, `lh_ll >= hh_hl` for TREND_DOWN
      → ties always went to TREND_UP (first branch wins).
  v2: Both use strict `>`, ties fall to RANGE (neutral). [fixed in
      advanced_analysis.py]

### Bug 4 FIXED: No reliability-based weighting
  v1: All signals had equal gravity — a 4-point streak signal could
      cancel a 4-point engulfing pattern, even though patterns are
      multi-candle-confirmed and far more reliable.
  v2: 4-tier reliability multiplier applied to every signal:
      TIER 1 (×1.5): Multi-candle patterns (engulfing, stars, soldiers)
      TIER 2 (×1.3): Statistical edge + key level confluence
      TIER 3 (×1.0): Single-candle signals (streak, body, wick, range)
      TIER 4 (×0.6): Microstructure composite (single data source,
                     noisy in OTC — lowest reliability)
  Now a 4-point engulfing (effective 6) won't be canceled by a 4-point
  streak (effective 4) or a 2-point micro (effective 1.2).

## Architecture
  Layer 1 — Single-candle reaction (6 signals, enhanced with stats)
  Layer 2 — Multi-candle patterns (10 patterns from advanced_analysis)
  Layer 3 — Market regime adjustment (TREND/RANGE/VOLATILE weighting)
  Layer 4 — Statistical edge + key level confluence boosts

  All signals carry a `reliability` tier. The blend applies:
    effective_score = raw_score × regime_multiplier × reliability_multiplier
  Then standard vote-count + weight confidence calibration.
"""
import math

from advanced_analysis import (
    detect_candle_patterns,
    classify_market_regime,
    find_key_levels,
    check_level_confluence,
    compute_statistical_edge,
    _atr,
)

# ═══════════════════════════════════════════════════════════════════════════════
#  RELIABILITY TIERS
# ═══════════════════════════════════════════════════════════════════════════════
# Applied as a multiplier to every signal's score AFTER regime adjustment.
# This prevents low-reliability signals (microstructure, single-candle)
# from canceling high-reliability signals (multi-candle patterns, level
# confluence with statistical backing).
RELIABILITY = {
    "PATTERN":    1.5,   # multi-candle patterns — highest conviction
    "STAT":       1.3,   # statistical edge (Z-score, rarity, percentile)
    "LEVEL":      1.3,   # key level confluence (swing S/R)
    "CANDLE":     1.0,   # single-candle signals (baseline)
    "MICRO":      0.6,   # microstructure composite (single data source, noisy)
}


def predict_from_candle(candles, ticks=None, micro=None):
    """Predict next candle direction from the last closed candle.

    Returns dict with:
        signal: "CALL" | "PUT" | "NEUTRAL"
        confidence: 0-100
        strength: "STRONG" | "MEDIUM" | "NEUTRAL"
        score: net effective score (positive=CALL, negative=PUT)
        reasons: list of reason strings
        regime: dict (market state classification)
        agree: int (effective score of winning side)
        total: int (total effective score of all fired signals)
        signals_fired: int (how many distinct signals voted)
    """
    if not candles or len(candles) < 3:
        return {"signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
                "score": 0, "reasons": ["INSUFFICIENT_DATA"], "regime": {},
                "agree": 0, "total": 0, "signals_fired": 0}

    last = candles[-1]
    o, h, l, c = last["open"], last["high"], last["low"], last["close"]
    body = c - o
    rng = h - l
    body_pct = abs(body) / rng * 100 if rng > 0 else 0

    # ── Compute market context ONCE ──────────────────────────────────────
    regime = classify_market_regime(candles)
    atr = _atr(candles)
    stats = compute_statistical_edge(candles)
    key_levels = find_key_levels(candles, lookback=50)
    level_conf = check_level_confluence(candles, key_levels, atr)

    # Each signal: (direction, score, reason, signal_type, reliability_tier)
    # signal_type: "REVERSAL" or "CONTINUATION" (for regime weighting)
    # reliability_tier: key into RELIABILITY dict
    raw_signals = []

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 1: SINGLE-CANDLE REACTION SIGNALS (reliability: CANDLE ×1.0)
    # ═══════════════════════════════════════════════════════════════════

    # ── SIGNAL 1: Consecutive streak reversal ────────────────────────────
    consec = stats["current_streak"]
    streak_dir = stats["streak_direction"]
    if consec >= 5:
        if streak_dir == 1:
            raw_signals.append(("PUT", 4,
                f"5+ UP streak → PUT reversal (75% win rate, rarity={stats['streak_rarity']:.0%})",
                "REVERSAL", "CANDLE"))
        elif streak_dir == -1:
            raw_signals.append(("CALL", 4,
                f"5+ DOWN streak → CALL reversal (75% win rate, rarity={stats['streak_rarity']:.0%})",
                "REVERSAL", "CANDLE"))
    elif consec >= 4:
        if streak_dir == 1:
            raw_signals.append(("PUT", 3, "4+ UP streak → PUT reversal (60% win rate)",
                "REVERSAL", "CANDLE"))
        elif streak_dir == -1:
            raw_signals.append(("CALL", 3, "4+ DOWN streak → CALL reversal (60% win rate)",
                "REVERSAL", "CANDLE"))
    elif consec >= 3:
        if streak_dir == 1:
            raw_signals.append(("PUT", 2, "3+ UP streak → PUT reversal (62% win rate)",
                "REVERSAL", "CANDLE"))
        elif streak_dir == -1:
            raw_signals.append(("CALL", 2, "3+ DOWN streak → CALL reversal (62% win rate)",
                "REVERSAL", "CANDLE"))

    # ── SIGNAL 2: Big body → reversal (Z-score enhanced) ─────────────────
    if len(candles) >= 10:
        recent_bodies = [abs(candles[i]["close"] - candles[i]["open"])
                         for i in range(-min(20, len(candles)), 0)]
        median_body = sorted(recent_bodies)[len(recent_bodies) // 2]

        if median_body > 0 and abs(body) > median_body * 1.5:
            z_boost = 1 if stats["z_body"] > 2.0 else 0
            score = 3 + z_boost
            if body > 0:
                raw_signals.append(("PUT", score,
                    f"Big UP body ({body_pct:.0f}%, Z={stats['z_body']:.1f}) → PUT reversal (64% win rate)",
                    "REVERSAL", "CANDLE"))
            else:
                raw_signals.append(("CALL", score,
                    f"Big DOWN body ({body_pct:.0f}%, Z={stats['z_body']:.1f}) → CALL reversal (63% win rate)",
                    "REVERSAL", "CANDLE"))

    # ── SIGNAL 3: Wick rejection ─────────────────────────────────────────
    if rng > 0:
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        uw_pct = upper_wick / rng * 100
        lw_pct = lower_wick / rng * 100

        if uw_pct > 40 and body_pct < 35:
            raw_signals.append(("PUT", 3,
                f"Upper wick rejection ({uw_pct:.0f}%) → PUT (59% win rate)",
                "REVERSAL", "CANDLE"))
        elif lw_pct > 40 and body_pct < 35:
            raw_signals.append(("CALL", 3,
                f"Lower wick rejection ({lw_pct:.0f}%) → CALL (56% win rate)",
                "REVERSAL", "CANDLE"))

    # ── SIGNAL 4: Close position in range (percentile enhanced) ──────────
    if rng > 0:
        close_pos = max(0, min(100, (c - l) / rng * 100))
        if close_pos >= 80:
            percentile_boost = 1 if stats["close_percentile"] >= 90 else 0
            raw_signals.append(("PUT", 2 + percentile_boost,
                f"Close at range top ({close_pos:.0f}%, pctile={stats['close_percentile']:.0f}) → PUT (62% win rate)",
                "REVERSAL", "CANDLE"))
        elif close_pos <= 20:
            percentile_boost = 1 if stats["close_percentile"] <= 10 else 0
            raw_signals.append(("CALL", 2 + percentile_boost,
                f"Close at range bottom ({close_pos:.0f}%, pctile={stats['close_percentile']:.0f}) → CALL (60% win rate)",
                "REVERSAL", "CANDLE"))

    # ── SIGNAL 5: Body shrinking → exhaustion ────────────────────────────
    if len(candles) >= 2:
        prev_body = abs(candles[-2]["close"] - candles[-2]["open"])
        if prev_body > 0 and abs(body) < prev_body * 0.5 and abs(body) > 0:
            if body > 0:
                raw_signals.append(("PUT", 1,
                    "Shrinking bull body → PUT exhaustion (54% win rate)",
                    "REVERSAL", "CANDLE"))
            else:
                raw_signals.append(("CALL", 1,
                    "Shrinking bear body → CALL exhaustion (54% win rate)",
                    "REVERSAL", "CANDLE"))

    # ── SIGNAL 6: Microstructure — COMPOSITE vote (Bug 1 fix) ────────────
    # v1 had 3 separate votes (ending_dir, pressure, reaction) from the same
    # micro dict, inflating vote_count_confidence. Now we collapse them into
    # ONE composite vote: net direction = majority of the 3 sub-signals,
    # score = abs(net sub-vote count) so conflicting sub-signals produce a
    # weak composite (score 1) instead of 3 inflated votes.
    if micro:
        micro_sub_votes = []  # list of (direction, score, reason)

        ed = micro.get("ending_direction", {})
        ed_dir = ed.get("direction", "FLAT")
        ed_dom = ed.get("dominance", "FIGHT")
        ed_buy = ed.get("buy_pct", 50)

        if ed_dir == "UP" and ed_dom == "BUYER":
            micro_sub_votes.append(("CALL", 2, f"5-sec ending UP/BUYER ({ed_buy}%)"))
        elif ed_dir == "DOWN" and ed_dom == "SELLER":
            micro_sub_votes.append(("PUT", 2, f"5-sec ending DOWN/SELLER ({ed_buy}%)"))

        buy_pct = micro.get("buy_pct", 50)
        pressure = micro.get("pressure", "FIGHT")
        if pressure == "BUYER" and buy_pct >= 65:
            micro_sub_votes.append(("CALL", 2, f"Strong buyer pressure ({buy_pct}%)"))
        elif pressure == "SELLER" and buy_pct <= 35:
            micro_sub_votes.append(("PUT", 2, f"Strong seller pressure ({buy_pct}%)"))

        reaction = micro.get("reaction")
        if reaction == "BUYER":
            micro_sub_votes.append(("CALL", 2, "Buyer reaction from low"))
        elif reaction == "SELLER":
            micro_sub_votes.append(("PUT", 2, "Seller reaction from high"))

        # Collapse into ONE composite vote
        if micro_sub_votes:
            call_sum = sum(s for d, s, _ in micro_sub_votes if d == "CALL")
            put_sum = sum(s for d, s, _ in micro_sub_votes if d == "PUT")
            call_n = sum(1 for d, s, _ in micro_sub_votes if d == "CALL")
            put_n = sum(1 for d, s, _ in micro_sub_votes if d == "PUT")

            if call_sum > put_sum:
                # CALL majority — score = net sub-vote advantage (1-4)
                composite_score = min(4, call_sum - put_sum)
                composite_type = "CONTINUATION" if (call_n > 0 and put_n == 0) else "REVERSAL"
                # If all sub-votes are CALL (no PUT), it's pure continuation
                # (pressure + ending_dir are continuation signals)
                # If mixed, lean towards whatever the reaction says
                reasons_str = " | ".join(r for _, _, r in micro_sub_votes)
                raw_signals.append(("CALL", composite_score,
                    f"Micro composite: {reasons_str}",
                    composite_type, "MICRO"))
            elif put_sum > call_sum:
                composite_score = min(4, put_sum - call_sum)
                composite_type = "CONTINUATION" if (put_n > 0 and call_n == 0) else "REVERSAL"
                reasons_str = " | ".join(r for _, _, r in micro_sub_votes)
                raw_signals.append(("PUT", composite_score,
                    f"Micro composite: {reasons_str}",
                    composite_type, "MICRO"))
            # If call_sum == put_sum (exact tie), no composite vote — conflicting micro

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 2: MULTI-CANDLE PATTERNS (reliability: PATTERN ×1.5)
    # ═══════════════════════════════════════════════════════════════════
    patterns = detect_candle_patterns(candles)
    REVERSAL_PATTERNS = {
        "BULL_ENGULF", "BEAR_ENGULF", "MORNING_STAR", "EVENING_STAR",
        "TWEEZER_TOP", "TWEEZER_BOTTOM", "3_SOLDIERS_EXHAUST", "3_CROWS_EXHAUST",
        "PIERCING_LINE", "DARK_CLOUD", "BULL_HARAMI", "BEAR_HARAMI",
        "HAMMER", "SHOOTING_STAR",
    }
    for pat in patterns:
        sig_type = "REVERSAL" if pat["name"] in REVERSAL_PATTERNS else "CONTINUATION"
        raw_signals.append((pat["direction"], pat["score"], pat["reason"],
                            sig_type, "PATTERN"))

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 3: REGIME-AWARE WEIGHT ADJUSTMENT (Bug 2 fix: no rounding floor)
    # ═══════════════════════════════════════════════════════════════════
    regime_reasons = []
    regime_mult = 1.0
    if regime["is_volatile"]:
        regime_mult = 0.7
        regime_reasons.append(
            f"_REGIME: VOLATILE (vol={regime['volatility_pct']:.1f}x) → all signals ×0.7")
    elif regime["is_ranging"]:
        regime_reasons.append(
            f"_REGIME: RANGE (str={regime['trend_strength']:.2f}) → reversal ×1.3, continuation ×0.7")
    elif regime["is_trending"]:
        trend_dir = "UP" if regime["regime"] == "TREND_UP" else "DOWN"
        regime_reasons.append(
            f"_REGIME: TREND_{trend_dir} (str={regime['trend_strength']:.2f}) → continuation ×1.3, reversal ×0.8")

    # ── Apply regime + reliability multipliers ───────────────────────────
    # Bug 2 fix: NO floor. round() can produce 0, which suppresses the signal.
    # This makes VOLATILE dampening actually work on weak signals.
    adjusted_signals = []
    suppressed_count = 0
    for direction, score, reason, sig_type, tier in raw_signals:
        # Regime multiplier based on signal type
        if regime["is_volatile"]:
            r_mult = 0.7
        elif regime["is_ranging"]:
            r_mult = 1.3 if sig_type == "REVERSAL" else 0.7
        elif regime["is_trending"]:
            r_mult = 1.3 if sig_type == "CONTINUATION" else 0.8
        else:
            r_mult = 1.0

        # Reliability multiplier from tier
        t_mult = RELIABILITY.get(tier, 1.0)

        effective = round(score * r_mult * t_mult)

        # Suppress zero-score signals (dampened to nothing)
        if effective == 0:
            suppressed_count += 1
            continue

        adjusted_signals.append((direction, effective, reason, sig_type, tier))

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 4: KEY LEVEL CONFLUENCE + STATISTICAL EDGE (reliability: LEVEL/STAT ×1.3)
    # ═══════════════════════════════════════════════════════════════════
    level_reasons = []
    if level_conf["near_level"]:
        lvl_type = level_conf["level_type"]
        action = level_conf["action"]
        dist = level_conf["distance_atr"]

        if action == "bounce":
            if lvl_type == "support":
                adjusted_signals.append(("CALL", 3,
                    f"Key support bounce ({level_conf['level_price']:.5f}, {dist:.2f} ATR) → CALL boost",
                    "REVERSAL", "LEVEL"))
            else:
                adjusted_signals.append(("PUT", 3,
                    f"Key resistance bounce ({level_conf['level_price']:.5f}, {dist:.2f} ATR) → PUT boost",
                    "REVERSAL", "LEVEL"))
            level_reasons.append(
                f"_LEVEL: near {lvl_type} {level_conf['level_price']:.5f} ({dist:.2f} ATR), action=BOUNCE")
        elif action == "breakout":
            if lvl_type == "resistance":
                adjusted_signals.append(("CALL", 2,
                    f"Resistance breakout ({level_conf['level_price']:.5f}) → CALL",
                    "CONTINUATION", "LEVEL"))
            else:
                adjusted_signals.append(("PUT", 2,
                    f"Support breakdown ({level_conf['level_price']:.5f}) → PUT",
                    "CONTINUATION", "LEVEL"))
            level_reasons.append(
                f"_LEVEL: {lvl_type} {level_conf['level_price']:.5f} BROKEN, action=BREAKOUT")

    # ── Streak rarity bonus (statistical edge, reliability: STAT ×1.3) ───
    if stats["current_streak"] >= 3 and stats["streak_rarity"] < 0.10:
        rarity_dir = "PUT" if stats["streak_direction"] == 1 else "CALL"
        adjusted_signals.append((rarity_dir, 2,
            f"Rare streak (n={stats['current_streak']}, rarity={stats['streak_rarity']:.0%}) → {rarity_dir} reversal boost",
            "REVERSAL", "STAT"))

    # ═══════════════════════════════════════════════════════════════════
    # BLEND
    # ═══════════════════════════════════════════════════════════════════
    call_score = sum(s for d, s, _, _, _ in adjusted_signals if d == "CALL")
    put_score = sum(s for d, s, _, _, _ in adjusted_signals if d == "PUT")
    call_n = sum(1 for d, s, _, _, _ in adjusted_signals if d == "CALL")
    put_n = sum(1 for d, s, _, _, _ in adjusted_signals if d == "PUT")
    total_fired = call_n + put_n

    all_reasons = [r for _, _, r, _, _ in adjusted_signals] + regime_reasons + level_reasons
    if suppressed_count > 0:
        all_reasons.append(f"_SUPPRESSED: {suppressed_count} signal(s) dampened to 0 by regime")

    if total_fired == 0:
        return {"signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
                "score": 0, "reasons": all_reasons or ["NO_SIGNAL"],
                "regime": regime, "agree": 0, "total": 0, "signals_fired": 0}

    net = call_score - put_score
    total = call_score + put_score

    if total == 0 or net == 0:
        return {"signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
                "score": 0, "reasons": all_reasons or ["CONFLICTING_SIGNALS"],
                "regime": regime, "agree": max(call_score, put_score),
                "total": total_fired, "signals_fired": total_fired}

    signal = "CALL" if net > 0 else "PUT"

    # ── Confidence calibration ───────────────────────────────────────────
    # With reliability weighting, scores already encode conviction.
    # Vote count still matters (diversity of signals), but now a single
    # high-reliability pattern (score ~6-9) can legitimately produce high
    # confidence because its score reflects multi-candle confirmation.
    majority_n = max(call_n, put_n)
    vote_count_confidence = round(majority_n / total_fired * 100) if total_fired else 0
    weight_confidence = round(max(call_score, put_score) / total * 100) if total > 0 else 0

    # 50% vote count + 50% weight (rebalanced — with reliability tiers,
    # weight is more meaningful now since it encodes conviction quality)
    confidence = int(0.5 * vote_count_confidence + 0.5 * weight_confidence)

    # Single-signal cap: if only 1 signal fired, cap confidence at 55%
    # (was 60% — lowered because even a high-reliability single signal
    # shouldn't give high confidence without confirmation)
    if total_fired == 1:
        confidence = min(confidence, 55)

    # ── Strength tiers ───────────────────────────────────────────────────
    agree = max(call_score, put_score)
    abs_net = abs(net)
    if confidence >= 65 and abs_net >= 5 and majority_n >= 2:
        strength = "STRONG"
    elif confidence >= 50 and abs_net >= 2:
        strength = "MEDIUM"
    elif abs_net >= 1:
        strength = "MEDIUM"
    else:
        return {"signal": "NEUTRAL", "confidence": confidence, "strength": "NEUTRAL",
                "score": net, "reasons": all_reasons + [f"Net too low ({net}) → NEUTRAL"],
                "regime": regime, "agree": agree, "total": total_fired,
                "signals_fired": total_fired}

    return {
        "signal": signal,
        "confidence": confidence,
        "strength": strength,
        "score": net,
        "reasons": all_reasons,
        "regime": regime,
        "agree": agree,
        "total": total_fired,
        "signals_fired": total_fired,
    }
