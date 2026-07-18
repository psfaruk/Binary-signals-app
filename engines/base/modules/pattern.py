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
}

# Patterns that are ALWAYS continuation (structural trend patterns).
ALWAYS_CONTINUATION = {
    "3_SOLDIERS", "3_CROWS",                # strong trend continuation
    "INSIDE_BREAK_UP", "INSIDE_BREAK_DN",   # breakout from consolidation
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
    strong_trend = is_trending and trend_strength > 0.5

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
            # Unknown pattern — default to reversal (safe default for
            # any future patterns added to detect_candle_patterns).
            sig_type = "REVERSAL"
            type_note = ""

        results.append(ModuleResult(
            module_name="pattern",
            direction=direction,
            score=pat["score"],
            confidence=pat["score"] * 18,  # 3→54, 4→72
            signal_type=sig_type,
            reliability="PATTERN",
            group=f"PATTERN_{name}",
            reasons=[pat["reason"] + type_note],
        ))
    return results
