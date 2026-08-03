"""
engines/base/modules/__init__.py — Package marker for shared prediction modules.

The 4 prediction modules live here:
  - candle_reaction
  - running_tick
  - pattern
  - key_level

FIX (MODULE-PRUNE-2026-08-03): removed indicator, otc_pattern, trend_follow
modules. Both OTC and Real engines now run the same 4 shared modules. The
engine-specific 6th module concept has been removed.
"""
