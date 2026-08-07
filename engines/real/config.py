"""engines/real/config.py — DATA-DRIVEN CONFIG (DEEP-FIX-2026-08-07 V2).

Only pattern (63.5% WR) + wickwall (55.6% WR) enabled. All others disabled.
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from core.constants import REAL_MODULES as _MODULES, ALLOWED_PAIRS_REAL

RELIABILITY = {
    "PATTERN": 1.2, "LEVEL": 1.0, "CANDLE": 1.0, "MICRO": 0.7,
}

DEFAULT_WEIGHTS = {
    "candle_reaction": 0.0, "pattern": 2.5, "key_level": 0.0,
    "market_state": 0.0, "wickwall": 1.5, "divergence": 0.0, "tickrun": 0.0,
    "multi_tf": 0.0, "momentum": 0.0,
}

PAIR_CONFIGS = {
    "EURUSD": {"profile": "default", "description": "Default", "weights": DEFAULT_WEIGHTS},
    "EURGBP": {"profile": "default", "description": "Default", "weights": DEFAULT_WEIGHTS},
    "AUDUSD": {"profile": "default", "description": "Default", "weights": DEFAULT_WEIGHTS},
    "USDJPY": {"profile": "default", "description": "Default", "weights": DEFAULT_WEIGHTS},
}

_config_keys = set(PAIR_CONFIGS.keys())
_allowlist_keys = set(ALLOWED_PAIRS_REAL)
assert _config_keys == _allowlist_keys, (
    f"PAIR_CONFIGS mismatch. Extra: {_config_keys - _allowlist_keys}. Missing: {_allowlist_keys - _config_keys}."
)

weight_adapter = PairWeightAdapter(
    pair_configs=PAIR_CONFIGS, default_weights=DEFAULT_WEIGHTS)

CONFIG = BlenderConfig(
    reliability=RELIABILITY, weight_adapter=weight_adapter,
    module_names=_MODULES, engine_name="real")
