"""
Type definitions for the prediction engine package.

All modules return ModuleResult objects. The blender combines them
into a final prediction. MarketContext is computed ONCE and shared
across all modules to avoid redundant computation.
"""
from dataclasses import dataclass, field
from typing import Literal, Optional

Direction = Literal["CALL", "PUT", "NEUTRAL"]
SignalType = Literal["REVERSAL", "CONTINUATION"]
ReliabilityTier = Literal["PATTERN", "STAT", "LEVEL", "CANDLE", "MICRO", "INDICATOR", "OTC"]


@dataclass
class ModuleResult:
    """One module's prediction output.

    Attributes:
        module_name: which module produced this (e.g. "candle_reaction")
        direction: CALL / PUT / NEUTRAL
        score: net score magnitude (always positive; direction encodes sign)
        confidence: 0-100 (module's own confidence in its vote)
        signal_type: REVERSAL or CONTINUATION (used for regime weighting)
        reliability: tier key for weight multiplier
        group: correlation group (BODY, WICK, PATTERN_*, LEVEL, STAT, MICRO, OTC, INDICATOR)
        reasons: list of human-readable reason strings
    """
    module_name: str
    direction: Direction
    score: int
    confidence: int
    signal_type: SignalType
    reliability: ReliabilityTier
    group: str
    reasons: list = field(default_factory=list)


@dataclass
class MarketContext:
    """Shared market context computed ONCE per candle close.

    Passed to every module so they don't recompute regime/ATR/stats.
    """
    regime: dict          # classify_market_regime output
    atr: float            # Average True Range
    stats: dict           # compute_statistical_edge output
    key_levels: list      # find_key_levels output
    level_confluence: dict  # check_level_confluence output
    ema9: float
    ema21: float
    vol_pct: float        # volatility ratio (current ATR / historical ATR)
    closes: list          # list of close prices (for indicators)


# Reliability tier multipliers — applied AFTER regime weighting.
# Higher tier = more trustworthy signal.
RELIABILITY = {
    "PATTERN":   1.5,   # multi-candle patterns (highest conviction)
    "STAT":      1.3,   # statistical edge (Z-score, rarity)
    "LEVEL":     1.3,   # key S/R level confluence
    "INDICATOR": 1.0,   # technical indicators (RSI, MACD, etc.)
    "CANDLE":    1.0,   # single-candle signals (baseline)
    "OTC":       1.2,   # OTC-specific patterns (slight bonus)
    "MICRO":     0.6,   # tick microstructure (single data source, noisy)
}
