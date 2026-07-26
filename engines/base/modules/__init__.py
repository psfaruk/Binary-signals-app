"""
engines/base/modules/__init__.py — Package marker for shared prediction modules.

All 7 prediction modules (5 shared + otc_pattern + trend_follow) live here.
The OTC and Real engines select which 6 modules to run via their config.py.

FIX (DEEP-AUDIT-2026-07-26 / F-10-34): all 7 modules audited by Agent A-05
and patched by Fix-Agent F-10. The trend_follow / candle_reaction / otc_pattern /
pattern / running_tick modules received defensive .get() defaults for
ctx.stats cold-start safety, dead-code removal (DISABLED signal blocks),
and standardized trend-strength thresholds (>= 0.5 moderate, > 0.7 strong).
No public API changes — module signatures unchanged.
"""
