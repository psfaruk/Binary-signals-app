"""
engines/base/modules/__init__.py — Package marker for shared prediction modules.

The 3 original prediction modules live here:
  - candle_reaction
  - pattern
  - key_level

Plus 4 newer modules added 2026-08-05: market_state, wickwall, divergence,
tickrun.

FIX (MODULE-PRUNE-2026-08-03): removed indicator, otc_pattern, trend_follow
modules. Both OTC and Real engines now run the same shared modules. The
engine-specific 6th module concept has been removed.

FIX (RUNNING-TICK-REMOVE-2026-08-05): removed running_tick.py — live
theory_votes data (12 sub-signals + composite, n=460-7742) confirmed no
measurable edge (48.7-51.4% win rate, Wilson 95% lower bound below
break-even in every case). See core/constants.py MODULE_NAMES comment.
"""
