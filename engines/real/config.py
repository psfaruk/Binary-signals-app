"""engines/real/config.py — AUTO-CALIBRATED CONFIG v3 (2026-08-03)

Generated from live Railway brain data (~1280 graded signals).
Per-pair per-module weights calibrated against actual win rates.

Calibration methodology (DEEP_v3, 2026-08-03):
- Pairs with < 40% win rate (and >= 8 samples) -> DISABLED entirely
- Per-module accuracy thresholds:
  - < 35%  -> weight = 0.1  (DISABLED)
  - 35-45% -> weight = 0.5  (DAMPENED)
  - 45-55% -> weight = 1.0  (BASELINE)
  - 55-65% -> weight = 1.5  (BOOSTED)
  - >= 65% -> weight = 1.8  (STRONG BOOST)
- Direction-specific patterns documented in comments

Source: /api/brain/learning on Railway production
Date: 2026-08-03
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from engines.base.modules.trend_follow import analyze as _trend_follow_analyze
from core.constants import REAL_MODULES as _MODULES


# ── Reliability tier multipliers (data-driven) ──────────────────────────
# USER FIX #5 (2026-08-03): LEVEL reliability 0.8 -> 1.0.
# Restored to let the per-pair weight be the sole dampener/booster
# (removes the double-dampen issue from BUG #8 in the source audit).
RELIABILITY = {
    "PATTERN":   1.0,
    "LEVEL":   1.0,   # was 0.8 — restored
    "TREND":   1.0,
    "INDICATOR":   0.9,
    "CANDLE":   1.0,
    "MICRO":   0.7,
}


# ── DEFAULT_WEIGHTS — for pairs with insufficient data (< 8 samples) ──
DEFAULT_WEIGHTS = {
    "candle_reaction":   1.0,
    "running_tick":   1.0,
    "pattern":   1.0,
    "indicator":   0.9,
    "key_level":   0.8,
    "trend_follow":   0.1,
}


# ── PER-PAIR CALIBRATED WEIGHTS (auto-generated from live data) ────────
PAIR_CONFIGS = {
    "AUDUSD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=46%) (n=28)
            "running_tick":   1.5,   # boost (acc=65% >= 55.0%) (n=34)
            "pattern":   1.0,   # baseline (acc=55%) (n=22)
            "indicator":   1.0,   # baseline (no data)
            "key_level":   1.0,   # baseline (no data)
            "trend_follow":   1.0,   # baseline (no data)
        },
        "description": "Calibrated: 56.0% win (n=84)",
    },
    "EURUSD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=51%) (n=49)
            "running_tick":   1.0,   # baseline (acc=53%) (n=60)
            "pattern":   1.0,   # baseline (acc=48%) (n=46)
            "indicator":   1.0,   # baseline (no data)
            "key_level":   1.0,   # baseline (acc=50%) (n=34)
            "trend_follow":   1.0,   # baseline (no data)
        },
        "description": "Calibrated: 50.8% win (n=189)",
    },
}


# ── BlenderConfig assembly ─────────────────────────────────────────────
weight_adapter = PairWeightAdapter(
    pair_configs=PAIR_CONFIGS,
    default_weights=DEFAULT_WEIGHTS,
)

CONFIG = BlenderConfig(
    module_6_name="trend_follow",
    module_6_fn=_trend_follow_analyze,
    reliability=RELIABILITY,
    weight_adapter=weight_adapter,
    module_names=_MODULES,
    engine_name="real",
)
