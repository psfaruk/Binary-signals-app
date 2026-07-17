"""
Engines package — category-aware prediction router.

Two completely separate engines live side-by-side:

    engines.otc  — for OTC pairs (broker-generated price feed)
        6 modules + Smart Blender tuned for mean-reversion behavior.
        Heavier weight on candle_reaction, otc_pattern, key_level.
        Payout floor: 85%.

    engines.real — for real-market pairs (live exchange prices)
        Same 6 modules + Smart Blender, but retuned for trend-following.
        Heavier weight on indicator (RSI/MACD/EMA), pattern (engulfing etc.).
        Payout floor: 70%.

The two engines share NO state — separate per_pair configs, separate
module weight defaults, separate reliability tier overrides. Each engine
has its own DB-adaptation cache (looked up by asset name only — OTC and
real never collide because real pairs have no "_otc" suffix).

Public API:
    from engines import predict
    result = predict(candles, ticks, micro, asset="EURUSD_otc", category="otc")
    result = predict(candles, ticks, micro, asset="EURUSD",     category="real")

If `category` is omitted, it is auto-detected from the asset name:
asset ending in "_otc" → otc, otherwise → real.
"""
from engines import otc as _otc_engine
from engines import real as _real_engine

__all__ = ["predict", "otc", "real"]


def predict(candles, ticks=None, micro=None, asset="", htf_trend="SIDEWAYS",
            period: int = 60, category: str = None) -> dict:
    """Route to the correct engine based on `category`.

    Args:
        candles, ticks, micro, asset: passed through unchanged.
        htf_trend: passed through unchanged.
        period: candle period in seconds, passed through unchanged.
        category: "otc" | "real" | None.
            If None, auto-detected from asset name:
                ends with "_otc" → "otc"
                else             → "real"

    Returns:
        The engine's prediction dict (signal, confidence, strength, etc.)
        with an extra "category" field for UI/logging.
    """
    if category is None:
        category = "otc" if asset.endswith("_otc") else "real"

    if category == "otc":
        result = _otc_engine.predict(
            candles, ticks, micro, asset=asset,
            htf_trend=htf_trend, period=period)
    elif category == "real":
        result = _real_engine.predict(
            candles, ticks, micro, asset=asset,
            htf_trend=htf_trend, period=period)
    else:
        raise ValueError(f"unknown category {category!r}; expected 'otc' or 'real'")

    # Echo the resolved category so the UI / signal_log can record which
    # engine produced this prediction (useful for per-engine accuracy
    # tracking in /api/stats).
    result = dict(result)
    result["category"] = category
    return result


# Convenience submodules — callers can also import the engine directly:
#   from engines.otc import predict as predict_otc
#   from engines.real import predict as predict_real
otc = _otc_engine
real = _real_engine
