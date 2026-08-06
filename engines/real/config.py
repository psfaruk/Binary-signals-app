"""engines/real/config.py — AUTO-CALIBRATED CONFIG v4 (2026-08-03)."""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from core.constants import REAL_MODULES as _MODULES

# Reliability tier multipliers
RELIABILITY = {
    "PATTERN": 1.2, "LEVEL": 1.0, "CANDLE": 1.0, "MICRO": 0.7,
}

# DEFAULT_WEIGHTS — for pairs with insufficient data
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.0, "pattern": 1.0, "key_level": 0.8,
    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5,
}

# Per-pair calibrated weights (auto-generated from live data)
PAIR_CONFIGS = {
    "AUDUSD": {
        "profile": "calibrated",
        "description": "Calibrated: 56.0% win (n=84)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.0, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "EURUSD": {
        "profile": "calibrated",
        "description": "Calibrated: 50.8% win (n=189)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.0, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
}

weight_adapter = PairWeightAdapter(
    pair_configs=PAIR_CONFIGS,
    default_weights=DEFAULT_WEIGHTS,
)

CONFIG = BlenderConfig(
    reliability=RELIABILITY,
    weight_adapter=weight_adapter,
    module_names=_MODULES,
    engine_name="real",
)
