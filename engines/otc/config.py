"""engines/otc/config.py — AUTO-CALIBRATED CONFIG v4 (2026-08-03)."""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from core.constants import OTC_MODULES as _MODULES

# Reliability tier multipliers
RELIABILITY = {
    "PATTERN": 1.4, "LEVEL": 1.0, "CANDLE": 1.0, "MICRO": 0.7,
}

# DEFAULT_WEIGHTS — for pairs with insufficient data
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.0, "pattern": 1.0, "key_level": 0.8,
    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5,
}

# Per-pair calibrated weights (auto-generated from live data)
PAIR_CONFIGS = {
    "AUDUSD_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 52.7% win (n=389)",
        "weights": {"candle_reaction": 1.0, "pattern": 0.5, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "BRLUSD_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 44.3% win (n=318)",
        "weights": {"candle_reaction": 0.5, "pattern": 0.5, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "EURUSD_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 49.7% win (n=314)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.0, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDARS_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 51.9% win (n=541)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.0, "key_level": 0.5,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDBDT_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 45.4% win (n=306)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.0, "key_level": 0.5,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDCHF_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 52.6% win (n=483)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.5, "key_level": 0.5,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDCOP_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 44.8% win (n=288)",
        "weights": {"candle_reaction": 0.5, "pattern": 0.5, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDDZD_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 52.2% win (n=467)",
        "weights": {"candle_reaction": 1.8, "pattern": 0.5, "key_level": 1.5,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDIDR_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 48.2% win (n=371)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.0, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDINR_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 56.1% win (n=196)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.8, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDJPY_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 53.6% win (n=491)",
        "weights": {"candle_reaction": 1.0, "pattern": 1.5, "key_level": 1.0,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    "USDPKR_otc": {
        "profile": "calibrated",
        "description": "Calibrated: 54.7% win (n=254)",
        "weights": {"candle_reaction": 1.5, "pattern": 1.0, "key_level": 1.5,
                    "market_state": 0.8, "wickwall": 0.5, "divergence": 0.5, "tickrun": 0.5}},
    # ─── Newly added OTC pairs (use default weights until calibrated) ───
    # FIX (CONFIG-DUP-2026-08-06): EURUSD_otc, USDJPY_otc, USDCHF_otc and
    # AUDUSD_otc were ALSO listed here as "default". In a Python dict literal
    # the later key wins, so those four pairs silently threw away the
    # calibrated weights defined above (e.g. USDJPY_otc's pattern weight 1.5
    # reverted to 1.0, USDDZD-style tuning lost) — the calibration block read
    # as if it were live but four of its twelve entries were dead. The
    # duplicates are removed; the calibrated blocks above are authoritative.
    "GBPUSD_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "USDCAD_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "NZDUSD_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "USDZAR_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "USDSGD_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "USDCNH_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "USDTHB_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "USDPHP_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "USDRUB_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "EURJPY_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "EURGBP_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "GBPJPY_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
    "EURAUD_otc": {
        "profile": "default",
        "description": "Default weights (awaiting calibration)",
        "weights": DEFAULT_WEIGHTS},
}

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
