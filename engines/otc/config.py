"""
engines/otc/config.py — DATA-DRIVEN CALIBRATED CONFIG (2026-07-29)

Generated from 2,426 live signals sampled from Railway production DB.
Per-pair per-module weights calibrated against actual win rates.

Calibration methodology:
- Pairs with < 45% win rate (and >= 20 samples) → DISABLED entirely
- Per-module per-direction accuracy computed:
  - < 45% accuracy (n >= 10) → weight = 0.1 (disabled)
  - 45-55% accuracy → baseline weight
  - 55-60% accuracy → weight = 1.5 (boosted)
  - >= 60% accuracy → weight = 1.8 (strongly boosted)

Source data: scripts/pair_module_matrix.json
Decisions log: scripts/calibration_decisions.json

Total signals analyzed: 2,426 (across 37 pairs)
Date: 2026-07-29
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from engines.base.modules.otc_pattern import analyze as _otc_pattern_analyze
from core.constants import OTC_MODULES


# ── Reliability tier multipliers ────────────────────────────────────────
RELIABILITY = {
    "PATTERN":   1.5,
    "LEVEL":     1.3,
    "TREND":     1.0,
    "INDICATOR": 1.4,   # BOOSTED from 1.2 — indicator is best module globally (51.3%)
    "CANDLE":    0.9,   # DAMPENED from 1.0 — candle_reaction is worst module (47.1%)
    "MICRO":     0.6,
}


# ── DEFAULT_WEIGHTS — used for pairs with insufficient data (< 20 samples) ──
# These weights reflect GLOBAL module accuracy across all OTC pairs:
#   indicator: 51.3% → 1.4 (best, boosted)
#   pattern:   49.9% → 1.1
#   key_level: 49.6% → 1.0
#   otc_pattern: 49.6% → 1.2 (OTC-specific, slightly boosted)
#   candle_reaction: 47.1% → 0.9 (worst, dampened)
#   running_tick: insufficient data → 1.0 (neutral)
DEFAULT_WEIGHTS = {
    "candle_reaction": 0.9,
    "running_tick":    1.0,
    "pattern":         1.1,
    "indicator":       1.4,
    "key_level":       1.0,
    "otc_pattern":     1.2,
}


# ── DISABLED PAIRS — win rate < 45%, do not generate signals ────────────
# These pairs are removed from PAIR_CONFIGS. The feed will not stream them
# (or if streamed, predictions return NEUTRAL via insufficient-data path).
# Disabled based on >= 20 graded signals:
#   USDIDR_otc: 36.0% win rate (n=50) — win rate 36.0% < 45.0% threshold


# ── PER-PAIR CALIBRATED WEIGHTS ────────────────────────────────────────
# Each weight is derived from actual win rate data.
# See scripts/calibration_decisions.json for full provenance.
PAIR_CONFIGS = {
    "BRLUSD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=46.3%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.8,   # STRONG BOOST (acc=60.9% >= 60%)
            "indicator":   1.5,   # boost (acc=55.9% >= 55.0%)
            "key_level":   1.5,   # boost (acc=56.8% >= 55.0%)
            "otc_pattern":   1.2,   # baseline (acc=47.5%)
        },
        "description": "Calibrated: 46.2% win rate (n=79)",
    },
    "CADCHF_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=54.3%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   0.1,   # DISABLED (acc=35.4% < 45.0%)
            "indicator":   1.5,   # boost (acc=55.8% >= 55.0%)
            "key_level":   1.0,   # baseline (acc=50.9%)
            "otc_pattern":   1.5,   # boost (acc=58.5% >= 55.0%)
        },
        "description": "Calibrated: 54.1% win rate (n=99)",
    },
    "EURNZD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=50.6%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.1,   # baseline (acc=46.6%)
            "indicator":   1.4,   # baseline (acc=47.1%)
            "key_level":   1.8,   # STRONG BOOST (acc=60.3% >= 60%)
            "otc_pattern":   1.2,   # baseline (acc=49.3%)
        },
        "description": "Calibrated: 57.3% win rate (n=89)",
    },
    "NZDCAD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=51.9%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.1,   # baseline (acc=52.6%)
            "indicator":   1.4,   # baseline (samples=6<10)
            "key_level":   1.0,   # baseline (acc=52.9%)
            "otc_pattern":   0.1,   # DISABLED (acc=38.1% < 45.0%)
        },
        "description": "Calibrated: 56.0% win rate (n=27)",
    },
    "NZDCHF_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=49.3%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.5,   # boost (acc=59.3% >= 55.0%)
            "indicator":   1.8,   # STRONG BOOST (acc=66.7% >= 60%)
            "key_level":   1.5,   # boost (acc=56.5% >= 55.0%)
            "otc_pattern":   1.2,   # baseline (acc=47.4%)
        },
        "description": "Calibrated: 61.0% win rate (n=82)",
    },
    "NZDJPY_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=45.0%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.8,   # STRONG BOOST (acc=70.0% >= 60%)
            "indicator":   1.8,   # STRONG BOOST (acc=63.6% >= 60%)
            "key_level":   1.8,   # STRONG BOOST (acc=83.3% >= 60%)
            "otc_pattern":   1.2,   # baseline (acc=52.4%)
        },
        "description": "Calibrated: 68.2% win rate (n=23)",
    },
    "USDARS_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=47.1%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.1,   # baseline (acc=52.9%)
            "indicator":   1.4,   # baseline (acc=50.0%)
            "key_level":   0.1,   # DISABLED (acc=44.7% < 45.0%)
            "otc_pattern":   1.2,   # baseline (acc=46.2%)
        },
        "description": "Calibrated: 50.9% win rate (n=58)",
    },
    "USDBDT_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=51.8%)
            "running_tick":   1.0,   # baseline (samples=1<10)
            "pattern":   1.1,   # baseline (acc=48.3%)
            "indicator":   1.4,   # baseline (acc=51.4%)
            "key_level":   1.0,   # baseline (acc=47.3%)
            "otc_pattern":   1.8,   # STRONG BOOST (acc=60.2% >= 60%)
        },
        "description": "Calibrated: 53.5% win rate (n=102)",
    },
    "USDCOP_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=53.8%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   0.1,   # DISABLED (acc=44.0% < 45.0%)
            "indicator":   0.1,   # DISABLED (acc=43.2% < 45.0%)
            "key_level":   1.0,   # baseline (acc=51.9%)
            "otc_pattern":   1.2,   # baseline (acc=51.9%)
        },
        "description": "Calibrated: 53.3% win rate (n=90)",
    },
    "USDDZD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=37.5% < 45.0%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.1,   # baseline (acc=46.7%)
            "indicator":   0.1,   # DISABLED (acc=43.8% < 45.0%)
            "key_level":   1.0,   # baseline (acc=50.0%)
            "otc_pattern":   1.2,   # baseline (acc=46.7%)
        },
        "description": "Calibrated: 50.0% win rate (n=34)",
    },
    "USDMXN_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=47.5%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.1,   # baseline (acc=46.7%)
            "indicator":   1.5,   # boost (acc=55.6% >= 55.0%)
            "key_level":   1.0,   # baseline (acc=46.4%)
            "otc_pattern":   1.2,   # baseline (acc=48.1%)
        },
        "description": "Calibrated: 47.0% win rate (n=66)",
    },
    "USDPKR_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=53.6%)
            "running_tick":   1.0,   # baseline (samples=2<10)
            "pattern":   0.1,   # DISABLED (acc=41.3% < 45.0%)
            "indicator":   1.8,   # STRONG BOOST (acc=60.6% >= 60%)
            "key_level":   1.0,   # baseline (acc=49.4%)
            "otc_pattern":   1.2,   # baseline (acc=52.7%)
        },
        "description": "Calibrated: 53.7% win rate (n=111)",
    },
}


# ── BlenderConfig assembly ─────────────────────────────────────────────
# FIX (CALIBRATION-FIX-2026-07-29): expose `weight_adapter` as a module-level
# export. server.py's /api/stats endpoint imports `weight_adapter` directly
# to compute adaptation_status — the previous calibrated config only exposed
# `CONFIG`, causing ImportError → adaptation_error on Railway.
weight_adapter = PairWeightAdapter(
    pair_configs=PAIR_CONFIGS,
    default_weights=DEFAULT_WEIGHTS,
)

CONFIG = BlenderConfig(
    module_6_name="otc_pattern",
    module_6_fn=_otc_pattern_analyze,
    reliability=RELIABILITY,
    weight_adapter=weight_adapter,
    module_names=OTC_MODULES,
    engine_name="otc",
)
