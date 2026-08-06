"""REAL-MARKET ENGINE — live exchange pair prediction (tuned for trend-following)."""
from engines.base.blender import predict as _base_predict
from engines.real.config import CONFIG as _REAL_CONFIG

CONFIG = _REAL_CONFIG


def predict(candles, ticks=None, micro=None, asset="", htf_trend="SIDEWAYS",
            period: int = 60, recent_accuracy=None) -> dict:
    """Real engine prediction — routes to the shared blender with Real config."""
    return _base_predict(candles, ticks=ticks, micro=micro, asset=asset,
                         htf_trend=htf_trend, period=period, config=_REAL_CONFIG,
                         recent_accuracy=recent_accuracy)


__all__ = ["predict", "CONFIG"]
