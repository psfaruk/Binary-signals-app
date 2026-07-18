"""
analyze_eoc.py — DEPRECATED shim.

The original 4,200-line theory-blend engine was removed 2026-07-13.
The remaining helpers (_round_level, _key_levels, _atr, _build_micro)
have moved to:
  - core.analysis      (_round_level, _key_levels, _atr)
  - core.microstructure (_build_micro)

This file re-exports them so existing imports in feed.py / sim_feed.py
keep working. Future code should import directly from core.*.

The dead `_parse_votes` function (which nothing called) has been removed
entirely.
"""
from core.analysis import _round_level, _key_levels, _atr
from core.microstructure import _build_micro

__all__ = ["_round_level", "_key_levels", "_atr", "_build_micro"]
