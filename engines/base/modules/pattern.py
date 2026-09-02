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
    """Detect multi-candle patterns; returns AT MOST ONE vote per direction.

    FIX (CONFLUENCE-V1 2026-09-02): the old implementation emitted one
    ModuleResult per detected pattern, and detect_candle_patterns() can flag
    several overlapping patterns on the SAME candle (tweezer-bottom +
    two-bar-rev + engulfing are often all true together). The old blender
    summed their scores inside one group — triple-counting a single
    observation. Now: keep only the single highest-score pattern per
    direction; secondary overlapping patterns are recorded as notes.
    """
    patterns = detect_candle_patterns(candles)
    if not patterns:
        return []

    candidates = []
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

        candidates.append((direction, sig_type, pat))

    if not candidates:
        return []

    results = []
    for direction in ("CALL", "PUT"):
        same_dir = [(st, p) for d, st, p in candidates if d == direction]
        if not same_dir:
            continue
        same_dir.sort(key=lambda x: -x[1]["score"])
        best_st, best = same_dir[0]
        reason_str = best.get("reason") or ""
        note = ""
        if len(same_dir) > 1:
            others = [p["name"] for _, p in same_dir[1:]]
            note = (f" [+{len(same_dir) - 1} overlapping pattern(s) deduped: "
                    f"{', '.join(others)}]")
        results.append(ModuleResult(
            module_name="pattern",
            direction=direction,
            score=best["score"],
            confidence=best["score"] * 18,  # 3->54, 2->36
            signal_type=best_st,
            reliability="PATTERN",
            group="PATTERN_REVERSAL" if best_st == "REVERSAL" else "PATTERN_CONTINUATION",
            reasons=[reason_str + note],
        ))
    return results
