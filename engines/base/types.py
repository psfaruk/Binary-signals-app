"""engines/base/types.py — Shared type definitions for BOTH engines."""
from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["CALL", "PUT", "NEUTRAL"]
SignalType = Literal["REVERSAL", "CONTINUATION"]
ReliabilityTier = Literal[
    "PATTERN", "LEVEL", "CANDLE", "MICRO",
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
    """One module's prediction output."""
    module_name: str
    direction: Direction
    score: int
    confidence: int
    signal_type: SignalType
    reliability: ReliabilityTier
    group: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class MarketContext:
    """Shared market context computed ONCE per candle close."""
    regime: dict[str, object]
    atr: float            # Average True Range
    stats: dict[str, object]
    key_levels: list[dict]
    level_confluence: dict[str, object]
    ema9: float
    ema21: float
    vol_pct: float        # volatility ratio (current ATR / historical ATR)
    closes: list[float]   # list of close prices (for indicators)
