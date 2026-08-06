"""engines/base/context.py — Shared market context computer."""
from core.analysis import (
    classify_market_regime,
    find_key_levels,
    check_level_confluence,
    compute_statistical_edge,
    _atr,
)
from engines.base.types import MarketContext

_MIN_CANDLES_FOR_CONTEXT = 3        # min candles for regime + ATR computation
_COLD_START_ATR_FLOOR = 0.0010      # ~10 pips for non-JPY pairs (USD-based)
_COLD_START_ATR_FLOOR_JPY = 0.10    # ~10 pips for JPY pairs (price ~150)
_KEY_LEVEL_LOOKBACK = 50            # default lookback for find_key_levels

__all__ = ["compute_context"]


def compute_atr(candles) -> float:
    """Compute Average True Range from candle list (wraps core.analysis._atr)."""
    return _atr(candles)


def compute_context(candles) -> MarketContext:
    """Compute all shared market context from candle list."""
    if not candles or len(candles) < _MIN_CANDLES_FOR_CONTEXT:
        # Cold-start defaults: NEUTRAL regime + JPY-aware ATR floor.
        _cold_atr = _COLD_START_ATR_FLOOR_JPY if _looks_like_jpy_pair(candles) \
                    else _COLD_START_ATR_FLOOR
        return MarketContext(
            regime={"regime": "COLD_START", "trend_strength": 0.0,
                    "volatility_pct": 1.0, "ema9": 0.0, "ema21": 0.0,
                    "is_trending": False, "is_ranging": False, "is_volatile": False},
            atr=_cold_atr,
            stats={"z_body": 0, "z_range": 0, "close_percentile": 50,
                   "streak_rarity": 0, "current_streak": 0, "streak_direction": 0},
            key_levels=[],
            level_confluence={"near_level": False, "level_type": None,
                              "level_price": None, "action": None, "distance_atr": 0},
            ema9=0.0, ema21=0.0, vol_pct=1.0,
            closes=[c["close"] for c in candles],
        )

    # Wrap analysis calls to fall back to cold-start on any failure.
    try:
        regime = classify_market_regime(candles)
        atr = compute_atr(candles)
        stats = compute_statistical_edge(candles)
        key_levels = find_key_levels(candles, lookback=_KEY_LEVEL_LOOKBACK)
        level_conf = check_level_confluence(candles, key_levels, atr)
    except Exception:
        _cold_atr = _COLD_START_ATR_FLOOR_JPY if _looks_like_jpy_pair(candles) \
                    else _COLD_START_ATR_FLOOR
        return MarketContext(
            regime={"regime": "COLD_START", "trend_strength": 0.0,
                    "volatility_pct": 1.0, "ema9": 0.0, "ema21": 0.0,
                    "is_trending": False, "is_ranging": False, "is_volatile": False},
            atr=_cold_atr,
            stats={"z_body": 0, "z_range": 0, "close_percentile": 50,
                   "streak_rarity": 0, "current_streak": 0, "streak_direction": 0},
            key_levels=[],
            level_confluence={"near_level": False, "level_type": None,
                              "level_price": None, "action": None, "distance_atr": 0},
            ema9=0.0, ema21=0.0, vol_pct=1.0,
            closes=[c["close"] for c in candles],
        )

    return MarketContext(
        regime=regime,
        atr=atr,
        stats=stats,
        key_levels=key_levels,
        level_confluence=level_conf,
        ema9=regime.get("ema9", 0.0),
        ema21=regime.get("ema21", 0.0),
        vol_pct=regime.get("volatility_pct", 1.0),
        closes=[c["close"] for c in candles],
    )


def _looks_like_jpy_pair(candles) -> bool:
    """Heuristic: detect JPY pairs by price magnitude (median close > 10)."""
    if not candles:
        return False
    try:
        closes = [c["close"] for c in candles if isinstance(c, dict) and "close" in c]
        if not closes:
            return False
        closes_sorted = sorted(closes)
        median = closes_sorted[len(closes_sorted) // 2]
        return median > 10.0
    except (TypeError, KeyError, IndexError):
        return False
