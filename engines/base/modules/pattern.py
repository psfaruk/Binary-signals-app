"""Module: Multi-Candle Pattern Engine — classic Japanese candlestick patterns."""
from core.analysis import detect_candle_patterns
from engines.base.types import ModuleResult, MarketContext

# Active patterns — all are ALWAYS_REVERSAL (structural reversal patterns).
ALWAYS_REVERSAL = {
    "TWEEZER_BOTTOM", "TWEEZER_TOP",
    "PIERCING_LINE", "DARK_CLOUD",
    "BEAR_HARAMI", "BULL_HARAMI",
    "BEAR_PIN_BAR", "BULL_PIN_BAR",
    "BULL_TWO_BAR_REV", "BEAR_TWO_BAR_REV",
    "DOJI_BEARISH", "DOJI_BULLISH",
    # FIX (USER-AUG-2026 / PATTERN-EXPANSION): newly added classical patterns
    "MORNING_STAR", "EVENING_STAR",
    "BULL_ENGULFING", "BEAR_ENGULFING",
    "DRAGONFLY_DOJI", "GRAVESTONE_DOJI",
}

# Continuation patterns (strong momentum, trend-extension signals).
ALWAYS_CONTINUATION = {
    "BULL_MARUBOZU", "BEAR_MARUBOZU",
    "THREE_WHITE_SOLDIERS", "THREE_BLACK_CROWS",
}


def analyze(candles, ctx: MarketContext) -> list:
    """Detect multi-candle patterns; returns one ModuleResult per detected pattern."""
    patterns = detect_candle_patterns(candles)
    if not patterns:
        return []

    results = []
    for pat in patterns:
        name = pat["name"]
        direction = pat["direction"]

        # Determine signal type: reversal vs continuation
        if name in ALWAYS_REVERSAL:
            sig_type = "REVERSAL"
        elif name in ALWAYS_CONTINUATION:
            sig_type = "CONTINUATION"
        else:
            continue  # unknown pattern, skip

        reason_str = pat.get("reason") or ""
        results.append(ModuleResult(
            module_name="pattern",
            direction=direction,
            score=pat["score"],
            confidence=pat["score"] * 18,  # 3->54, 2->36
            signal_type=sig_type,
            reliability="PATTERN",
            group="PATTERN_REVERSAL" if sig_type == "REVERSAL" else "PATTERN_CONTINUATION",
            reasons=[reason_str],
        ))
    return results
