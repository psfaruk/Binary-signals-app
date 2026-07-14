"""
Candle Reaction Engine — ULTRA-ADVANCED v3 (2026-07-14 calibration v2)

5-layer signal blending with correlation grouping, trend exhaustion gate,
pattern confluence requirement, and volatility-scaled thresholds.

## What's new in v3 (vs v2)

### Improvement 1: Signal Correlation Grouping
  v2: streak(4) + big_body(3) + close_extreme(2) all fire on the same
      candle body → counted as 3 INDEPENDENT votes → inflated confidence.
  v3: Body-derived signals (streak, big_body, close_extreme, body_shrink)
      collapse into ONE "BODY" group vote. Score = max + corroboration
      bonus. Vote count can't be inflated by 4 correlated signals.

### Improvement 2: Trend Exhaustion Gate
  v2: In TREND regime, ALL reversal signals get ×0.8 — even when the
      trend is clearly exhausting (body shrinking + wick rejection).
  v3: If ≥2 exhaustion indicators present (body_shrink + wick_reject +
      streak≥4 + rarity<10%), reversal signals get ×1.0 (no penalty)
      or ×1.2 (boost) instead of ×0.8. Catches trend-end reversals
      that v2 systematically missed.

### Improvement 3: Pattern Confluence Requirement for STRONG
  v2: 3 correlated single-candle signals could produce STRONG without
      any multi-candle pattern confirmation.
  v3: STRONG requires ≥1 PATTERN-tier signal agreeing with the majority.
      Without pattern confirmation, cap at MEDIUM. Patterns are
      multi-candle-confirmed and far more reliable than single-candle.

### Improvement 4: Volatility-Scaled Thresholds
  v2: Fixed thresholds (5+ streak, 1.5× median body) — in high volatility
      these fire too easily (noise), in low volatility too rarely.
  v3: Thresholds scale with volatility_pct:
      - vol > 1.3: streak needs +1, body needs 2× median
      - vol < 0.7: streak needs -1, body needs 1.3× median
      - normal: unchanged

### Improvement 5: Group-Aware Confidence Calibration
  v2: vote_count_confidence = majority_n / total_fired — but total_fired
      counted correlated signals as independent.
  v3: Count unique GROUPS voting (BODY, WICK, MICRO, PATTERN*, LEVEL,
      STAT) instead of raw signals. A 4-signal BODY group counts as 1
      group vote. Confidence reflects true signal diversity.

## Architecture
  Layer 1 — Single-candle signals (6 signals, grouped into BODY + WICK)
  Layer 1b — Microstructure composite (1 vote, MICRO group)
  Layer 2 — Multi-candle patterns (10 patterns, each own group)
  [Collapse BODY group into 1 vote]
  Layer 3 — Regime adjustment (with exhaustion gate override)
  Layer 4 — Statistical edge + key level confluence (STAT, LEVEL groups)
  [Blend with group-aware vote counting]
  [Pattern confluence check for STRONG]
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
RELIABILITY = {
    "PATTERN":    1.5,
    "STAT":       1.3,
    "LEVEL":      1.3,
    "CANDLE":     1.0,
    "MICRO":      0.6,
}


def _collapse_group(signals, group_name):
    """Collapse a group of correlated signals into ONE composite vote.

    Direction = majority (by score sum).
    Score = max score + 1 if ≥3 signals agree (corroboration bonus).
    Reason = combined string of all sub-reasons.

    This prevents 4 correlated body-signals from inflating vote count.
    """
    if not signals:
        return None

    call_sum = sum(s for d, s, _, _, _, _ in signals if d == "CALL")
    put_sum = sum(s for d, s, _, _, _, _ in signals if d == "PUT")
    call_n = sum(1 for d, s, _, _, _, _ in signals if d == "CALL")
    put_n = sum(1 for d, s, _, _, _, _ in signals if d == "PUT")

    if call_sum > put_sum:
        direction = "CALL"
        max_score = max(s for d, s, _, _, _, _ in signals if d == "CALL")
        agree_n = call_n
    elif put_sum > call_sum:
        direction = "PUT"
        max_score = max(s for d, s, _, _, _, _ in signals if d == "PUT")
        agree_n = put_n
    else:
        return None  # exact tie — no group vote

    # Corroboration bonus: if ≥3 signals in the group agree, +1
    bonus = 1 if agree_n >= 3 else 0
    score = max_score + bonus

    # Signal type: if ALL signals are REVERSAL, group is REVERSAL;
    # if ALL are CONTINUATION, group is CONTINUATION; mixed → REVERSAL
    # (body signals are mostly reversal in OTC mean-reverting markets)
    types = set(t for _, _, _, t, _, _ in signals)
    sig_type = "REVERSAL" if "REVERSAL" in types else "CONTINUATION"

    # Combined reason
    reasons_str = " | ".join(r for _, _, r, _, _, _ in signals)
    reason = f"[{group_name}] {reasons_str}"

    # Tier: use the highest tier in the group (CANDLE by default)
    tier = "CANDLE"

    return (direction, score, reason, sig_type, tier, group_name)


def predict_from_candle(candles, ticks=None, micro=None):
    """Predict next candle direction from the last closed candle.

    Returns dict with:
        signal: "CALL" | "PUT" | "NEUTRAL"
        confidence: 0-100
        strength: "STRONG" | "MEDIUM" | "NEUTRAL"
        score: net effective score
        reasons: list of reason strings
        regime: dict (market state classification)
        agree: int (effective score of winning side)
        total: int (total effective score)
        signals_fired: int (how many distinct groups voted)
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

    # ═══════════════════════════════════════════════════════════════════
    # IMPROVEMENT 4: Volatility-scaled thresholds
    # ═══════════════════════════════════════════════════════════════════
    vol_pct = regime["volatility_pct"]
    if vol_pct > 1.3:
        # High volatility — require stronger signals (more noise)
        streak_thresh_5 = 6   # need 6+ instead of 5+
        streak_thresh_4 = 5   # need 5+ instead of 4+
        streak_thresh_3 = 4   # need 4+ instead of 3+
        body_mult = 2.0       # need 2× median body instead of 1.5×
        vol_note = f"_VOL_SCALE: HIGH (vol={vol_pct:.1f}x) → stricter thresholds"
    elif vol_pct < 0.7:
        # Low volatility — lower thresholds (less noise, signals more meaningful)
        streak_thresh_5 = 4
        streak_thresh_4 = 3
        streak_thresh_3 = 2
        body_mult = 1.3
        vol_note = f"_VOL_SCALE: LOW (vol={vol_pct:.1f}x) → looser thresholds"
    else:
        # Normal volatility — standard thresholds
        streak_thresh_5 = 5
        streak_thresh_4 = 4
        streak_thresh_3 = 3
        body_mult = 1.5
        vol_note = ""

    # Each signal: (direction, score, reason, signal_type, reliability_tier, group)
    raw_signals = []

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 1: SINGLE-CANDLE REACTION SIGNALS
    # Body-derived signals → group="BODY" (will be collapsed)
    # Wick-derived signals → group="WICK" (independent)
    # ═══════════════════════════════════════════════════════════════════

    # ── SIGNAL 1: Consecutive streak reversal (BODY group) ───────────────
    consec = stats["current_streak"]
    streak_dir = stats["streak_direction"]
    if consec >= streak_thresh_5:
        if streak_dir == 1:
            raw_signals.append(("PUT", 4,
                f"{consec}+ UP streak → PUT reversal (75% win rate, rarity={stats['streak_rarity']:.0%})",
                "REVERSAL", "CANDLE", "BODY"))
        elif streak_dir == -1:
            raw_signals.append(("CALL", 4,
                f"{consec}+ DOWN streak → CALL reversal (75% win rate, rarity={stats['streak_rarity']:.0%})",
                "REVERSAL", "CANDLE", "BODY"))
    elif consec >= streak_thresh_4:
        if streak_dir == 1:
            raw_signals.append(("PUT", 3, f"{consec}+ UP streak → PUT reversal (60% win rate)",
                "REVERSAL", "CANDLE", "BODY"))
        elif streak_dir == -1:
            raw_signals.append(("CALL", 3, f"{consec}+ DOWN streak → CALL reversal (60% win rate)",
                "REVERSAL", "CANDLE", "BODY"))
    elif consec >= streak_thresh_3:
        if streak_dir == 1:
            raw_signals.append(("PUT", 2, f"{consec}+ UP streak → PUT reversal (62% win rate)",
                "REVERSAL", "CANDLE", "BODY"))
        elif streak_dir == -1:
            raw_signals.append(("CALL", 2, f"{consec}+ DOWN streak → CALL reversal (62% win rate)",
                "REVERSAL", "CANDLE", "BODY"))

    # ── SIGNAL 2: Big body → reversal (BODY group, vol-scaled) ───────────
    if len(candles) >= 10:
        recent_bodies = [abs(candles[i]["close"] - candles[i]["open"])
                         for i in range(-min(20, len(candles)), 0)]
        median_body = sorted(recent_bodies)[len(recent_bodies) // 2]

        if median_body > 0 and abs(body) > median_body * body_mult:
            z_boost = 1 if stats["z_body"] > 2.0 else 0
            score = 3 + z_boost
            if body > 0:
                raw_signals.append(("PUT", score,
                    f"Big UP body ({body_pct:.0f}%, Z={stats['z_body']:.1f}, {body_mult}x median) → PUT reversal",
                    "REVERSAL", "CANDLE", "BODY"))
            else:
                raw_signals.append(("CALL", score,
                    f"Big DOWN body ({body_pct:.0f}%, Z={stats['z_body']:.1f}, {body_mult}x median) → CALL reversal",
                    "REVERSAL", "CANDLE", "BODY"))

    # ── SIGNAL 3: Wick rejection (WICK group — independent) ──────────────
    if rng > 0:
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        uw_pct = upper_wick / rng * 100
        lw_pct = lower_wick / rng * 100

        if uw_pct > 40 and body_pct < 35:
            raw_signals.append(("PUT", 3,
                f"Upper wick rejection ({uw_pct:.0f}%) → PUT (59% win rate)",
                "REVERSAL", "CANDLE", "WICK"))
        elif lw_pct > 40 and body_pct < 35:
            raw_signals.append(("CALL", 3,
                f"Lower wick rejection ({lw_pct:.0f}%) → CALL (56% win rate)",
                "REVERSAL", "CANDLE", "WICK"))

    # ── SIGNAL 4: Close position in range (BODY group) ───────────────────
    if rng > 0:
        close_pos = max(0, min(100, (c - l) / rng * 100))
        if close_pos >= 80:
            percentile_boost = 1 if stats["close_percentile"] >= 90 else 0
            raw_signals.append(("PUT", 2 + percentile_boost,
                f"Close at range top ({close_pos:.0f}%, pctile={stats['close_percentile']:.0f}) → PUT",
                "REVERSAL", "CANDLE", "BODY"))
        elif close_pos <= 20:
            percentile_boost = 1 if stats["close_percentile"] <= 10 else 0
            raw_signals.append(("CALL", 2 + percentile_boost,
                f"Close at range bottom ({close_pos:.0f}%, pctile={stats['close_percentile']:.0f}) → CALL",
                "REVERSAL", "CANDLE", "BODY"))

    # ── SIGNAL 5: Body shrinking → exhaustion (BODY group) ───────────────
    body_shrink_fired = False
    if len(candles) >= 2:
        prev_body = abs(candles[-2]["close"] - candles[-2]["open"])
        if prev_body > 0 and abs(body) < prev_body * 0.5 and abs(body) > 0:
            body_shrink_fired = True
            if body > 0:
                raw_signals.append(("PUT", 1,
                    "Shrinking bull body → PUT exhaustion (54% win rate)",
                    "REVERSAL", "CANDLE", "BODY"))
            else:
                raw_signals.append(("CALL", 1,
                    "Shrinking bear body → CALL exhaustion (54% win rate)",
                    "REVERSAL", "CANDLE", "BODY"))

    # ── SIGNAL 6: Microstructure composite (MICRO group) ─────────────────
    if micro:
        micro_sub_votes = []
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

        if micro_sub_votes:
            call_sum = sum(s for d, s, _ in micro_sub_votes if d == "CALL")
            put_sum = sum(s for d, s, _ in micro_sub_votes if d == "PUT")
            call_n = sum(1 for d, s, _ in micro_sub_votes if d == "CALL")
            put_n = sum(1 for d, s, _ in micro_sub_votes if d == "PUT")

            if call_sum > put_sum:
                composite_score = min(4, call_sum - put_sum)
                composite_type = "CONTINUATION" if (call_n > 0 and put_n == 0) else "REVERSAL"
                reasons_str = " | ".join(r for _, _, r in micro_sub_votes)
                raw_signals.append(("CALL", composite_score,
                    f"Micro composite: {reasons_str}",
                    composite_type, "MICRO", "MICRO"))
            elif put_sum > call_sum:
                composite_score = min(4, put_sum - call_sum)
                composite_type = "CONTINUATION" if (put_n > 0 and call_n == 0) else "REVERSAL"
                reasons_str = " | ".join(r for _, _, r in micro_sub_votes)
                raw_signals.append(("PUT", composite_score,
                    f"Micro composite: {reasons_str}",
                    composite_type, "MICRO", "MICRO"))

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 2: MULTI-CANDLE PATTERNS (each pattern = own group)
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
        # Each pattern gets its own group so they're counted as independent votes
        raw_signals.append((pat["direction"], pat["score"], pat["reason"],
                            sig_type, "PATTERN", f"PATTERN_{pat['name']}"))

    # ═══════════════════════════════════════════════════════════════════
    # IMPROVEMENT 1: Collapse BODY group into 1 vote
    # ═══════════════════════════════════════════════════════════════════
    body_signals = [s for s in raw_signals if s[5] == "BODY"]
    non_body_signals = [s for s in raw_signals if s[5] != "BODY"]

    collapsed_body = _collapse_group(body_signals, "BODY")
    if collapsed_body:
        raw_signals = non_body_signals + [collapsed_body]
    else:
        raw_signals = non_body_signals

    # ═══════════════════════════════════════════════════════════════════
    # IMPROVEMENT 2: Trend Exhaustion Gate
    # ═══════════════════════════════════════════════════════════════════
    # Count exhaustion indicators (independent of signal direction)
    exhaustion_indicators = 0
    if body_shrink_fired:
        exhaustion_indicators += 1
    # Check if wick rejection fired (any direction)
    wick_fired = any(s[5] == "WICK" for s in raw_signals)
    if wick_fired:
        exhaustion_indicators += 1
    if stats["current_streak"] >= 4:
        exhaustion_indicators += 1
    if stats["streak_rarity"] < 0.10 and stats["current_streak"] >= 3:
        exhaustion_indicators += 1

    is_exhausting = exhaustion_indicators >= 2
    is_strongly_exhausting = exhaustion_indicators >= 3

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 3: REGIME-AWARE WEIGHT ADJUSTMENT (with exhaustion gate)
    # ═══════════════════════════════════════════════════════════════════
    regime_reasons = []
    if vol_note:
        regime_reasons.append(vol_note)

    if regime["is_volatile"]:
        regime_reasons.append(
            f"_REGIME: VOLATILE (vol={regime['volatility_pct']:.1f}x) → all signals ×0.7")
    elif regime["is_ranging"]:
        regime_reasons.append(
            f"_REGIME: RANGE (str={regime['trend_strength']:.2f}) → reversal ×1.3, continuation ×0.7")
    elif regime["is_trending"]:
        trend_dir = "UP" if regime["regime"] == "TREND_UP" else "DOWN"
        if is_strongly_exhausting:
            regime_reasons.append(
                f"_REGIME: TREND_{trend_dir} BUT strongly exhausting ({exhaustion_indicators} indicators) → reversal ×1.2 (override)")
        elif is_exhausting:
            regime_reasons.append(
                f"_REGIME: TREND_{trend_dir} BUT exhausting ({exhaustion_indicators} indicators) → reversal ×1.0 (no penalty)")
        else:
            regime_reasons.append(
                f"_REGIME: TREND_{trend_dir} (str={regime['trend_strength']:.2f}) → continuation ×1.3, reversal ×0.8")

    # ── Apply regime + reliability multipliers ───────────────────────────
    adjusted_signals = []
    suppressed_count = 0
    for direction, score, reason, sig_type, tier, group in raw_signals:
        # Regime multiplier (with exhaustion gate override for reversal in trend)
        if regime["is_volatile"]:
            r_mult = 0.7
        elif regime["is_ranging"]:
            r_mult = 1.3 if sig_type == "REVERSAL" else 0.7
        elif regime["is_trending"]:
            if sig_type == "CONTINUATION":
                r_mult = 1.3
            else:  # REVERSAL
                # Exhaustion gate: override the ×0.8 penalty
                if is_strongly_exhausting:
                    r_mult = 1.2  # boost (trend is dying)
                elif is_exhausting:
                    r_mult = 1.0  # no penalty
                else:
                    r_mult = 0.8  # standard trend penalty
        else:
            r_mult = 1.0

        t_mult = RELIABILITY.get(tier, 1.0)
        effective = round(score * r_mult * t_mult)

        if effective == 0:
            suppressed_count += 1
            continue

        adjusted_signals.append((direction, effective, reason, sig_type, tier, group))

    # ═══════════════════════════════════════════════════════════════════
    # LAYER 4: KEY LEVEL CONFLUENCE + STATISTICAL EDGE
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
                    "REVERSAL", "LEVEL", "LEVEL"))
            else:
                adjusted_signals.append(("PUT", 3,
                    f"Key resistance bounce ({level_conf['level_price']:.5f}, {dist:.2f} ATR) → PUT boost",
                    "REVERSAL", "LEVEL", "LEVEL"))
            level_reasons.append(
                f"_LEVEL: near {lvl_type} {level_conf['level_price']:.5f} ({dist:.2f} ATR), action=BOUNCE")
        elif action == "breakout":
            if lvl_type == "resistance":
                adjusted_signals.append(("CALL", 2,
                    f"Resistance breakout ({level_conf['level_price']:.5f}) → CALL",
                    "CONTINUATION", "LEVEL", "LEVEL"))
            else:
                adjusted_signals.append(("PUT", 2,
                    f"Support breakdown ({level_conf['level_price']:.5f}) → PUT",
                    "CONTINUATION", "LEVEL", "LEVEL"))
            level_reasons.append(
                f"_LEVEL: {lvl_type} {level_conf['level_price']:.5f} BROKEN, action=BREAKOUT")

    # ── Streak rarity bonus (STAT group) ─────────────────────────────────
    if stats["current_streak"] >= 3 and stats["streak_rarity"] < 0.10:
        rarity_dir = "PUT" if stats["streak_direction"] == 1 else "CALL"
        adjusted_signals.append((rarity_dir, 2,
            f"Rare streak (n={stats['current_streak']}, rarity={stats['streak_rarity']:.0%}) → {rarity_dir} reversal boost",
            "REVERSAL", "STAT", "STAT"))

    # ═══════════════════════════════════════════════════════════════════
    # BLEND (group-aware)
    # ═══════════════════════════════════════════════════════════════════
    call_score = sum(s for d, s, _, _, _, _ in adjusted_signals if d == "CALL")
    put_score = sum(s for d, s, _, _, _, _ in adjusted_signals if d == "PUT")

    # IMPROVEMENT 5: Count unique GROUPS (not raw signals) for vote count
    call_groups = set(g for d, s, _, _, _, g in adjusted_signals if d == "CALL")
    put_groups = set(g for d, s, _, _, _, g in adjusted_signals if d == "PUT")
    all_groups = call_groups | put_groups
    total_groups = len(all_groups)

    all_reasons = [r for _, _, r, _, _, _ in adjusted_signals] + regime_reasons + level_reasons
    if suppressed_count > 0:
        all_reasons.append(f"_SUPPRESSED: {suppressed_count} signal(s) dampened to 0 by regime")

    if total_groups == 0:
        return {"signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
                "score": 0, "reasons": all_reasons or ["NO_SIGNAL"],
                "regime": regime, "agree": 0, "total": 0, "signals_fired": 0}

    net = call_score - put_score
    total = call_score + put_score

    if total == 0 or net == 0:
        return {"signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
                "score": 0, "reasons": all_reasons or ["CONFLICTING_SIGNALS"],
                "regime": regime, "agree": max(call_score, put_score),
                "total": total_groups, "signals_fired": total_groups}

    signal = "CALL" if net > 0 else "PUT"

    # ── Group-aware confidence calibration ───────────────────────────────
    # Count unique groups voting for the majority direction
    majority_groups = call_groups if signal == "CALL" else put_groups
    minority_groups = put_groups if signal == "CALL" else call_groups
    majority_group_n = len(majority_groups)

    # Vote count confidence: unique groups for majority / total unique groups
    vote_count_confidence = round(majority_group_n / total_groups * 100) if total_groups else 0
    weight_confidence = round(max(call_score, put_score) / total * 100) if total > 0 else 0

    confidence = int(0.5 * vote_count_confidence + 0.5 * weight_confidence)

    # Single-group cap: if only 1 group voted, cap confidence at 55%
    if total_groups == 1:
        confidence = min(confidence, 55)

    # ── Strength tiers ───────────────────────────────────────────────────
    agree = max(call_score, put_score)
    abs_net = abs(net)

    # IMPROVEMENT 3: Pattern confluence requirement for STRONG
    # STRONG requires ≥1 PATTERN-tier signal agreeing with majority
    has_pattern_confluence = any(
        t == "PATTERN" and d == signal
        for d, s, _, t, _, _ in adjusted_signals
    )

    if (confidence >= 65 and abs_net >= 5 and majority_group_n >= 2
            and has_pattern_confluence):
        strength = "STRONG"
    elif (confidence >= 65 and abs_net >= 5 and majority_group_n >= 2
          and not has_pattern_confluence):
        # Would be STRONG but no pattern confirmation → cap at MEDIUM
        strength = "MEDIUM"
        all_reasons.append("_DOWNGRADE: STRONG→MEDIUM (no pattern confluence)")
    elif confidence >= 50 and abs_net >= 2:
        strength = "MEDIUM"
    elif abs_net >= 1:
        strength = "MEDIUM"
    else:
        return {"signal": "NEUTRAL", "confidence": confidence, "strength": "NEUTRAL",
                "score": net, "reasons": all_reasons + [f"Net too low ({net}) → NEUTRAL"],
                "regime": regime, "agree": agree, "total": total_groups,
                "signals_fired": total_groups}

    return {
        "signal": signal,
        "confidence": confidence,
        "strength": strength,
        "score": net,
        "reasons": all_reasons,
        "regime": regime,
        "agree": agree,
        "total": total_groups,
        "signals_fired": total_groups,
    }
