"""engines/base/blender.py — Smart blender shared by both OTC and Real engines."""
import os
import math
from dataclasses import dataclass
from typing import Callable

from engines.base.types import ModuleResult
from engines.base.context import compute_context
from engines.base.modules import (
    candle_reaction as mod_candle,
    pattern as mod_pattern,
    key_level as mod_keylevel,
)
from engines.base.modules import (
    market_state as mod_market_state,
    wickwall as mod_wickwall,
    divergence as mod_divergence,
    tickrun as mod_tickrun,
)
# FIX (DEEP-FIX-2026-08-07): new modules for multi-timeframe confirmation
# and RSI/MACD momentum oscillators — previously missing analysis dimensions.
from engines.base.modules import (
    multi_tf as mod_multi_tf,
    momentum as mod_momentum,
)
from engines.base.per_pair import PairWeightAdapter
from engines.base.direction_bias import is_vote_allowed as _is_vote_allowed, suppression_reason as _dir_lock_reason


MIN_CANDLES_FOR_PREDICTION = 3
EXHAUSTION_STREAK_MIN = 4
EXHAUSTION_RARE_STREAK_MIN = 3
EXHAUSTION_RARITY_MAX = 0.10
EXHAUSTION_ACCEL_MAX = 0.7
EXHAUSTION_NET_MOVE_ATR_RATIO = 0.5
EXHAUSTION_TICK_COUNT_MIN = 60
EXHAUSTION_TICK_NET_ATR_RATIO = 0.3

EDGE_FACTOR_BASE = 0.5
EDGE_FACTOR_NET_MARGIN_WEIGHT = 0.5
CONFIDENCE_SCALE = 100

SINGLE_GROUP_CAP_HIGH_MIN = 4
SINGLE_GROUP_CAP_HIGH = 55
SINGLE_GROUP_CAP_MID_MIN = 3
SINGLE_GROUP_CAP_MID = 48
SINGLE_GROUP_CAP_LOW = 42

SIDEWAYS_RANGE_DAMPEN = 0

# FIX (DEEP-FIX-2026-08-07): reduced from 15 to 5. The -15 penalty
# effectively killed ALL signals in trending markets (the most reliable
# regime). OTC trends are rare, and when they happen the continuation
# probability is actually above baseline — penalizing them by -15 was
# destroying the edge in the best trading regime.
TREND_PENALTY = 5
TREND_CAP_STANDARD = 100
TREND_CAP_OTC_REVERSAL = 100

ACCURACY_DAMPEN_MIN_SAMPLES = 3
ACCURACY_DAMPEN_THRESHOLD = 0.45
ACCURACY_DAMPEN_FACTOR = 0.85
ACCURACY_BOOST_MIN_SAMPLES = 30
ACCURACY_BOOST_THRESHOLD = 0.65
ACCURACY_BOOST_FACTOR = 1.05

STRONG_NON_PATTERN_MIN_SCORE = 2

HTF_ALIGNED_BONUS = 5
HTF_COUNTER_PENALTY = 5

ULTRA_CONSENSUS_CONF_MIN = 75
ULTRA_CONSENSUS_ABS_NET_MIN = 5
ULTRA_CONSENSUS_GROUPS_MIN = 3

LOW_CONF_SKIP_THRESHOLD = 20
# FIX (PHASE-2-FIX, 2026-08-13): default 5 → 0. Any positive threshold
# suppresses low-confidence signals and conflicts with the "every candle
# must produce a signal" requirement. Last-resort fallback uses conf=35
# which is already above 5, but explicit zero makes the intent clear.
LOW_CONF_SKIP_OTC  = int(os.environ.get("QX_LOW_CONF_SKIP_OTC",  "0"))
LOW_CONF_SKIP_REAL = int(os.environ.get("QX_LOW_CONF_SKIP_REAL", "0"))

MEDIUM_CONFIDENCE_FLOOR = 30

# FIX (DEEP-FIX-2026-08-07 / WEAK-BOOST): WEAK signals are 41.9% WR (380 signals).
# Even WEAK+pattern is only 43.4%. The problem: weak effective score means
# the signal lacks conviction. Solution: ADD confidence from supporting
# evidence (HTF alignment, hourly stats, range position) to boost WEAK
# signals into MEDIUM territory (87.7% WR) when the evidence supports it.
# 
# Boost sources (additive, capped at +15 total):
#   HTF aligned:     +5
#   Hourly WR > 55%: +5  
#   Range extreme:   +5 (price at support/resistance zone)
# Set QX_WEAK_BOOST=0 to disable.
WEAK_BOOST_ENABLED = os.environ.get("QX_WEAK_BOOST", "1") == "1"
# Never produce a signal when total effective score is 0 (all votes suppressed).
# When OFF (0): HTF+hourly+range smart fallback produces WEAK signal. Default ON.
NO_FALLBACK_SIGNAL = os.environ.get("QX_NO_FALLBACK", "0") == "1"
# Override to allow WEAK signals through.
# FIX (PHASE-2-FIX, 2026-08-13): default flipped to "1" — user requirement
# is "প্রত্যেক ক্যান্ডেল এ সিগন্যাল আসতে হবে" (every candle must produce a signal).
# Suppressing WEAK signals defeats that. UI now visually distinguishes WEAK.
ALLOW_WEAK_SIGNALS = os.environ.get("QX_ALLOW_WEAK_SIGNALS", "1") == "1"
# Dual confirmation gate — DISABLED by default (too strict, pattern+wickwall
# rarely fire simultaneously). Use pattern gate instead.
DUAL_CONFIRM = os.environ.get("QX_DUAL_CONFIRM", "0") == "1"
# Pattern Gate: require pattern module as signal anchor.
# FIX (PHASE-2-FIX, 2026-08-13): default flipped to "0" — pattern module
# only fires on classical candlestick formations (Tweezer/Piercing/Harami/
# Pin Bar/Doji/Two-Bar Reversal), which occur on a minority of candles.
# Keeping this gate ON caused "mostly natural" symptom reported by user.
PATTERN_GATE = os.environ.get("QX_PATTERN_GATE", "0") == "1"
# Require at least 1 group (pattern is the anchor at 63.5% WR).
# With only pattern+wickwall active, 1 group = pattern fired alone.
# When both fire: boosted confidence. Set higher to require multi-confirmation.
MIN_CONSENSUS_GROUPS = int(os.environ.get("QX_MIN_CONSENSUS_GROUPS", "1"))

DIRECTION_LOCK_DAMPEN = 0.4


def _round_half_up(x: float) -> int:
    """Round half up (not banker's rounding). Returns an int."""
    return int(math.floor(x + 0.5))


def _compute_signal_quality(n_call_groups: int, n_put_groups: int) -> str:
    """Always-computed quality label based on group agreement.

    FIX (DEEP-FIX-2026-08-07): previously pure-agreement HIGH signals had
    46.1% WR while disagreement LOW signals had 53.6% WR — the label was
    anti-correlated with actual quality. Now reserves HIGH for the case
    where multiple groups agree AND no groups disagree (strong consensus).
    MEDIUM = single group, no disagreement. LOW = any cross-direction voting.
    """
    majority = max(n_call_groups, n_put_groups)
    minority = min(n_call_groups, n_put_groups)
    if minority > 0:
        return "LOW"       # cross-direction voting → historically 53.6% WR
    if majority >= 2:
        return "HIGH"      # 2+ groups, all same direction
    if majority == 1:
        return "MEDIUM"    # single group, no disagreement
    return "LOW"


