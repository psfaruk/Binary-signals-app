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

# FIX (DEEP-AUDIT-2026-07-26 / F-04-35, F-04-36, F-04-37): extracted magic
# numbers into named module constants for clarity and tunability. (Audit
# A-02 #57, #58, #63; A-06 #31, #32, #33.)
_MIN_CANDLES_FOR_CONTEXT = 3        # min candles for regime + ATR computation
_COLD_START_ATR_FLOOR = 0.0010      # ~10 pips for non-JPY pairs (USD-based)
_COLD_START_ATR_FLOOR_JPY = 0.10    # ~10 pips for JPY pairs (price ~150)
_KEY_LEVEL_LOOKBACK = 50            # default lookback for find_key_levels

__all__ = ["compute_context"]


# FIX (DEEP-AUDIT-2026-07-26 / F-04-33): wrapper for the private `_atr`
# function in core.analysis. Importing `_atr` directly violates the private-
# symbol convention; this wrapper provides a public surface without modifying
# core.analysis (which is another agent's scope). (Audit A-02 #61, A-06 #30.)
def compute_atr(candles) -> float:
    """Compute Average True Range from candle list (wraps core.analysis._atr)."""
    return _atr(candles)


def compute_context(candles) -> MarketContext:
    """Compute all shared market context from candle list.

    Args:
        candles: list of closed candle dicts with keys time, open, high, low, close

    Returns:
        MarketContext dataclass with regime, atr, stats, key_levels,
        level_confluence, ema9, ema21, vol_pct, closes

    FIX (DEEP-AUDIT-2026-07-26 / F-04-29): cold-start `regime` string changed
    from `"RANGE"` to `"COLD_START"`. The previous version contradicted the
    contract `is_ranging: regime == "RANGE"` (per core/analysis.py:482) by
    setting `regime="RANGE"` AND `is_ranging=False`. Downstream code reading
    `regime["regime"] == "RANGE"` would apply ranging logic (reversal ×1.3
    boost in the blender) while the boolean flag said "no range" — the FIX
    comment at lines 30-39 documented the intent but didn't fix the string.
    Now `"COLD_START"` matches none of the known regimes (RANGE/TREND_UP/
    TREND_DOWN/VOLATILE), so neither string-keyed nor boolean-keyed logic
    fires. (Audit A-06 #18.)

    FIX (DEEP-AUDIT-2026-07-26 / F-04-30): cold-start ATR floor is now
    JPY-pair-aware. Previously `0.0010` (~10 pips for USD pairs) was applied
    uniformly — but for JPY pairs (price ~150), 10 pips = 0.10, not 0.0010.
    The exhaustion gate at blender.py:240 uses `atr * 0.5`; for JPY pairs
    the wrong floor was 100x too small, causing the gate to fire on any
    micro-move. (Audit A-02 #58.)

    FIX (DEEP-AUDIT-2026-07-26 / F-04-31): cold-start ema9/ema21 now use
    `0.0` (float) instead of int `0` — matches MarketContext type annotation
    `float`. (Audit A-02 #59.)

    FIX (DEEP-AUDIT-2026-07-26 / F-04-34): wrapped the analysis calls in
    try/except so a malformed candle list or analysis bug doesn't crash the
    entire prediction — falls back to cold-start MarketContext. (Audit
    A-02 #62.)
    """
    # FIX (DEEP-AUDIT-2026-07-26 / F-04-32): dead `else []` removed — the
    # `if not candles` branch above already returns early, so at this point
    # `candles` is guaranteed truthy. (Audit A-02 #60, A-06 #94.)
    if not candles or len(candles) < _MIN_CANDLES_FOR_CONTEXT:
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-11): cold-start defaults
        # were biased toward reversal (is_ranging=True boosts reversal ×1.3
        # via the blender regime multiplier) AND triggered the exhaustion
        # gate too easily (atr=0.0001 → exhaustion threshold atr*0.5 =
        # 0.00005, almost any net move passes). A new stream that just
        # connected would emit reversal signals with inflated confidence
        # based on only 3 candles — no statistical basis. Now: NEUTRAL
        # regime (all flags False) so no regime multiplier applies, and a
        # larger ATR floor (~10 pips) so the exhaustion gate only fires on
        # genuine moves.
        # FIX (DEEP-AUDIT-2026-07-26 / F-04-29): changed `regime` string from
        # "RANGE" to "COLD_START" — see function-level docstring for the
        # consistency rationale.
        # FIX (DEEP-AUDIT-2026-07-26 / F-04-30): ATR floor is now JPY-aware.
        # Sniff the asset name from the first candle if available; default
        # to the non-JPY floor when uncertain.
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

    # FIX (DEEP-AUDIT-2026-07-26 / F-04-34): wrap analysis calls in try/except
    # so a malformed candle list or analysis bug doesn't crash the entire
    # prediction — falls back to cold-start MarketContext. (Audit A-02 #62.)
    try:
        regime = classify_market_regime(candles)
        atr = compute_atr(candles)
        stats = compute_statistical_edge(candles)
        # FIX (DEEP-AUDIT-2026-07-26 / F-04-37): use named constant for the
        # key-level lookback (was inline magic number 50). (Audit A-02 #63,
        # A-06 #33.)
        key_levels = find_key_levels(candles, lookback=_KEY_LEVEL_LOOKBACK)
        level_conf = check_level_confluence(candles, key_levels, atr)
    except Exception:
        # Defensive fallback: any analysis failure degrades gracefully to
        # cold-start context. The next candle close will retry. The broad
        # `except Exception` is intentional — we don't know which analysis
        # call raised, and crashing the prediction pipeline is worse than
        # emitting a conservative cold-start context.
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
    """Heuristic: detect JPY pairs by price magnitude.

    JPY pairs trade ~100-200 (e.g., USDJPY=150, GBPJPY=180). Non-JPY majors
    trade ~0.7-2.0 (e.g., EURUSD=1.08). If the median close > 10, assume JPY
    and use the JPY-scaled ATR floor.

    We can't see the asset name from the candle list (candles only have OHLC),
    so we sniff via price magnitude. This is a heuristic — if a non-JPY pair
    somehow trades > 10 (e.g., a token-priced asset), we'll over-scale the ATR
    floor, which is the safe direction (floor too high → exhaustion gate
    doesn't fire → no false exhaustion boost).
    """
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
