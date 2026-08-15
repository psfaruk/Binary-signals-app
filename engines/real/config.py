"""engines/real/config.py — DATA-DRIVEN CONFIG (USER-AUG-2026 V3).

FIX (USER-AUG-2026): Activated 7 previously-dead modules + added 4 new
strategy modules with pair-specific weights based on web research
(Task 3 findings).
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from core.constants import REAL_MODULES as _MODULES, ALLOWED_PAIRS_REAL

RELIABILITY = {
    "PATTERN": 1.2, "LEVEL": 1.0, "CANDLE": 1.0, "MICRO": 0.7,
}

DEFAULT_WEIGHTS = {
    "candle_reaction": 1.0,
    "pattern":         3.0,
    "key_level":       1.5,
    "market_state":    0.8,
    "wickwall":        2.0,
    "divergence":      1.0,
    "tickrun":         0.8,
    "multi_tf":        1.2,
    "momentum":        1.5,
    "bollinger_rsi":   2.0,
    "stochastic":      1.5,
    "ema_ribbon":      1.2,
    "sr_bounce":       1.8,
}

# Real-pair strategy mapping (Task 3 research)
PAIR_CONFIGS = {
    "EURUSD": {
        "profile": "calibrated", "description": "EURUSD: BB+RSI+Engulfing primary (most liquid)",
        "weights": {**DEFAULT_WEIGHTS,
                    "bollinger_rsi": 2.5,
                    "sr_bounce": 2.2,
                    "pattern": 3.0,
                    "ema_ribbon": 1.0},
    },
    "EURGBP": {
        "profile": "calibrated", "description": "EURGBP: BB bounce primary (range-bound)",
        "weights": {**DEFAULT_WEIGHTS,
                    "bollinger_rsi": 2.5,
                    "stochastic": 1.8,
                    "ema_ribbon": 0.8},
    },
    "AUDUSD": {
        "profile": "calibrated", "description": "AUDUSD: balanced trend + BB",
        "weights": {**DEFAULT_WEIGHTS,
                    "ema_ribbon": 2.0,
                    "bollinger_rsi": 2.0,
                    "sr_bounce": 1.8},
    },
    "USDJPY": {
        "profile": "calibrated", "description": "USDJPY: trend-following primary (sustained trends)",
        "weights": {**DEFAULT_WEIGHTS,
                    "ema_ribbon": 2.5,
                    "momentum": 2.0,
                    "bollinger_rsi": 1.5},
    },
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