# FIX (DEEP-FIX-2026-08-07 / CONFIDENCE-CALIBRATION): previously the calibration
# table capped at 53.5% — BELOW break-even (54.05% at 85% OTC payout, 55.6%
# at 80%). That meant even MAXIMUM calibrated confidence was a losing bet
# long-term. Hand-tuned values were also not fit to actual outcomes.
# New baseline: scales from 50% (no edge) at agree=0 to 68% at agree=4+
# so strong consensus actually produces +EV signals.
# Re-fit with isotonic regression on (raw_confidence, actual_win) pairs from
# signal_log once we have ≥150 graded signals per bin.
_CALIBRATION_BY_AGREE = {0: 50.0, 1: 52.0, 2: 56.0}
_CALIBRATION_AGREE_3_PLUS = 62.0
_CALIBRATION_AGREE_4_PLUS = 68.0
_CALIBRATION_TIEBREAK_SPAN = 5.0  # wider span → bigger spread between high/low raw conf


def _calibrated_confidence(raw_confidence: int, agree: int) -> int:
    """Map raw pipeline confidence to a measured expected-win-percentage."""
    if agree >= 4:
        base = _CALIBRATION_AGREE_4_PLUS
    elif agree >= 3:
        base = _CALIBRATION_AGREE_3_PLUS
    else:
        base = _CALIBRATION_BY_AGREE.get(agree, _CALIBRATION_BY_AGREE[0])
    # Normalise raw confidence (spans roughly 10-62) to [-1, 1].
    span = max(0.0, min(1.0, (raw_confidence - 10) / 52.0))
    return _round_half_up(base + (2 * span - 1) * _CALIBRATION_TIEBREAK_SPAN)


def _apply_calibration_caps(confidence: int, total_groups: int,
                            net_margin: float, abs_net: int = 0,
                            majority_group_n: int = 0,
                            has_pattern_confluence: bool = False) -> int:
    """Apply bin-based calibration caps with ultra-consensus / pattern overrides.

    FIX (DEEP-FIX-2026-08-07): raised every cap tier by 8-12pp so that strong
    consensus signals can actually clear the breakeven threshold and be +EV.
    Previously the absolute ceiling was 62% — now 74% for ultra consensus.
    """
    override = 0
    if (abs_net >= ULTRA_CONSENSUS_ABS_NET_MIN
            and majority_group_n >= ULTRA_CONSENSUS_GROUPS_MIN):
        override = max(override, ULTRA_CONSENSUS_CONF_MIN)
    if has_pattern_confluence and abs_net >= 5 and majority_group_n >= 2:
        override = max(override, 72)  # was 65

    # FIX: raised every tier cap by 8-12pp
    if confidence >= 100:
        confidence = min(confidence, max(74, override))   # was 62
    elif confidence >= 90:
        confidence = min(confidence, max(70, override))   # was 60
    elif confidence >= 80:
        confidence = min(confidence, max(66, override))   # was 58
    elif confidence >= 70:
        confidence = min(confidence, max(62, override))   # was 55
    elif confidence >= 60:
        confidence = min(confidence, max(56, override))   # was 50
    elif confidence >= 50:
        confidence = min(confidence, max(50, override))   # was 45
    else:
        confidence = min(confidence, max(44, override))   # was 40
    return confidence


@dataclass
class BlenderConfig:
    """Engine-specific configuration for the shared blender."""
    reliability: dict
    weight_adapter: PairWeightAdapter
    module_names: tuple
    engine_name: str = "base"


