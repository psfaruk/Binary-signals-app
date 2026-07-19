"""
engines/base/blender.py — Smart blender (shared by both engines).

Combines 6 modules into a final CALL/PUT/NEUTRAL prediction. This is
the SINGLE source of truth for the blending algorithm — previously
duplicated 442 lines × 2 between engines/otc/blender.py and
engines/real/blender.py, identical except for ONE module name.

Pipeline:
  1. Compute shared MarketContext ONCE
  2. Run all 6 modules (5 shared + 1 engine-specific 6th module)
  3. Collapse correlated groups (BODY signals → 1 vote)
  4. Apply regime-aware weighting (TREND/RANGE/VOLATILE + exhaustion gate)
  5. Apply per-pair module weighting (USDPKR → boost reversal, EURUSD → boost indicator)
  6. Apply reliability tier multipliers (PATTERN ×1.5 > STAT/LEVEL ×1.3 > CANDLE ×1.0 > MICRO ×0.6)
  7. Blend: confidence-weighted vote
  8. Pattern confluence check for STRONG
  9. Group-aware confidence calibration
  10. Strength tier determination

Engine-specific configuration is passed in via a `BlenderConfig` dataclass:
  - module_6_name: "otc_pattern" or "trend_follow"
  - module_6_fn: the analyze() function for the 6th module
  - reliability: dict of reliability tier → multiplier
  - weight_adapter: PairWeightAdapter instance
  - module_names: tuple of 6 module names (for the breakdown display)
"""
import math
from dataclasses import dataclass, field
from typing import Callable

from engines.base.types import ModuleResult, MarketContext
from engines.base.context import compute_context
from engines.base.modules import (
    candle_reaction as mod_candle,
    running_tick as mod_tick,
    pattern as mod_pattern,
    indicator as mod_indicator,
    key_level as mod_keylevel,
)
from engines.base.per_pair import PairWeightAdapter


@dataclass
class BlenderConfig:
    """Engine-specific configuration for the shared blender.

    Encapsulates everything that differs between the OTC and Real engines:
      - module_6_name: name of the 6th module ("otc_pattern" or "trend_follow")
      - module_6_fn: the 6th module's analyze() function
      - reliability: dict of reliability tier → multiplier
      - weight_adapter: PairWeightAdapter instance (with engine-specific
                        PAIR_CONFIGS and DEFAULT_WEIGHTS baked in)
      - module_names: tuple of 6 module names (for breakdown display)
      - engine_name: short label for debug logs ("otc" or "real")
    """
    module_6_name: str
    module_6_fn: Callable
    reliability: dict
    weight_adapter: PairWeightAdapter
    module_names: tuple
    engine_name: str = "base"


