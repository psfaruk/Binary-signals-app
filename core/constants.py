"""
core/constants.py — Single source of truth for shared constants.

All modules that need the list of prediction modules MUST import from here.
Previously this list was duplicated in 4 places (db.py, server.py,
module_performance_report.py, static/index.html) and they had drifted out
of sync — `trend_follow` was missing from /api/stats but present in db.py.

Single source of truth prevents that drift.
"""
import os

# FIX (DEEP-AUDIT-2026-07-26 / F-08-01): centralised DB path resolution.
# Multiple modules (stats.py, auto_tune.py, time_patterns.py) each computed
# DB_PATH independently via `os.environ.get("DB_PATH", <repo-root>)`. The
# stats.py fallback hard-coded the relative path "signals.db" which opened
# the WRONG database when the CWD was not the project root (A-04 problem 64).
# Centralising here guarantees every caller resolves to the same path.
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "signals.db"),
)

# All prediction modules, in canonical order (matches blender.py pipeline).
# Used by:
#   - db.per_module_accuracy()        (parsing signal_log reasons)
#   - server.py /api/stats            (per-module win-rate report)
#   - module_performance_report.py    (CLI version of /api/stats)
#   - static/js/common.js             (frontend module breakdown display)
#   - engines/base/blender.py         (_module_breakdown helper)
#
# FIX (MODULE-PRUNE-2026-08-03): removed "indicator", "otc_pattern",
# "trend_follow" — these 3 modules have been deleted from the codebase.
# Both engines now run the same 4 shared modules.
MODULE_NAMES = (
    "candle_reaction",
    "running_tick",
    "pattern",
    "key_level",
)

# Human-readable display names for the UI.
MODULE_DISPLAY_NAMES = {
    "candle_reaction": "Candle Reaction",
    "running_tick":    "Running Tick",
    "pattern":         "Pattern",
    "key_level":       "Key Level",
}

# Modules used by each engine.
# FIX (MODULE-PRUNE-2026-08-03): both engines now use the same 4 modules.
# The engine-specific 6th module concept (otc_pattern / trend_follow) has
# been removed — those modules are deleted.
OTC_MODULES = (
    "candle_reaction", "running_tick", "pattern", "key_level",
)
REAL_MODULES = (
    "candle_reaction", "running_tick", "pattern", "key_level",
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

# ───────────────────────────────────────────────────────────────────────────
# FIX (DEEP-AUDIT-2026-07-26 / F-08-02): env-configurable magic numbers
# for auto_tune / stats / time_patterns. Previously these were hardcoded
# across multiple modules (A-04 problems 50, 62, 67, 218, 287, 322, 342,
# 477, 482) and could only be tuned by editing source. Centralising them
# here lets operators adjust thresholds without code changes; defaults
# preserve the prior behaviour so no existing deployment breaks.
# ───────────────────────────────────────────────────────────────────────────

# core/auto_tune.py — weight clamp + sample threshold + change-detection
AUTO_TUNE_MIN_SAMPLES = int(os.environ.get("AUTO_TUNE_MIN_SAMPLES", "20"))
AUTO_TUNE_MAX_WEIGHT = float(os.environ.get("AUTO_TUNE_MAX_WEIGHT", "1.5"))
AUTO_TUNE_MIN_WEIGHT = float(os.environ.get("AUTO_TUNE_MIN_WEIGHT", "0.1"))
AUTO_TUNE_WEIGHT_CHANGE_THRESHOLD = float(
    os.environ.get("AUTO_TUNE_WEIGHT_CHANGE_THRESHOLD", "0.01")
)
AUTO_TUNE_MAX_ROWS = int(os.environ.get("AUTO_TUNE_MAX_ROWS", "5000"))

# core/stats.py — sample window size
STATS_MAX_ROWS = int(os.environ.get("STATS_MAX_ROWS", "5000"))

# core/time_patterns.py — per-dimension sample thresholds
# Lookups require >= 30 samples (15 for the hour dimension, which spreads
# data across 24 buckets). Aligning the recompute default with the lookup
# threshold stops thousands of 3-29-sample rows from being stored but
# never consulted (A-04 problem 12).
TIME_PATTERN_MIN_SAMPLES = int(os.environ.get("TIME_PATTERN_MIN_SAMPLES", "30"))
TIME_PATTERN_MIN_SAMPLES_HOUR = int(
    os.environ.get("TIME_PATTERN_MIN_SAMPLES_HOUR", "15")
)
TIME_PATTERN_REGIME_MIN_SAMPLES = int(
    os.environ.get("TIME_PATTERN_REGIME_MIN_SAMPLES", "30")
)
TIME_PATTERN_TAG_MIN_SAMPLES = int(
    os.environ.get("TIME_PATTERN_TAG_MIN_SAMPLES", "30")
)
TIME_PATTERN_RECOMPUTE_MIN_SAMPLES = int(
    os.environ.get("TIME_PATTERN_RECOMPUTE_MIN_SAMPLES", "30")
)
TIME_PATTERN_CACHE_TTL = float(os.environ.get("TIME_PATTERNS_CACHE_TTL", "5"))
TIME_PATTERN_DAYS_WINDOW = int(os.environ.get("PATTERN_DAYS_WINDOW", "14"))