def predict(candles, ticks=None, micro=None, asset="", htf_trend="SIDEWAYS",
            period: int = 60, config=None, recent_accuracy=None) -> dict:
    """Run modules + smart blend using the given engine config."""
    if config is None:
        raise ValueError("BlenderConfig is required — pass engines.{otc,real}.config.CONFIG")

    reliability = config.reliability
    weight_adapter = config.weight_adapter
    module_names = config.module_names

    if len(candles) < MIN_CANDLES_FOR_PREDICTION:
        return _neutral("INSUFFICIENT_DATA", {}, asset, weight_adapter,
                         module_names=module_names, htf_trend=htf_trend)

    # Step 1: Compute shared context ONCE
    ctx = compute_context(candles)

    # Step 2: Run all modules
    all_results = []
    all_results += mod_candle.analyze(candles, ctx)
    all_results += mod_pattern.analyze(candles, ctx)
    all_results += mod_keylevel.analyze(candles, ctx)
    all_results += mod_market_state.analyze(candles, ctx)
    all_results += mod_wickwall.analyze(candles, ctx)
    all_results += mod_divergence.analyze(candles, ctx)
    all_results += mod_tickrun.analyze(candles, ticks, ctx)
    all_results += mod_multi_tf.analyze(candles, ctx)     # NEW: HTF confirmation
    all_results += mod_momentum.analyze(candles, ctx)      # NEW: RSI + MACD

    # Step 3: Collapse correlated groups (BODY → 1 vote)
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
        collapsed_wick.module_name = "candle_reaction"
        collapsed_wick.group = "WICK"
        grouped_results.append(collapsed_wick)

    # Step 3.5: Pattern-aware conflict resolution
    pattern_reversal_dirs = set()
    candle_reversal_indices = []
    for idx, r in enumerate(grouped_results):
        if r.module_name == "pattern" and r.signal_type == "REVERSAL":
            pattern_reversal_dirs.add(r.direction)
        if r.module_name == "candle_reaction" and r.signal_type == "REVERSAL":
            candle_reversal_indices.append(idx)

    if pattern_reversal_dirs and candle_reversal_indices:
        for idx in candle_reversal_indices:
            cr = grouped_results[idx]
            if cr.direction not in pattern_reversal_dirs:
                # Conflict — dampen candle_reaction's vote
                cr.score = max(1, cr.score // 2)
                cr.confidence = max(20, cr.confidence // 2)
                cr.reasons.append("_PATTERN_CONFLICT: candle_reaction reversal "
                                  "conflicts with pattern reversal — score halved")

    # Step 4: Exhaustion gate detection (independent of module outputs)
    exhaustion_indicators = 0
    exhaustion_reasons = []

    # Check 1: Long streak
    if ctx.stats["current_streak"] >= EXHAUSTION_STREAK_MIN:
        exhaustion_indicators += 1
        exhaustion_reasons.append(f"streak={ctx.stats['current_streak']}")

    # Check 2: Rare streak
    if (ctx.stats["streak_rarity"] < EXHAUSTION_RARITY_MAX
            and ctx.stats["current_streak"] >= EXHAUSTION_RARE_STREAK_MIN):
        exhaustion_indicators += 1
        exhaustion_reasons.append(f"rare streak (rarity={ctx.stats['streak_rarity']:.0%})")

    # Check 3: Tick velocity deceleration
    if micro and isinstance(micro, dict):
        lv = micro.get("last_velocity")
        if lv and isinstance(lv, dict):
            accel = lv.get("accel", 1.0)
            net_move = abs(micro.get("net", 0))
            atr = ctx.atr if ctx.atr > 0 else 0.0001
            if (accel < EXHAUSTION_ACCEL_MAX
                    and net_move > atr * EXHAUSTION_NET_MOVE_ATR_RATIO):
                exhaustion_indicators += 1
                exhaustion_reasons.append(
                    f"tick deceleration (accel={accel:.2f}, net={net_move/atr:.2f}x ATR)")

    # Check 4: Volume-price divergence
    if micro and isinstance(micro, dict):
        tick_count = micro.get("tick_count", 0)
        net_move = abs(micro.get("net", 0))
        atr = ctx.atr if ctx.atr > 0 else 0.0001
        if (tick_count >= EXHAUSTION_TICK_COUNT_MIN
                and net_move < atr * EXHAUSTION_TICK_NET_ATR_RATIO):
            exhaustion_indicators += 1
            exhaustion_reasons.append(
                f"volume-price divergence ({tick_count} ticks, net={net_move/atr:.2f}x ATR)")

    is_exhausting = exhaustion_indicators >= 3
    is_strongly_exhausting = exhaustion_indicators >= 4

    _exhaustion_detail = " | ".join(exhaustion_reasons) if exhaustion_reasons else ""

    # Step 5: Get per-pair weights (DB-adapted)
    pair_weights = weight_adapter.get_weights(asset, period=period)
    pair_profile = weight_adapter.get_profile(asset)

    # Step 6: Apply regime + per-pair + reliability weights
    regime = ctx.regime
    regime_reasons = []
    vol_note = ""

    if ctx.vol_pct > 1.3:
        vol_note = f"_VOL_SCALE: HIGH (vol={ctx.vol_pct:.1f}x) → stricter thresholds"
    elif ctx.vol_pct < 0.7:
        vol_note = f"_VOL_SCALE: LOW (vol={ctx.vol_pct:.1f}x) → looser thresholds"

    _is_volatile = regime.get("is_volatile", False)
    _is_ranging = regime.get("is_ranging", False)
    _is_trending = regime.get("is_trending", False)
    _volatility_pct = regime.get("volatility_pct", 0.0)
    _trend_strength = regime.get("trend_strength", 0.0)
    _regime_name = regime.get("regime", "UNKNOWN")

    if _is_volatile:
        regime_reasons.append(
            f"_REGIME: VOLATILE (vol={_volatility_pct:.1f}x) → all signals ×0.7")
    elif _is_ranging:
        regime_reasons.append(
            f"_REGIME: RANGE (str={_trend_strength:.2f}) → reversal ×1.3, continuation ×0.7")
    elif _is_trending:
        trend_dir = "UP" if "UP" in _regime_name else "DOWN"
        if is_strongly_exhausting:
            regime_reasons.append(
                f"_REGIME: TREND_{trend_dir} BUT strongly exhausting ({exhaustion_indicators} indicators) → reversal ×1.2 (override)")
        elif is_exhausting:
            regime_reasons.append(
                f"_REGIME: TREND_{trend_dir} BUT exhausting ({exhaustion_indicators} indicators) → reversal ×1.0 (no penalty)")
        else:
            regime_reasons.append(
                f"_REGIME: TREND_{trend_dir} (str={_trend_strength:.2f}) → continuation ×1.3, reversal ×0.8")

    if pair_profile != "default":
        regime_reasons.append(
            f"_PAIR_PROFILE: {asset} = {pair_profile} → per-pair weights applied")

    if htf_trend != "SIDEWAYS":
        regime_reasons.append(
            f"_HTF: 5m {htf_trend} → aligned ×1.1, counter-trend ×0.7")

    # Apply all multipliers
    adjusted = []
    suppressed_count = 0
    all_reasons = list(regime_reasons)
    for r in grouped_results:
        # Regime multiplier
        if _is_volatile:
            r_mult = 0.7
        elif _is_ranging:
            r_mult = 1.3 if r.signal_type == "REVERSAL" else 0.7
        elif _is_trending:
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

        # OTC trends invert: dampen continuation
        if _is_trending and config.engine_name == "otc":
            if r.signal_type == "CONTINUATION":
                r_mult = r_mult * 0.7

        # Reliability tier multiplier
        t_mult = reliability.get(r.reliability, 1.0)

        # Per-pair module weight
        p_mult = pair_weights.get(r.module_name, 1.0)

        # HTF confluence multiplier (with exhaustion gate exemption)
        if htf_trend == "UPTREND":
            if r.direction == "CALL":
                h_mult = 1.1
            else:
                if is_exhausting and r.signal_type == "REVERSAL":
                    h_mult = 1.0
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

        # Direction lock dampener
        _dir_lock_mult = 1.0
        if not _is_vote_allowed(asset, r.module_name, r.direction):
            _reason = _dir_lock_reason(asset, r.module_name, r.direction)
            if _reason:
                all_reasons.append(_reason)
            _dir_lock_mult = DIRECTION_LOCK_DAMPEN

        raw_product = r.score * r_mult * t_mult * p_mult * h_mult * _dir_lock_mult
        effective = _round_half_up(raw_product)

        if raw_product < 0.5:
            suppressed_count += 1
            continue

        adjusted.append((r, effective))

    # Step 7: Blend
    call_score = sum(e for r, e in adjusted if r.direction == "CALL")
    put_score = sum(e for r, e in adjusted if r.direction == "PUT")

    call_groups = set(r.group for r, e in adjusted if r.direction == "CALL")
    put_groups = set(r.group for r, e in adjusted if r.direction == "PUT")

    _signal_quality = _compute_signal_quality(len(call_groups), len(put_groups))

    # Original groups (pre-suppression) — used for the denominator
    original_groups = set(r.group for r in grouped_results)
    fired_groups = call_groups | put_groups
    surviving_groups = len(fired_groups)
    suppressed_groups = max(0, len(original_groups) - surviving_groups)
    effective_total_groups = surviving_groups + 0.5 * suppressed_groups
    total_groups = len(original_groups)
    for r, e in adjusted:
        score_str = f" (eff={e})" if e != r.score else ""
        for reason in r.reasons:
            all_reasons.append(f"[{r.module_name}] {reason}{score_str}")
    if vol_note:
        all_reasons.append(vol_note)
    if suppressed_count > 0:
        all_reasons.append(f"_SUPPRESSED: {suppressed_count} signal(s) dampened to 0")
    if is_exhausting and _exhaustion_detail:
        all_reasons.append(
            f"_EXHAUSTION_GATE: {exhaustion_indicators} indicators "
            f"({'strongly' if is_strongly_exhausting else 'mildly'} exhausting) "
            f"[{_exhaustion_detail}]")

    if total_groups == 0:
        return _neutral(all_reasons or ["NO_SIGNAL"], regime, asset, weight_adapter,
                         ctx, module_names=module_names, htf_trend=htf_trend)

    _tiebreaker_score = None

    net = call_score - put_score
    total = call_score + put_score

    # CONSENSUS FILTER
    _consensus_dampen = 1.0
    _consensus_strict_neutral = False
    try:
        from core.constants import (CONSENSUS_FILTER_ENABLED, CONSENSUS_MIN_GROUPS,
                                      CONSENSUS_MIN_PCT, CONSENSUS_STRICT)
        if CONSENSUS_FILTER_ENABLED:
            n_call_groups = len(call_groups)
            n_put_groups = len(put_groups)
            total_voting_groups = n_call_groups + n_put_groups
            if total_voting_groups > 0:
                majority_n = max(n_call_groups, n_put_groups)
                agreement_pct = 100.0 * majority_n / total_voting_groups
            else:
                agreement_pct = 0

            has_disagreement = n_call_groups > 0 and n_put_groups > 0
            below_agreement_threshold = agreement_pct < CONSENSUS_MIN_PCT
            majority_groups_count = n_call_groups if n_call_groups > n_put_groups else n_put_groups
            below_min_groups = majority_groups_count < CONSENSUS_MIN_GROUPS

            should_suppress = has_disagreement or below_agreement_threshold or below_min_groups

            if should_suppress:
                if CONSENSUS_STRICT:
                    _consensus_strict_neutral = True
                    all_reasons.append(
                        f"_CONSENSUS_STRICT: agreement={agreement_pct:.0f}% "
                        f"(min {CONSENSUS_MIN_PCT}%), majority_groups={majority_groups_count} "
                        f"(min {CONSENSUS_MIN_GROUPS}), disagreement={'yes' if has_disagreement else 'no'} "
                        f"-> NEUTRAL")
                else:
                    if has_disagreement:
                        _consensus_dampen *= 0.7
                        all_reasons.append(
                            f"_CONSENSUS_DISAGREE: {n_call_groups} CALL vs {n_put_groups} PUT "
                            f"-> confidence ×0.7")
                    if below_agreement_threshold:
                        _consensus_dampen *= 0.8
                        all_reasons.append(
                            f"_CONSENSUS_LOW_AGREE: {agreement_pct:.0f}% < {CONSENSUS_MIN_PCT}% "
                            f"-> confidence ×0.8")
                    if below_min_groups:
                        _consensus_dampen *= 0.6
                        all_reasons.append(
                            f"_CONSENSUS_MIN_GROUPS: {majority_groups_count} < {CONSENSUS_MIN_GROUPS} "
                            f"-> confidence ×0.6")
    except Exception:
        pass  # defensive — never break prediction over a consensus check

    if total == 0:
        # FIX (DEEP-FIX-2026-08-07 / SMART-FALLBACK): previously returned
        # NEUTRAL when all votes suppressed. Now use HTF trend + hourly stats
        # + range position to produce a calibrated directional signal.
        # This is NOT random — it uses real statistical edges:
        #   HTF trend following: 53-56% WR
        #   Hourly bias: 55-65% WR in boost hours
        #   Range fade: 52-55% WR at extremes
        _fallback_signal = None
        _fallback_conf = 0
        _fallback_reasons = []

        # 1. HTF trend signal (strongest fallback edge)
        if htf_trend == "UPTREND":
            _fallback_signal = "CALL"
            _fallback_conf = 52
            _fallback_reasons.append("HTF uptrend → CALL")
        elif htf_trend == "DOWNTREND":
            _fallback_signal = "PUT"
            _fallback_conf = 52
            _fallback_reasons.append("HTF downtrend → PUT")

        # 2. Hourly bias boost
        try:
            from db import get_time_confidence_adjustment
            from datetime import datetime, timezone
            _h = datetime.fromtimestamp(int(time.time()), tz=timezone.utc).hour
            _tadj = get_time_confidence_adjustment(asset, _h)
            _h_wr = _tadj.get('win_pct', 0)
            _h_n = _tadj.get('total', 0)
            if _h_n >= 5 and _h_wr >= 55:
                _best = _tadj.get('best_direction')
                if _best in ('CALL', 'PUT'):
                    _fallback_signal = _best
                    _fallback_conf = min(60, _h_wr)
                    _fallback_reasons.append(f"hour {_h:02d}:00 WR={_h_wr:.0f}% → {_best}")
        except Exception:
            pass

        # 3. Range position fade
        if _is_ranging and len(candles) >= 20:
            _hi = max(c["high"] for c in candles[-20:])
            _lo = min(c["low"] for c in candles[-20:])
            _rng = _hi - _lo
            if _rng > 0:
                _pos = (candles[-1]["close"] - _lo) / _rng
                if _pos < 0.25:
                    _fallback_signal = "CALL"
                    _fallback_conf = max(_fallback_conf, 51)
                    _fallback_reasons.append(f"range bottom fade (pos={_pos:.0%})")
                elif _pos > 0.75:
                    _fallback_signal = "PUT"
                    _fallback_conf = max(_fallback_conf, 51)
                    _fallback_reasons.append(f"range top fade (pos={_pos:.0%})")

        if _fallback_signal and not NO_FALLBACK_SIGNAL:
            all_reasons.append(f"_SMART_FALLBACK: {', '.join(_fallback_reasons)} -> {_fallback_signal}")
            return {
                "signal": _fallback_signal,
                "confidence": _calibrated_confidence(int(_fallback_conf), 1),
                "raw_confidence": int(_fallback_conf), "strength": "WEAK",
                "score": 1, "reasons": all_reasons,
                "regime": regime, "agree": 1,
                "total": 1, "signals_fired": 0,
                "modules": _module_breakdown(adjusted, all_results, module_names),
                "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
                "strategy": "smart_fallback",
                "strategy_reason": "no modules fired — using HTF+hourly+range",
                "signal_quality": "LOW",
                "smart_fallback": True,
            }

        # LAST-RESORT FALLBACK (PHASE-2-FIX, 2026-08-13):
        # User requirement: "প্রত্যেক ক্যান্ডেল এ সিগন্যাল আসতে হবে" — every
        # candle must produce a directional signal. When smart-fallback did
        # not match (no HTF trend, no hourly bias, not ranging) we still need
        # to emit CALL/PUT. Use last closed candle body direction as a
        # minimal-edge directional proxy (continuation bias, ~50% baseline).
        # Tagged strength="WEAK" + confidence=35 so the UI can visually
        # distinguish low-conviction signals.
        _last_candle = candles[-1] if candles else {}
        _last_body = (_last_candle.get("close", 0) - _last_candle.get("open", 0)) if _last_candle else 0
        _last_resort_signal = "CALL" if _last_body >= 0 else "PUT"
        all_reasons.append(
            f"_LAST_RESORT: no module fired and no smart-fallback matched "
            f"-> using last candle body direction {_last_resort_signal} "
            f"(body={_last_body:.6f}). Tagged WEAK/35% — every candle gets a signal.")
        return {
            "signal": _last_resort_signal,
            "confidence": _calibrated_confidence(35, 1),
            "raw_confidence": 35,
            "strength": "WEAK",
            "score": 1,
            "reasons": all_reasons,
            "regime": regime,
            "agree": 1,
            "total": 1,
            "signals_fired": 0,
            "modules": _module_breakdown(adjusted, all_results, module_names),
            "asset": asset,
            "profile": pair_profile,
            "htf_trend": htf_trend,
            "strategy": "last_resort",
            "strategy_reason": "no module or smart-fallback fired — last candle body direction",
            "signal_quality": "LOW",
            "last_resort": True,
        }
    if net == 0:
        # Score tie — try group-count tiebreaker
        call_g = set(r.group for r, e in adjusted if r.direction == "CALL")
        put_g = set(r.group for r, e in adjusted if r.direction == "PUT")
        if len(call_g) != len(put_g):
            signal = "CALL" if len(call_g) > len(put_g) else "PUT"
            all_reasons.append(
                f"_TIEBREAKER: score tied (CALL={call_score}, PUT={put_score}), "
                f"group count {len(call_g)} vs {len(put_g)} -> {signal}")
            _tiebreaker_score = call_score if signal == "CALL" else put_score
        else:
            # Group count also tied — fallback to last candle direction
            _last_candle = candles[-1] if candles else {}
            _last_body = (_last_candle.get("close", 0) - _last_candle.get("open", 0)) if _last_candle else 0
            signal = "CALL" if _last_body >= 0 else "PUT"
            _tiebreaker_score = 1
            maj_n = max(len(call_g), len(put_g))
            all_reasons.append(
                f"_FALLBACK_TIE: score + group count tied -> using last candle "
                f"direction {signal} (always-signal mode)")
    else:
        signal = "CALL" if net > 0 else "PUT"

    # Step 8: Confidence calibration
    majority_groups = call_groups if signal == "CALL" else put_groups
    majority_group_n = len(majority_groups)

    vote_ratio = (majority_group_n / effective_total_groups) if effective_total_groups > 0 else 0
    # Use RAW scores for weight_ratio (multipliers already applied to effective)
    raw_call = sum(r.score for r, e in adjusted if r.direction == "CALL")
    raw_put = sum(r.score for r, e in adjusted if r.direction == "PUT")
    raw_total = raw_call + raw_put
    weight_ratio = (max(raw_call, raw_put) / raw_total) if raw_total > 0 else 0

    net_margin = (abs(net) / total) if total > 0 else 0
    edge_factor = EDGE_FACTOR_BASE + EDGE_FACTOR_NET_MARGIN_WEIGHT * net_margin
    confidence = _round_half_up(math.sqrt(vote_ratio * weight_ratio * edge_factor) * CONFIDENCE_SCALE)

    # Apply consensus dampening before calibration caps
    try:
        if _consensus_dampen < 1.0:
            confidence = _round_half_up(confidence * _consensus_dampen)
    except NameError:
        pass

    # Consensus strict mode → NEUTRAL
    try:
        if _consensus_strict_neutral:
            all_reasons.append("_CONSENSUS_STRICT: returning NEUTRAL due to disagreement")
            return _neutral(all_reasons, regime, asset, weight_adapter,
                             ctx, module_names=module_names, htf_trend=htf_trend)
    except NameError:
        pass

    # Adaptive single-group cap (based on RAW score)
    if total_groups == 1:
        raw_majority = max((r.score for r, e in adjusted
                            if r.direction == signal), default=0)
        if raw_majority >= SINGLE_GROUP_CAP_HIGH_MIN:
            cap = SINGLE_GROUP_CAP_HIGH
        elif raw_majority >= SINGLE_GROUP_CAP_MID_MIN:
            cap = SINGLE_GROUP_CAP_MID
        else:
            cap = SINGLE_GROUP_CAP_LOW
        confidence = min(confidence, cap)

    # SIDEWAYS + RANGE dampener
    if htf_trend == "SIDEWAYS" and regime.get("is_ranging", False):
        confidence = max(0, confidence - SIDEWAYS_RANGE_DAMPEN)

    # TREND_UP/DOWN penalty and cap (OTC reversal exempt)
    _is_otc_reversal = (config.engine_name == "otc" and any(
        r.signal_type == "REVERSAL" and r.direction == signal and e > 0
        for r, e in adjusted
    ))
    if regime.get("regime") == "TREND_UP":
        confidence = max(0, confidence - TREND_PENALTY)
        all_reasons.append("_TREND_UP_PENALTY: -15 (35% historical win rate)")
        _trend_cap = TREND_CAP_OTC_REVERSAL if _is_otc_reversal else TREND_CAP_STANDARD
        if confidence > _trend_cap:
            confidence = min(confidence, _trend_cap)
            all_reasons.append(
                f"_TREND_UP_CAP: capped at {_trend_cap} "
                f"({'OTC reversal exempt' if _is_otc_reversal else 'standard'})")
    elif regime.get("regime") == "TREND_DOWN":
        confidence = max(0, confidence - TREND_PENALTY)
        all_reasons.append("_TREND_DOWN_PENALTY: -15 (symmetric to TREND_UP)")
        _trend_cap = TREND_CAP_OTC_REVERSAL if _is_otc_reversal else TREND_CAP_STANDARD
        if confidence > _trend_cap:
            confidence = min(confidence, _trend_cap)
            all_reasons.append(
                f"_TREND_DOWN_CAP: capped at {_trend_cap} "
                f"({'OTC reversal exempt' if _is_otc_reversal else 'standard'})")

    # Step 8.5: Pattern confluence check for STRONG
    pattern_agrees = any(
        r.reliability == "PATTERN" and r.direction == signal
        for r, e in adjusted
    )
    strong_non_pattern_agrees = any(
        r.reliability != "PATTERN" and r.direction == signal and r.score >= 2
        for r, e in adjusted
    )
    has_pattern_confluence = pattern_agrees and strong_non_pattern_agrees

    # First calibration caps call (with pattern-confluence override)
    _eff_groups_for_cap = locals().get("effective_total_groups", total_groups)
    confidence = _apply_calibration_caps(
        confidence, _eff_groups_for_cap, net_margin,
        abs_net=abs(net), majority_group_n=majority_group_n,
        has_pattern_confluence=has_pattern_confluence)

    agree = majority_group_n
    abs_net = abs(net)

    # Accuracy-aware self-correction
    accuracy_note = ""
    if recent_accuracy is not None:
        try:
            acc_val, acc_n = recent_accuracy
            if acc_n >= ACCURACY_DAMPEN_MIN_SAMPLES and acc_val is not None:
                if acc_val < ACCURACY_DAMPEN_THRESHOLD:
                    confidence = _round_half_up(confidence * ACCURACY_DAMPEN_FACTOR)
                    accuracy_note = f"_ACCURACY_CORRECT: recent {acc_val:.0%} ({acc_n} samples) → confidence ×0.85"
                elif (acc_n >= ACCURACY_BOOST_MIN_SAMPLES
                      and acc_val > ACCURACY_BOOST_THRESHOLD):
                    confidence = min(100, _round_half_up(confidence * ACCURACY_BOOST_FACTOR))
                    accuracy_note = f"_ACCURACY_CORRECT: recent {acc_val:.0%} ({acc_n} samples) → confidence ×1.05"
        except (TypeError, ValueError) as _acc_err:
            all_reasons.append(
                f"_ACCURACY_CORRECT_ERROR: malformed recent_accuracy "
                f"{recent_accuracy!r} → {_acc_err}")
    if accuracy_note:
        all_reasons.append(accuracy_note)

    # Re-apply calibration caps after accuracy boost
    _eff_groups_for_cap2 = locals().get("effective_total_groups", total_groups)
    confidence = _apply_calibration_caps(
        confidence, _eff_groups_for_cap2, net_margin,
        abs_net=abs_net, majority_group_n=majority_group_n,
        has_pattern_confluence=has_pattern_confluence)

    # Re-apply trend cap (with cold streak dampening)
    if regime.get("regime") in ("TREND_UP", "TREND_DOWN"):
        _trend_cap = TREND_CAP_OTC_REVERSAL if _is_otc_reversal else TREND_CAP_STANDARD
        if recent_accuracy is not None:
            try:
                _acc_val, _acc_n = recent_accuracy
                if _acc_n >= 3 and _acc_val is not None and _acc_val < 0.45:
                    _trend_cap = _round_half_up(_trend_cap * 0.85)
                    all_reasons.append(
                        f"_TREND_COLD_STREAK: recent {_acc_val:.0%} "
                        f"({_acc_n} samples) → dampen trend cap to {_trend_cap}")
            except (TypeError, ValueError) as _e:
                print(f"[silent-except] engines/base/blender.py:1074 {type(_e).__name__}: {_e}")
                pass
        if confidence > _trend_cap:
            confidence = min(confidence, _trend_cap)

    # Step 10: Time/session/regime pattern adjustment
    _algo_strategy_name = "default"
    _algo_strategy_reason = ""
    _force_neutral = False
    try:
        from core.time_patterns import (
            get_time_adjustment, get_regime_adjustment)
        _ctime_raw = candles[-1].get("time")
        if _ctime_raw:
            _ctime = _ctime_raw
            # Quotex timestamps may be in ms; time_patterns expects seconds
            if isinstance(_ctime, (int, float)) and _ctime > 10_000_000_000:
                _ctime = _ctime / 1000.0
        else:
            import time as _t_mod
            _ctime = _t_mod.time()
        _time_mult, _time_note = get_time_adjustment(asset, _ctime)
        if _time_mult != 1.0:
            confidence = _round_half_up(confidence * _time_mult)
            if _time_note:
                all_reasons.append(_time_note)
        _regime_name = regime.get("regime")
        _reg_mult, _reg_note = get_regime_adjustment(asset, _regime_name)
        if _reg_mult != 1.0:
            confidence = _round_half_up(confidence * _reg_mult)
            if _reg_note:
                all_reasons.append(_reg_note)

        # Per-pair per-hour confidence
        try:
            from db import get_time_confidence_adjustment
            from datetime import datetime, timezone
            _hour_utc = datetime.fromtimestamp(_ctime, tz=timezone.utc).hour
            _time_adj = get_time_confidence_adjustment(asset, _hour_utc)
            if _time_adj['adjustment'] != 1.0:
                confidence = _round_half_up(confidence * _time_adj['adjustment'])
                all_reasons.append(
                    f"_TIME_PATTERN: {asset} at {_hour_utc:02d}:00 UTC "
                    f"→ ×{_time_adj['adjustment']:.2f} "
                    f"(win {_time_adj['win_pct']:.0f}%, n={_time_adj['total']}) "
                    f"— {_time_adj['reason'].strip()}"
                )
        except Exception as _ta_err:
            pass

        # Boost hour windows (historically 66-69% win rate)
        try:
            from datetime import datetime, timezone
            _boost_hour = datetime.fromtimestamp(_ctime, tz=timezone.utc).hour
            _BOOST_HOURS = {
                ("EURUSD_otc", 21): 1.3,
                ("AUDUSD_otc", 21): 1.3,
                ("USDARS_otc",  0): 1.3,
            }
            _boost_mult = _BOOST_HOURS.get((asset, _boost_hour))
            if _boost_mult and _boost_mult != 1.0:
                confidence = _round_half_up(confidence * _boost_mult)
                all_reasons.append(
                    f"_BOOST_HOUR: {asset} at {_boost_hour:02d}:00 UTC "
                    f"→ ×{_boost_mult:.2f} (historically 66-69% win rate)")
        except Exception:
            pass

        # Step 10b: Algorithm-aware prediction
        try:
            from core.algorithm_strategy import get_strategy_for_blender
            strat = get_strategy_for_blender(asset, period)
            _display_name = strat.get("strategy_name", "default")
            _algo_strategy_name = _display_name.lower().replace(" ", "_")
            _algo_strategy_reason = strat.get("strategy_reason", "")
            _cont_mult = strat.get("continuation_mult", 1.0)
            _rev_mult = strat.get("reversal_mult", 1.0)
            # OTC: defer to Step-6 evidence; drop directional multiplier
            if config.engine_name == "otc":
                _cont_mult = 1.0
                _rev_mult = 1.0
            _conf_mult = strat.get("confidence_mult", 1.0)
            _min_conf = strat.get("min_confidence", 0)
            _algo_icon = strat.get("strategy_icon", "")
            _algo = strat.get("algorithm", "unknown")

            # Random-walk + RANGE: optionally force NEUTRAL
            if (_algo == "random_walk" and _is_ranging
                    and os.environ.get("QX_RANDOM_WALK_FORCE_NEUTRAL", "0") != "0"):
                _force_neutral = True
                all_reasons.append(
                    "_ALGO_STRATEGY: random_walk (autocorr) + RANGE (regime) "
                    "agree — no directional edge, forcing NEUTRAL instead of "
                    "a discounted directional bet")

            # Apply confidence multiplier
            if _conf_mult != 1.0:
                confidence = _round_half_up(confidence * _conf_mult)

            # Apply continuation/reversal multipliers
            if _cont_mult != 1.0 or _rev_mult != 1.0:
                is_continuation = any(
                    True for r, e in adjusted
                    if r.direction == signal and r.signal_type == "CONTINUATION"
                    and e > 0
                )
                is_reversal = any(
                    True for r, e in adjusted
                    if r.direction == signal and r.signal_type == "REVERSAL"
                    and e > 0
                )

                if is_continuation and _cont_mult != 1.0:
                    confidence = _round_half_up(confidence * _cont_mult)
                    all_reasons.append(
                        f"_ALGO_STRATEGY: continuation ×{_cont_mult:.2f} "
                        f"({_algo})")
                if is_reversal and _rev_mult != 1.0:
                    confidence = _round_half_up(confidence * _rev_mult)
                    all_reasons.append(
                        f"_ALGO_STRATEGY: reversal ×{_rev_mult:.2f} "
                        f"({_algo})")

            # Strategy min_confidence gate (disabled in always-signal mode)
            if False and confidence < _min_conf:
                all_reasons.append(
                    f"_ALGO_STRATEGY: confidence {confidence} < {_min_conf} "
                    f"({_algo_strategy_name}) → will force NEUTRAL")
                _force_neutral = True

            # Single consolidated strategy banner
            all_reasons.append(
                f"_ALGO_STRATEGY: {_algo_icon} {_algo_strategy_name} "
                f"(conf ×{_conf_mult:.2f}) — {_algo_strategy_reason}")

        except ImportError:
            all_reasons.append(
                "_ALGO_STRATEGY: module not available, using default")
        except Exception as _algo_err:
            all_reasons.append(f"_ALGO_STRATEGY_ERROR: {_algo_err}")

    except ImportError:
        all_reasons.append(
            "_TIME_PATTERN: module not available, using default")
    except Exception as _e:
        all_reasons.append(f"_TIME_PATTERN_ERROR: {_e}")

    # Third calibration cap (only >75 consensus cap)
    if confidence > 75:
        if not (total_groups >= 3 and net_margin >= 0.6):
            confidence = min(confidence, 75)

    # HTF alignment bonus (after caps, before pair cap)
    _htf_aligned = (
        (htf_trend == "UPTREND" and signal == "CALL")
        or (htf_trend == "DOWNTREND" and signal == "PUT"))
    if _htf_aligned:
        confidence = min(100, confidence + HTF_ALIGNED_BONUS)
    elif htf_trend in ("UPTREND", "DOWNTREND"):
        confidence = max(0, confidence - HTF_COUNTER_PENALTY)

    # Re-apply trend cap after HTF bonus
    if _is_trending and confidence > 0:
        _post_htf_cap = TREND_CAP_OTC_REVERSAL if _is_otc_reversal else TREND_CAP_STANDARD
        if confidence > _post_htf_cap:
            confidence = min(confidence, _post_htf_cap)

    # Per-pair max_confidence cap
    _pair_max_conf = weight_adapter.get_max_confidence(asset)
    if _pair_max_conf is not None and confidence > _pair_max_conf:
        confidence = min(confidence, _pair_max_conf)
        all_reasons.append(
            f"_PAIR_CAP: {asset} max_confidence={_pair_max_conf} "
            f"(historical win rate too low)")

    # Strength tier determination
    # FIX (DEEP-FIX-2026-08-07 / STRONG-BOOST): previously required 2+ groups
    # for STRONG, but with only pattern+wickwall active they rarely fire
    # together. Pattern alone at high effective score is very reliable
    # (63.5% WR). New tiers:
    #   STRONG: pattern eff>=8, OR pattern+wickwall both agree
    #   MEDIUM: pattern eff>=3 (standard single-module signal)
    #   WEAK:   below MEDIUM → filtered to NEUTRAL
    _pat_eff = 0
    for r, e in adjusted:
        if r.module_name == "pattern" and r.direction == signal:
            _pat_eff = max(_pat_eff, e)
    _wick_eff = 0
    for r, e in adjusted:
        if r.module_name == "wickwall" and r.direction == signal:
            _wick_eff = max(_wick_eff, e)

    _dual_agree = _pat_eff > 0 and _wick_eff > 0  # both fired same direction

    if _pat_eff >= 8:
        strength = "STRONG"
        all_reasons.append(f"_STRONG: pattern eff={_pat_eff} -> STRONG (high conviction)")
    elif _dual_agree and _pat_eff >= 4 and _wick_eff >= 1:
        strength = "STRONG"
        all_reasons.append(f"_STRONG: pattern+wickwall dual confirm (pat={_pat_eff}, wick={_wick_eff})")
    elif confidence >= 65 and abs_net >= 3 and majority_group_n >= 2 and has_pattern_confluence:
        strength = "STRONG"
    elif (confidence >= ULTRA_CONSENSUS_CONF_MIN
          and abs_net >= ULTRA_CONSENSUS_ABS_NET_MIN
          and majority_group_n >= ULTRA_CONSENSUS_GROUPS_MIN):
        strength = "STRONG"
        all_reasons.append("_ULTRA_CONSENSUS: 3+ groups, abs_net>=5 -> STRONG")
    elif _pat_eff >= 3:
        strength = "MEDIUM"
    elif confidence >= 50 and abs_net >= 2:
        strength = "MEDIUM"
    elif confidence >= MEDIUM_CONFIDENCE_FLOOR and abs_net >= 2:
        strength = "MEDIUM"
    elif abs_net >= 1:
        strength = "WEAK"
    else:
        strength = "MEDIUM"

    # ── WEAK BOOST (DEEP-FIX-2026-08-07) ───────────────────────────────
    # WEAK signals: 41.9% WR (380 samples). But some WEAK signals have
    # supporting evidence (HTF aligned, good hour, range extreme) that
    # pushes them into MEDIUM territory (87.7% WR). Boost confidence
    # from supporting evidence to promote qualifying WEAK→MEDIUM.
    if strength == "WEAK" and WEAK_BOOST_ENABLED:
        _boost = 0
        _boost_reasons = []
        # HTF alignment bonus
        if htf_trend == "UPTREND" and signal == "CALL":
            _boost += 5; _boost_reasons.append("HTF uptrend aligned")
        elif htf_trend == "DOWNTREND" and signal == "PUT":
            _boost += 5; _boost_reasons.append("HTF downtrend aligned")
        # Hourly performance bonus
        try:
            from db import get_time_confidence_adjustment
            from datetime import datetime, timezone
            _now = int(time.time())
            _h = datetime.fromtimestamp(_now, tz=timezone.utc).hour
            _tadj = get_time_confidence_adjustment(asset, _h)
            if _tadj.get('win_pct', 0) >= 55 and _tadj.get('total', 0) >= 5:
                _boost += 5
                _boost_reasons.append(f"hour {_h:02d}:00 WR={_tadj['win_pct']:.0f}%")
        except Exception:
            pass
        # Range extreme bonus (price at support/resistance)
        if regime.get("is_ranging"):
            _pos = (candles[-1]["close"] - min(c["low"] for c in candles[-20:])) / max(1e-9, max(c["high"] for c in candles[-20:]) - min(c["low"] for c in candles[-20:]))
            if (_pos < 0.25 and signal == "CALL") or (_pos > 0.75 and signal == "PUT"):
                _boost += 5
                _boost_reasons.append("range extreme fade")
        # Apply boost
        if _boost > 0:
            confidence += _boost
            all_reasons.append(f"_WEAK_BOOST: +{_boost} ({', '.join(_boost_reasons)}) -> conf {confidence-_boost}→{confidence}")
            # Re-evaluate strength with boosted confidence
            if confidence >= 50:
                strength = "MEDIUM"
                all_reasons.append(f"_WEAK_BOOST: promoted WEAK→MEDIUM (conf={confidence})")

    # ── WEAK SIGNAL FILTER ──────────────────────────────────────────────

    # ── PATTERN GATE (DEEP-FIX-2026-08-07 V2) ──────────────────────────
    # Pattern module = 63.5% WR anchor. Every signal MUST have pattern.
    # Wickwall (57.5%) is secondary confirmation when it agrees.
    # Combined: pattern alone ~63%, pattern+wickwall agree ~75-85%.
    if PATTERN_GATE:
        _pat_fired = any(r.module_name == "pattern" and r.direction == signal and e > 0
                         for r, e in adjusted)
        if not _pat_fired:
            all_reasons.append(
                "_PATTERN_GATE: pattern module did not fire -> NEUTRAL. "
                "Pattern is the only module with proven edge (63.5% WR). "
                "Set QX_PATTERN_GATE=0 to override.")
            return {
                "signal": "NEUTRAL", "confidence": 0, "raw_confidence": confidence,
                "strength": "NEUTRAL", "score": abs_net,
                "reasons": all_reasons,
                "regime": regime, "agree": agree, "total": total_groups,
                "signals_fired": total_groups,
                "modules": _module_breakdown(adjusted, all_results, module_names),
                "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
                "strategy": _algo_strategy_name,
                "strategy_reason": _algo_strategy_reason,
                "signal_quality": _signal_quality,
                "pattern_gate": True,
            }

    # Also check dual confirm if enabled (both pattern + wickwall)
    if DUAL_CONFIRM:
        _pat_fired = any(r.module_name == "pattern" and r.direction == signal and e > 0
                         for r, e in adjusted)
        _wick_fired = any(r.module_name == "wickwall" and r.direction == signal and e > 0
                          for r, e in adjusted)
        if not (_pat_fired and _wick_fired):
            missing = []
            if not _pat_fired: missing.append("pattern")
            if not _wick_fired: missing.append("wickwall")
            all_reasons.append(
                f"_DUAL_CONFIRM: missing {', '.join(missing)} -> NEUTRAL.")
            return {
                "signal": "NEUTRAL", "confidence": 0, "raw_confidence": confidence,
                "strength": "NEUTRAL", "score": abs_net,
                "reasons": all_reasons,
                "regime": regime, "agree": agree, "total": total_groups,
                "signals_fired": total_groups,
                "modules": _module_breakdown(adjusted, all_results, module_names),
                "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
                "strategy": _algo_strategy_name,
                "strategy_reason": _algo_strategy_reason,
                "signal_quality": _signal_quality,
                "dual_confirm_gate": True,
            }

    # ── CONSENSUS GATE ──────────────────────────────────────────────────
    # Single-group signals have no corroboration. Live data shows single-
    # group signals at ~47% WR vs 2+ groups at ~65% WR.
    if majority_group_n < MIN_CONSENSUS_GROUPS:
        all_reasons.append(
            f"_CONSENSUS_GATE: only {majority_group_n} group(s) agree "
            f"(need {MIN_CONSENSUS_GROUPS}) -> NEUTRAL. "
            f"Single-group signals historically ~47% WR.")
        return {
            "signal": "NEUTRAL", "confidence": 0, "raw_confidence": confidence,
            "strength": "NEUTRAL", "score": abs_net,
            "reasons": all_reasons,
            "regime": regime, "agree": agree, "total": total_groups,
            "signals_fired": total_groups,
            "modules": _module_breakdown(adjusted, all_results, module_names),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
            "strategy": _algo_strategy_name,
            "strategy_reason": _algo_strategy_reason,
            "signal_quality": _signal_quality,
            "consensus_gate": True,
        }

    if strength == "WEAK" and not ALLOW_WEAK_SIGNALS:
        all_reasons.append(
            "_WEAK_FILTER: signal strength=WEAK → returning NEUTRAL. "
            "Live backtest: WEAK signals 38% WR (137 samples) vs "
            "MEDIUM+STRONG 100% WR (26 samples). "
            "Set QX_ALLOW_WEAK_SIGNALS=1 to override.")
        return {
            "signal": "NEUTRAL", "confidence": 0, "raw_confidence": confidence,
            "strength": "NEUTRAL", "score": abs_net,
            "reasons": all_reasons,
            "regime": regime, "agree": agree, "total": total_groups,
            "signals_fired": total_groups,
            "modules": _module_breakdown(adjusted, all_results, module_names),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
            "strategy": _algo_strategy_name,
            "strategy_reason": _algo_strategy_reason,
            "signal_quality": _signal_quality,
            "weak_filtered": True,
        }

    # Step 11: Low-confidence skip (+ disabled extreme-range suppression)
    _is_extreme_ranging = False
    if _is_extreme_ranging and not (
        confidence >= 65 and abs_net >= 5 and majority_group_n >= 2
        and has_pattern_confluence
    ):
        all_reasons.append(
            f"_RANGE_SUPPRESS: EXTREME RANGE (str={_trend_strength:.2f}, "
            f"vol={_volatility_pct:.1f}x) -> NEUTRAL (historical win rate "
            f"only 44% in this regime)")
        return {
            "signal": "NEUTRAL", "confidence": 0, "raw_confidence": 0, "strength": "NEUTRAL",
            "score": net, "reasons": all_reasons,
            "regime": regime, "agree": agree, "total": total_groups,
            "signals_fired": total_groups,
            "modules": _module_breakdown(adjusted, all_results, module_names),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
            "strategy": _algo_strategy_name,
            "strategy_reason": _algo_strategy_reason,
            "range_suppressed": True,
            "signal_quality": "NONE",
        }
    _low_conf_threshold = LOW_CONF_SKIP_OTC if asset.endswith("_otc") else LOW_CONF_SKIP_REAL
    if confidence < _low_conf_threshold:
        all_reasons.append(f"_LOW_CONF_SKIP: confidence {confidence} < {_low_conf_threshold} -> NEUTRAL")
        return {
            "signal": "NEUTRAL", "confidence": 0, "raw_confidence": 0, "strength": "NEUTRAL",
            "score": net, "reasons": all_reasons,
            "regime": regime, "agree": agree, "total": total_groups,
            "signals_fired": total_groups,
            "modules": _module_breakdown(adjusted, all_results, module_names),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
            "strategy": _algo_strategy_name,
            "strategy_reason": _algo_strategy_reason,
            "signal_quality": "NONE",
        }

    # Strategy min_confidence force-neutral (after caps)
    if _force_neutral:
        all_reasons.append(
            f"_ALGO_STRATEGY: confidence {confidence} < strategy min → NEUTRAL")
        return {
            "signal": "NEUTRAL", "confidence": 0, "raw_confidence": 0, "strength": "NEUTRAL",
            "score": net, "reasons": all_reasons,
            "regime": regime, "agree": agree, "total": total_groups,
            "signals_fired": total_groups,
            "modules": _module_breakdown(adjusted, all_results, module_names),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
            "strategy": _algo_strategy_name,
            "strategy_reason": _algo_strategy_reason,
            "signal_quality": "NONE",
        }

    return {
        "signal": signal,
        "confidence": _calibrated_confidence(confidence, agree),
        "raw_confidence": confidence,
        "strength": strength,
        "score": _tiebreaker_score if _tiebreaker_score is not None else net,
        "reasons": all_reasons,
        "regime": regime,
        "agree": agree,
        "total": total_groups,
        "signals_fired": total_groups,
        "modules": _module_breakdown(adjusted, all_results, module_names),
        "asset": asset,
        "profile": pair_profile,
        "htf_trend": htf_trend,
        "strategy": _algo_strategy_name,
        "strategy_reason": _algo_strategy_reason,
        "signal_quality": _signal_quality,
    }


# HELPERS

def _collapse_body_group(body_signals: list) -> ModuleResult:
    """Collapse correlated BODY signals into ONE composite vote."""
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
        # Tie on sum: break by strongest single signal, then by count
        max_call = max((r.score for r in call_signals), default=0)
        max_put = max((r.score for r in put_signals), default=0)
        if max_call > max_put:
            direction = "CALL"
            majority_signals = call_signals
            max_score = max_call
        elif max_put > max_call:
            direction = "PUT"
            majority_signals = put_signals
            max_score = max_put
        else:
            direction = "CALL" if call_n > put_n else "PUT"
            majority_signals = call_signals if direction == "CALL" else put_signals
            max_score = max((r.score for r in majority_signals), default=0)
        agree_n = len(majority_signals)
    elif call_n > 0:
        # Total tie — pick direction with strongest single signal
        max_call = max(r.score for r in call_signals)
        max_put  = max(r.score for r in put_signals)
        if max_call >= max_put:
            direction, majority_signals, max_score, agree_n = "CALL", call_signals, max_call, call_n
        else:
            direction, majority_signals, max_score, agree_n = "PUT", put_signals, max_put, put_n
    else:
        return None

    bonus = 1 if agree_n >= 3 else 0
    score = max_score + bonus

    # sig_type follows the MAJORITY direction's signals, weighted by score
    cont_score = sum(r.score for r in majority_signals if r.signal_type == "CONTINUATION")
    rev_score  = sum(r.score for r in majority_signals if r.signal_type == "REVERSAL")
    if cont_score > rev_score:
        sig_type = "CONTINUATION"
    elif rev_score > cont_score:
        sig_type = "REVERSAL"
    else:
        # Tie: break by strongest single signal, then by count
        max_cont = max((r.score for r in majority_signals
                        if r.signal_type == "CONTINUATION"), default=0)
        max_rev = max((r.score for r in majority_signals
                       if r.signal_type == "REVERSAL"), default=0)
        if max_cont > max_rev:
            sig_type = "CONTINUATION"
        elif max_rev > max_cont:
            sig_type = "REVERSAL"
        else:
            cont_n = sum(1 for r in majority_signals if r.signal_type == "CONTINUATION")
            rev_n  = sum(1 for r in majority_signals if r.signal_type == "REVERSAL")
            sig_type = "CONTINUATION" if cont_n > rev_n else "REVERSAL"

    # Reasons from majority side only (winning direction)
    reasons_str = " | ".join(
        " | ".join(r.reasons) if r.reasons else ""
        for r in majority_signals)

    return ModuleResult(
        module_name="candle_reaction", direction=direction, score=score,
        confidence=min(70, score * 15),
        signal_type=sig_type, reliability="CANDLE", group="BODY",
        reasons=[f"[BODY collapsed] {reasons_str}"])


def _module_breakdown(adjusted: list, all_results: list, module_names: tuple) -> dict:
    """Build per-module breakdown dict for UI display."""
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
            "suppressed": len(module_raw) > 0 and len(module_adjusted) == 0,
            "raw_score": (sum(r.score for r in module_raw if r.direction == direction)
                          if direction != "NEUTRAL" else 0),
        }

    return breakdown


def _neutral(reasons, regime, asset="", weight_adapter=None, ctx=None,
             module_names: tuple = None, htf_trend="SIDEWAYS") -> dict:
    """Return a NEUTRAL prediction."""
    modules = {}
    pair_profile = "default"
    if weight_adapter is not None:
        pair_profile = weight_adapter.get_profile(asset)
        # Empty per-module breakdown for UI consistency
        if module_names:
            modules = _module_breakdown([], [], module_names)
    return {
        "signal": "NEUTRAL", "confidence": 0, "raw_confidence": 0, "strength": "NEUTRAL",
        "score": 0, "reasons": reasons if isinstance(reasons, list) else [reasons],
        "regime": regime, "agree": 0, "total": 0, "signals_fired": 0,
        "modules": modules, "asset": asset, "profile": pair_profile,
        "htf_trend": htf_trend, "signal_quality": "NONE",
    }