def predict(candles, ticks=None, micro=None, asset="", htf_trend="SIDEWAYS",
            period: int = 60, config=None) -> dict:
    """Run 6 modules + smart blend using the given engine config.

    Args:
        candles: list of closed candle dicts (time, open, high, low, close)
        ticks: tick list for the closed candle (optional)
        micro: microstructure dict (optional)
        asset: pair name for per-pair weighting (e.g. "EURUSD_otc")
        htf_trend: "UPTREND" | "DOWNTREND" | "SIDEWAYS" from 5m EMA confluence.
        period: candle period in seconds (default 60).
        config: BlenderConfig dataclass (REQUIRED) — encapsulates the
            engine-specific reliability multipliers, weight adapter, and
            the 6th module.

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
    if config is None:
        raise ValueError("BlenderConfig is required — pass engines.{otc,real}.config.BLENDER_CONFIG")

    reliability = config.reliability
    weight_adapter = config.weight_adapter
    module_6_fn = config.module_6_fn
    module_names = config.module_names

    if not candles or len(candles) < 3:
        return _neutral("INSUFFICIENT_DATA", {}, asset, weight_adapter,
                         module_names=module_names, htf_trend=htf_trend)

    # ── Step 1: Compute shared context ONCE ──────────────────────────────
    ctx = compute_context(candles)

    # ── Step 2: Run all 6 modules ────────────────────────────────────────
    all_results = []
    all_results += mod_candle.analyze(candles, ctx)
    all_results += mod_tick.analyze(candles, ticks, micro, ctx)
    all_results += mod_pattern.analyze(candles, ctx)
    all_results += mod_indicator.analyze(candles, ctx)
    all_results += mod_keylevel.analyze(candles, ctx)
    all_results += module_6_fn(candles, ctx)

    if not all_results:
        return _neutral("NO_SIGNAL", ctx.regime, asset, weight_adapter,
                         module_names=module_names, htf_trend=htf_trend)

    # ── Step 3: Collapse correlated groups (BODY → 1 vote) ───────────────
    # FIX (Bug 10, deep audit 2026-07-19): previously only collapsed
    # group="BODY", leaving BODY_CONT and WICK_CONT to vote independently.
    # Since candle_reaction can produce 4 signals in a strong trend
    # (BODY + BODY_CONT + WICK + WICK_CONT), the module could cast 4
    # separate votes — over-weighting one source. BODY_CONT and BODY both
    # use recent close prices, so they share underlying data and should
    # be collapsed together. WICK_CONT and WICK similarly share wick data.
    body_signals = [r for r in all_results if r.group in ("BODY", "BODY_CONT")]
    wick_signals = [r for r in all_results if r.group in ("WICK", "WICK_CONT")]
    non_body_wick = [r for r in all_results
                     if r.group not in ("BODY", "BODY_CONT", "WICK", "WICK_CONT")]
    collapsed_body = _collapse_body_group(body_signals)
    collapsed_wick = _collapse_body_group(wick_signals)
    grouped_results = non_body_wick
    if collapsed_body:
        grouped_results.append(collapsed_body)
    if collapsed_wick:
        collapsed_wick.module_name = "candle_reaction"  # keep source label
        collapsed_wick.group = "WICK"  # normalize for breakdown display
        grouped_results.append(collapsed_wick)

    # ── Step 4: Exhaustion gate detection ────────────────────────────────
    # FIX (2026-07-18, structural bias): the original exhaustion gate had
    # 4 checks, 2 of which depended on reversal-module outputs (BODY
    # exhaustion reason + WICK signal from candle_reaction). This was
    # self-reinforcing — the gate fired exactly when reversal modules
    # already voted, doubling their weight via the "strongly_exhausting"
    # override.
    #
    # FIX (2026-07-18, partial fix): added 2 INDEPENDENT tick-volume
    # checks. But user noted the OLD checks (1, 2) were still present,
    # so self-reinforcement was only diluted, not eliminated.
    #
    # FIX (2026-07-18, final): REMOVED the 2 reversal-module-dependent
    # checks entirely. Now ALL exhaustion checks are INDEPENDENT of
    # module outputs — they source from raw statistics + microstructure:
    #
    #   1. Long streak (statistical — current_streak >= 4)
    #   2. Rare streak (statistical — streak_rarity < 0.10 + streak >= 3)
    #   3. Tick velocity deceleration (microstructure.last_velocity.accel < 0.7
    #      after a strong move)
    #   4. Volume-price divergence (high tick_count, small net move)
    #
    # The gate now requires genuine independent confirmation. A reversal
    # module's BODY/WICK signal no longer counts toward exhaustion — the
    # gate is a TRUE second opinion, not an echo of the reversal modules.
    exhaustion_indicators = 0
    exhaustion_reasons = []

    # Check 1: Long streak (statistical — independent of modules)
    if ctx.stats["current_streak"] >= 4:
        exhaustion_indicators += 1
        exhaustion_reasons.append(f"streak={ctx.stats['current_streak']}")

    # Check 2: Rare streak (statistical — independent of modules)
    if ctx.stats["streak_rarity"] < 0.10 and ctx.stats["current_streak"] >= 3:
        exhaustion_indicators += 1
        exhaustion_reasons.append(f"rare streak (rarity={ctx.stats['streak_rarity']:.0%})")

    # Check 3: Tick velocity deceleration — INDEPENDENT of modules.
    # Sourced from microstructure.last_velocity, not from any module vote.
    if micro and isinstance(micro, dict):
        lv = micro.get("last_velocity")
        if lv and isinstance(lv, dict):
            accel = lv.get("accel", 1.0)
            net_move = abs(micro.get("net", 0))
            atr = ctx.atr if ctx.atr > 0 else 0.0001
            # Deceleration: accel < 0.7 means recent 5-tick speed is < 70%
            # of recent 10-tick speed. Combined with a meaningful net move
            # (>0.5 ATR), this indicates the move is losing momentum.
            if accel < 0.7 and net_move > atr * 0.5:
                exhaustion_indicators += 1
                exhaustion_reasons.append(
                    f"tick deceleration (accel={accel:.2f}, net={net_move/atr:.2f}x ATR)")

    # Check 4: Volume-price divergence — INDEPENDENT of modules.
    # High tick activity but small net move = price stalling = exhaustion.
    # FIX (Bug 23, deep audit 2026-07-19): threshold `tick_count >= 30`
    # was too loose — most running candles have 100+ ticks, so this fired
    # on almost every prediction, contributing to over-triggering of the
    # exhaustion gate. Raised to `tick_count >= 60` (still below typical
    # 1-minute OTC tick count of 200-600, but high enough to filter
    # genuinely low-activity candles where divergence is meaningless).
    if micro and isinstance(micro, dict):
        tick_count = micro.get("tick_count", 0)
        net_move = abs(micro.get("net", 0))
        atr = ctx.atr if ctx.atr > 0 else 0.0001
        if tick_count >= 60 and net_move < atr * 0.3:
            exhaustion_indicators += 1
            exhaustion_reasons.append(
                f"volume-price divergence ({tick_count} ticks, net={net_move/atr:.2f}x ATR)")

    # Thresholds: with only 4 independent checks (was 6), require 2 for
    # "exhausting" and 3 for "strongly exhausting". This keeps the gate
    # selective — it fires only when multiple independent signals agree.
    is_exhausting = exhaustion_indicators >= 2
    is_strongly_exhausting = exhaustion_indicators >= 3

    # Store exhaustion reasons for the prediction output so the UI can
    # show WHY the gate fired (transparency for the independent checks).
    _exhaustion_detail = " | ".join(exhaustion_reasons) if exhaustion_reasons else ""

    # ── Step 5: Get per-pair weights (DB-adapted) ──────────────────────
    pair_weights = weight_adapter.get_weights(asset, period=period)
    pair_profile = weight_adapter.get_profile(asset)

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
        t_mult = reliability.get(r.reliability, 1.0)

        # Per-pair module weight
        p_mult = pair_weights.get(r.module_name, 1.0)

        # HTF confluence multiplier.
        # FIX (HTF vs exhaustion conflict, 2026-07-19, AUDIT-ENGINES #10):
        # The previous version applied HTF ×0.7 unconditionally to
        # counter-trend signals. But when the exhaustion gate has fired
        # (is_exhausting/is_strongly_exhausting), the whole POINT of the
        # gate is to BOOST counter-trend reversal signals (×1.2 in the
        # regime multiplier). Applying HTF ×0.7 on top negates that
        # boost: 1.2 × 0.7 = 0.84 — the exact reversal the exhaustion
        # gate wants to boost gets DAMPENED, not boosted.
        # Now: when the exhaustion gate has fired AND the signal is a
        # REVERSAL, skip the HTF ×0.7 penalty (use 1.0). The HTF ×1.1
        # bonus for aligned continuation signals is preserved — those
        # represent trend resumption, which is fine even when the gate
        # has fired (the gate is just a "watch out" flag, not a hard
        # reversal guarantee).
        if htf_trend == "UPTREND":
            if r.direction == "CALL":
                h_mult = 1.1
            else:
                # Counter-trend PUT in an UPTREND. If the exhaustion gate
                # has fired, this reversal signal is what the gate wants
                # to amplify — don't let HTF ×0.7 undo that.
                if is_exhausting and r.signal_type == "REVERSAL":
                    h_mult = 1.0  # neutral — let regime ×1.2 stand
                else:
                    h_mult = 0.7
        elif htf_trend == "DOWNTREND":
            if r.direction == "PUT":
                h_mult = 1.1
            else:
                if is_exhausting and r.signal_type == "REVERSAL":
                    h_mult = 1.0
                else:
                    h_mult = 0.7
        else:
            h_mult = 1.0

        effective = round(r.score * r_mult * t_mult * p_mult * h_mult)

        if effective == 0:
            suppressed_count += 1
            continue

        adjusted.append((r, effective))

    # ── Step 7: Blend ────────────────────────────────────────────────────
    # FIX (suppressed-group inflation, 2026-07-19, AUDIT-ENGINES #84):
    # The previous version computed `total_groups` from `adjusted` only
    # — meaning a group whose every signal was suppressed (effective=0)
    # would simply VANISH from the denominator. This inflates vote_ratio
    # and confidence: e.g. if 3 groups fired but 1 was fully suppressed,
    # vote_ratio = majority/2 instead of majority/3.
    # Now we compute total_groups from the ORIGINAL grouped_results
    # (pre-suppression), so suppressed groups still count as "fired but
    # dampened to zero" — they don't disappear from the denominator.
    call_score = sum(e for r, e in adjusted if r.direction == "CALL")
    put_score = sum(e for r, e in adjusted if r.direction == "PUT")

    call_groups = set(r.group for r, e in adjusted if r.direction == "CALL")
    put_groups = set(r.group for r, e in adjusted if r.direction == "PUT")
    # Original groups (before suppression) — used for the denominator.
    original_groups = set(r.group for r in grouped_results)
    fired_groups = call_groups | put_groups
    # Use original_groups for denominator so suppressed groups count.
    total_groups = len(original_groups) if original_groups else 0
    # If nothing survived suppression, fired_groups is empty — but
    # original_groups may be non-empty (all suppressed). In that case
    # we still want to report NEUTRAL, not crash on divide-by-zero.

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
    # FIX (2026-07-18): surface exhaustion gate detail so the UI shows
    # WHICH independent checks fired, not just that the gate triggered.
    if is_exhausting and _exhaustion_detail:
        all_reasons.append(
            f"_EXHAUSTION_GATE: {exhaustion_indicators} indicators "
            f"({'strongly' if is_strongly_exhausting else 'mildly'} exhausting) "
            f"[{_exhaustion_detail}]")

    if total_groups == 0:
        return _neutral(all_reasons or ["NO_SIGNAL"], regime, asset, weight_adapter,
                         ctx, module_names=module_names, htf_trend=htf_trend)

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
            "modules": _module_breakdown(adjusted, all_results, module_names),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
        }

    signal = "CALL" if net > 0 else "PUT"

    # ── Step 8: Confidence calibration ───────────────────────────────────
    majority_groups = call_groups if signal == "CALL" else put_groups
    majority_group_n = len(majority_groups)

    vote_ratio = (majority_group_n / total_groups) if total_groups else 0
    majority_score = max(call_score, put_score)
    weight_ratio = (majority_score / total) if total > 0 else 0

    # Geometric mean: sensitive to BOTH breadth and depth.
    confidence = int(math.sqrt(vote_ratio * weight_ratio) * 100)

    # FIX (BUG G — confidence != conviction, 2026-07-19):
    # The geometric-mean above can read ~100% even when two groups vote
    # CALL with the weakest possible scores (1+1) and nothing opposes them
    # (vote_ratio=1.0, weight_ratio~1.0). That advertises a "STRONG, 100%
    # confident" CALL that is really a coin-flip — the #1 reason users see
    # high-confidence signals that turn out wrong.
    #
    # Now we scale confidence by the EDGE MARGIN: how decisively the majority
    # score beats the minority score, relative to total conviction.
    #   margin = abs(call_score - put_score) / (call_score + put_score)
    # A unanimous, large, one-sided vote => margin ~1.0 (no discount).
    # A 1-vs-0 or 1+1 vs 0 vote => margin ~1.0 too, BUT a tiny-score 1+1
    # vs 0 has low `total`, so we additionally require a minimum net score.
    # We cap the discount so a genuinely strong vote is never punished.
    if total > 0:
        margin = abs(call_score - put_score) / total
        # A weak vote (abs_net < 3) is at best a MEDIUM-confidence idea.
        # Blend the geometric confidence with the margin so it can never
        # exceed ~ (margin-driven ceiling).
        margin_ceiling = int(45 + 50 * margin)  # margin 0->45, 1->95
        confidence = min(confidence, margin_ceiling)
        # Floor the "single tiny vote" case: a lone score-1 vote is at most
        # ~55% even if margin is high (low sample of conviction).
        if abs_net < 3:
            confidence = min(confidence, 55)

    # HTF alignment bonus.
    if htf_trend == "UPTREND" and signal == "CALL":
        confidence = min(100, confidence + 5)
    elif htf_trend == "DOWNTREND" and signal == "PUT":
        confidence = min(100, confidence + 5)
    elif htf_trend in ("UPTREND", "DOWNTREND") and (
        (htf_trend == "UPTREND" and signal == "PUT")
        or (htf_trend == "DOWNTREND" and signal == "CALL")
    ):
        confidence = max(0, confidence - 5)

    # Adaptive single-group cap.
    if total_groups == 1:
        max_eff = majority_score
        if max_eff >= 6:
            cap = 70
        elif max_eff >= 4:
            cap = 62
        else:
            cap = 55
        confidence = min(confidence, cap)

    # ── Step 9: Pattern confluence check for STRONG ──────────────────────
    pattern_agrees = any(
        r.reliability == "PATTERN" and r.direction == signal
        for r, e in adjusted
    )
    strong_non_pattern_agrees = any(
        r.reliability != "PATTERN" and r.direction == signal and e >= 3
        for r, e in adjusted
    )
    has_pattern_confluence = pattern_agrees and strong_non_pattern_agrees

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
            "modules": _module_breakdown(adjusted, all_results, module_names),
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
        "modules": _module_breakdown(adjusted, all_results, module_names),
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

    FIX (2026-07-18, structural bias): the old logic set sig_type =
    "REVERSAL" if ANY signal in the group was REVERSAL, else
    "CONTINUATION". Since candle_reaction's 5 BODY signals were all
    REVERSAL, the collapsed vote was always REVERSAL — even when a
    new CONTINUATION signal (BODY_CONT group, but if it were in BODY)
    had more score. Now sig_type follows the MAJORITY direction's
    signal_type, weighted by score. This makes the collapse fair when
    both REVERSAL and CONTINUATION BODY signals are present.

    NOTE: BODY_CONT and WICK_CONT signals use separate groups so they
    are NOT collapsed with BODY/WICK — they vote independently. This
    ensures continuation signals aren't drowned out by the reversal
    majority in the BODY group.

    FIX (tie-breaker, 2026-07-19, AUDIT-ENGINES #11): the previous
    version returned None on a tie (call_sum == put_sum), silently
    DROPPING the entire BODY group. This had two bad effects:
      (a) If BODY was the only group that fired, total_groups == 0
          and the engine returned NEUTRAL — even though real signals
          fired. The user sees no signal when there genuinely is one.
      (b) If other groups fired too, total_groups was 1 less than it
          should be — inflating vote_ratio and confidence.
    Now we resolve the tie deterministically: pick the direction with
    more signals (count), then by max single score, then default to
    NEUTRAL (return None) only if BOTH are completely empty.
    """
    if not body_signals:
        return None

    call_signals = [r for r in body_signals if r.direction == "CALL"]
    put_signals  = [r for r in body_signals if r.direction == "PUT"]
    call_sum = sum(r.score for r in call_signals)
    put_sum  = sum(r.score for r in put_signals)
    call_n = len(call_signals)
    put_n  = len(put_signals)

    if call_sum > put_sum:
        direction = "CALL"
        max_score = max(r.score for r in call_signals)
        agree_n = call_n
        majority_signals = call_signals
    elif put_sum > call_sum:
        direction = "PUT"
        max_score = max(r.score for r in put_signals)
        agree_n = put_n
        majority_signals = put_signals
    elif call_n != put_n:
        # Tie on score — break by count (more signals of one direction).
        direction = "CALL" if call_n > put_n else "PUT"
        majority_signals = call_signals if direction == "CALL" else put_signals
        max_score = max(r.score for r in majority_signals)
        agree_n = len(majority_signals)
    elif call_n > 0:
        # Total tie — pick the direction with the strongest single signal.
        max_call = max(r.score for r in call_signals)
        max_put  = max(r.score for r in put_signals)
        if max_call >= max_put:
            direction, majority_signals, max_score, agree_n = "CALL", call_signals, max_call, call_n
        else:
            direction, majority_signals, max_score, agree_n = "PUT", put_signals, max_put, put_n
    else:
        # Truly empty (shouldn't happen — caught above) — return None.
        return None

    bonus = 1 if agree_n >= 3 else 0
    score = max_score + bonus

    # FIX: sig_type follows the MAJORITY direction's signals, weighted
    # by score. If the majority of CALL-score comes from CONTINUATION
    # signals, the collapsed vote is CONTINUATION. This is fair.
    cont_score = sum(r.score for r in majority_signals if r.signal_type == "CONTINUATION")
    rev_score  = sum(r.score for r in majority_signals if r.signal_type == "REVERSAL")
    if cont_score > rev_score:
        sig_type = "CONTINUATION"
    elif rev_score > cont_score:
        sig_type = "REVERSAL"
    else:
        # Tie — fall back to majority count
        cont_n = sum(1 for r in majority_signals if r.signal_type == "CONTINUATION")
        rev_n  = sum(1 for r in majority_signals if r.signal_type == "REVERSAL")
        sig_type = "CONTINUATION" if cont_n > rev_n else "REVERSAL"

    reasons_str = " | ".join(r.reasons[0] if r.reasons else "" for r in body_signals)

    return ModuleResult(
        module_name="candle_reaction", direction=direction, score=score,
        confidence=min(70, score * 15),
        signal_type=sig_type, reliability="CANDLE", group="BODY",
        reasons=[f"[BODY collapsed] {reasons_str}"])


