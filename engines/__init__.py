"""
Engines package — 6-module prediction engine with Smart Blender.

Public API:
    from engines import predict
    result = predict(candles, ticks, micro, asset="EURUSD_otc")

Architecture:
    6 independent modules → Smart Blender → final prediction

    Module 1: candle_reaction  — single-candle price action
    Module 2: running_tick     — tick microstructure composite
    Module 3: pattern          — multi-candle patterns (engulfing, star, etc.)
    Module 4: indicator        — RSI, MACD, EMA, Bollinger, Stochastic
    Module 5: key_level        — support/resistance, round numbers
    Module 6: otc_pattern      — OTC-specific mean-reversion patterns

    Blender:
    - Correlation grouping (BODY signals collapse)
    - Regime-aware weighting (TREND/RANGE/VOLATILE + exhaustion gate)
    - Per-pair adaptation (USDPKR → boost reversal, EURUSD → boost indicator)
    - Reliability tiers (PATTERN ×1.5 > STAT/LEVEL ×1.3 > CANDLE ×1.0 > MICRO ×0.6)
    - Pattern confluence requirement for STRONG
    - Group-aware confidence calibration
"""
from engines.blender import predict

__all__ = ["predict"]
