"""
core/constants.py — Single source of truth for shared constants.

All modules that need the list of prediction modules MUST import from here.
Previously this list was duplicated in 4 places (db.py, server.py,
module_performance_report.py, static/index.html) and they had drifted out
of sync — `trend_follow` was missing from /api/stats but present in db.py.

Single source of truth prevents that drift.
"""

# All prediction modules, in canonical order (matches blender.py pipeline).
# Used by:
#   - db.per_module_accuracy()        (parsing signal_log reasons)
#   - server.py /api/stats            (per-module win-rate report)
#   - module_performance_report.py    (CLI version of /api/stats)
#   - static/js/common.js             (frontend module breakdown display)
#   - engines/base/blender.py         (_module_breakdown helper)
MODULE_NAMES = (
    "candle_reaction",
    "running_tick",
    "pattern",
    "indicator",
    "key_level",
    "otc_pattern",      # OTC engine's 6th module (mean-reversion)
    "trend_follow",     # Real engine's 6th module (momentum continuation)
)

# Human-readable display names for the UI.
MODULE_DISPLAY_NAMES = {
    "candle_reaction": "Candle Reaction",
    "running_tick":    "Running Tick",
    "pattern":         "Pattern",
    "indicator":       "Indicator",
    "key_level":       "Key Level",
    "otc_pattern":     "OTC Pattern",
    "trend_follow":    "Trend Follow",
}

# Modules used by each engine (5 shared + 1 engine-specific).
OTC_MODULES = (
    "candle_reaction", "running_tick", "pattern",
    "indicator", "key_level", "otc_pattern",
)
REAL_MODULES = (
    "candle_reaction", "running_tick", "pattern",
    "indicator", "key_level", "trend_follow",
)

# Allowed candle periods (seconds). Whitelisted to prevent bogus streams
# (e.g. period=-1 or period=999999) from being created.
ALLOWED_PERIODS = frozenset({15, 30, 60, 120, 180, 300, 600, 900, 1800, 3600})

# FIX (DEAD-CODE-2026-07-21): removed DEFAULT_PAYOUT_FLOOR_REAL,
# DEFAULT_PAYOUT_FLOOR_OTC, DEFAULT_SIGNAL_DELAY_SEC, ZONE_LOSS_GUARD —
# all four were never imported anywhere. feed.py and sim_feed.py read
# these values directly from os.environ.get(...) with their own hardcoded
# defaults, and ZONE_LOSS_GUARD is defined locally in both feed.py and
# sim_feed.py.
