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
"""
from advanced_analysis import detect_candle_patterns
from engines.otc.types import ModuleResult, MarketContext

REVERSAL_PATTERNS = {
    "BULL_ENGULF", "BEAR_ENGULF", "MORNING_STAR", "EVENING_STAR",
    "TWEEZER_TOP", "TWEEZER_BOTTOM", "3_SOLDIERS_EXHAUST", "3_CROWS_EXHAUST",
    "PIERCING_LINE", "DARK_CLOUD", "BULL_HARAMI", "BEAR_HARAMI",
    "HAMMER", "SHOOTING_STAR",
}


def analyze(candles, ctx: MarketContext) -> list:
    """Detect multi-candle patterns.

    Returns list of ModuleResult objects, one per detected pattern.
    Each pattern has its own group for independent vote counting.
    """
    patterns = detect_candle_patterns(candles)
    results = []
    for pat in patterns:
        sig_type = "REVERSAL" if pat["name"] in REVERSAL_PATTERNS else "CONTINUATION"
        results.append(ModuleResult(
            module_name="pattern",
            direction=pat["direction"],
            score=pat["score"],
            confidence=pat["score"] * 18,  # 3→54, 4→72
            signal_type=sig_type,
            reliability="PATTERN",
            group=f"PATTERN_{pat['name']}",
            reasons=[pat["reason"]],
        ))
    return results
