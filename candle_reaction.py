"""
Legacy compatibility wrapper — delegates to engines.predict().

This file kept so existing imports (from candle_reaction import predict_from_candle)
continue to work. The actual prediction logic lives in the engines/ package.
"""
from engines import predict


def predict_from_candle(candles, ticks=None, micro=None, asset=""):
    """Predict next candle direction.

    Delegates to engines.predict() which runs 6 independent modules
    + Smart Blender.
    """
    return predict(candles, ticks, micro, asset=asset)
