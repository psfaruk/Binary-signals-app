"""
engines/base/context.py — Shared market context computer.

Computes the MarketContext ONCE per candle close and passes it to every
module so they don't recompute regime/ATR/stats/key_levels.

Uses `core.analysis` as the single source of truth for analysis functions.
"""
from core.analysis import (
    classify_market_regime,
    find_key_levels,
    check_level_confluence,
    compute_statistical_edge,
    _atr,
)
from engines.base.types import MarketContext


def compute_context(candles) -> MarketContext:
    """Compute all shared market context from candle list.

    Args:
        candles: list of closed candle dicts with keys time, open, high, low, close

    Returns:
        MarketContext dataclass with regime, atr, stats, key_levels,
        level_confluence, ema9, ema21, vol_pct, closes
    """
    if not candles or len(candles) < 3:
        return MarketContext(
            regime={"regime": "RANGE", "trend_strength": 0.0,
                    "volatility_pct": 1.0, "ema9": 0, "ema21": 0,
                    "is_trending": False, "is_ranging": True, "is_volatile": False},
            atr=0.0001,
            stats={"z_body": 0, "z_range": 0, "close_percentile": 50,
                   "streak_rarity": 0, "current_streak": 0, "streak_direction": 0},
            key_levels=[],
            level_confluence={"near_level": False, "level_type": None,
                              "level_price": None, "action": None, "distance_atr": 0},
            ema9=0, ema21=0, vol_pct=1.0,
            closes=[c["close"] for c in candles] if candles else [],
        )

    regime = classify_market_regime(candles)
    atr = _atr(candles)
    stats = compute_statistical_edge(candles)
    key_levels = find_key_levels(candles, lookback=50)
    level_conf = check_level_confluence(candles, key_levels, atr)

    return MarketContext(
        regime=regime,
        atr=atr,
        stats=stats,
        key_levels=key_levels,
        level_confluence=level_conf,
        ema9=regime.get("ema9", 0),
        ema21=regime.get("ema21", 0),
        vol_pct=regime.get("volatility_pct", 1.0),
        closes=[c["close"] for c in candles],
    )
