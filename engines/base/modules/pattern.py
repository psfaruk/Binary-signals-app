"""
Module 3: Multi-Candle Pattern Engine

Detects 10+ classic Japanese candlestick patterns across the last 2-4
candles. These are HIGHER-CONVICTION than single-candle signals because
they capture inter-candle dynamics.

Patterns (reliability: PATTERN ×1.5):
  - Bullish/Bearish Engulfing (~65%)
  - Morning/Evening Star (~70%)
  - Tweezer Top/Bottom (~60%)
  - Three White Soldiers / Three Black Crows (~60%)
  - Three Soldiers/Crows Exhaustion (~65% reversal)
  - Piercing Line / Dark Cloud Cover (~63%)
  - Bullish/Bearish Harami (~58%)
  - Inside Bar Breakout (~58%)
  - Hammer / Shooting Star (~62%)

Each pattern gets its own group (PATTERN_*) so they're counted as
independent votes in the blender.

FIX (2026-07-18, structural bias): previously 14/18 patterns were
hardcoded as REVERSAL and only 4 as CONTINUATION. This created a
structural bias against trend-following in the OTC engine.

Now the signal_type is determined REGIME-CONDITIONALLY:
  - In a strong TREND regime (trend_strength > 0.5), engulfing patterns
    in the TREND DIRECTION are classified as CONTINUATION (momentum
    push), not REVERSAL. This matches reality: a bullish engulfing
    during a strong uptrend is a momentum continuation signal, not a
    reversal.
  - In RANGE or weak-trend regimes, classical reversal interpretation
    is kept (engulfing after a streak = reversal).
  - Pure reversal patterns (Morning Star, Evening Star, Tweezer,
    Harami, Hammer, Shooting Star) stay REVERSAL regardless of regime
    — these are structurally reversal patterns by definition.
  - Pure continuation patterns (3 Soldiers, 3 Crows, Inside Bar
    Breakout) stay CONTINUATION regardless of regime.
"""
from core.analysis import detect_candle_patterns
from engines.base.types import ModuleResult, MarketContext

# Patterns that are ALWAYS reversal (structural reversal patterns).
# These represent exhaustion/rejection at extremes and don't have a
# meaningful continuation interpretation.
ALWAYS_REVERSAL = {
    "MORNING_STAR", "EVENING_STAR",        # 3-candle reversal at extreme
    "TWEEZER_TOP", "TWEEZER_BOTTOM",        # rejection at same price level
    "3_SOLDIERS_EXHAUST", "3_CROWS_EXHAUST",# exhausted trend → reversal
    "PIERCING_LINE", "DARK_CLOUD",          # 2-candle reversal
    "BULL_HARAMI", "BEAR_HARAMI",           # inside-body reversal
    "HAMMER", "SHOOTING_STAR",              # single-candle rejection
    # NEW (THEORY-RESEARCH-2026-08-03): added new reversal patterns
    "BULL_PIN_BAR", "BEAR_PIN_BAR",         # pin bar rejection (63% WR)
    "BULL_TWO_BAR_REV", "BEAR_TWO_BAR_REV", # two-bar reversal (62% WR)
    "DOJI_BEARISH", "DOJI_BULLISH",         # doji after trend (58% WR)
}

# Patterns that are ALWAYS continuation (structural trend patterns).
ALWAYS_CONTINUATION = {
    "3_SOLDIERS", "3_CROWS",                # strong trend continuation
    "INSIDE_BREAK_UP", "INSIDE_BREAK_DN",   # breakout from consolidation (disabled in analysis.py)
}

# Patterns that are REGIME-CONDITIONAL — engulfing patterns can be
# either reversal or continuation depending on trend context.
#   - In strong trend (trend_strength > 0.5), engulfing IN trend dir
#     = CONTINUATION (momentum push)
#   - In range/weak trend, engulfing = REVERSAL (classical interp)
REGIME_CONDITIONAL = {
    "BULL_ENGULF", "BEAR_ENGULF",
}


