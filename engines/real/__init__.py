"""
REAL-MARKET ENGINE — 6-module prediction engine with Smart Blender,
tuned for live exchange pairs (no "_otc" suffix).

Public API:
    from engines.real import predict
    result = predict(candles, ticks, micro, asset="EURUSD")

Architecture:
    Same 6 modules + Smart Blender as the OTC engine, but retuned for
    real-market behavior:
      - Indicators (RSI/MACD/EMA) are MORE reliable (boosted to ×1.3)
      - Tick microstructure is more meaningful (real volume) — ×0.7
      - Per-pair configs favor trend-following (continuation bias)
      - Mean-reversion (otc_pattern) is dampened — real markets trend harder

    Module 1: candle_reaction  — single-candle price action
    Module 2: running_tick     — tick microstructure composite
    Module 3: pattern          — multi-candle patterns (engulfing, star, etc.)
    Module 4: indicator        — RSI, MACD, EMA, Bollinger, Stochastic (boosted)
    Module 5: key_level        — support/resistance, round numbers
    Module 6: otc_pattern      — mean-reversion patterns (dampened on real)

    Blender:
    - Correlation grouping (BODY signals collapse)
    - Regime-aware weighting (TREND/RANGE/VOLATILE + exhaustion gate)
    - Per-pair adaptation (REAL PAIR_CONFIGS, DB bucket = asset w/o _otc)
    - Reliability tiers (INDICATOR ×1.3, MICRO ×0.7 — both boosted vs OTC)
    - Pattern confluence requirement for STRONG
    - Group-aware confidence calibration
"""
from engines.real.blender import predict

__all__ = ["predict"]