def _module_breakdown(adjusted: list, all_results: list, module_names: tuple) -> dict:
    """Build per-module breakdown for UI display.

    Returns dict mapping module_name → {direction, score, reasons, fired}
    """
    breakdown = {}

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


def _neutral(reasons, regime, asset="", weight_adapter=None, ctx=None,
             module_names: tuple = None, htf_trend="SIDEWAYS") -> dict:
    """Return a NEUTRAL prediction.

    Args:
        reasons: list of reason strings (or a single string).
        regime: regime dict from MarketContext (or {} when ctx not yet built).
        asset: pair name.
        weight_adapter: PairWeightAdapter (for pair profile lookup).
        ctx: MarketContext if available (currently unused for module breakdown
            since fired=False entries aren't rendered by the UI — kept for
            future use / API consumers).
        module_names: tuple of 6 module names (from BlenderConfig). Required
            to build the per-module breakdown dict if you want empty
            `fired: False` entries on NEUTRAL returns.
        htf_trend: MUST be threaded through from the caller — otherwise the
            UI shows stale "SIDEWAYS" for every NEUTRAL prediction (Bug C1).
    """
    modules = {}
    pair_profile = "default"
    if weight_adapter is not None:
        pair_profile = weight_adapter.get_profile(asset)
        # Build empty per-module breakdown so the UI's module panel stays
        # consistent (all entries show fired=False). Skip if module_names
        # not provided (defensive — older callers).
        if module_names:
            modules = _module_breakdown([], [], module_names)
    return {
        "signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
        "score": 0, "reasons": reasons if isinstance(reasons, list) else [reasons],
        "regime": regime, "agree": 0, "total": 0, "signals_fired": 0,
        "modules": modules, "asset": asset, "profile": pair_profile,
        "htf_trend": htf_trend,
    }
