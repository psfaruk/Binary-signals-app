"""
OTC-MARKET ENGINE — 6-module prediction engine, tuned for broker-generated
OTC pairs (asset names ending in "_otc").

This is a SEPARATE engine from engines.real — it has its own:
  - PAIR_CONFIGS (per-pair static weight priors)
  - DEFAULT_WEIGHTS
  - RELIABILITY tier multipliers (OTC tuning: dampen indicators, boost OTC patterns)
  - Module 6: otc_pattern (mean-reversion detector)

But it SHARES the blender algorithm, the context computer, the types,
and 5 of 6 modules with the Real engine — all imported from engines.base.

Public API:
    from engines.otc import predict
    result = predict(candles, ticks, micro, asset="EURUSD_otc")

Architecture:
    6 independent modules → Smart Blender → final prediction

    Module 1: candle_reaction  — single-candle price action
    Module 2: running_tick     — tick microstructure composite
    Module 3: pattern          — multi-candle patterns (engulfing, star, etc.)
    Module 4: indicator        — RSI, MACD, EMA, Bollinger, Stochastic
    Module 5: key_level        — support/resistance, round numbers
    Module 6: otc_pattern      — OTC-specific mean-reversion patterns (boosted)

    Tuned for OTC mean-reversion behavior:
      - otc_pattern module gets bonus (×1.2 reliability, 1.2-1.5 weight)
      - indicator dampened on exotic OTC pairs (broker noise)
      - candle_reaction + key_level weighted higher on mean-reverting pairs
"""
from engines.base.blender import predict as _base_predict
from engines.otc.config import CONFIG as _OTC_CONFIG, CONFIG


def predict(candles, ticks=None, micro=None, asset="", htf_trend="SIDEWAYS",
            period: int = 60) -> dict:
    """OTC engine prediction — routes to the shared blender with OTC config.

    Args:
        candles: list of closed candle dicts (time, open, high, low, close)
        ticks: tick list for the closed candle (optional)
        micro: microstructure dict (optional)
        asset: OTC pair name (e.g. "EURUSD_otc")
        htf_trend: "UPTREND" | "DOWNTREND" | "SIDEWAYS"
        period: candle period in seconds (default 60)

    Returns:
        Prediction dict (signal, confidence, strength, score, reasons, etc.)
    """
    return _base_predict(candles, ticks=ticks, micro=micro, asset=asset,
                         htf_trend=htf_trend, period=period, config=_OTC_CONFIG)


__all__ = ["predict", "CONFIG"]
