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
import os
import math
from dataclasses import dataclass
from typing import Callable

# FIX (DEEP-AUDIT-2026-07-26 / F-02-16): `MarketContext` was imported but
# never used as a type annotation in this file (the `ctx` parameter in
# `_neutral` is intentionally untyped — see PROB 123). Removed to silence
# the unused-import warning.
from engines.base.types import ModuleResult
from engines.base.context import compute_context
from engines.base.modules import (
    candle_reaction as mod_candle,
    running_tick as mod_tick,
    pattern as mod_pattern,
    indicator as mod_indicator,
    key_level as mod_keylevel,
)
from engines.base.per_pair import PairWeightAdapter
# USER FIX #7 (2026-08-03): per-pair per-module direction lock.
# Filters out votes in the wrong direction for combos where live brain
# data shows >=15pp CALL/PUT win-rate gap. See engines/base/direction_bias.py.
from engines.base.direction_bias import is_vote_allowed as _is_vote_allowed, suppression_reason as _dir_lock_reason


# FIX (DEEP-AUDIT-2026-07-26 / F-02-CONST): centralised magic-number
# constants previously hardcoded throughout `predict()`. Grouping them
# here makes tuning / backtest iteration easier and removes ~25 inline
# magic numbers (per audit PROB 19, 20, 46-50, 56, 89-91, 94, 96, 97).
MIN_CANDLES_FOR_PREDICTION = 3            # PROB 56 / 108
EXHAUSTION_STREAK_MIN = 4                 # PROB 19 (long-streak check)
EXHAUSTION_RARE_STREAK_MIN = 3            # PROB 19 (rare-streak check)
EXHAUSTION_RARITY_MAX = 0.10              # PROB 19
EXHAUSTION_ACCEL_MAX = 0.7                # PROB 19 (tick deceleration)
EXHAUSTION_NET_MOVE_ATR_RATIO = 0.5       # PROB 19
EXHAUSTION_TICK_COUNT_MIN = 60            # PROB 19 (volume-price divergence)
EXHAUSTION_TICK_NET_ATR_RATIO = 0.3       # PROB 19

EDGE_FACTOR_BASE = 0.5                    # PROB 89 (edge_factor baseline)
EDGE_FACTOR_NET_MARGIN_WEIGHT = 0.5       # PROB 89
CONFIDENCE_SCALE = 100                   # PROB 90 (sqrt -> percentage scale)

SINGLE_GROUP_CAP_HIGH_MIN = 6            # PROB 20 / 91 (raw_majority >= 6)
SINGLE_GROUP_CAP_HIGH = 55
SINGLE_GROUP_CAP_MID_MIN = 4            # raw_majority >= 4
SINGLE_GROUP_CAP_MID = 48
SINGLE_GROUP_CAP_LOW = 42

SIDEWAYS_RANGE_DAMPEN = 5                # PROB 48 (confidence -5)

TREND_PENALTY = 15                        # PROB 49 (TREND_UP/DOWN -15)
TREND_CAP_STANDARD = 45                  # PROB 49
TREND_CAP_OTC_REVERSAL = 55              # PROB 49

ACCURACY_DAMPEN_MIN_SAMPLES = 3          # PROB 50
ACCURACY_DAMPEN_THRESHOLD = 0.45
ACCURACY_DAMPEN_FACTOR = 0.85
ACCURACY_BOOST_MIN_SAMPLES = 30
ACCURACY_BOOST_THRESHOLD = 0.65
ACCURACY_BOOST_FACTOR = 1.05

STRONG_NON_PATTERN_MIN_SCORE = 2         # PROB 94

HTF_ALIGNED_BONUS = 5                    # PROB 46 (HTF +5)
HTF_COUNTER_PENALTY = 5                  # PROB 46 (HTF -5)

ULTRA_CONSENSUS_CONF_MIN = 75           # PROB 96
ULTRA_CONSENSUS_ABS_NET_MIN = 8
ULTRA_CONSENSUS_GROUPS_MIN = 4

LOW_CONF_SKIP_THRESHOLD = 20             # PROB 97 (confidence < 20 -> NEUTRAL)
# FIX (WINRATE-BOOST #3, 2026-07-28): per-engine low-confidence skip threshold.
# 29/30 OTC candles are random_walk (97%) — at 50% expected accuracy,
# confidence-20 signals are coin flips with extra dampening. Raising the
# threshold for OTC suppresses low-quality signals and improves win rate.
# Real market pairs have genuine trends, so the lower threshold is kept.
# Overridable via env: QX_LOW_CONF_SKIP_OTC, QX_LOW_CONF_SKIP_REAL.
LOW_CONF_SKIP_OTC  = int(os.environ.get("QX_LOW_CONF_SKIP_OTC",  "30"))
LOW_CONF_SKIP_REAL = int(os.environ.get("QX_LOW_CONF_SKIP_REAL", "25"))

MEDIUM_CONFIDENCE_FLOOR = 30             # PROB 15 (strength tier MEDIUM gate)

# FIX (DIRECTION-LOCK-OVERFIT-RISK, 2026-08-03): direction_bias.py's
# DIRECTION_LOCK map is generated from live win-rate gaps with only
# >=8 samples per side required (no significance test) — at n=8 the
# standard error of a win-rate estimate is ~17pp, so a documented
# "15pp gap" can easily be noise rather than a real broker bias.
# disabled_pairs.py already hit this exact problem today and switched
# from a hard block to a partial penalty (Fix #8 REVISED) because the
# hard block was "too aggressive" and threw away real signal. Apply
# the same fix here: dampen an against-the-lock vote instead of
# zeroing it, so a genuinely strong signal can still partially survive
# if a given lock entry turns out to be noise rather than real bias.
DIRECTION_LOCK_DAMPEN = 0.4


# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-05 + AUDIT-LIVE-2-05): Python 3's
# built-in `round()` uses banker's rounding (round-half-to-EVEN), not
# round-half-up. This produces non-monotonic results at .5 boundaries:
#   round(0.5) = 0 (not 1), round(1.5) = 2, round(2.5) = 2 (not 3),
#   round(49.5) = 50, round(50.5) = 50 (not 51), round(51.5) = 52.
# With 5 successive round() calls in the multiplier cascade (time × regime ×
# confidence × continuation × reversal), the compounded error biases
# confidence DOWNWARD by 1-3 points and clusters values at even numbers.
# `_round_half_up` uses the mathematically standard round-half-up convention,
# which is monotonic and what the calibration comments assume.
def _round_half_up(x: float) -> int:
    """Round half up (not banker's rounding). Returns an int."""
    return int(math.floor(x + 0.5))


