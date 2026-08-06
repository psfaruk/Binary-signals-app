"""Module: Candle Reaction Engine — Rising/falling closes momentum signal."""
from engines.base.types import ModuleResult, MarketContext


def analyze(candles, ctx: MarketContext) -> list:
    """Run trend-continuation signal (SIGNAL 6). Fires only in strong trends."""
    results = []
    if not candles or len(candles) < 3:
        return results

    last = candles[-1]
    o, h, l, c = last["open"], last["high"], last["low"], last["close"]
    body = c - o
    rng = h - l
    body_pct = abs(body) / rng * 100 if rng > 0 else 0

    regime = ctx.regime
    is_trending = regime.get("is_trending", False)
    trend_regime = regime.get("regime", "RANGE")
    trend_strength = regime.get("trend_strength", 0.0)

    # SIGNAL 6: 3+ monotonic rising/falling closes with non-trivial bodies.
    if is_trending and trend_strength > 0.5 and len(candles) >= 3:
        c1_close = candles[-3]["close"]
        c2_close = candles[-2]["close"]
        c3_close = candles[-1]["close"]
        b1 = abs(candles[-3]["close"] - candles[-3]["open"])
        b2 = abs(candles[-2]["close"] - candles[-2]["open"])
        r1 = candles[-3]["high"] - candles[-3]["low"]
        r2 = candles[-2]["high"] - candles[-2]["low"]
        # Monotonic rising closes
        if c1_close < c2_close < c3_close:
            if (r1 > 0 and r2 > 0 and rng > 0
                    and b1/r1 >= 0.30 and b2/r2 >= 0.30 and body_pct >= 30
                    and trend_regime == "TREND_UP"):
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="CALL", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="CANDLE", group="BODY_CONT",
                    reasons=[f"Rising closes momentum (3 UP, str={trend_strength:.2f}) -> CALL continuation"]))
        # Monotonic falling closes
        elif c1_close > c2_close > c3_close:
            if (r1 > 0 and r2 > 0 and rng > 0
                    and b1/r1 >= 0.30 and b2/r2 >= 0.30 and body_pct >= 30
                    and trend_regime == "TREND_DOWN"):
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="PUT", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="CANDLE", group="BODY_CONT",
                    reasons=[f"Falling closes momentum (3 DOWN, str={trend_strength:.2f}) -> PUT continuation"]))

    return results
