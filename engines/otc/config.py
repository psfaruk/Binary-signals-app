"""engines/otc/config.py — AUTO-CALIBRATED CONFIG v4 (2026-08-03)

Generated from live Railway brain data (~1280 graded signals).
Per-pair per-module weights calibrated against actual win rates.

FIX (MODULE-PRUNE-2026-08-03): removed `indicator` and `otc_pattern`
modules entirely. Both engines now run the same 4 shared modules:
candle_reaction, running_tick, pattern, key_level.

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
from core.constants import OTC_MODULES as _MODULES


# ── Reliability tier multipliers (data-driven) ──────────────────────────
# FIX (MODULE-PRUNE-2026-08-03): removed "INDICATOR", "OTC", "TREND" tiers —
# the corresponding modules have been deleted.
RELIABILITY = {
    "PATTERN":   1.3,
    "LEVEL":     1.0,
    "CANDLE":    1.0,
    "MICRO":     0.7,
}


# ── DEFAULT_WEIGHTS — for pairs with insufficient data (< 8 samples) ──
# FIX (MODULE-PRUNE-2026-08-03): removed indicator + otc_pattern entries.
DEFAULT_WEIGHTS = {
    "candle_reaction":   1.0,
    "running_tick":      1.0,
    "pattern":           1.0,
    "key_level":         0.8,
}


# ── PER-PAIR CALIBRATED WEIGHTS (auto-generated from live data) ────────
# FIX (MODULE-PRUNE-2026-08-03): removed indicator + otc_pattern from
# every pair's weights dict. Comments for those entries are also removed.
PAIR_CONFIGS = {
    "AUDUSD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=54%) (n=80)
            "running_tick":      1.0,   # baseline (acc=54%) (n=90)
            "pattern":           0.5,   # dampened (acc=44% < 45.0%) (n=64)
            "key_level":         1.0,   # baseline (acc=51%) (n=49)
        },
        "description": "Calibrated: 52.7% win (n=389)",
    },
    "BRLUSD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.5,   # dampened (acc=45% < 45.0%) (n=58)
            "running_tick":      1.0,   # baseline (acc=46%) (n=71)
            "pattern":           0.5,   # dampened (acc=44% < 45.0%) (n=57)
            "key_level":         1.0,   # baseline (acc=47%) (n=32)
        },
        "description": "Calibrated: 44.3% win (n=318)",
    },
    "EURUSD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=48%) (n=66)
            "running_tick":      1.0,   # baseline (acc=54%) (n=69)
            "pattern":           1.0,   # baseline (acc=48%) (n=56)
            "key_level":         1.0,   # baseline (acc=49%) (n=41)
        },
        "description": "Calibrated: 49.7% win (n=314)",
    },
    "USDARS_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=50%) (n=114)
            "running_tick":      1.0,   # baseline (acc=53%) (n=123)
            "pattern":           1.0,   # baseline (acc=53%) (n=97)
            "key_level":         0.5,   # dampened (acc=40% < 45.0%) (n=67)
        },
        "description": "Calibrated: 51.9% win (n=541)",
    },
    "USDBDT_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (no data)
            "running_tick":      1.0,   # baseline (acc=53%) (n=105)
            "pattern":           1.0,   # baseline (acc=49%) (n=87)
            "key_level":         0.5,   # dampened (acc=43% < 45.0%) (n=46)
        },
        "description": "Calibrated: 45.4% win (n=306)",
    },
    "USDCHF_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=49%) (n=97)
            "running_tick":      1.0,   # baseline (acc=52%) (n=120)
            "pattern":           1.5,   # boost (acc=57% >= 55.0%) (n=87)
            "key_level":         0.5,   # dampened (acc=43% < 45.0%) (n=58)
        },
        "description": "Calibrated: 52.6% win (n=483)",
    },
    "USDCOP_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.5,   # dampened (acc=44% < 45.0%) (n=59)
            "running_tick":      0.5,   # dampened (acc=44% < 45.0%) (n=73)
            "pattern":           0.5,   # dampened (acc=40% < 45.0%) (n=20)
            "key_level":         1.0,   # baseline (acc=47%) (n=32)
        },
        "description": "Calibrated: 44.8% win (n=288)",
    },
    "USDDZD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.8,   # STRONG (acc=67% >= 65.0%) (n=87)
            "running_tick":      0.5,   # dampened (acc=44% < 45.0%) (n=109)
            "pattern":           0.5,   # dampened (acc=39% < 45.0%) (n=90)
            "key_level":         1.5,   # boost (acc=65% >= 55.0%) (n=57)
        },
        "description": "Calibrated: 52.2% win (n=467)",
    },
    "USDIDR_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=50%) (n=68)
            "running_tick":      1.0,   # baseline (acc=55%) (n=88)
            "pattern":           1.0,   # baseline (acc=50%) (n=66)
            "key_level":         1.0,   # baseline (acc=49%) (n=47)
        },
        "description": "Calibrated: 48.2% win (n=371)",
    },
    "USDINR_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=49%) (n=41)
            "running_tick":      1.0,   # baseline (acc=53%) (n=53)
            "pattern":           1.8,   # STRONG (acc=65% >= 65.0%) (n=40)
            "key_level":         1.0,   # baseline (no data)
        },
        "description": "Calibrated: 56.1% win (n=196)",
    },
    "USDJPY_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=51%) (n=96)
            "running_tick":      1.0,   # baseline (acc=54%) (n=114)
            "pattern":           1.5,   # boost (acc=57% >= 55.0%) (n=88)
            "key_level":         1.0,   # baseline (acc=50%) (n=60)
        },
        "description": "Calibrated: 53.6% win (n=491)",
    },
    "USDPKR_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.5,   # boost (acc=59% >= 55.0%) (n=46)
            "running_tick":      1.0,   # baseline (acc=47%) (n=49)
            "pattern":           1.0,   # baseline (acc=50%) (n=46)
            "key_level":         1.5,   # boost (acc=59% >= 55.0%) (n=32)
        },
        "description": "Calibrated: 54.7% win (n=254)",
    },
}


# ── BlenderConfig assembly ─────────────────────────────────────────────
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
