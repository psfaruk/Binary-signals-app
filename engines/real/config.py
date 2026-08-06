"""engines/real/config.py — AUTO-CALIBRATED CONFIG (15-pair allowlist enforced).

FIX (PAIR-ALLOWLIST-2026-08-07): trimmed PAIR_CONFIGS to EXACTLY the 4 real
pairs in core.constants.ALLOWED_PAIRS_REAL. Previously had only 2 calibrated
entries (AUDUSD, EURUSD) — EURGBP and USDJPY used silent default fallback.
Now: all 4 pairs explicitly configured, matching the allowlist.
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from core.constants import REAL_MODULES as _MODULES, ALLOWED_PAIRS_REAL

# Reliability tier multipliers
RELIABILITY = {
    "PATTERN": 1.2, "LEVEL": 1.0, "CANDLE": 1.0, "MICRO": 0.7,
}

# DEFAULT_WEIGHTS — for pairs with insufficient data
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.0, "pattern": 1.0, "key_level": 0.8,
    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5,
}

# Per-pair calibrated weights for the 4 real pairs in the allowlist.
PAIR_CONFIGS = {
    "EURUSD": {
        "profile": "calibrated",
        "description": "Calibrated: 50.8% win (n=189)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.0, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "EURGBP": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "AUDUSD": {
        "profile": "calibrated",
        "description": "Calibrated: 56.0% win (n=84)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.0, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDJPY": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
}

# SAFETY CHECK: ensure PAIR_CONFIGS keys match the allowlist exactly.
_config_keys = set(PAIR_CONFIGS.keys())
_allowlist_keys = set(ALLOWED_PAIRS_REAL)
assert _config_keys == _allowlist_keys, (
    f"engines/real/config.py PAIR_CONFIGS keys mismatch ALLOWED_PAIRS_REAL. "
    f"Extra in config: {_config_keys - _allowlist_keys}. "
    f"Missing from config: {_allowlist_keys - _config_keys}. "
    f"Edit core/constants.py AND this file together."
)

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
