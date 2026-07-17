"""
OTC-MARKET ENGINE — 6-module prediction engine with Smart Blender,
tuned for broker-generated OTC pairs (asset names ending in "_otc").

Public API:
    from engines.otc import predict
    result = predict(candles, ticks, micro, asset="EURUSD_otc")

Architecture:
    6 independent modules → Smart Blender → final prediction
    Tuned for OTC mean-reversion behavior:
      - otc_pattern module gets bonus (×1.2 reliability, 1.2-1.5 weight)
      - indicator dampened on exotic OTC pairs (broker noise)
      - candle_reaction + key_level weighted higher on mean-reverting pairs

    Module 1: candle_reaction  — single-candle price action
    Module 2: running_tick     — tick microstructure composite
    Module 3: pattern          — multi-candle patterns (engulfing, star, etc.)
    Module 4: indicator        — RSI, MACD, EMA, Bollinger, Stochastic
    Module 5: key_level        — support/resistance, round numbers
    Module 6: otc_pattern      — OTC-specific mean-reversion patterns (boosted)

    Blender:
    - Correlation grouping (BODY signals collapse)
    - Regime-aware weighting (TREND/RANGE/VOLATILE + exhaustion gate)
    - Per-pair adaptation (OTC PAIR_CONFIGS, DB bucket = asset with _otc)
    - Reliability tiers (PATTERN ×1.5 > STAT/LEVEL ×1.3 > OTC ×1.2 > CANDLE ×1.0 > INDICATOR ×1.0 > MICRO ×0.6)
    - Pattern confluence requirement for STRONG
    - Group-aware confidence calibration
"""
from engines.otc.blender import predict

__all__ = ["predict"]
