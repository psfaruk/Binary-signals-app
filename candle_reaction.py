"""
Legacy compatibility wrapper — delegates to engines.predict().

This file kept so existing imports (from candle_reaction import predict_from_candle)
continue to work. The actual prediction logic lives in the engines/ package,
which routes between engines.otc and engines.real based on the `category`
parameter (auto-detected from asset name if not specified).
"""
from engines import predict


def predict_from_candle(candles, ticks=None, micro=None, asset="",
                        htf_trend="SIDEWAYS", period=60, category=None):
    """Predict next candle direction.

    Delegates to engines.predict() which routes to engines.otc.predict or
    engines.real.predict based on `category` (auto-detected from asset
    name: ends with "_otc" → otc, otherwise → real).

    Args:
        candles: list of closed candle dicts
        ticks: tick list for the closed candle (optional)
        micro: microstructure dict (optional)
        asset: pair name (e.g. "EURUSD_otc" for OTC, "EURUSD" for real)
        htf_trend: "UPTREND" | "DOWNTREND" | "SIDEWAYS" from 5m EMA confluence
        period: candle period in seconds
        category: "otc" | "real" | None (auto-detected from asset name if None)

    Returns:
        Prediction dict with signal, confidence, strength, category, etc.
    """
    return predict(candles, ticks, micro, asset=asset,
                   htf_trend=htf_trend, period=period, category=category)
