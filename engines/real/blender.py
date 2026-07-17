"""
Smart Blender — combines 6 independent modules into final prediction.

Pipeline:
  1. Compute shared MarketContext ONCE
  2. Run all 6 modules (candle_reaction, running_tick, pattern,
     indicator, key_level, otc_pattern)
  3. Collapse correlated groups (BODY signals → 1 vote)
  4. Apply regime-aware weighting (TREND/RANGE/VOLATILE + exhaustion gate)
  5. Apply per-pair module weighting (USDPKR → boost reversal, EURUSD → boost indicator)
  6. Apply reliability tier multipliers (PATTERN ×1.5 > STAT/LEVEL ×1.3 > CANDLE ×1.0 > MICRO ×0.6)
  7. Blend: confidence-weighted vote
  8. Pattern confluence check for STRONG
  9. Group-aware confidence calibration
  10. Strength tier determination

This is the ONLY public entry point: predict()
"""
from engines.real.types import ModuleResult, MarketContext, RELIABILITY
from engines.real.context import compute_context
from engines.real.per_pair import get_weights, get_profile
from engines.real.modules import (
    candle_reaction as mod_candle,
    running_tick as mod_tick,
    pattern as mod_pattern,
    indicator as mod_indicator,
    key_level as mod_keylevel,
    otc_pattern as mod_otc,
)


