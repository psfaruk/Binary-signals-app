"""Module: Multi-Candle Pattern Engine — classic Japanese candlestick patterns."""
from core.analysis import detect_candle_patterns
from engines.base.types import ModuleResult, MarketContext

# Active patterns — all are ALWAYS_REVERSAL (structural reversal patterns).
ALWAYS_REVERSAL = {
    "TWEEZER_BOTTOM",
    "PIERCING_LINE",
    "DARK_CLOUD",
    "BEAR_HARAMI",
    "BEAR_PIN_BAR",
    "BULL_TWO_BAR_REV",
    "BEAR_TWO_BAR_REV",
    "DOJI_BEARISH",
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

        if name not in ALWAYS_REVERSAL:
            continue

        sig_type = "REVERSAL"
        reason_str = pat.get("reason") or ""
        results.append(ModuleResult(
            module_name="pattern",
            direction=direction,
            score=pat["score"],
            confidence=pat["score"] * 18,  # 3->54, 2->36
            signal_type=sig_type,
            reliability="PATTERN",
            group="PATTERN_REVERSAL",
            reasons=[reason_str],
        ))
    return results
