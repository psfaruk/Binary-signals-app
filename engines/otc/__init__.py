"""OTC-MARKET ENGINE — broker-generated OTC pair prediction (tuned for mean-reversion)."""
from engines.base.blender import predict as _base_predict
from engines.otc.config import CONFIG


def predict(candles, ticks=None, micro=None, asset="", htf_trend="SIDEWAYS",
            period: int = 60, recent_accuracy=None) -> dict:
    """OTC engine prediction — routes to the shared blender with OTC config."""
    return _base_predict(candles, ticks=ticks, micro=micro, asset=asset,
                         htf_trend=htf_trend, period=period, config=CONFIG,
                         recent_accuracy=recent_accuracy)


__all__ = ["predict", "CONFIG"]