def predict(candles, ticks=None, micro=None, asset="", htf_trend="SIDEWAYS",
            period: int = 60) -> dict:
    """Main entry point — runs 6 modules + smart blend.

    Args:
        candles: list of closed candle dicts (time, open, high, low, close)
        ticks: tick list for the closed candle (optional)
        micro: microstructure dict (optional)
        asset: pair name for per-pair weighting (e.g. "EURUSD_otc")
        htf_trend: "UPTREND" | "DOWNTREND" | "SIDEWAYS" from 5m EMA confluence.
            Counter-HTF signals get a 0.7 penalty; aligned signals get 1.1 boost.
            "SIDEWAYS" (uncertain HTF) leaves signals unaffected.
        period: candle period in seconds (default 60). Used by per_pair
            DB-adaptation to look up the right historical accuracy bucket.

    Returns dict with:
        signal: "CALL" | "PUT" | "NEUTRAL"
        confidence: 0-100
        strength: "STRONG" | "MEDIUM" | "NEUTRAL"
        score: net effective score
        reasons: list of reason strings
        regime: dict
        agree: int
        total: int (unique groups voted)
        signals_fired: int (unique groups)
        modules: dict of per-module breakdown for UI
        asset: str
        profile: str (pair behavior profile)
        htf_trend: str (echo for UI/logging)
    """
    if not candles or len(candles) < 3:
        return _neutral("INSUFFICIENT_DATA", {}, asset)

    # ── Step 1: Compute shared context ONCE ──────────────────────────────
    ctx = compute_context(candles)

    # ── Step 2: Run all 6 modules ────────────────────────────────────────
    all_results = []
    all_results += mod_candle.analyze(candles, ctx)
    all_results += mod_tick.analyze(candles, ticks, micro, ctx)
    all_results += mod_pattern.analyze(candles, ctx)
    all_results += mod_indicator.analyze(candles, ctx)
    all_results += mod_keylevel.analyze(candles, ctx)
    all_results += mod_otc.analyze(candles, ctx)

    if not all_results:
        return _neutral("NO_SIGNAL", ctx.regime, asset)

    # ── Step 3: Collapse correlated groups (BODY → 1 vote) ───────────────
    body_signals = [r for r in all_results if r.group == "BODY"]
    non_body = [r for r in all_results if r.group != "BODY"]
    collapsed_body = _collapse_body_group(body_signals)
    grouped_results = non_body + ([collapsed_body] if collapsed_body else [])

    # ── Step 4: Exhaustion gate detection ────────────────────────────────
    # FIX (Bug #9, 2026-07-17): exhaustion detection now runs on the
    # COLLAPSED grouped_results (post-collapse), not on raw all_results.
    # Previously a signal could be detected as "exhaustion" from a body
    # sub-signal that was later dropped during BODY-group collapse, so the
    # exhaustion flag was inconsistent with the actual voting set.
    exhaustion_indicators = 0
    if any(r.group == "BODY" and "exhaustion" in " ".join(r.reasons).lower()
           for r in grouped_results):
        exhaustion_indicators += 1
    if any(r.group == "WICK" for r in grouped_results):
        exhaustion_indicators += 1
    if ctx.stats["current_streak"] >= 4:
        exhaustion_indicators += 1
    if ctx.stats["streak_rarity"] < 0.10 and ctx.stats["current_streak"] >= 3:
        exhaustion_indicators += 1
    is_exhausting = exhaustion_indicators >= 2
    is_strongly_exhausting = exhaustion_indicators >= 3

    # ── Step 5: Get per-pair weights (DB-adapted) ──────────────────────
    pair_weights = get_weights(asset, period=period)
    pair_profile = get_profile(asset)

    # ── Step 6: Apply regime + per-pair + reliability weights ────────────
    regime = ctx.regime
    regime_reasons = []
    vol_note = ""

    if ctx.vol_pct > 1.3:
        vol_note = f"_VOL_SCALE: HIGH (vol={ctx.vol_pct:.1f}x) → stricter thresholds"
    elif ctx.vol_pct < 0.7:
        vol_note = f"_VOL_SCALE: LOW (vol={ctx.vol_pct:.1f}x) → looser thresholds"

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

    if pair_profile != "default":
        regime_reasons.append(
            f"_PAIR_PROFILE: {asset} = {pair_profile} → per-pair weights applied")

    if htf_trend != "SIDEWAYS":
        regime_reasons.append(
            f"_HTF: 5m {htf_trend} → aligned ×1.1, counter-trend ×0.7")

    # Apply all multipliers
    adjusted = []
    suppressed_count = 0
    for r in grouped_results:
        # Regime multiplier
        if regime["is_volatile"]:
            r_mult = 0.7
        elif regime["is_ranging"]:
            r_mult = 1.3 if r.signal_type == "REVERSAL" else 0.7
        elif regime["is_trending"]:
            if r.signal_type == "CONTINUATION":
                r_mult = 1.3
            else:
                if is_strongly_exhausting:
                    r_mult = 1.2
                elif is_exhausting:
                    r_mult = 1.0
                else:
                    r_mult = 0.8
        else:
            r_mult = 1.0

        # Reliability tier multiplier
        t_mult = RELIABILITY.get(r.reliability, 1.0)

        # Per-pair module weight
        p_mult = pair_weights.get(r.module_name, 1.0)

        # HTF confluence multiplier (NEW — was previously computed but discarded).
        # Counter-HTF CALL signals (HTF=DOWN) and counter-HTF PUT signals (HTF=UP)
        # get a 0.7 penalty. Aligned signals get 1.1 boost. SIDEWAYS = neutral.
        if htf_trend == "UPTREND":
            h_mult = 1.1 if r.direction == "CALL" else 0.7
        elif htf_trend == "DOWNTREND":
            h_mult = 1.1 if r.direction == "PUT" else 0.7
        else:
            h_mult = 1.0

        effective = round(r.score * r_mult * t_mult * p_mult * h_mult)

        if effective == 0:
            suppressed_count += 1
            continue

        adjusted.append((r, effective))

    # ── Step 7: Blend ────────────────────────────────────────────────────
    call_score = sum(e for r, e in adjusted if r.direction == "CALL")
    put_score = sum(e for r, e in adjusted if r.direction == "PUT")

    call_groups = set(r.group for r, e in adjusted if r.direction == "CALL")
    put_groups = set(r.group for r, e in adjusted if r.direction == "PUT")
    all_groups = call_groups | put_groups
    total_groups = len(all_groups)

    all_reasons = []
    for r, e in adjusted:
        score_str = f" (eff={e})" if e != r.score else ""
        for reason in r.reasons:
            all_reasons.append(f"[{r.module_name}] {reason}{score_str}")
    all_reasons += regime_reasons
    if vol_note:
        all_reasons.append(vol_note)
    if suppressed_count > 0:
        all_reasons.append(f"_SUPPRESSED: {suppressed_count} signal(s) dampened to 0")

    if total_groups == 0:
        return _neutral(all_reasons or ["NO_SIGNAL"], regime, asset, ctx)

    net = call_score - put_score
    total = call_score + put_score

    if total == 0 or net == 0:
        # Count majority groups for display even on NEUTRAL
        call_g = set(r.group for r, e in adjusted if r.direction == "CALL")
        put_g = set(r.group for r, e in adjusted if r.direction == "PUT")
        maj_n = max(len(call_g), len(put_g)) if (call_g or put_g) else 0
        return {
            "signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
            "score": 0, "reasons": all_reasons or ["CONFLICTING_SIGNALS"],
            "regime": regime, "agree": maj_n,
            "total": total_groups, "signals_fired": total_groups,
            "modules": _module_breakdown(adjusted, all_results),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
        }

    signal = "CALL" if net > 0 else "PUT"

    # ── Step 8: Confidence calibration (refactored) ─────────────────────
    # FIX (Bug #13-14, 2026-07-17): the old blend mixed two non-comparable
    # metrics (group-count % and weighted-score %) by simple averaging,
    # which inflated confidence when correlated modules agreed and capped
    # strong-but-isolated signals at 55%. Now:
    #   1. Use the GEOMETRIC mean of vote-count and weight ratios (sensitive
    #      to BOTH breadth and depth, but doesn't compound them linearly).
    #   2. Replace the flat 55% single-group cap with an adaptive cap that
    #      scales up with effective score — a lone PATTERN signal with
    #      effective score >= 6 has earned more trust than a lone score=1.
    #   3. Apply a small HTF-alignment bonus when the signal agrees with
    #      the 5m trend (already captured in effective score, but a direct
    #      confidence bump makes UI grade reflect it).
    majority_groups = call_groups if signal == "CALL" else put_groups
    majority_group_n = len(majority_groups)

    vote_ratio = (majority_group_n / total_groups) if total_groups else 0
    majority_score = max(call_score, put_score)
    weight_ratio = (majority_score / total) if total > 0 else 0

    # Geometric mean: sensitive to BOTH breadth and depth, but a low value
    # in either pulls the result down (avoids the 50%+50% compounding bug).
    import math as _math
    confidence = int(_math.sqrt(vote_ratio * weight_ratio) * 100)

    # HTF alignment bonus (only when HTF is directional AND signal agrees).
    if htf_trend == "UPTREND" and signal == "CALL":
        confidence = min(100, confidence + 5)
    elif htf_trend == "DOWNTREND" and signal == "PUT":
        confidence = min(100, confidence + 5)
    elif htf_trend in ("UPTREND", "DOWNTREND") and (
        (htf_trend == "UPTREND" and signal == "PUT")
        or (htf_trend == "DOWNTREND" and signal == "CALL")
    ):
        confidence = max(0, confidence - 5)

    # Adaptive single-group cap: a lone strong signal deserves more trust
    # than a lone weak one. Was: flat min(55). Now: cap scales with the
    # signal's effective score.
    if total_groups == 1:
        max_eff = majority_score  # effective score of the single voting group
        if max_eff >= 6:
            cap = 70
        elif max_eff >= 4:
            cap = 62
        else:
            cap = 55
        confidence = min(confidence, cap)

    # ── Step 9: Pattern confluence check for STRONG ──────────────────────
    # FIX (Bug #12, 2026-07-17): tighten pattern confluence.
    # Previously ANY non-pattern group (even a score-1 stochastic
    # continuation) counted as confluence. Now requires a non-pattern
    # group with effective score >= 3 — i.e. a substantive confirmation,
    # not noise.
    pattern_agrees = any(
        r.reliability == "PATTERN" and r.direction == signal
        for r, e in adjusted
    )
    strong_non_pattern_agrees = any(
        r.reliability != "PATTERN" and r.direction == signal and e >= 3
        for r, e in adjusted
    )
    has_pattern_confluence = pattern_agrees and strong_non_pattern_agrees

    # FIX: 'agree' = number of unique groups voting for majority direction
    # (NOT score). The HTML label says 'N/M modules' so this must be a count.
    agree = majority_group_n
    abs_net = abs(net)

    if (confidence >= 65 and abs_net >= 5 and majority_group_n >= 2
            and has_pattern_confluence):
        strength = "STRONG"
    elif (confidence >= 65 and abs_net >= 5 and majority_group_n >= 2
          and not has_pattern_confluence):
        strength = "MEDIUM"
        all_reasons.append("_DOWNGRADE: STRONG→MEDIUM (no strong pattern confluence)")
    elif confidence >= 50 and abs_net >= 2:
        strength = "MEDIUM"
    elif abs_net >= 1:
        strength = "MEDIUM"
    else:
        return {
            "signal": "NEUTRAL", "confidence": confidence, "strength": "NEUTRAL",
            "score": net, "reasons": all_reasons + [f"Net too low ({net}) → NEUTRAL"],
            "regime": regime, "agree": agree, "total": total_groups,
            "signals_fired": total_groups,
            "modules": _module_breakdown(adjusted, all_results),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
        }

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
        "modules": _module_breakdown(adjusted, all_results),
        "asset": asset,
        "profile": pair_profile,
        "htf_trend": htf_trend,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _collapse_body_group(body_signals: list) -> ModuleResult:
    """Collapse correlated BODY signals into ONE composite vote.

    Direction = majority by score sum.
    Score = max + 1 corroboration bonus if ≥3 agree.
    """
    if not body_signals:
        return None

    call_sum = sum(r.score for r in body_signals if r.direction == "CALL")
    put_sum = sum(r.score for r in body_signals if r.direction == "PUT")
    call_n = sum(1 for r in body_signals if r.direction == "CALL")
    put_n = sum(1 for r in body_signals if r.direction == "PUT")

    if call_sum > put_sum:
        direction = "CALL"
        max_score = max(r.score for r in body_signals if r.direction == "CALL")
        agree_n = call_n
    elif put_sum > call_sum:
        direction = "PUT"
        max_score = max(r.score for r in body_signals if r.direction == "PUT")
        agree_n = put_n
    else:
        return None

    bonus = 1 if agree_n >= 3 else 0
    score = max_score + bonus

    types = set(r.signal_type for r in body_signals)
    sig_type = "REVERSAL" if "REVERSAL" in types else "CONTINUATION"

    reasons_str = " | ".join(r.reasons[0] if r.reasons else "" for r in body_signals)

    return ModuleResult(
        module_name="candle_reaction", direction=direction, score=score,
        confidence=min(70, score * 15),
        signal_type=sig_type, reliability="CANDLE", group="BODY",
        reasons=[f"[BODY collapsed] {reasons_str}"])