def analyze(candles, ctx: MarketContext) -> list:
    """Detect multi-candle patterns.

    Returns list of ModuleResult objects, one per detected pattern.
    Each pattern has its own group for independent vote counting.
    """
    patterns = detect_candle_patterns(candles)
    if not patterns:
        return []

    results = []
    regime = ctx.regime
    is_trending = regime.get("is_trending", False)
    trend_strength = regime.get("trend_strength", 0.0)
    trend_regime = regime.get("regime", "RANGE")  # TREND_UP / TREND_DOWN / RANGE / VOLATILE
    # Strong trend threshold — only classify engulfing as continuation
    # when trend is clearly established.
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-33): raise the threshold from
    # > 0.5 to > 0.7 to align with candle_reaction's strong-dampen threshold.
    # At trend_strength = 0.6, an engulfing pattern was classified as
    # CONTINUATION here (getting the ×1.3 trend-continuation multiplier in
    # the blender) while streak signals in candle_reaction got only moderate
    # dampening — inconsistent treatment of moderate trends. Now both modules
    # require trend_strength > 0.7 for the strong/continuation case.
    # FIX (DEEP-AUDIT-2026-07-26 / F-10-27, A-05 P96/S2): documented the
    # standardization — `>= 0.5` is the moderate-trend boundary,
    # `> 0.7` (strict) is the strong-trend boundary, used consistently
    # across candle_reaction, otc_pattern, trend_follow, and this module.
    # Kept as strict `> 0.7` for backward compat with the AUDIT-4-33 fix.
    strong_trend = is_trending and trend_strength > 0.7

    for pat in patterns:
        name = pat["name"]
        direction = pat["direction"]

        if name in ALWAYS_REVERSAL:
            sig_type = "REVERSAL"
            type_note = ""
        elif name in ALWAYS_CONTINUATION:
            sig_type = "CONTINUATION"
            type_note = ""
        elif name in REGIME_CONDITIONAL:
            # Engulfing: in strong trend, engulfing in trend direction
            # is continuation (momentum push). Otherwise reversal.
            if strong_trend:
                if trend_regime == "TREND_UP" and direction == "CALL":
                    sig_type = "CONTINUATION"
                    type_note = f" (trend-continuation: strong uptrend, trend_str={trend_strength:.2f})"
                elif trend_regime == "TREND_DOWN" and direction == "PUT":
                    sig_type = "CONTINUATION"
                    type_note = f" (trend-continuation: strong downtrend, trend_str={trend_strength:.2f})"
                else:
                    # Engulfing against the trend → still reversal
                    sig_type = "REVERSAL"
                    type_note = f" (counter-trend reversal: {trend_regime}, trend_str={trend_strength:.2f})"
            else:
                sig_type = "REVERSAL"
                type_note = f" (range reversal: regime={trend_regime})"
        else:
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-31): SKIP unknown
            # patterns instead of defaulting to REVERSAL. The previous
            # "safe default = REVERSAL" biased the engine toward reversal for
            # any unrecognized pattern, and a truly safe default is to NOT
            # emit a signal at all (so the unknown pattern doesn't contribute
            # a vote based on a guessed classification). All current patterns
            # are classified, so this only affects FUTURE patterns added to
            # detect_candle_patterns without updating ALWAYS_* sets.
            # FIX (DEEP-AUDIT-2026-07-26 / F-10-28, A-05 P110): kept the
            # `continue` (rather than adding a debug log) — module-level
            # logging would require importing the logger and risk circular
            # imports. Operators can detect unknown patterns via the
            # module-breakdown UI when signals are missing.
            continue

        # FIX (DEEP-AUDIT-2026-07-26 / F-10-29, A-05 P98): defensive None
        # handling for `pat["reason"]` — detect_candle_patterns always
        # returns a string, but if a future regression sets it to None,
        # the string concatenation `pat["reason"] + type_note` would crash
        # with TypeError. Coalesce to empty string.
        reason_str = (pat.get("reason") or "") + type_note
        results.append(ModuleResult(
            module_name="pattern",
            direction=direction,
            score=pat["score"],
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-32): documented the
            # multiplier reasoning. Confidence = score * 18 chosen so score 3
            # → 54 (matching candle_reaction's big-body reversal conf) and
            # score 4 → 72. Score 2 → 36 (above LOW_CONF_SKIP=20). Score 1
            # → 18 (below LOW_CONF_SKIP, would be skipped) — but
            # detect_candle_patterns currently returns scores 2-4 only, so
            # the score-1 case doesn't arise in practice.
            # FIX (DEEP-AUDIT-2026-07-26 / F-10-30, A-05 P97): documented
            # the magic multiplier `18` as PATTERN_CONFIDENCE_PER_SCORE —
            # the value maps candle_pattern scores (2-4) to confidence
            # values (36-72) that align with candle_reaction's reversal
            # signal range. Kept inline rather than as a module-level
            # constant for backward compat (no functional change).
            confidence=pat["score"] * 18,  # PATTERN_CONFIDENCE_PER_SCORE=18; 3→54, 4→72
            signal_type=sig_type,
            reliability="PATTERN",
            # FIX (WINRATE-BOOST #5, 2026-07-28): collapse all PATTERN_* groups
            # into two canonical groups (REVERSAL/CONTINUATION) so correlated
            # patterns don't inflate the vote count. Previously each pattern
            # got its own group (PATTERN_BULL_ENGULF, PATTERN_HAMMER, etc.),
            # so 3 correlated patterns firing same-direction counted as 3
            # independent groups — inflating both call_score AND
            # majority_groups count, satisfying the >75 consensus cap with
            # correlated data. Now: REVERSAL patterns share one group,
            # CONTINUATION patterns share another. This matches how
            # candle_reaction's BODY group is collapsed.
            group="PATTERN_REVERSAL" if sig_type == "REVERSAL" else "PATTERN_CONTINUATION",
            reasons=[reason_str],
        ))
    return results
