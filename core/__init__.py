"""
core/__init__.py — Package marker for the shared `core` library.

Exposes:
    constants — single source of truth for module names, allowed periods, etc.
    analysis  — pure-function analysis library (regime, patterns, ATR, EMA, key levels)
    microstructure — tick-level microstructure builder
    stats     — shared module-stats computer used by /api/stats and the CLI report
"""
from core import constants  # noqa: F401
