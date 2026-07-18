"""
advanced_analysis.py — DEPRECATED shim.

All analysis functions have moved to `core.analysis`. This file remains
as a thin re-export so existing imports keep working during migration.

Future code should import directly from `core.analysis`:
    from core.analysis import classify_market_regime, find_key_levels, _atr
"""
# Re-export everything from core.analysis
from core.analysis import *  # noqa: F401, F403
from core.analysis import (
    _atr, _ema, _body, _abs_body, _range,
    detect_candle_patterns,
    classify_market_regime,
    find_key_levels,
    check_level_confluence,
    compute_statistical_edge,
    _round_level, round_level,
    _key_levels, key_levels_rich,
)