# FIX (DEEP-AUDIT-2026-07-26 / F-02-05 / F-02-21): extracted the bin-cap
# calibration logic (previously duplicated at lines ~744-760 and ~858-886)
# into a single helper. Also fixes the 5-point discontinuous jump at the
# confidence=70 boundary:
#   - Previous behaviour: 60-69% bin capped at 60, 70-79% bin capped at 65
#     → a 1-point raw increase (69→70) produced a 5-point capped jump
#     (60→65). This created a "magic cliff" where small upstream changes
#     (e.g., time-of-day multiplier ×1.01) could push confidence past 70
#     and gain 5 points of capped confidence.
#   - New behaviour: 60-79% bin unified, capped at 60. This matches the
#     dev's original intent (per the historical-accuracy comments above
#     the previous block: 70-79% bin = 50% actual win rate → cap at 60,
#     same as 60-69% bin = 49% actual → cap at 60). The "monotonic caps"
#     property is preserved (each bin's cap is still >= the previous bin's
#     cap), and the discontinuity is eliminated.
# Returns the calibrated confidence (int).
def _apply_calibration_caps(confidence: int, total_groups: int,
                            net_margin: float, abs_net: int = 0,
                            majority_group_n: int = 0,
                            has_pattern_confluence: bool = False) -> int:
    """Apply bin-based calibration caps.

    USER REQUIREMENT (2026-08-03, Fix #2 — Confidence Calibration):
    Live brain data shows the engine is OVERCONFIDENT:
      70-79% confidence -> only 52% actual win (expected ~75%)
      60-69% confidence -> only 52% actual win (expected ~65%)
      50-59% confidence -> only 45% actual win (expected ~55%)
    Recommendation from /api/brain/insights: cap each bin at the actual
    win-rate ceiling + ~10pp safety margin.

    New caps (2026-08-03):
      >= 90   -> 55   (was 55 — kept)
      80-89   -> 55   (was 60 — tightened: 80% claimed but only 51% actual)
      70-79   -> 50   (was 60 — tightened: 70% claimed but only 52% actual)
      60-69   -> 45   (was 60 — tightened: 60% claimed but only 52% actual)
      50-59   -> 40   (NEW bin — was uncapped; 50% claimed but only 45% actual)
      < 50    -> unchanged (already low-confidence path)

    Ultra-consensus + pattern-confluence overrides preserved (raise the
    ceiling to 75 / 65 respectively when those rare strong-signal paths
    fire — see the historical comment below).

    Historical accuracy ceilings (7623 live signals, pre-2026-08-03):
      100%   -> 44% actual   (cap was 50)
      90-99  -> 46% actual   (cap was 55)
      80-89  -> 51% actual   (cap was 60)
      60-79  -> ~49-50% actual (cap was 60 unified)

    FIX (2026-07-27 / strong-unreachable): these bins clamp EVERY input
    >= 60 down to at most 60. But the STRONG strength tier (see the
    final strength determination near the end of predict()) requires
    confidence >= 65 (pattern-confluence path) or >= 75 (ultra-consensus
    path, no pattern needed — ULTRA_CONSENSUS_CONF_MIN). With every
    signal capped at 60 first, NEITHER path could ever fire — STRONG was
    unreachable for every prediction, full stop, regardless of how
    strong the actual consensus was. (There used to be a `confidence >
    75: ... cap 75` block here meant to let strong signals survive, but
    it checked confidence AFTER the bins already clamped it to <=60, so
    it was equally dead — removed, then this replaces it properly.)
    Fix: when a signal objectively meets the SAME thresholds the STRONG
    tier itself checks for below (not an independent guess), raise the
    ceiling to exactly the floor that tier needs — 75 for ultra-
    consensus, 65 for pattern-confluence — instead of the standard 60.
    This only RAISES THE CEILING; it never manufactures confidence a
    signal didn't earn (a raw 68 with ultra-consensus stays 68, not 75).
    Neither override is used by the first (pre-Step-9) call site, since
    has_pattern_confluence isn't known yet there — only abs_net /
    majority_group_n (ultra-consensus) apply at that call; the second
    call (after Step 9) can supply all three.
    """
    override = 0
    if (abs_net >= ULTRA_CONSENSUS_ABS_NET_MIN
            and majority_group_n >= ULTRA_CONSENSUS_GROUPS_MIN):
        override = max(override, ULTRA_CONSENSUS_CONF_MIN)  # 75
    if has_pattern_confluence and abs_net >= 5 and majority_group_n >= 2:
        override = max(override, 65)

    # USER FIX #2 (2026-08-03): tighter calibration caps based on live
    # brain data. The historical "every bin → 60" approach masked the
    # engine's overconfidence but did not actually improve win rate.
    # The new caps clamp displayed confidence closer to the actual win
    # rate ceiling, so users don't over-bet on weak signals.
    if confidence >= 100:
        confidence = min(confidence, max(50, override))
    elif confidence >= 90:
        confidence = min(confidence, max(55, override))
    elif confidence >= 80:
        confidence = min(confidence, max(55, override))   # was 60 -> 55
    elif confidence >= 70:
        confidence = min(confidence, max(50, override))   # was 60 -> 50
    elif confidence >= 60:
        confidence = min(confidence, max(45, override))   # was 60 -> 45
    elif confidence >= 50:
        confidence = min(confidence, max(40, override))   # NEW: was uncapped
    # < 50: leave unchanged (already low-confidence path)
    return confidence


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
            period: int = 60, config=None, recent_accuracy=None) -> dict:
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
        recent_accuracy: optional tuple (accuracy_float, sample_count) from
            db.recent_accuracy(). When provided AND sample_count >= 8, the
            blender applies accuracy-aware self-correction:
              - recent_accuracy < 0.45 → confidence *= 0.85 (dampen)
              - recent_accuracy > 0.60 → confidence *= 1.05 (boost, capped 100)
            This lets the engine self-correct when it's been wrong recently.

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
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-104): error message referenced
        # `BLENDER_CONFIG` but the actual config object is named `CONFIG` in
        # both engines/otc/config.py and engines/real/config.py. Updated.
        raise ValueError("BlenderConfig is required — pass engines.{otc,real}.config.CONFIG")

    reliability = config.reliability
    weight_adapter = config.weight_adapter
    module_6_fn = config.module_6_fn
    module_names = config.module_names

    # FIX (DEEP-AUDIT-2026-07-26 / F-02-108 / F-02-56): `not candles` is
    # redundant — `len(candles) < MIN_CANDLES_FOR_PREDICTION` is True for
    # the empty-list case too. Use the centralised constant for the
    # minimum-candle threshold (was magic `3`).
    if len(candles) < MIN_CANDLES_FOR_PREDICTION:
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
    # FIX (OTC-DEEP Phase 1, 2026-07-23): pass `asset` to the 6th module
    # so it can query the algorithm_monitor's current state for this
    # asset and gate signals based on the live algorithm guess.
    # The module_6_fn wrapper accepts (candles, ctx, asset=None) — if the
    # underlying analyze() doesn't accept asset, the wrapper ignores it.
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-25): the previous code caught
    # ALL TypeErrors, including ones raised INSIDE the module's analyze
    # function (e.g., `asset.upper()` when `asset=None`). The fallback then
    # called module_6_fn(candles, ctx) — which would ALSO fail or produce
    # degraded results without the asset context. Now: only catch the
    # specific TypeError from argument-count mismatch (the error message
    # contains "positional argument" or "unexpected keyword argument").
    # Internal TypeErrors are re-raised so they're visible (and caught by
    # the outer try/except in the prediction pipeline if any).
    try:
        all_results += module_6_fn(candles, ctx, asset)
    except TypeError as _te:
        _msg = str(_te).lower()
        if ("positional argument" in _msg
                or "unexpected keyword argument" in _msg
                or "takes" in _msg):
            # Module's analyze() doesn't accept asset — fall back to 2-arg call.
            all_results += module_6_fn(candles, ctx)
        else:
            # Internal TypeError — re-raise so it's not masked.
            raise

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
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-19): use centralised constant
    # EXHAUSTION_STREAK_MIN (was magic `4`).
    if ctx.stats["current_streak"] >= EXHAUSTION_STREAK_MIN:
        exhaustion_indicators += 1
        exhaustion_reasons.append(f"streak={ctx.stats['current_streak']}")

    # Check 2: Rare streak (statistical — independent of modules)
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-19): use centralised constants
    # EXHAUSTION_RARITY_MAX / EXHAUSTION_RARE_STREAK_MIN (was magic 0.10/3).
    if (ctx.stats["streak_rarity"] < EXHAUSTION_RARITY_MAX
            and ctx.stats["current_streak"] >= EXHAUSTION_RARE_STREAK_MIN):
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
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-19): use centralised
            # constants EXHAUSTION_ACCEL_MAX / EXHAUSTION_NET_MOVE_ATR_RATIO.
            if (accel < EXHAUSTION_ACCEL_MAX
                    and net_move > atr * EXHAUSTION_NET_MOVE_ATR_RATIO):
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
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-19): use centralised constants
        # EXHAUSTION_TICK_COUNT_MIN / EXHAUSTION_TICK_NET_ATR_RATIO.
        if (tick_count >= EXHAUSTION_TICK_COUNT_MIN
                and net_move < atr * EXHAUSTION_TICK_NET_ATR_RATIO):
            exhaustion_indicators += 1
            exhaustion_reasons.append(
                f"volume-price divergence ({tick_count} ticks, net={net_move/atr:.2f}x ATR)")

    # Thresholds: with only 4 independent checks (was 6), require 2 for
    # "exhausting" and 3 for "strongly exhausting". This keeps the gate
    # selective — it fires only when multiple independent signals agree.
    # FIX (BUG-T tuning, 2026-07-20): backtest showed the exhaustion gate
    # fired too often (37% of predictions had is_exhausting=True) and those
    # predictions were LESS accurate (47%) than non-exhausting (52%).
    # Raised threshold to 3 for "exhausting" and 4 for "strongly exhausting"
    # — now fires only on genuine multi-signal exhaustion.
    is_exhausting = exhaustion_indicators >= 3
    is_strongly_exhausting = exhaustion_indicators >= 4

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

    # FIX (DEEP-AUDIT-2026-07-26 / F-02-18): use `.get()` with safe defaults
    # for all regime fields — `classify_market_regime` may return a partial
    # dict in edge cases (e.g., cold-start) and direct `[]` indexing would
    # raise KeyError, crashing the prediction.
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
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-14): previously
        # `trend_dir = "UP" if regime["regime"] == "TREND_UP" else "DOWN"`
        # defaulted to DOWN for unknown regimes (e.g., "TREND", "STRONG_TREND_UP").
        # Now: substring-match "UP" so any regime containing "UP" is treated as UP.
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
    # USER FIX #7 (2026-08-03): initialize all_reasons early so the
    # direction-lock suppression block (inside the for-loop below) can
    # append reasons. Previously all_reasons was only initialized later
    # (around line 698), but the direction-lock check runs inside the
    # loop BEFORE that point — causing UnboundLocalError. Move the init
    # up here so it's available throughout the prediction path.
    all_reasons = list(regime_reasons)
    for r in grouped_results:
        # Regime multiplier
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-18): use safe `_is_*` locals
        # (with defaults) instead of direct `regime["..."]` indexing.
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

        # FIX (live data, 2026-07-20): real Quotex OTC data shows TREND_UP
        # regime has 47.1% accuracy and TREND_DOWN has 42.9% — continuation
        # signals are WRONG in OTC trends (broker reverses them). For OTC
        # engine, INVERT the regime multiplier in trends: boost REVERSAL
        # and dampen CONTINUATION. Real engine keeps normal behavior.
        #
        # FIX (DEEP-AUDIT-2026-07-26 / A-02-CRIT-1):
        # The previous code UNCONDITIONALLY set r_mult to 0.7 / 1.3 here,
        # completely OVERWRITING the 3-state exhaustion gate (0.8 / 1.0 /
        # 1.2) computed above. This meant that in OTC trends:
        #   - Strongly exhausting + REVERSAL: gate said 1.2, but OTC override
        #     set it to 1.3 (small boost preserved).
        #   - Mildly exhausting + REVERSAL: gate said 1.0 (no penalty), but
        #     OTC override set it to 1.3 (bonus added).
        #   - NOT exhausting + REVERSAL: gate said 0.8 (penalty), but OTC
        #     override set it to 1.3 (BIG bonus — completely wrong!).
        # The third case is the worst: a non-exhausting OTC trend REVERSAL
        # signal got a 1.3 boost when the gate wanted to dampen it to 0.8.
        # This means OTC trend REVERSAL signals were emitted even when the
        # trend was strong and not exhausting — exactly the wrong time.
        # Fix: MULTIPLY the gate's r_mult by the OTC inversion factor,
        # preserving the gate's relative dampening/boosting.
        #
        # USER FIX #4 (2026-08-03): REMOVED the OTC inversion ×1.3 boost.
        # Live brain data shows OTC trend-reversal signals at cap=55 only
        # win 35-44% — the inversion boost was amplifying weak signals
        # that then hit the trend cap (55) and traded anyway with negative
        # EV. The continuation dampen (×0.7) is KEPT — continuation in OTC
        # trends is still wrong (broker reverses them). But we no longer
        # BOOST the reversal side; the gate's natural 0.8/1.0/1.2 stands.
        # Net effect: fewer OTC trend-reversal signals emitted, those that
        # do fire are the genuinely-exhausting ones the gate wanted to boost.
        if _is_trending and config.engine_name == "otc":
            if r.signal_type == "CONTINUATION":
                # OTC trends reverse, don't continue — dampen continuation.
                # Apply on top of the gate's value (which may already be 1.3
                # for non-exhausting, or 1.0 for exhausting).
                r_mult = r_mult * 0.7  # was: r_mult = 0.7
            # REVERSAL: no longer boosted — gate value (0.8/1.0/1.2) stands.
            # (Previous code: r_mult = r_mult * 1.3 — removed 2026-08-03.)

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

        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-05 + AUDIT-3-12): use
        # _round_half_up (not round) so 0.5 → 1 (not 0 via banker's rounding).
        # Also: only suppress if the RAW product (pre-round) is < 0.5, not if
        # the rounded value is 0. A raw score 1 with multipliers product=0.5
        # was previously `round(0.5) = 0` → suppressed. Now: 0.5 ≥ 0.5 →
        # survives with effective=1. This eliminates the asymmetric 0.50→0 /
        # 0.51→1 suppression boundary.
        #
        # USER FIX #7 (2026-08-03): per-pair per-module direction lock.
        # If (asset, r.module_name) is in the DIRECTION_LOCK map and the
        # module's vote direction does NOT match the locked direction,
        # suppress the vote entirely (skip to next iteration). Live brain
        # data shows these combos lose >=15pp more often in the wrong
        # direction, so filtering them out is +EV.
        # FIX (DIRECTION-LOCK-OVERFIT-RISK, 2026-08-03): changed from a
        # hard suppress (`continue`, score forced to 0) to a partial
        # dampen (×DIRECTION_LOCK_DAMPEN) — see constant definition above
        # for rationale. The vote still counts, just weighted down, so it
        # can still contribute if the module's signal was genuinely strong.
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
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-19): previously used
    # `len(original_groups)` as the denominator, which FULLY counted
    # suppressed groups. This was too aggressive: with 5 groups firing
    # but 3 suppressed, vote_ratio = 2/5 = 0.4 — a 60% reduction. In
    # volatile regimes (×0.7 multiplier on all signals), many signals are
    # suppressed, deflating confidence even when the surviving signals
    # strongly agree. The original intent (don't let suppressed groups
    # VANISH from the denominator) is preserved, but suppressed groups
    # now count as 0.5 instead of 1.0 — a milder dampening. With 5 groups
    # (3 suppressed): effective_total = 2 + 0.5*3 = 3.5, vote_ratio = 2/3.5
    # = 0.57 (was 0.4). The sqrt() in the confidence formula further
    # softens the difference. Use `fired_groups` count for the integer
    # `total_groups` field (preserved for display/API compatibility) but
    # compute `effective_total_groups` for the vote_ratio denominator.
    surviving_groups = len(fired_groups)
    suppressed_groups = max(0, len(original_groups) - surviving_groups)
    effective_total_groups = surviving_groups + 0.5 * suppressed_groups
    # Integer total_groups for display / API consumers (use original count
    # for backward compat — `signals_fired` and `total` fields).
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-39): `len(original_groups)` already
    # returns 0 for an empty set — the `if original_groups else 0` guard
    # was redundant. Removed.
    total_groups = len(original_groups)
    # If nothing survived suppression, fired_groups is empty — but
    # original_groups may be non-empty (all suppressed). In that case
    # we still want to report NEUTRAL, not crash on divide-by-zero.
    # NOTE (USER FIX #7, 2026-08-03): all_reasons was already initialized
    # above (around line 524) to include regime_reasons + any direction-
    # lock suppression reasons appended inside the multiplier loop.
    # Do NOT reset it here — that would erase those reasons.
    for r, e in adjusted:
        score_str = f" (eff={e})" if e != r.score else ""
        for reason in r.reasons:
            all_reasons.append(f"[{r.module_name}] {reason}{score_str}")
    # NOTE (USER FIX #7, 2026-08-03): all_reasons was pre-initialized with
    # regime_reasons above (line ~524) so the direction-lock block could
    # append. Skip the duplicate `all_reasons += regime_reasons` here.
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
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-08 + AUDIT-3-33): this
        # branch is UNREACHABLE in practice — `grouped_results` is built from
        # `all_results` (which was non-empty after the line-134 check), so
        # `original_groups` is always non-empty and `total_groups > 0`. The
        # branch is kept as defensive code (in case a future refactor allows
        # `grouped_results` to become empty), and the `_neutral` call correctly
        # passes `ctx` (a MarketContext) as the 5th positional argument —
        # `_neutral(reasons, regime, asset, weight_adapter, ctx, ...)` — so
        # there is no type mismatch. The audit incorrectly flagged this as a
        # type bug, but the actual positional ordering is correct.
        return _neutral(all_reasons or ["NO_SIGNAL"], regime, asset, weight_adapter,
                         ctx, module_names=module_names, htf_trend=htf_trend)

    # FIX (DEEP-AUDIT-2026-07-26 / F-02-04): initialise the tiebreaker
    # score override. When the group-count tiebreaker fires (net==0 but
    # group counts differ), the final return's `score` field should
    # reflect the majority direction's voting strength, NOT the (zero)
    # `net` value. The internal `net` stays 0 so confidence calibration
    # (which uses net_margin = abs(net)/total) stays dampened as the
    # original dev intended — only the *displayed* score field changes.
    # Previously the final return at line ~1372 returned `score=net=0`
    # for tiebreaker CALL/PUT signals, breaking consumers that expect
    # `score>0` for non-NEUTRAL signals (UI showed "CALL 55%" but
    # score=0 in DB).
    _tiebreaker_score = None

    net = call_score - put_score
    total = call_score + put_score

    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-24): when net == 0 (perfect
    # score tie), use GROUP COUNT as a tiebreaker before returning NEUTRAL.
    # A 3-CALL-group vs 1-PUT-group tie (where PUT has higher per-group
    # score, e.g. CALL effective [2,2,1] vs PUT effective [5]) previously
    # returned NEUTRAL, discarding the 3-vs-1 CALL consensus. Now: if the
    # group counts differ, pick the direction with more groups (the
    # consensus side) and proceed to confidence calibration. Only return
    # NEUTRAL if the group counts are ALSO tied. Total==0 (all signals
    # suppressed) still returns NEUTRAL — no consensus to break.
    #
    # USER FIX #3 (2026-08-03, REVISED): RANGE regime suppression.
    # Live brain data (/api/brain/insights REGIME_PATTERN) shows the engine
    # wins only 44% in RANGE regime (154/348).
    #
    # IMPORTANT CORRECTION (2026-08-03, after live deploy): the original
    # Fix #3 suppressed ALL signals when is_ranging==True. But Quotex OTC
    # pairs are classified as RANGE ~81% of the time (random_walk algo),
    # so this suppressed nearly ALL OTC signals — only 9 predictions in
    # several hours vs 1280 before. Win rate dropped to 33%.
    #
    # REVISED approach: only suppress when the RANGE is EXTREME — i.e.,
    # trend_strength is very low (< 0.1) AND not volatile (volatility_pct
    # < 1.5x). This still catches the worst ranging periods but lets
    # mild/moderate ranging markets produce signals (with dampened
    # confidence via the existing SIDEWAYS_RANGE_DAMPEN -5 above).
    #
    # This check is performed LATER in the flow (after confidence + abs_net
    # + majority_group_n + has_pattern_confluence are all computed) so we
    # can apply the ultra-consensus exception. See the second RANGE block
    # below near the LOW_CONF_SKIP check.

    if total == 0:
        call_g = set(r.group for r, e in adjusted if r.direction == "CALL")
        put_g = set(r.group for r, e in adjusted if r.direction == "PUT")
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-85): `max(len(call_g), len(put_g))`
        # already returns 0 for two empty sets — the `if (call_g or put_g)
        # else 0` guard was redundant. Removed.
        maj_n = max(len(call_g), len(put_g))
        # FIX (USER FIX #7+#8, 2026-08-03): include strategy keys so
        # downstream consumers (tests, DB log) don't crash when all
        # votes were suppressed by direction lock. Use safe fallback
        # because _algo_strategy_name is computed later in the flow.
        _strat = locals().get('_algo_strategy_name', 'default')
        _strat_r = locals().get('_algo_strategy_reason', '')
        return {
            "signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
            "score": 0, "reasons": all_reasons or ["CONFLICTING_SIGNALS"],
            "regime": regime, "agree": maj_n,
            "total": total_groups, "signals_fired": total_groups,
            "modules": _module_breakdown(adjusted, all_results, module_names),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
            "strategy": _strat,
            "strategy_reason": _strat_r,
        }
    if net == 0:
        # Score tie — try group-count tiebreaker.
        call_g = set(r.group for r, e in adjusted if r.direction == "CALL")
        put_g = set(r.group for r, e in adjusted if r.direction == "PUT")
        if len(call_g) != len(put_g):
            # Group-count tiebreaker: pick the direction with more groups.
            signal = "CALL" if len(call_g) > len(put_g) else "PUT"
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-107): use ASCII `->` not
            # unicode `→` so the reason renders in all terminals/logs.
            all_reasons.append(
                f"_TIEBREAKER: score tied (CALL={call_score}, PUT={put_score}), "
                f"group count {len(call_g)} vs {len(put_g)} -> {signal}")
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-04): set _tiebreaker_score
            # to the majority direction's raw score so the final return's
            # `score` field reflects actual voting strength. Previously
            # the final return used `net` (=0) as `score`, so the UI
            # showed "CALL 55%" but score=0 — breaking consumers that
            # expect score>0 for non-NEUTRAL signals.
            _tiebreaker_score = call_score if signal == "CALL" else put_score
            # The internal `net` stays 0 so downstream edge_factor stays
            # dampened (0.5 baseline) — confidence is dampened to reflect
            # the score tie, as the original dev intended.
        else:
            # Group count also tied — return NEUTRAL.
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-85): redundant `else 0` removed.
            maj_n = max(len(call_g), len(put_g))
            return {
                "signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
                "score": 0, "reasons": all_reasons or ["CONFLICTING_SIGNALS"],
                "regime": regime, "agree": maj_n,
                "total": total_groups, "signals_fired": total_groups,
                "modules": _module_breakdown(adjusted, all_results, module_names),
                "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
            }
    else:
        signal = "CALL" if net > 0 else "PUT"

    # ── Step 8: Confidence calibration ───────────────────────────────────
    majority_groups = call_groups if signal == "CALL" else put_groups
    majority_group_n = len(majority_groups)

    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-19): use effective_total_groups
    # (suppressed count as 0.5) for the vote_ratio denominator. This is a
    # milder dampening than the previous `total_groups` (suppressed count as
    # 1.0) and prevents over-deflation in volatile regimes.
    vote_ratio = (majority_group_n / effective_total_groups) if effective_total_groups > 0 else 0
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-06): `majority_score = max(call_score,
    # put_score)` was computed here but NEVER referenced again — dead
    # variable. The single-group cap at line ~722 uses `raw_majority`
    # (raw scores) instead, per AUDIT-3-04. Deleted.
    # majority_score = max(call_score, put_score)
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-17): use RAW scores for
    # weight_ratio, not EFFECTIVE (post-multiplier) scores. The multipliers
    # were already applied at line 384 (effective = round(raw * r_mult *
    # t_mult * p_mult * h_mult)). Using effective scores in weight_ratio
    # applies the multipliers a SECOND time via the confidence formula
    # (sqrt(vote_ratio * weight_ratio * ...)). A heavily-boosted module
    # (e.g., otc_pattern on USDPKR_otc with weight 1.8) gets its boost
    # applied TWICE: once to `effective` (raw 2 → effective 4-5), and again
    # via `weight_ratio` (which now favors the boosted side). Using raw
    # scores for weight_ratio removes the double-count while keeping the
    # multipliers' effect on `effective` (which feeds `total` and
    # `majority_score` for the single-group cap and strength tiers).
    raw_call = sum(r.score for r, e in adjusted if r.direction == "CALL")
    raw_put = sum(r.score for r, e in adjusted if r.direction == "PUT")
    raw_total = raw_call + raw_put
    weight_ratio = (max(raw_call, raw_put) / raw_total) if raw_total > 0 else 0

    # FIX (BUG-G, deep audit 2026-07-20): the previous confidence formula
    # `sqrt(vote_ratio * weight_ratio) * 100` saturated to ~100% whenever a
    # single group of weak votes dominated. With 2 groups where 1 fires
    # strongly (vote_ratio=0.5) AND has all the score (weight_ratio=1.0),
    # sqrt(0.5*1.0)=0.707 → 71% — already "MEDIUM" with no real consensus.
    # Worse: with 1 group only (vote_ratio=1.0, weight_ratio=1.0),
    # confidence = 100 — clearly bogus. We now scale by NET EDGE MARGIN
    # so confidence tracks the actual winning margin, not the ceiling.
    #   net_margin = |call_score - put_score| / total
    # Confidence = sqrt(vote_ratio * weight_ratio * (0.5 + 0.5*net_margin)) * 100
    # — when net_margin → 1 (unanimous), confidence = sqrt(vote_ratio*weight_ratio)*100 (original)
    # — when net_margin → 0 (dead heat), confidence = sqrt(vote_ratio*weight_ratio) * ~70
    # Combined with the single-group cap below, this prevents weak consensus
    # from ever showing as 90-100%.
    net_margin = (abs(net) / total) if total > 0 else 0
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-89): use centralised EDGE_FACTOR_BASE
    # / EDGE_FACTOR_NET_MARGIN_WEIGHT constants (was magic 0.5/0.5).
    edge_factor = EDGE_FACTOR_BASE + EDGE_FACTOR_NET_MARGIN_WEIGHT * net_margin  # ∈ [0.5, 1.0]
    # FIX (DEEP-ANALYSIS, 2026-07-23): use round() instead of int() for
    # the initial confidence computation too. int(math.sqrt(...) * 100)
    # truncates — e.g. sqrt(0.5*1.0*0.7) = 0.5916 → 59.16 → int() = 59,
    # but round() = 59 (same in this case, but for 0.5918 → 59.18 → int 59,
    # round 59). The difference is small but consistent with the round()
    # fixes applied to all the multipliers downstream.
    confidence = _round_half_up(math.sqrt(vote_ratio * weight_ratio * edge_factor) * CONFIDENCE_SCALE)

    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-14): the HTF alignment bonus
    # (+5/-5) was previously applied HERE, BEFORE the single-group cap and
    # the v2 calibration caps. The caps then erased the bonus — a raw
    # confidence of 95 + HTF-aligned (+5 → 100) got capped to 50, but
    # without the bonus 95 → cap 55. The HTF-aligned signal ended up LOWER
    # than the non-aligned signal — the bonus was INVERTED by the caps.
    # The HTF bonus is now applied AFTER all calibration caps (see below,
    # just before the pair_max_conf cap) so the bonus actually survives.
    # The bonus is still subject to the pair_max_conf cap (hard limit).

    # Adaptive single-group cap.
    # FIX (BUG-CA, 2026-07-20): the previous caps (70/62/55) still allowed
    # single-group predictions to show 70%+ confidence, but the backtest
    # showed single-group confidence-100 predictions were only 35% accurate
    # on the real engine. Tightened to 65/55/48 — still allows meaningful
    # single-group predictions but prevents the 70-100% overconfidence.
    # FIX (calibration v2, 2026-07-20): further tightened after 4h backtest
    # showed 100-109% bin at 40% accuracy and 80-89% at 41.7%. Now caps at
    # 55/48/42 to prevent any single-group prediction from exceeding 55%.
    if total_groups == 1:
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-04): base the single-group
        # cap on the RAW score (pre-multiplier), not the EFFECTIVE score
        # (post-multiplier). The previous code used `majority_score` (effective),
        # which double-counted the multipliers: a boosted signal (high effective
        # but low raw) got a HIGHER cap than a dampened signal (low effective
        # but high raw) — penalizing the already-dampened signal a SECOND time.
        # Now the cap is based on the underlying signal strength (raw score),
        # so dampened and boosted signals with the same raw score get the same
        # cap. This eliminates the double-penalty for dampened modules (e.g.,
        # `trend_follow` on Real with weight 0.1) and the double-boost for
        # heavily-boosted modules (e.g., `otc_pattern` on USDPKR_otc).
        raw_majority = max((r.score for r, e in adjusted
                            if r.direction == signal), default=0)
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-20 / F-02-91): use centralised
        # constants for the single-group cap thresholds and values (was
        # magic 6/4/55/48/42).
        if raw_majority >= SINGLE_GROUP_CAP_HIGH_MIN:
            cap = SINGLE_GROUP_CAP_HIGH
        elif raw_majority >= SINGLE_GROUP_CAP_MID_MIN:
            cap = SINGLE_GROUP_CAP_MID
        else:
            cap = SINGLE_GROUP_CAP_LOW
        confidence = min(confidence, cap)

    # FIX (SIDEWAYS-dampener, 2026-07-20): backtest showed predictions made
    # when HTF is SIDEWAYS AND regime is RANGE were only 45% accurate on OTC.
    # This is the "no trend on either timeframe" condition — the hardest to
    # predict. Apply a 5-point confidence dampener to prevent overconfidence.
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-48): use centralised constant
    # SIDEWAYS_RANGE_DAMPEN (was magic 5).
    if htf_trend == "SIDEWAYS" and regime.get("is_ranging", False):
        confidence = max(0, confidence - SIDEWAYS_RANGE_DAMPEN)

    # BRAIN-LEARNED (2026-07-20): live data showed TREND_UP regime has 44%
    # accuracy. Apply confidence penalty in TREND_UP.
    # FIX (P1-ISSUE-028, 2026-07-22): the blanket penalty punished BOTH
    # CALL (continuation — should be confident) AND PUT (counter-trend —
    # should be dampened). Now only dampen counter-trend signals. A CALL
    # in TREND_UP is the engine correctly following the trend — penalizing
    # it for being right is wrong.
    #
    # FIX (WIN-RATE-BOOST #2, 2026-07-23): brain insights showed TREND_UP
    # regime has only 35% win rate — the WORST regime for OTC. The previous
    # -8 penalty was not enough. Now we apply a heavier -15 penalty AND
    # cap confidence at 45 for ALL signals in TREND_UP (both CALL and PUT).
    # This makes TREND_UP signals effectively NEUTRAL most of the time
    # (since the low_conf_skip at line ~840 converts <20 to NEUTRAL, and
    # 45 is close to that threshold). The OTC broker reverses trends, so
    # TREND_UP is the hardest regime to predict — skipping is +EV.
    #
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-2-02 + AUDIT-LIVE-1-05):
    # EXEMPT REVERSAL signals from the TREND_UP cap when the OTC inversion
    # is active (config.engine_name == "otc"). The OTC inversion (lines
    # 339-347) boosts reversal ×1.3 in OTC trends because the broker
    # reverses them — reversal is the RIGHT bet. But the TREND_UP cap at 45
    # immediately killed the boosted reversal, making STRONG unreachable
    # for OTC-trend-reversal setups (the engine's BEST OTC-trend setup).
    # Now: for OTC engine + reversal signals in TREND_UP, use a higher cap
    # (55) so the boosted reversal can reach MEDIUM/STRONG. Continuation
    # signals in TREND_UP (which the OTC inversion DAMPENS) keep the
    # original 45 cap. This partially mitigates AUDIT-LIVE-1-05 (the
    # conflict between blender OTC inversion and algorithm_strategy
    # trend_following) — full resolution requires removing either the OTC
    # inversion or the trend_following multipliers (out of scope for this
    # batch; the algorithm_strategy.py side was fixed in FIX-BATCH-2).
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-2-17): ALWAYS log the
    # _TREND_UP_PENALTY reason (not only when the cap fires). The previous
    # code only appended the reason when `confidence > 45` — so a raw
    # confidence of 60 (-15 = 45, no cap) had NO reason logged even though
    # the -15 penalty was applied. DB analysis couldn't detect which
    # signals were penalized. Now: log the -15 penalty always; log the
    # cap separately when it fires.
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-22 / F-02-92): compute
    # `_is_otc_reversal` ONCE here — previously duplicated 3× (TREND_UP
    # block at line ~828, TREND_DOWN block at line ~846, and the
    # re-application block ~1010). Now both branches + the re-application
    # use this single computation. Also: use centralised constants
    # TREND_PENALTY / TREND_CAP_STANDARD / TREND_CAP_OTC_REVERSAL
    # (was magic 15 / 45 / 55) — per audit PROB 49.
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
        # Symmetric: TREND_DOWN also dampened (though brain didn't flag it
        # as severely, we apply the same penalty for consistency).
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-2-17): always log.
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-2-02): same OTC
        # reversal exemption as TREND_UP.
        confidence = max(0, confidence - TREND_PENALTY)
        all_reasons.append("_TREND_DOWN_PENALTY: -15 (symmetric to TREND_UP)")
        _trend_cap = TREND_CAP_OTC_REVERSAL if _is_otc_reversal else TREND_CAP_STANDARD
        if confidence > _trend_cap:
            confidence = min(confidence, _trend_cap)
            all_reasons.append(
                f"_TREND_DOWN_CAP: capped at {_trend_cap} "
                f"({'OTC reversal exempt' if _is_otc_reversal else 'standard'})")

    # BRAIN-LEARNED (2026-07-20): confidence calibration from 7623 live signals
    # 100% bin: 44% actual → cap at 50
    # 90-99% bin: 46% actual → cap at 55
    # 80-89% bin: 51% actual → cap at 60
    # 70-79% bin: 50% actual → cap at 60
    # 60-69% bin: 49% actual → cap at 55
    #
    # AUDIT-3-02 FIX (2026-07-25): made caps monotonic.
    # AUDIT-LIVE-2-01 FIX (2026-07-25): the AUDIT-3-02 fix was PARTIAL —
    # 70-79 capped at 65 but 60-69 capped at 70, so 70→65 < 69→69 (DROP).
    # LIVE DB showed per-bin win rate was noisy at boundaries because of this.
    # Now: full monotonic mapping where each bin's cap > previous bin's cap.
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-05 / F-02-21): replaced the inline
    # bin-cap block with a call to the centralised `_apply_calibration_caps`
    # helper. The helper also fixes the 5-point discontinuous jump at the
    # confidence=70 boundary (was: 60-69 → 60, 70-79 → 65; now: 60-79 → 60).
    # FIX (CRASH-FIX-2026-07-26 / EN-007): pass effective_total_groups
    # (post-suppression) instead of total_groups (pre-suppression) so
    # the >75 consensus cap reflects the TRUE consensus, not the
    # original vote count. Previously a 3-group signal with 1 suppressed
    # could exceed 75 if all 3 originally voted — even though only 2
    # contributed to the final prediction. If effective_total_groups
    # isn't yet computed at this call site (early in the function), use
    # the locals().get fallback.
    _eff_groups_for_cap = locals().get("effective_total_groups", total_groups)
    # FIX (2026-07-27 / strong-unreachable): pass abs_net/majority_group_n
    # so the ultra-consensus override (see _apply_calibration_caps) can
    # raise the ceiling to 75 when earned. has_pattern_confluence isn't
    # computed until Step 9 (below) — not available at this call site.
    confidence = _apply_calibration_caps(
        confidence, _eff_groups_for_cap, net_margin,
        abs_net=abs(net), majority_group_n=majority_group_n)

    # ── Step 9: Pattern confluence check for STRONG ──────────────────────
    # FIX (BUG-BQ, 2026-07-20): pattern_agrees only checked reliability ==
    # "PATTERN", missing "OTC" reliability (otc_pattern module). An OTC
    # pattern agreement now counts toward pattern confluence.
    pattern_agrees = any(
        r.reliability in ("PATTERN", "OTC") and r.direction == signal
        for r, e in adjusted
    )
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-18): use RAW score (r.score)
    # instead of EFFECTIVE score (e) for the confluence check. The previous
    # `e >= 3` conflated signal strength with multiplier strength: a raw
    # score 1 signal with heavy multipliers (1.5 × 1.5 × 1.3 = 2.925 →
    # round 3) qualified, but a raw score 2 signal with dampener (0.7 →
    # round 1) didn't. The check should be "did any non-pattern module
    # produce a strong RAW signal", not "did the multipliers boost a weak
    # signal to effective 3+". Now uses r.score >= 2 (raw), so all
    # non-pattern modules with a real raw-score-2+ signal count toward
    # confluence regardless of multipliers. This makes STRONG achievable
    # for genuinely-strong setups where the modules agreed but dampening
    # multipliers reduced the effective score.
    strong_non_pattern_agrees = any(
        r.reliability not in ("PATTERN", "OTC") and r.direction == signal and r.score >= 2
        for r, e in adjusted
    )
    has_pattern_confluence = pattern_agrees and strong_non_pattern_agrees

    agree = majority_group_n
    abs_net = abs(net)

    # FIX (BUG-I, 2026-07-20): accuracy-aware self-correction.
    # When recent_accuracy is provided AND we have enough samples (>=8),
    # scale confidence based on recent performance:
    #   - recent_accuracy < 0.45 → confidence *= 0.85 (dampen — engine is cold)
    #   - recent_accuracy > 0.60 → confidence *= 1.05 (boost — engine is hot)
    # This lets the engine self-correct when it's been wrong recently,
    # preventing overconfidence during cold streaks.
    #
    # FIX (AUDIT-CORE #13, 2026-07-21): MOVED THIS BLOCK BEFORE the
    # calibration-v2 caps (above). Previously the boost (×1.05) was applied
    # AFTER all caps, so a confidence of 60 (capped by calibration v2)
    # could be boosted to 63 — bypassing the cap that was meant to prevent
    # overconfidence. Now the boost/dampen happens FIRST, then the caps
    # clamp the final value. The dampen path was already correct (×0.85
    # only reduces further); only the boost path was buggy.
    #
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-26): DOCUMENT the boost
    # ordering. The boost is applied to the POST-CAP confidence (the v2
    # caps at lines 680-694 already ran), so the boost effect is bounded
    # by the cap thresholds. For raw 95 (cap 55 → boost ×1.05 = 58), the
    # boost is +3 points. For raw 60 (no cap → boost ×1.05 = 63), the
    # boost is +3 points. The current ordering (cap-then-boost) is
    # intentional — it prevents the boost from pushing confidence above
    # the calibration ceiling. The alternative (boost-then-cap) would
    # erase the boost for high-confidence signals (95 → boost 100 → cap 50
    # = -45). The current behavior is BETTER for hot engines. No code
    # change — this comment documents the rationale.
    accuracy_note = ""
    if recent_accuracy is not None:
        try:
            acc_val, acc_n = recent_accuracy
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-2-06): lower the
            # dampen threshold from acc_n >= 8 to acc_n >= 3. The previous
            # threshold meant 8 wrong signals had to accumulate before ANY
            # dampening fired — too slow for cold-start protection (a
            # fast-firing pair at 50 signals/day would burn 8 trades in 4
            # hours before the dampener kicked in). Now dampening fires
            # after 3 samples (still requires 3+ samples to avoid pure
            # noise on the first 1-2 signals). The boost threshold stays
            # high (acc_n >= 30, see below) to avoid boosting on noise.
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-50): use centralised
            # constants ACCURACY_DAMPEN_MIN_SAMPLES / _THRESHOLD / _FACTOR
            # and ACCURACY_BOOST_MIN_SAMPLES / _THRESHOLD / _FACTOR
            # (was magic 3/0.45/0.85 and 30/0.65/1.05).
            if acc_n >= ACCURACY_DAMPEN_MIN_SAMPLES and acc_val is not None:
                if acc_val < ACCURACY_DAMPEN_THRESHOLD:
                    # FIX (AUDIT-DEEP #08, 2026-07-23): use round() not int()
                    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-05): _round_half_up
                    confidence = _round_half_up(confidence * ACCURACY_DAMPEN_FACTOR)
                    accuracy_note = f"_ACCURACY_CORRECT: recent {acc_val:.0%} ({acc_n} samples) → confidence ×0.85"
                # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-2-07): raise
                # the boost threshold from acc_n >= 8 / acc_val > 0.60 to
                # acc_n >= 30 / acc_val > 0.65. A 61% win rate over 50
                # samples is within the 95% CI [36, 64] for a true 50%
                # rate — it's NOISE. Boosting on noise inflates confidence
                # for coin-flip signals. Now: require 30+ samples AND >65%
                # win rate before boosting. This eliminates boost-thrashing
                # for pairs whose win rate oscillates around 55-60%.
                elif (acc_n >= ACCURACY_BOOST_MIN_SAMPLES
                      and acc_val > ACCURACY_BOOST_THRESHOLD):
                    confidence = min(100, _round_half_up(confidence * ACCURACY_BOOST_FACTOR))
                    accuracy_note = f"_ACCURACY_CORRECT: recent {acc_val:.0%} ({acc_n} samples) → confidence ×1.05"
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-51): previously `except
        # (TypeError, ValueError): pass` silently swallowed malformed
        # `recent_accuracy` (e.g., tuple with None, wrong length) with
        # NO log/warning — the accuracy self-correction just didn't fire,
        # but the rest of the prediction proceeded as if no recent_accuracy
        # was provided. Now: append a reason so the failure is visible.
        except (TypeError, ValueError) as _acc_err:
            all_reasons.append(
                f"_ACCURACY_CORRECT_ERROR: malformed recent_accuracy "
                f"{recent_accuracy!r} → {_acc_err}")
    if accuracy_note:
        all_reasons.append(accuracy_note)

    # FIX (AUDIT-CORE #13, 2026-07-21): RE-APPLY all calibration caps AFTER
    # the recent_accuracy boost so the boost cannot bypass them. The boost
    # block above may have raised confidence past the caps — clamp again.
    # AUDIT-3-02 FIX (2026-07-25): monotonically non-decreasing caps.
    # AUDIT-LIVE-2-01 FIX (2026-07-25): match the first cap block exactly.
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-21): replaced the duplicated inline
    # block (was 12 lines) with a single call to the centralised helper.
    # FIX (CRASH-FIX-2026-07-26 / EN-007): same as the earlier call —
    # use effective_total_groups for the >75 consensus cap so suppressed
    # groups don't count toward the consensus requirement.
    _eff_groups_for_cap2 = locals().get("effective_total_groups", total_groups)
    # FIX (2026-07-27 / strong-unreachable): now that Step 9 has run,
    # has_pattern_confluence is known — pass all three so either STRONG
    # path's floor (65 or 75) can be honoured instead of squashed to 60.
    confidence = _apply_calibration_caps(
        confidence, _eff_groups_for_cap2, net_margin,
        abs_net=abs_net, majority_group_n=majority_group_n,
        has_pattern_confluence=has_pattern_confluence)
    # FIX (WIN-RATE-BOOST #2, 2026-07-23): re-apply the TREND_UP/DOWN cap
    # after the accuracy boost + calibration caps. The previous code only
    # re-applied a -8 penalty for counter-trend signals. Now we re-apply
    # the full -15 penalty + 45 cap for ALL signals in TREND_UP/DOWN,
    # matching the first application above. This ensures the penalty
    # survives the boost/cap cascade.
    #
    # AUDIT-3-01 FIX (2026-07-25): the previous code re-applied BOTH the
    # -15 penalty AND the cap 45. Combined with the first application at
    # line 520-533, this DOUBLED the penalty (-30 total) — a raw confidence
    # of 80 in TREND_UP became 32 instead of the intended 45. Now we ONLY
    # re-apply the cap (no -15), which preserves the penalty from the first
    # application while preventing the accuracy boost from exceeding 45.
    #
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-2-02 + AUDIT-LIVE-2-03):
    # (a) Use the OTC reversal exemption here too (cap 55 for OTC reversal
    # signals, 45 otherwise) — matches the first application.
    # (b) During a cold streak (recent_accuracy < 0.45 with acc_n >= 3),
    # DAMPEN THE CAP ITSELF (45 → 38) instead of dampening confidence
    # before the cap. The previous code applied ×0.85 to confidence BEFORE
    # the TREND_UP cap — so a 60-confidence cold-streak signal became
    # 60×0.85=51 → cap 45 → 45, SAME as a non-cold-streak 60-confidence
    # signal (60 → cap 45 → 45). The dampener was NEUTRALIZED by the cap
    # for all confidence >= 53 (53×0.85=45.05 → 45). Now: dampen the cap
    # directly — a cold-streak TREND_UP signal gets cap 38, so 60 → cap 38
    # (was 45). The self-correction mechanism now works in the WORST
    # regime (TREND_UP), where it's needed most.
    if regime.get("regime") in ("TREND_UP", "TREND_DOWN"):
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-22): removed the third duplicate
        # computation of `_is_otc_reversal` — now uses the single value
        # computed at line ~830 above. Also use centralised constants.
        _trend_cap = TREND_CAP_OTC_REVERSAL if _is_otc_reversal else TREND_CAP_STANDARD
        # Dampen the cap during cold streaks.
        if recent_accuracy is not None:
            try:
                _acc_val, _acc_n = recent_accuracy
                if _acc_n >= 3 and _acc_val is not None and _acc_val < 0.45:
                    _trend_cap = _round_half_up(_trend_cap * 0.85)
                    all_reasons.append(
                        f"_TREND_COLD_STREAK: recent {_acc_val:.0%} "
                        f"({_acc_n} samples) → dampen trend cap to {_trend_cap}")
            except (TypeError, ValueError) as _e:
                print(f"[silent-except] engines/base/blender.py:1074 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
                pass
        if confidence > _trend_cap:
            confidence = min(confidence, _trend_cap)

    # ── Step 10: Time/session/regime pattern adjustment (BACKTEST-DRIVEN) ──
    # FIX (BACKTEST-2026-07-21): apply per-pair time-of-day, session, dow,
    # regime, and tag adjustments derived from historical signal_log data.
    # The backtest revealed:
    #   - Win rate varies 49-58% by hour-of-day
    #   - TREND_DOWN regime 59% vs RANGE 50%
    #   - WITH_REGIME / COUNTER_REGIME tags 56% vs untagged 50%
    # The adjustment is a multiplicative factor in [0.5, 1.5] applied to
    # confidence, then re-clamped to the calibration caps.
    # Skip in test contexts where core.time_patterns isn't available.
    #
    # FIX (AUDIT-DEEP #03, 2026-07-23): initialize the strategy variables
    # BEFORE the try blocks so the final return statement doesn't rely on
    # the fragile `'_algo_strategy_name' in dir()` idiom. The previous code
    # set `_algo_strategy_name` only inside a try block that could be
    # skipped (ImportError on core.time_patterns or core.algorithm_strategy).
    # When skipped, `dir()` inside the function returns local names that
    # DON'T include `_algo_strategy_name`, and the return statement falls
    # back to "default". This worked in practice but is brittle — if any
    # intermediate line raised an exception after the variable was set but
    # before the return, the fallback might mask a real strategy. Now the
    # variables are initialized up-front and unconditionally available.
    _algo_strategy_name = "default"
    _algo_strategy_reason = ""
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-22): initialize _force_neutral
    # up-front so the final return path can reference it even if the inner
    # try block (algorithm_strategy) failed with ImportError. When the inner
    # block successfully determines that confidence < _min_conf, it sets
    # _force_neutral = True; the final return path then converts the signal
    # to NEUTRAL (instead of returning early and bypassing calibration caps
    # + pair_max_conf cap + LOW_CONF_SKIP).
    _force_neutral = False
    try:
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-11): `get_tag_adjustment` was
        # imported but NEVER called — the comment at line ~1135 admitted
        # "Tag-based adjustment ... skip for now, will be added in a
        # follow-up". Removed the dead import (and the TODO marker) to
        # silence the unused-import warning and prevent confusion.
        from core.time_patterns import (
            get_time_adjustment, get_regime_adjustment)
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-40): the `if candles else 0`
        # guard is dead — `candles` was already checked at line 185
        # (`if len(candles) < MIN_CANDLES_FOR_PREDICTION:`) and the
        # function returns early if candles is empty. Removed.
        # FIX (CRASH-FIX-2026-07-26 / EN-004): if the candle dict lacks a
        # `time` key (common in backtest/test contexts), `.get("time")`
        # returns None, and `_ctime or 0 = 0`, so `get_time_adjustment`
        # computes `datetime.fromtimestamp(0)` = 1970-01-01 = hour 0.
        # Time-of-day patterns are then looked up for hour 0 (ASIAN
        # session) instead of the current hour, applying wrong session
        # confidence multipliers to every prediction. Now: if the candle
        # has no time, fall back to the current wall-clock time.
        _ctime_raw = candles[-1].get("time")
        if _ctime_raw:
            _ctime = _ctime_raw
            # Quotex timestamps are sometimes in ms; time_patterns expects
            # seconds (datetime.fromtimestamp).
            if isinstance(_ctime, (int, float)) and _ctime > 10_000_000_000:
                _ctime = _ctime / 1000.0
        else:
            import time as _t_mod
            _ctime = _t_mod.time()
        _time_mult, _time_note = get_time_adjustment(asset, _ctime)
        if _time_mult != 1.0:
            # FIX (AUDIT-DEEP #08, 2026-07-23): use round() instead of int()
            # so a confidence of 63 * 1.06 = 66.78 → 67 (not 66). The previous
            # int() truncation lost ~0.5% per multiplier and compounded across
            # the 4-5 successive multipliers applied here (time, regime,
            # strategy confidence, continuation/reversal) — a 5-step cascade
            # could lose up to 2.5 percentage points of confidence to
            # truncation alone. round() gives the mathematically correct
            # integer confidence.
            confidence = _round_half_up(confidence * _time_mult)  # FIX (AUDIT-3-05): _round_half_up
            if _time_note:
                all_reasons.append(_time_note)
        _regime_name = regime.get("regime")
        _reg_mult, _reg_note = get_regime_adjustment(asset, _regime_name)
        if _reg_mult != 1.0:
            confidence = _round_half_up(confidence * _reg_mult)  # FIX (AUDIT-3-05): _round_half_up
            if _reg_note:
                all_reasons.append(_reg_note)

        # ── NEW (TIME-OF-DAY 2026-07-30): per-pair per-hour confidence ────
        # User insight: "কিছু পেয়ার 70% উইন রেট পায়, কিন্তু দিনের ভিন্ন সময়ে
        # 30% এর নিচে পায়। আবার খারাপ পেয়ার ভালো সময়ে 60% পায়।"
        # This uses the new pair_hourly_patterns table (more granular than
        # the existing get_time_adjustment which uses time_patterns table).
        # Adjustment range: 0.6 to 1.2 (stronger than the ±6% nudge above).
        # Only activates when total >= 5 samples for statistical validity.
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
            # Non-critical — don't crash prediction if DB unavailable
            pass

        # USER FIX #9 (2026-08-03): BOOST HOUR ×1.3 confidence multiplier.
        # Live brain data (/api/quotex-algo-detect boost_hours) shows 3
        # specific (asset, hour_utc) windows with 66-69% historical win
        # rate — significantly above the 50% baseline. When a signal is
        # emitted during one of these boost windows, multiply confidence
        # by 1.3 to surface it more prominently to the user.
        # Source: /api/quotex-algo-detect boost_hours (samples >= 20).
        try:
            from datetime import datetime, timezone
            _boost_hour = datetime.fromtimestamp(_ctime, tz=timezone.utc).hour
            _BOOST_HOURS = {
                # (asset, hour_utc): multiplier — only entries with >=20 samples
                ("EURUSD_otc", 21): 1.3,   # 69.2% win (n=26)
                ("AUDUSD_otc", 21): 1.3,   # 68.1% win (n=47)
                ("USDARS_otc",  0): 1.3,   # 65.9% win (n=44)
            }
            _boost_mult = _BOOST_HOURS.get((asset, _boost_hour))
            if _boost_mult and _boost_mult != 1.0:
                confidence = _round_half_up(confidence * _boost_mult)
                all_reasons.append(
                    f"_BOOST_HOUR: {asset} at {_boost_hour:02d}:00 UTC "
                    f"→ ×{_boost_mult:.2f} (historically 66-69% win rate)")
        except Exception:
            pass

        # FIX (DEEP-AUDIT-2026-07-26 / F-02-11): tag-based adjustment was
        # planned but never implemented — see dead-import removal above.
        # The previous TODO comment ("skip for now, will be added in a
        # follow-up") is removed along with the dead `get_tag_adjustment`
        # import.

        # ── Step 10b: Algorithm-aware prediction (DEEP IMPL 2026-07-23) ──
        # DEEP IMPLEMENTATION: uses the new core/algorithm_strategy.py module
        # which determines the optimal trading strategy based on Quotex's
        # detected algorithm state. Supports 6 strategies:
        #   - trend_following (trending algo → boost continuation ×1.3)
        #   - mean_reversion (reversing algo → boost reversal ×1.3)
        #   - neutral (random_walk → reduce confidence ×0.8)
        #   - cautious (payout just changed → reduce confidence ×0.7)
        #   - reset (tick density shifted → reduce confidence ×0.85)
        #   - unknown (not enough data → default conservative)
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-16): the inner re-init of
        # `_algo_strategy_name = "default"` was redundant — the outer init at
        # line 719 already sets it. The inner re-init masked the outer one if
        # the inner try block failed, but the except handler at line ~870
        # doesn't reset it either, so a partially-set strategy name could
        # leak. The outer init is the single source of truth.
        try:
            from core.algorithm_strategy import get_strategy_for_blender
            # FIX (CRASH-FIX-2026-07-26 / EN-005): previously called
            # `get_strategy_for_blender(asset)` WITHOUT period — defaults
            # to period=60. For 15s/30s/120s/300s/1800s/3600s candles,
            # the strategy lookup used the 60s strategy, applying wrong
            # continuation/reversal/confidence multipliers and wrong
            # min_confidence threshold. This silently degraded prediction
            # quality for non-60s pairs.
            # The blender has `period` in scope here (parameter passed
            # to the outer analyze function). Pass it through.
            strat = get_strategy_for_blender(asset, period)
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-15): use the strategy
            # KEY (e.g., "trend_following") for the prediction's "strategy"
            # field, not the human-readable display name (e.g., "Trend
            # Following"). The `/api/current-strategy` endpoint returns the
            # key (via determine_strategy's "strategy" field), so using the
            # key here makes the two sources consistent — UI components and
            # brain/stats queries that compare the two will now match. The
            # `get_strategy_for_blender` wrapper doesn't expose the strategy
            # key directly, so derive it from the display name (lowercase +
            # replace spaces with underscores). This is a heuristic but works
            # for all 6 known strategies ("Trend Following" → "trend_following",
            # "Mean Reversion" → "mean_reversion", "Neutral" → "neutral",
            # "Cautious" → "cautious", "Reset" → "reset", "Unknown" → "unknown").
            # The full fix would add `"strategy": result["strategy"]` to
            # `get_strategy_for_blender`'s return dict in
            # core/algorithm_strategy.py (out of scope for this batch).
            _display_name = strat.get("strategy_name", "default")
            _algo_strategy_name = _display_name.lower().replace(" ", "_")
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-07): use `.get()` with
            # sensible defaults for ALL strategy fields (was direct `[]`
            # indexing — a malformed strategy dict would raise KeyError,
            # caught by the broad `except Exception` at line ~1290 which
            # then appended `_ALGO_STRATEGY_ERROR` and silently degraded
            # to the default strategy. Now: explicit defaults preserve
            # the strategy name lookup while preventing the KeyError.
            _algo_strategy_reason = strat.get("strategy_reason", "")
            _cont_mult = strat.get("continuation_mult", 1.0)
            _rev_mult = strat.get("reversal_mult", 1.0)
            # FIX (DUAL-CLASSIFIER-CONFLICT, 2026-08-03): this continuation/
            # reversal multiplier comes from a DIFFERENT classifier (tick
            # autocorrelation in core/algorithm_strategy.py, tuned on a
            # ~24h sample, no OTC/Real split) than the one already applied
            # in Step 6 above (EMA trend-strength regime, tuned on a
            # 7623-signal live sample, OTC-specific). For OTC, Step 6 found
            # the OPPOSITE of what this module assumes: continuation in a
            # TREND_UP/DOWN regime wins only 47.1%/42.9% (not the ~65%
            # this module's docstring claims for "trending" algorithm
            # state). The two layers were stacking multiplicatively in
            # opposite directions on the SAME continuation-vs-reversal
            # question — e.g. an OTC trend continuation signal got Step
            # 6's dampen AND THEN this module's ×1.3 boost, compounding
            # into a net effect neither layer's own evidence supports.
            # For OTC, defer to the larger, OTC-specific, more recent
            # Step-6 evidence: drop this module's DIRECTIONAL multiplier
            # entirely. Its non-directional confidence_mult/min_confidence
            # (cautious/reset/random-walk dampening below) make no
            # continuation-vs-reversal claim, so they still apply to both
            # engines unchanged.
            if config.engine_name == "otc":
                _cont_mult = 1.0
                _rev_mult = 1.0
            _conf_mult = strat.get("confidence_mult", 1.0)
            _min_conf = strat.get("min_confidence", 0)
            _algo_icon = strat.get("strategy_icon", "")
            # FIX (CRASH-FIX-2026-07-26 / ROOT-CAUSE): `_algo` was previously
            # assigned at line ~1288 (after the if-blocks that USE it at
            # lines 1247 and 1268). When is_continuation/is_reversal fired,
            # the f-string `({_algo})` raised NameError, caught by the broad
            # `except Exception` at the end of this block, silently degrading
            # the algorithm-strategy path to default — meaning the per-pair
            # continuation/reversal multipliers were never actually applied
            # in production. Moved BEFORE its first use.
            _algo = strat.get("algorithm", "unknown")

            # FIX (RANDOM-WALK-DUAL-CONFIRM, 2026-08-03): two INDEPENDENT
            # classifiers each separately flag "no exploitable direction"
            # here — Step 6's regime=RANGE (EMA/trend-strength based) and
            # this module's algo=random_walk (tick-autocorrelation based).
            # On live OTC data these overlap most of the time (algorithm_
            # monitor.py comment: ~97% of OTC candles are random_walk;
            # Step 6 comment: ~81% are RANGE). Previously, even when BOTH
            # independent checks agreed there was no structure, the engine
            # still emitted a directional CALL/PUT at confidence ×0.8 with
            # min_confidence only 25 — a low bar that let most of these
            # through as discounted-but-still-directional bets. A discount
            # cannot turn a coin flip into a real edge — the honest
            # response when two unrelated methods both say "no pattern" is
            # NEUTRAL, not "NEUTRAL-ish". Gated behind an env var so this
            # can be relaxed if signal volume matters more than precision
            # for a given deployment.
            if (_algo == "random_walk" and _is_ranging
                    and os.environ.get("QX_RANDOM_WALK_FORCE_NEUTRAL", "1") != "0"):
                _force_neutral = True
                all_reasons.append(
                    "_ALGO_STRATEGY: random_walk (autocorr) + RANGE (regime) "
                    "agree — no directional edge, forcing NEUTRAL instead of "
                    "a discounted directional bet")

            # Apply confidence multiplier (overall scaling)
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-42): `signal != "NEUTRAL"
            # is always True at this point — `signal` was set to "CALL" or
            # "PUT" at line ~643 or ~668 and is never "NEUTRAL" here.
            # The check was dead. Removed.
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-08): strategy banner was
            # previously appended TWICE — once here (confidence ×N) and
            # once at line ~1280 (strategy name + reason). Consolidated
            # into a single banner at the end of this block. The
            # confidence multiplier is still applied here.
            if _conf_mult != 1.0:
                confidence = _round_half_up(confidence * _conf_mult)  # FIX (AUDIT-3-05): _round_half_up

            # Apply continuation/reversal multipliers
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-42): `signal != "NEUTRAL"`
            # always True — removed (was dead check).
            if _cont_mult != 1.0 or _rev_mult != 1.0:
                # Determine if this signal is continuation or reversal.
                #
                # FIX (AUDIT-DEEP-A1, 2026-07-23): the previous code used
                # `all_results` (raw module outputs, including signals that
                # were later suppressed to effective=0 by regime/pair/HTF
                # multipliers). A suppressed CONTINUATION signal would
                # still set is_continuation=True, which then triggered
                # the algorithm-strategy continuation multiplier — applying
                # ×1.3 (or whatever _cont_mult) to a signal that was
                # actually dampened out by the engine. Conceptually wrong:
                # the algorithm strategy should only see signals that
                # actually contributed to the final prediction.
                # Now we use `adjusted` (the post-suppression list) so
                # only signals that actually voted with non-zero effective
                # score count toward the continuation/reversal decision.
                is_continuation = any(
                    True for r, e in adjusted
                    if r.direction == signal and r.signal_type == "CONTINUATION"
                    and e > 0  # only signals that survived suppression
                )
                is_reversal = any(
                    True for r, e in adjusted
                    if r.direction == signal and r.signal_type == "REVERSAL"
                    and e > 0  # only signals that survived suppression
                )

                if is_continuation and _cont_mult != 1.0:
                    confidence = _round_half_up(confidence * _cont_mult)  # FIX (AUDIT-3-05): _round_half_up
                    all_reasons.append(
                        f"_ALGO_STRATEGY: continuation ×{_cont_mult:.2f} "
                        f"({_algo})")
                # AUDIT-3-07 FIX (2026-07-25): the previous `elif` meant
                # that if BOTH continuation AND reversal signals voted in
                # the same direction (common when multiple modules fire),
                # only the continuation multiplier applied. In a mean_reversion
                # strategy (reversing algo), a mixed-signal prediction got
                # ×0.7 (dampened continuation) instead of ×1.3 (boosted
                # reversal) — the OPPOSITE of the strategy's intent. Now we
                # use a separate `if` so each multiplier applies independently
                # when its signal type is present. If both fire, both
                # multipliers apply (compounding) — which matches the intent:
                # a mixed signal in mean_reversion should get the reversal
                # boost, not the continuation dampening.
                # FIX (DEEP-AUDIT-2026-07-26 / F-02-43): the previous
                # `any(r for r, e in adjusted if ...)` pattern yielded
                # ModuleResult objects (always truthy) which was confusing.
                # Now uses `any(True for ...)` for clarity.
                if is_reversal and _rev_mult != 1.0:
                    confidence = _round_half_up(confidence * _rev_mult)  # FIX (AUDIT-3-05): _round_half_up
                    all_reasons.append(
                        f"_ALGO_STRATEGY: reversal ×{_rev_mult:.2f} "
                        f"({_algo})")

            # Apply strategy-specific min_confidence override
            # (e.g., cautious requires 30+, neutral requires 25+)
            # This replaces the hardcoded LOW_CONF_SKIP threshold.
            #
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-21 + AUDIT-LIVE-1-14):
            # REMOVED the hard random_walk cap (was `if confidence > 50:
            # confidence = min(confidence, 50)`). The cap STACKED with the
            # neutral strategy's ×0.8 confidence multiplier — a 70-confidence
            # signal became 70×0.8=56, then capped at 50. The cap was a HARD
            # CEILING applied AFTER the proportional dampener, compressing
            # the 70-90 range to 50 and losing the ability to distinguish
            # "fairly confident" from "very confident". With 29/30 OTC
            # candles being random_walk (97%), this affected nearly all OTC
            # signals. Now: rely on the ×0.8 confidence_mult alone — it
            # proportionally dampens confidence without flattening the high
            # end. The calibration caps (50/55/60/65) and the >75 consensus
            # cap still apply downstream, preventing runaway confidence.
            # The `_algo` variable is moved above (near other strategy fields)
            # so it's available to the continuation/reversal f-strings above.

            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-22): the previous code
            # returned NEUTRAL EARLY here, bypassing the final calibration
            # caps, pair_max_conf cap, and LOW_CONF_SKIP. A confidence of 25
            # (below min_conf=30) returned NEUTRAL with confidence=25, but
            # the final path would have returned NEUTRAL with confidence=0
            # (after LOW_CONF_SKIP if the pair cap pushed it below 20). The
            # early return was inconsistent with the final return path.
            # Now: set a `_force_neutral` flag and fall through to the final
            # return, which applies all remaining caps and the LOW_CONF_SKIP
            # (which sets confidence=0 for NEUTRAL signals). The signal is
            # changed to "NEUTRAL" only at the FINAL return — confidence
            # calibration and pair cap still apply to the original signal's
            # confidence value, but the returned signal is NEUTRAL.
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-42): `signal != "NEUTRAL"`
            # is always True here — `signal` is CALL or PUT at this point.
            # Removed the dead check.
            if confidence < _min_conf:
                all_reasons.append(
                    f"_ALGO_STRATEGY: confidence {confidence} < {_min_conf} "
                    f"({_algo_strategy_name}) → will force NEUTRAL")
                _force_neutral = True
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-34): the `else:
            # _force_neutral = False` branch was redundant — `_force_neutral`
            # was already initialized to `False` at line ~1110 above.
            # Removed.

            # Store strategy in result for logging.
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-08): CONSOLIDATED strategy
            # banner — was previously split into two appends (confidence
            # ×N at line ~1185 and strategy name + reason here). Now: a
            # single line includes both the confidence multiplier info AND
            # the strategy reason, eliminating the duplicate `_ALGO_STRATEGY:`
            # lines in the reasons list (UI/log showed two strategy lines
            # per prediction, inflating the reasons list and confusing DB
            # analysis).
            all_reasons.append(
                f"_ALGO_STRATEGY: {_algo_icon} {_algo_strategy_name} "
                f"(conf ×{_conf_mult:.2f}) — {_algo_strategy_reason}")

        except ImportError:
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-52): previously `pass` —
            # silent fallback when `core.algorithm_strategy` is not
            # importable (test context). Now: append a reason so the
            # fallback is visible in DB analysis.
            all_reasons.append(
                "_ALGO_STRATEGY: module not available, using default")
        except Exception as _algo_err:
            # FIX (DEEP-AUDIT-2026-07-26 / F-02-54): removed the nested
            # inner `try: all_reasons.append(...); except Exception: pass`
            # — `list.append` cannot raise under normal conditions, so the
            # inner try/except was overly defensive paranoia.
            all_reasons.append(f"_ALGO_STRATEGY_ERROR: {_algo_err}")

    except ImportError:
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-53): previously `pass` —
        # silent fallback when `core.time_patterns` is not importable.
        # Now: append a reason so the fallback is visible.
        all_reasons.append(
            "_TIME_PATTERN: module not available, using default")
    except Exception as _e:
        # Never let pattern lookup break the prediction pipeline.
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-55): removed the nested inner
        # `try: all_reasons.append(...); except Exception: pass` —
        # `list.append` cannot raise under normal conditions.
        all_reasons.append(f"_TIME_PATTERN_ERROR: {_e}")

    # FIX (BACKTEST-2026-07-21): re-apply calibration caps one more time
    # after the pattern adjustments, so the boosted confidence can't
    # exceed the caps. The pattern adjustments can raise confidence
    # significantly (up to ×1.5), which would bypass the calibration.
    #
    # AUDIT-3-03 FIX (2026-07-25): this is the THIRD application of the
    # calibration caps (after lines 541-556 and 619-631). Each application
    # can erase intentional boosts from intermediate adjustments. Now we
    # apply ONLY the >75 consensus cap here — the per-bin caps (50/55/60/65)
    # are already enforced by the second block, and any boost from time/
    # regime/strategy is bounded by the >75 rule. This preserves the time/
    # pattern DB query effect while still preventing runaway confidence.
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-21-note): the third application is
    # INTENTIONALLY narrow (only the >75 consensus cap, NOT the bin caps).
    # Per the dev's comment above, re-applying the bin caps here would
    # erase the time/strategy boost effect — only the hard >75 ceiling
    # is re-applied. This is not a duplicate of the first two bin-cap
    # blocks; it's a separate, narrower clamp for the post-pattern stage.
    if confidence > 75:
        if not (total_groups >= 3 and net_margin >= 0.6):
            confidence = min(confidence, 75)

    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-14): apply the HTF alignment
    # bonus (+5/-5) HERE, AFTER all calibration caps but BEFORE the
    # pair_max_conf cap. Previously the bonus was applied at line ~482
    # (before the caps), so the caps erased it. Now the bonus survives the
    # calibration cascade and only the pair_max_conf cap (hard limit) can
    # clamp it. The bonus affects strength tier determination (which uses
    # confidence) so placing it before the strength tiers is correct.
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-46): use centralised constants
    # HTF_ALIGNED_BONUS / HTF_COUNTER_PENALTY (was magic 5/5).
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-47): simplified the complex
    # conditional — was a verbose `elif htf_trend in (...) and ((htf ==
    # UPTREND and signal == PUT) or (htf == DOWNTREND and signal ==
    # CALL))`. Now: compute `aligned` once and use a simple if/elif.
    _htf_aligned = (
        (htf_trend == "UPTREND" and signal == "CALL")
        or (htf_trend == "DOWNTREND" and signal == "PUT"))
    if _htf_aligned:
        confidence = min(100, confidence + HTF_ALIGNED_BONUS)
    elif htf_trend in ("UPTREND", "DOWNTREND"):
        # Counter-trend signal in either direction.
        confidence = max(0, confidence - HTF_COUNTER_PENALTY)
    # FIX (WINRATE-BOOST #1, 2026-07-28): re-apply TREND cap after HTF bonus.
    # Previously the HTF +5 bonus was applied AFTER the TREND cap (45/55),
    # so a signal correctly dampened to 45 (TREND_UP has 35% win rate per
    # brain data) immediately re-inflated to 50, defeating the brain-learned
    # insight. Now: if we're in a trend regime, re-clamp confidence to the
    # trend cap after the HTF bonus. This preserves the dampening intent
    # while still allowing HTF-aligned signals in non-trending regimes to
    # get the full bonus. The pair_max_conf cap (next block) still applies
    # as the final hard limit.
    if _is_trending and confidence > 0:
        _post_htf_cap = TREND_CAP_OTC_REVERSAL if _is_otc_reversal else TREND_CAP_STANDARD
        if confidence > _post_htf_cap:
            confidence = min(confidence, _post_htf_cap)

    # FIX (WIN-RATE-BOOST #1, 2026-07-23): per-pair max_confidence cap.
    # Some pairs (e.g., USDMXN_otc with 0% historical win rate) should
    # have their confidence capped at a very low level so they effectively
    # emit NEUTRAL most of the time (since the low_conf_skip threshold at
    # line ~830 converts signals below confidence 20 to NEUTRAL). This
    # prevents bad pairs from generating wrong trades while still allowing
    # the pair to fire if there's genuinely strong confluence.
    _pair_max_conf = weight_adapter.get_max_confidence(asset)
    if _pair_max_conf is not None and confidence > _pair_max_conf:
        confidence = min(confidence, _pair_max_conf)
        all_reasons.append(
            f"_PAIR_CAP: {asset} max_confidence={_pair_max_conf} "
            f"(historical win rate too low)")

    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-2-09): add a WEAK strength
    # tier for abs_net=1 signals (previously MEDIUM). A single dampened
    # indicator signal (effective score 1) shouldn't be MEDIUM — the user
    # sees too many MEDIUM signals backed by a single weak vote. abs_net=1
    # now gets WEAK; abs_net >= 2 keeps MEDIUM.
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-2-10): add an ULTRA-CONSENSUS
    # path to STRONG — STRONG is now achievable without pattern confluence
    # if the consensus is VERY strong (4+ groups, abs_net >= 8, confidence
    # >= 75). Previously STRONG required has_pattern_confluence, which is
    # only set when the (rare) candlestick-pattern module fires. 90-95% of
    # candles had no pattern module vote → STRONG was unreachable.
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-30): the previous `else:
    # return NEUTRAL` branch was UNREACHABLE — `net != 0` (checked at line
    # 449) guarantees `abs_net >= 1`, so the `elif abs_net >= 1` always
    # catches. The dead branch is removed; a defensive `strength = "MEDIUM"`
    # fallback is kept (mathematically unreachable but safe against future
    # refactors that allow `net == 0` to reach this point).
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-96): use centralised constants
    # ULTRA_CONSENSUS_CONF_MIN / _ABS_NET_MIN / _GROUPS_MIN (was magic
    # 75/8/4).
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-15): add a confidence floor for the
    # `elif abs_net >= 2: MEDIUM` branch — was firing for ANY confidence
    # (even confidence=20 with abs_net=2 was MEDIUM, which is overly
    # generous). Now requires confidence >= MEDIUM_CONFIDENCE_FLOOR (30)
    # for the standalone MEDIUM tier; signals with abs_net >= 2 but low
    # confidence fall through to WEAK (next elif).
    if (confidence >= 65 and abs_net >= 5 and majority_group_n >= 2
            and has_pattern_confluence):
        strength = "STRONG"
    elif (confidence >= ULTRA_CONSENSUS_CONF_MIN
          and abs_net >= ULTRA_CONSENSUS_ABS_NET_MIN
          and majority_group_n >= ULTRA_CONSENSUS_GROUPS_MIN):
        # NEW: ultra-strong consensus without pattern confluence.
        strength = "STRONG"
        all_reasons.append(
            "_ULTRA_CONSENSUS: 4+ groups, abs_net>=8 -> STRONG (no pattern needed)")
    elif (confidence >= 65 and abs_net >= 5 and majority_group_n >= 2
          and not has_pattern_confluence):
        strength = "MEDIUM"
        all_reasons.append("_DOWNGRADE: STRONG->MEDIUM (no strong pattern confluence)")
    elif confidence >= 50 and abs_net >= 2:
        strength = "MEDIUM"
    elif confidence >= MEDIUM_CONFIDENCE_FLOOR and abs_net >= 2:
        strength = "MEDIUM"
    elif abs_net >= 1:
        strength = "WEAK"
    else:
        # Defensive fallback (mathematically unreachable — see comment above).
        strength = "MEDIUM"

    # ── Step 11: Low-confidence skip (BACKTEST-DRIVEN) ─────────────────────
    # FIX (SIGNAL-FIX-2026-07-22): lowered from 30 to 20. The previous
    # threshold of 30 was too aggressive — 4 of 6 all-time OTC pairs had
    # 0 graded signals because all their predictions fell in the 20-29
    # confidence range and were forced to NEUTRAL. At confidence 20, the
    # engine is still uncertain but the EOC analysis found SOMETHING —
    # better to emit a low-conviction signal than nothing at all.
    # The chop-guard (3+ losses in same zone) still catches genuinely
    # bad predictions and converts them to NEUTRAL.
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-20): set confidence=0 in the
    # NEUTRAL return — the previous code returned the post-cap confidence
    # (e.g., 15), producing "NEUTRAL 15%" which confused downstream
    # consumers expecting NEUTRAL signals to have confidence=0.
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-97): use centralised constant
    # LOW_CONF_SKIP_THRESHOLD (was magic 20).
    # NOTE (USER FIX #3, 2026-08-03, REVISED): the RANGE suppression block
    # has been moved HERE (after confidence + abs_net + majority_group_n
    # + has_pattern_confluence are all computed). The original location
    # earlier in the flow was unreachable for some signals AND when moved
    # before net==0 it caused UnboundLocalError on `confidence`. Now it's
    # in the right place: only fires for EXTREME ranging markets (low
    # trend_strength + low volatility) and exempts ultra-consensus signals.
    _is_extreme_ranging = (
        _is_ranging
        and _trend_strength < 0.10
        and _volatility_pct < 1.5
    )
    if _is_extreme_ranging and not (
        confidence >= 65 and abs_net >= 5 and majority_group_n >= 2
        and has_pattern_confluence
    ):
        all_reasons.append(
            f"_RANGE_SUPPRESS: EXTREME RANGE (str={_trend_strength:.2f}, "
            f"vol={_volatility_pct:.1f}x) -> NEUTRAL (historical win rate "
            f"only 44% in this regime)")
        return {
            "signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
            "score": net, "reasons": all_reasons,
            "regime": regime, "agree": agree, "total": total_groups,
            "signals_fired": total_groups,
            "modules": _module_breakdown(adjusted, all_results, module_names),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
            "strategy": _algo_strategy_name,
            "strategy_reason": _algo_strategy_reason,
            "range_suppressed": True,
        }
    _low_conf_threshold = LOW_CONF_SKIP_OTC if asset.endswith("_otc") else LOW_CONF_SKIP_REAL
    if confidence < _low_conf_threshold:
        all_reasons.append(f"_LOW_CONF_SKIP: confidence {confidence} < {_low_conf_threshold} -> NEUTRAL")
        return {
            "signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
            "score": net, "reasons": all_reasons,
            "regime": regime, "agree": agree, "total": total_groups,
            "signals_fired": total_groups,
            "modules": _module_breakdown(adjusted, all_results, module_names),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
            "strategy": _algo_strategy_name,
            "strategy_reason": _algo_strategy_reason,
        }

    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-22): if the strategy
    # min_confidence check set _force_neutral, convert the signal to
    # NEUTRAL here (after all caps + LOW_CONF_SKIP have been applied).
    # This ensures the strategy min_confidence check is consistent with
    # the final return path (instead of returning early with a pre-cap
    # confidence value). Set confidence=0 to match the LOW_CONF_SKIP
    # behavior for NEUTRAL signals.
    if _force_neutral:
        all_reasons.append(
            f"_ALGO_STRATEGY: confidence {confidence} < strategy min → NEUTRAL")
        return {
            "signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
            "score": net, "reasons": all_reasons,
            "regime": regime, "agree": agree, "total": total_groups,
            "signals_fired": total_groups,
            "modules": _module_breakdown(adjusted, all_results, module_names),
            "asset": asset, "profile": pair_profile, "htf_trend": htf_trend,
            "strategy": _algo_strategy_name,
            "strategy_reason": _algo_strategy_reason,
        }

    return {
        "signal": signal,
        "confidence": confidence,
        "strength": strength,
        # FIX (DEEP-AUDIT-2026-07-26 / F-02-04): when the group-count
        # tiebreaker fired (net == 0 but group counts differ), return the
        # majority direction's score instead of `net` (=0). Previously the
        # final return used `score=net=0` for tiebreaker CALL/PUT signals,
        # so UI showed "CALL 55%" but DB stored score=0 — breaking
        # consumers that expect score>0 for non-NEUTRAL signals.
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
        # FIX (AUDIT-DEEP #03, 2026-07-23): variables are now unconditionally
        # initialized at the top of Step 10, so we can reference them directly
        # without the fragile `'_algo_strategy_name' in dir()` idiom.
        "strategy": _algo_strategy_name,
        "strategy_reason": _algo_strategy_reason,
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
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-13): when scores tie,
        # break by STRONGEST single signal first (max r.score), then by
        # count. The previous code broke by COUNT only, so 3 weak CALL
        # signals (score 1 each, sum 3) beat 2 PUT signals (score 1 and 2,
        # sum 3) — the strong PUT signal (score 2) was dropped. Now: pick
        # the direction whose strongest single signal is higher. Only fall
        # back to count if BOTH the strongest single scores are equal.
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
            # Full tie on max single score — fall back to count.
            direction = "CALL" if call_n > put_n else "PUT"
            majority_signals = call_signals if direction == "CALL" else put_signals
            max_score = max((r.score for r in majority_signals), default=0)
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
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-27): break the sig_type
        # tie by STRONGEST single signal, not by count. The previous code
        # used `cont_n > rev_n`, so 2 weak CONTINUATION signals (score 1
        # each) beat 1 strong REVERSAL signal (score 2) in a TREND regime
        # — flipping the regime multiplier (continuation ×1.3 vs reversal
        # ×0.8) in favor of the weaker-but-more-numerous signal type. Now:
        # pick the type whose strongest single signal is higher; only fall
        # back to count if BOTH max scores are equal.
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

    # FIX (DEEP-AUDIT-2026-07-26 / F-02-29): previously iterated
    # `body_signals` (ALL signals, both CALL and PUT). If 3 CALL signals
    # and 1 PUT signal collapsed into a CALL result, the reasons included
    # the PUT signal's reason — which CONTRADICTS the CALL direction.
    # Now: iterate `majority_signals` only (the winning side).
    # FIX (DEEP-AUDIT-2026-07-26 / F-02-112): previously only included
    # the FIRST reason from each signal (`r.reasons[0]`); the other
    # reasons were silently dropped. Now: include ALL reasons via
    # `" | ".join(r.reasons)`.
    reasons_str = " | ".join(
        " | ".join(r.reasons) if r.reasons else ""
        for r in majority_signals)

    # FIX (DEEP-AUDIT-2026-07-26 / F-02-30): the `confidence=min(70, score * 15)`
    # field is set here but NEVER read by the blender's effective-score
    # calculation (which uses `r.score * multipliers`, not `r.confidence`).
    # The audit (PROB 30) flagged this as a dead field. Kept for backward
    # compatibility with external consumers that may read ModuleResult.confidence
    # — removing it could break the ModuleResult dataclass contract.
    # TODO (DEEP-AUDIT-2026-07-26 / F-02-30): either use `confidence` as a
    # blend weight in the effective-score calculation, or remove the field
    # from ModuleResult construction (verify no consumers read it first).
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
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-29): add "suppressed"
            # and "raw_score" fields so the UI can distinguish a module
            # whose signals were ALL suppressed (fired=True but score=0)
            # from a module that genuinely didn't fire. The previous code
            # showed "fired: True, direction: NEUTRAL, score: 0" for fully-
            # suppressed modules — confusing. Now: suppressed=True when
            # the module fired (module_raw non-empty) but ALL its signals
            # were dampened to zero (module_adjusted empty). raw_score
            # shows the pre-suppression score for the displayed direction.
            "suppressed": len(module_raw) > 0 and len(module_adjusted) == 0,
            "raw_score": (sum(r.score for r in module_raw if r.direction == direction)
                          if direction != "NEUTRAL" else 0),
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
