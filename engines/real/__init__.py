"""
REAL-MARKET ENGINE — 6-module prediction engine, tuned for live exchange
pairs (no "_otc" suffix).

This is a SEPARATE engine from engines.otc — it has its own:
  - PAIR_CONFIGS (per-pair static weight priors, keyed by BARE symbol)
  - DEFAULT_WEIGHTS (favor continuation/trend modules)
  - RELIABILITY tier multipliers (Real tuning: boost INDICATOR to 1.3,
    add TREND tier at 1.3, boost MICRO to 0.7)
  - Module 6: trend_follow (momentum continuation detector)

But it SHARES the blender algorithm, the context computer, the types,
and 5 of 6 modules with the OTC engine — all imported from engines.base.

Public API:
    from engines.real import predict
    result = predict(candles, ticks, micro, asset="EURUSD")

Architecture:
    6 independent modules → Smart Blender → final prediction

    Module 1: candle_reaction  — single-candle price action
    Module 2: running_tick     — tick microstructure composite
    Module 3: pattern          — multi-candle patterns (boosted)
    Module 4: indicator        — RSI, MACD, EMA, Bollinger, Stochastic (boosted)
    Module 5: key_level        — support/resistance, round numbers (boosted)
    Module 6: trend_follow     — momentum continuation, EMA alignment, breakouts

    Tuned for real-market behavior:
      - Indicators (RSI/MACD/EMA) are MORE reliable (boosted to ×1.3)
      - Tick microstructure is more meaningful (real volume) — ×0.7
      - Per-pair configs favor trend-following (continuation bias)
      - Mean-reversion logic is NOT used (real markets trend harder)
"""
from engines.base.blender import predict as _base_predict
from engines.real.config import CONFIG as _REAL_CONFIG, CONFIG


def predict(candles, ticks=None, micro=None, asset="", htf_trend="SIDEWAYS",
            period: int = 60, recent_accuracy=None) -> dict:
    """Real engine prediction — routes to the shared blender with Real config.

    Args:
        candles: list of closed candle dicts (time, open, high, low, close)
        ticks: tick list for the closed candle (optional)
        micro: microstructure dict (optional)
        asset: Real pair name (e.g. "EURUSD" — no _otc suffix)
        htf_trend: "UPTREND" | "DOWNTREND" | "SIDEWAYS"
        period: candle period in seconds (default 60)
        recent_accuracy: optional (accuracy, sample_count) for self-correction

    Returns:
        Prediction dict (signal, confidence, strength, score, reasons, etc.)
    """
    return _base_predict(candles, ticks=ticks, micro=micro, asset=asset,
                         htf_trend=htf_trend, period=period, config=_REAL_CONFIG,
                         recent_accuracy=recent_accuracy)


__all__ = ["predict", "CONFIG"]
