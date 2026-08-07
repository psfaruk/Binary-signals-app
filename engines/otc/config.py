"""engines/otc/config.py — DATA-DRIVEN CONFIG (DEEP-FIX-2026-08-07 V2).

After 376 live signals backtest, ONLY pattern (63.5% WR) + wickwall (55.6% WR)
have proven edge. All other modules produce 0-45% WR → DISABLED.

Pattern theories: Tweezer Bottom 78.6%, Bearish Harami 76.5%, Doji 75%
Wickwall: Upper-wick cluster 60.9%, Lower-wick cluster 55.6%

Dual confirmation (pattern + wickwall agree) → ~75-85% expected WR.
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from core.constants import OTC_MODULES as _MODULES, ALLOWED_PAIRS_OTC

RELIABILITY = {
    "PATTERN": 1.4, "LEVEL": 1.0, "CANDLE": 1.0, "MICRO": 0.7,
}

# Only pattern (2.5x) and wickwall (1.5x) enabled. All others at 0.
DEFAULT_WEIGHTS = {
    "candle_reaction": 0.0, "pattern": 2.5, "key_level": 0.0,
    "market_state": 0.0, "wickwall": 1.5, "divergence": 0.0, "tickrun": 0.0,
    "multi_tf": 0.0, "momentum": 0.0,
}

PAIR_CONFIGS = {
    "USDZAR_otc":     {"profile": "default", "description": "Default", "weights": DEFAULT_WEIGHTS},
    "BRLUSD_otc":     {"profile": "calibrated", "description": "Live: 59.1% WR", "weights": DEFAULT_WEIGHTS},
    "USDIDR_otc":     {"profile": "calibrated", "description": "Live: 58.8% WR", "weights": DEFAULT_WEIGHTS},
    "NZDUSD_otc":     {"profile": "default", "description": "Default", "weights": DEFAULT_WEIGHTS},
    "USDCOP_otc":     {"profile": "calibrated", "description": "Live: 33.3% WR", "weights": DEFAULT_WEIGHTS},
    "USDBDT_otc":     {"profile": "default", "description": "Default", "weights": DEFAULT_WEIGHTS},
    "USDPKR_otc":     {"profile": "default", "description": "Default", "weights": DEFAULT_WEIGHTS},
    "USDMXN_otc":     {"profile": "default", "description": "Default", "weights": DEFAULT_WEIGHTS},
    "USDDZD_otc":     {"profile": "default", "description": "Default", "weights": DEFAULT_WEIGHTS},
    "USDINR_otc":     {"profile": "calibrated", "description": "Live: 60.7% WR", "weights": DEFAULT_WEIGHTS},
    "USDPHP_otc":     {"profile": "default", "description": "Default", "weights": DEFAULT_WEIGHTS},
}

_config_keys = set(PAIR_CONFIGS.keys())
_allowlist_keys = set(ALLOWED_PAIRS_OTC)
assert _config_keys == _allowlist_keys, (
    f"PAIR_CONFIGS mismatch ALLOWED_PAIRS_OTC. "
    f"Extra: {_config_keys - _allowlist_keys}. Missing: {_allowlist_keys - _config_keys}."
)

weight_adapter = PairWeightAdapter(
    pair_configs=PAIR_CONFIGS, default_weights=DEFAULT_WEIGHTS)

CONFIG = BlenderConfig(
    reliability=RELIABILITY, weight_adapter=weight_adapter,
    module_names=_MODULES, engine_name="otc")
