"""Module: Candle Reaction Engine — Momentum continuation signals.

FIX (DEEP-FIX-2026-08-07): conditions relaxed from 3 candles to 2 candles
with 20% body (was 30%). Previously produced ZERO votes because 3 consecutive
30%-body candles almost never happen on 1-min OTC data.
"""
from engines.base.types import ModuleResult, MarketContext


def analyze(candles, ctx: MarketContext) -> list:
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

    # SIGNAL: 2+ monotonic rising/falling closes with non-trivial bodies.
    # FIX: reduced from 3 candles + 30% body → 2 candles + 20% body
    if len(candles) >= 2:
        c1, c2 = candles[-2], candles[-1]
        b1 = abs(c1["close"] - c1["open"])
        b2 = abs(c2["close"] - c2["open"])
        r1 = c1["high"] - c1["low"]
        r2 = c2["high"] - c2["low"]

        # Monotonic rising closes
        if c1["close"] < c2["close"]:
            if (r1 > 0 and r2 > 0 and b1/r1 >= 0.20 and b2/r2 >= 0.20):
                score, conf = 2, 56
                if is_trending and trend_regime == "TREND_UP":
                    score, conf = 3, 62
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="CALL",
                    score=score, confidence=conf,
                    signal_type="CONTINUATION", reliability="CANDLE", group="BODY",
                    reasons=[f"Rising closes (2 UP) -> CALL continuation"]))

        # Monotonic falling closes
        elif c1["close"] > c2["close"]:
            if (r1 > 0 and r2 > 0 and b1/r1 >= 0.20 and b2/r2 >= 0.20):
                score, conf = 2, 56
                if is_trending and trend_regime == "TREND_DOWN":
                    score, conf = 3, 62
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="PUT",
                    score=score, confidence=conf,
                    signal_type="CONTINUATION", reliability="CANDLE", group="BODY",
                    reasons=[f"Falling closes (2 DOWN) -> PUT continuation"]))

    return results
