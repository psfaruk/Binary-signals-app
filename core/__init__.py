"""
core/__init__.py — Package marker for the shared `core` library.

Exposes only `constants` as a re-exported submodule. Other modules
(analysis, microstructure, stats, time_patterns, auto_tune, brain,
algorithm_*) are imported lazily by their callers to avoid import-time
side effects (e.g. stats.py opening a DB connection at import time would
crash unit tests that don't have signals.db on disk).
"""
