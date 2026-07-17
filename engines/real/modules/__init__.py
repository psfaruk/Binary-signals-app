"""REAL engine modules package — 6 independent prediction modules.

Module 6 is `trend_follow` (NOT `otc_pattern` like the OTC engine).
Real markets trend harder than OTC, so we detect momentum continuation
rather than mean-reversion.

Modules are imported as submodules (NOT function aliases) so the blender
can do `from engines.real.modules import candle_reaction as mod_candle`
and then call `mod_candle.analyze(candles, ctx)`.
"""
