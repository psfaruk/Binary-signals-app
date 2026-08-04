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

# ───────────────────────────────────────────────────────────────────────────
# THEORY-PRUNE-2026-08-04 (PHYSICAL DELETION)
# ───────────────────────────────────────────────────────────────────────────
# After 3 rounds of backtest-driven disabling (Round 1, 2, 3), 20 of 35
# theories were found net-harmful. They have now been PHYSICALLY DELETED
# from the source code (engines/base/modules/*, core/analysis.py) —
# not just disabled via `if False:` guards.
#
# The DISABLED_THEORIES frozenset and is_theory_disabled() helper have
# been removed since they are no longer needed.
#
# See git history (commits ff0f610, a730d22, 1e3c6dd, d69f25b) for the
# original disabling logic and backtest analysis.
#
# Active theories (15) now live exclusively in:
#   - engines/base/modules/candle_reaction.py (SIGNAL 6 only)
#   - engines/base/modules/key_level.py (Signals 1, 3, 6, 7)
#   - engines/base/modules/pattern.py (8 patterns)
#   - engines/base/modules/running_tick.py (Micro composite)


# ───────────────────────────────────────────────────────────────────────────
# CONSENSUS FILTER (CONSENSUS-FILTER-2026-08-04)
# ───────────────────────────────────────────────────────────────────────────
# When theories disagree (some vote CALL, some vote PUT on the same candle),
# the signal's win rate drops to 30.6% — 12.4pp worse than consensus signals
# (43.0%). This filter requires all module-groups to agree on direction
# before emitting a final CALL/PUT signal. If they disagree, the signal
# becomes NEUTRAL (no trade).
#
# Backtest results (383 signals, 3 rounds of pruning):
#   - Round 0 (no filter):                42.0% win rate (383 trades)
#   - Round 2 + soft consensus dampen:    49.5% win rate (186 trades)
#   - Round 3 + 100% consensus + min 2:   66.7% win rate (15 trades)
#
# The filter is intentionally aggressive — it converts most signals to
# NEUTRAL. To make it less aggressive, set CONSENSUS_FILTER_ENABLED=False
# (env var) or remove the check in blender.py.
#
# CONSENSUS_STRICT=1 (default after Round 3): disagreement → NEUTRAL
# CONSENSUS_STRICT=0: disagreement → confidence dampened (soft mode)
#
# CONSENSUS_MIN_PCT: minimum % of theories that must agree on direction.
#   - 100 = require unanimity (all theories same direction)
#   - 90  = allow 1 dissenter in 10
#   - 50  = simple majority
#
# CONSENSUS_MIN_GROUPS: minimum number of theory-groups required to vote
# in the same direction before a signal is emitted.
#   - 2 = require at least 2 theories to corroborate
#   - 3 = require at least 3 theories (very strict)
CONSENSUS_FILTER_ENABLED = os.environ.get("CONSENSUS_FILTER_ENABLED", "1") == "1"
CONSENSUS_STRICT = os.environ.get("CONSENSUS_STRICT", "1") == "1"
CONSENSUS_MIN_PCT = float(os.environ.get("CONSENSUS_MIN_PCT", "100"))
CONSENSUS_MIN_GROUPS = int(os.environ.get("CONSENSUS_MIN_GROUPS", "2"))


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
