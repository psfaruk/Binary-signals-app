"""
Legacy compatibility wrapper — delegates to engines.predict().

This file kept so existing imports (from candle_reaction import predict_from_candle)
continue to work. The actual prediction logic lives in the engines/ package.
"""
from engines import predict


def predict_from_candle(candles, ticks=None, micro=None, asset="",
                        htf_trend="SIDEWAYS", period=60):
    """Predict next candle direction.

    Delegates to engines.predict() which runs 6 independent modules
    + Smart Blender. htf_trend is passed through so the blender can
    apply HTF confluence weighting (was previously computed in feed.py
    but discarded — Bug #1, fixed 2026-07-17). period is passed through
    so per_pair DB-adaptation can look up the right (asset, period) bucket
    (Bug #5, fixed 2026-07-17).
    """
    return predict(candles, ticks, micro, asset=asset,
                   htf_trend=htf_trend, period=period)
