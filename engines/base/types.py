"""
engines/base/types.py — Shared type definitions for BOTH engines.

The OTC and Real engines share the SAME ModuleResult and MarketContext
dataclasses. Only the RELIABILITY multipliers differ between them —
those live in `engines/otc/config.py` and `engines/real/config.py`.
"""
from dataclasses import dataclass, field
from typing import Literal

# FIX (DEEP-AUDIT-2026-07-26 / F-04-22): removed dead `"STAT"` option from
# `ReliabilityTier` — no module in engines/base/modules/ emits
# `reliability="STAT"`, and both real/config.py and otc/config.py no longer
# have a "STAT" key in their RELIABILITY dicts (removed per FIX comments at
# real/config.py:36-40 and otc/config.py:29-34). (Audit A-02 #28, A-06 #28.)
Direction = Literal["CALL", "PUT", "NEUTRAL"]
SignalType = Literal["REVERSAL", "CONTINUATION"]
ReliabilityTier = Literal[
    "PATTERN", "LEVEL", "CANDLE", "MICRO",
    # FIX (MODULE-PRUNE-2026-08-03): removed "INDICATOR", "OTC", "TREND"
    # — the indicator / otc_pattern / trend_follow modules have been deleted.
]

__all__ = [
    "Direction",
    "SignalType",
    "ReliabilityTier",
    "ModuleResult",
    "MarketContext",
]


@dataclass
class ModuleResult:
    """One module's prediction output.

    Attributes:
        module_name: which module produced this (e.g. "candle_reaction")
        direction: CALL / PUT / NEUTRAL
            (NOTE: modules always emit CALL or PUT; NEUTRAL is reserved for
            the blender's `_neutral(...)` return when no module fires — kept
            in the Literal for type compatibility with that return path.)
        score: net score magnitude (always positive; direction encodes sign).
            NOTE: typed as `int` because the blender rounds via
            `_round_half_up` before storing. Modules must return int; if a
            module computes a float, it must round before constructing the
            ModuleResult. (Audit A-02 #65.)
        confidence: 0-100 (module's own confidence in its vote)
        signal_type: REVERSAL or CONTINUATION (used for regime weighting)
        reliability: tier key for weight multiplier
        group: correlation group — actual groups emitted by the remaining
            4 modules include BODY, BODY_CONT, WICK, WICK_CONT, MICRO_SR,
            FIB, SR_FLIP, TRENDLINE, MICRO, LEVEL, PATTERN_REVERSAL,
            PATTERN_CONTINUATION.
            FIX (MODULE-PRUNE-2026-08-03): removed IND_* / TREND_* / OTC_*
            groups — the indicator / otc_pattern / trend_follow modules
            have been deleted from the codebase.
        reasons: list of human-readable reason strings
    """
    module_name: str
    direction: Direction
    score: int
    confidence: int
    signal_type: SignalType
    reliability: ReliabilityTier
    group: str
    # FIX (DEEP-AUDIT-2026-07-26 / F-04-24): typed as `list[str]` (was bare
    # `list`). (Audit A-02 #66, A-06 #46.)
    reasons: list[str] = field(default_factory=list)


@dataclass
class MarketContext:
    """Shared market context computed ONCE per candle close.

    Passed to every module so they don't recompute regime/ATR/stats.

    NOTE: The context is NOT frozen (mutable dataclass) because the blender
    mutates some ModuleResult fields (collapsed_wick.module_name / group at
    blender.py:187-188). Modules receiving `ctx` MUST NOT mutate it.
    (Audit A-06 #48 — frozen=True would require nested immutability work.)
    """
    # FIX (DEEP-AUDIT-2026-07-26 / F-04-25): typed `dict` and `list` fields
    # with parameters (was bare `dict` / `list`). (Audit A-02 #47, A-06 #47.)
    regime: dict[str, object]
    atr: float            # Average True Range
    stats: dict[str, object]
    key_levels: list[dict]
    level_confluence: dict[str, object]
    ema9: float
    ema21: float
    vol_pct: float        # volatility ratio (current ATR / historical ATR)
    closes: list[float]   # list of close prices (for indicators)
