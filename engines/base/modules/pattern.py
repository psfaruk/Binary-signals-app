"""
Module: Multi-Candle Pattern Engine

Detects classic Japanese candlestick patterns across the last 2-4 candles.
These are HIGHER-CONVICTION than single-candle signals because they
capture inter-candle dynamics.

After THEORY-PRUNE-2026-08-04 (3 rounds), only 8 patterns remain active.
All patterns are now ALWAYS_REVERSAL (no continuation/conditional patterns).

Active patterns (reliability: PATTERN ×1.2):
  - Tweezer Bottom          (~60% win, n=148 in production)
  - Piercing Line           (~63% win, n=17)
  - Dark Cloud Cover        (~63% win, n=13)
  - Bearish Harami          (~58% win, n=110)
  - Bearish Pin Bar         (~63% win, n=89)
  - Bullish Two-Bar Reversal (~62% win, n=82)
  - Bearish Two-Bar Reversal (~62% win, n=75)
  - Doji Bearish            (~58% win, n=27)

Each pattern gets its own group (PATTERN_REVERSAL) for vote counting.
"""
from core.analysis import detect_candle_patterns
from engines.base.types import ModuleResult, MarketContext

# Active patterns — all are ALWAYS_REVERSAL (structural reversal patterns).
# After Round 1+2+3 pruning (2026-08-04), only these 8 patterns remain.
ALWAYS_REVERSAL = {
    "TWEEZER_BOTTOM",          # 71% win, n=148 (Round 3 — kept despite direction bias)
    "PIERCING_LINE",           # 53% win, n=17 (borderline, kept for sample size)
    "DARK_CLOUD",              # 62% win, n=13
    "BEAR_HARAMI",             # 58% win, n=110 (kept; Bearish side outperforms Bullish)
    "BEAR_PIN_BAR",            # 63% win, n=89
    "BULL_TWO_BAR_REV",        # 56% win, n=82 (borderline)
    "BEAR_TWO_BAR_REV",        # 59% win, n=75
    "DOJI_BEARISH",            # 65% win, n=27
}


def analyze(candles, ctx: MarketContext) -> list:
    """Detect multi-candle patterns.

    Returns list of ModuleResult objects, one per detected pattern.
    Each pattern shares the same group (PATTERN_REVERSAL) for collapsed
    vote counting.
    """
    patterns = detect_candle_patterns(candles)
    if not patterns:
        return []

    results = []
    for pat in patterns:
        name = pat["name"]
        direction = pat["direction"]

        if name not in ALWAYS_REVERSAL:
            # Unknown pattern — skip (defensive; should not happen since
            # detect_candle_patterns only returns the 8 active patterns)
            continue

        sig_type = "REVERSAL"
        reason_str = pat.get("reason") or ""
        results.append(ModuleResult(
            module_name="pattern",
            direction=direction,
            score=pat["score"],
            # Confidence = score * 18 (3→54, 2→36)
            confidence=pat["score"] * 18,
            signal_type=sig_type,
            reliability="PATTERN",
            group="PATTERN_REVERSAL",
            reasons=[reason_str],
        ))
    return results