def _module_breakdown(adjusted: list, all_results: list) -> dict:
    """Build per-module breakdown for UI display.

    Returns dict mapping module_name → {direction, score, reasons, fired}
    """
    breakdown = {}
    module_names = ["candle_reaction", "running_tick", "pattern",
                    "indicator", "key_level", "otc_pattern"]

    for mname in module_names:
        module_adjusted = [(r, e) for r, e in adjusted if r.module_name == mname]
        module_raw = [r for r in all_results if r.module_name == mname]

        if not module_raw:
            breakdown[mname] = {
                "direction": "NEUTRAL", "score": 0, "reasons": [], "fired": False
            }
            continue

        call_sum = sum(e for r, e in module_adjusted if r.direction == "CALL")
        put_sum = sum(e for r, e in module_adjusted if r.direction == "PUT")

        if call_sum > put_sum:
            direction = "CALL"
            score = call_sum - put_sum
        elif put_sum > call_sum:
            direction = "PUT"
            score = put_sum - call_sum
        else:
            direction = "NEUTRAL"
            score = 0

        reasons = []
        for r in module_raw:
            reasons.extend(r.reasons)

        breakdown[mname] = {
            "direction": direction,
            "score": score,
            "reasons": reasons,
            "fired": len(module_raw) > 0,
        }

    return breakdown


def _neutral(reasons, regime, asset="", ctx=None, htf_trend="SIDEWAYS") -> dict:
    """Return a NEUTRAL prediction."""
    modules = {}
    if ctx:
        modules = _module_breakdown([], [])
    return {
        "signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
        "score": 0, "reasons": reasons if isinstance(reasons, list) else [reasons],
        "regime": regime, "agree": 0, "total": 0, "signals_fired": 0,
        "modules": modules, "asset": asset, "profile": get_profile(asset),
        "htf_trend": htf_trend,
    }
