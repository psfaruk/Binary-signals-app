"""engines/otc/config.py — AUTO-CALIBRATED CONFIG (15-pair allowlist enforced).

FIX (PAIR-ALLOWLIST-2026-08-07): trimmed PAIR_CONFIGS to EXACTLY the 11 OTC
pairs in core.constants.ALLOWED_PAIRS_OTC. Previously had 26 entries (12
calibrated + 14 default) — 14 were for pairs the user never trades, and 5
calibrated entries (EURUSD_otc, USDJPY_otc, USDCHF_otc, AUDUSD_otc, USDARS_otc)
were for pairs REMOVED from the allowlist. Those phantom configs caused
confusion in stats and adaptation_status. Now: only 11 pairs, all matching
the allowlist. Adding a pair = edit core/constants.py + add entry here.
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from core.constants import OTC_MODULES as _MODULES, ALLOWED_PAIRS_OTC

# Reliability tier multipliers
RELIABILITY = {
    "PATTERN": 1.4, "LEVEL": 1.0, "CANDLE": 1.0, "MICRO": 0.7,
}

# DEFAULT_WEIGHTS — for pairs with insufficient data
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.0, "pattern": 1.0, "key_level": 0.8,
    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5,
}

# Per-pair calibrated weights (auto-generated from live data).
# Only the 11 OTC pairs in the allowlist. Pairs not here use DEFAULT_WEIGHTS.
PAIR_CONFIGS = {
    "USDZAR_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "BRLUSD_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 44.3% win (n=318)",
        "weights": {"candle_reaction": 0.5, "pattern": 0.5, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDIDR_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 48.2% win (n=371)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.0, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "NZDUSD_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "USDCOP_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 44.8% win (n=288)",
        "weights": {"candle_reaction": 0.5, "pattern": 0.5, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDBDT_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 45.4% win (n=306)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.0, "key_level": 0.5,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDPKR_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 54.7% win (n=254)",
        "weights": {"candle_reaction": 1.5, "pattern": 1.0, "key_level": 1.5,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDMXN_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "USDDZD_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 52.2% win (n=467)",
        "weights": {"candle_reaction": 1.8, "pattern": 0.5, "key_level": 1.5,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDINR_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 56.1% win (n=196)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.8, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDPHP_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
}

# SAFETY CHECK: ensure PAIR_CONFIGS keys match the allowlist exactly.
_config_keys = set(PAIR_CONFIGS.keys())
_allowlist_keys = set(ALLOWED_PAIRS_OTC)
assert _config_keys == _allowlist_keys, (
    f"engines/otc/config.py PAIR_CONFIGS keys mismatch ALLOWED_PAIRS_OTC. "
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
    engine_name="otc",
)
