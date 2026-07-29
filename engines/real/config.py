"""
engines/real/config.py — DATA-DRIVEN CALIBRATED CONFIG (2026-07-29)

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
from engines.base.modules.trend_follow import analyze as _trend_follow_analyze
from core.constants import REAL_MODULES


# ── Reliability tier multipliers ────────────────────────────────────────
RELIABILITY = {
    "PATTERN":   1.5,
    "LEVEL":     1.3,
    "TREND":     1.0,
    "INDICATOR": 1.4,   # BOOSTED from 1.2 — indicator is best module globally (51.3%)
    "CANDLE":    0.9,   # DAMPENED from 1.0 — candle_reaction is worst module (47.1%)
    "MICRO":     0.7,   # Real-market ticks more meaningful than OTC
}


# ── DEFAULT_WEIGHTS ────────────────────────────────────────────────────
# trend_follow kept at 0.1 — 0 graded signals in production (effectively dead)
DEFAULT_WEIGHTS = {
    "candle_reaction": 0.9,
    "running_tick":    1.0,
    "pattern":         1.1,
    "indicator":       1.4,
    "key_level":       1.0,
    "trend_follow":    0.1,
}


# ── DISABLED PAIRS — win rate < 45%, do not generate signals ────────────
# These pairs are removed from PAIR_CONFIGS. The feed will not stream them.
#   EURUSD: 43.8% win rate (n=77) — win rate 43.8% < 45.0% threshold
#   USDCAD: 39.4% win rate (n=39) — win rate 39.4% < 45.0% threshold
#   CHFJPY: 43.9% win rate (n=42) — win rate 43.9% < 45.0% threshold
#   EURCHF: 30.6% win rate (n=41) — win rate 30.6% < 45.0% threshold
#   AUDCAD: 41.0% win rate (n=40) — win rate 41.0% < 45.0% threshold


# ── PER-PAIR CALIBRATED WEIGHTS ────────────────────────────────────────
PAIR_CONFIGS = {
    "AUDCHF": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=47.5%)
            "running_tick":   1.0,   # baseline (samples=2<10)
            "pattern":   0.1,   # DISABLED (acc=39.1% < 45.0%)
            "indicator":   1.8,   # STRONG BOOST (acc=65.4% >= 60%)
            "key_level":   1.0,   # baseline (acc=50.0%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 58.5% win rate (n=97)",
    },
    "AUDJPY": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=42.7% < 45.0%)
            "running_tick":   1.0,   # baseline (samples=1<10)
            "pattern":   1.5,   # boost (acc=56.3% >= 55.0%)
            "indicator":   1.5,   # boost (acc=57.9% >= 55.0%)
            "key_level":   0.1,   # DISABLED (acc=44.6% < 45.0%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 54.1% win rate (n=116)",
    },
    "AUDUSD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=42.6% < 45.0%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.1,   # baseline (acc=50.0%)
            "indicator":   1.4,   # baseline (acc=52.0%)
            "key_level":   0.1,   # DISABLED (acc=39.4% < 45.0%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 48.8% win rate (n=85)",
    },
    "CADJPY": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=49.0%)
            "running_tick":   1.0,   # baseline (samples=1<10)
            "pattern":   1.1,   # baseline (acc=50.0%)
            "indicator":   0.1,   # DISABLED (acc=44.4% < 45.0%)
            "key_level":   0.1,   # DISABLED (acc=39.5% < 45.0%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 53.3% win rate (n=120)",
    },
    "EURAUD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=35.9% < 45.0%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.5,   # boost (acc=58.8% >= 55.0%)
            "indicator":   1.4,   # baseline (acc=53.3%)
            "key_level":   1.0,   # baseline (acc=45.5%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 53.5% win rate (n=44)",
    },
    "EURCAD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=41.7% < 45.0%)
            "running_tick":   1.0,   # baseline (samples=2<10)
            "pattern":   1.8,   # STRONG BOOST (acc=65.6% >= 60%)
            "indicator":   0.1,   # DISABLED (acc=44.0% < 45.0%)
            "key_level":   0.1,   # DISABLED (acc=44.1% < 45.0%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 51.7% win rate (n=121)",
    },
    "EURGBP": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=52.8%)
            "running_tick":   1.0,   # baseline (samples=4<10)
            "pattern":   0.1,   # DISABLED (acc=38.1% < 45.0%)
            "indicator":   1.4,   # baseline (acc=48.6%)
            "key_level":   1.0,   # baseline (acc=49.2%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 57.9% win rate (n=109)",
    },
    "EURJPY": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=42.6% < 45.0%)
            "running_tick":   1.0,   # baseline (samples=2<10)
            "pattern":   1.5,   # boost (acc=56.8% >= 55.0%)
            "indicator":   1.5,   # boost (acc=56.5% >= 55.0%)
            "key_level":   1.0,   # baseline (acc=51.3%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 47.2% win rate (n=75)",
    },
    "GBPAUD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.9,   # baseline (acc=52.6%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.5,   # boost (acc=57.1% >= 55.0%)
            "indicator":   1.4,   # baseline (acc=53.3%)
            "key_level":   1.0,   # baseline (acc=50.0%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 50.9% win rate (n=55)",
    },
    "GBPCAD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=44.6% < 45.0%)
            "running_tick":   1.0,   # baseline (samples=1<10)
            "pattern":   1.1,   # baseline (acc=52.6%)
            "indicator":   0.1,   # DISABLED (acc=42.6% < 45.0%)
            "key_level":   1.8,   # STRONG BOOST (acc=67.6% >= 60%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 50.0% win rate (n=82)",
    },
    "GBPUSD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=42.5% < 45.0%)
            "running_tick":   1.0,   # baseline (samples=3<10)
            "pattern":   1.5,   # boost (acc=59.4% >= 55.0%)
            "indicator":   1.5,   # boost (acc=57.7% >= 55.0%)
            "key_level":   0.1,   # DISABLED (acc=42.0% < 45.0%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 60.5% win rate (n=125)",
    },
    "USDCHF": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=41.2% < 45.0%)
            "running_tick":   1.0,   # baseline (samples=0<10)
            "pattern":   1.5,   # boost (acc=55.0% >= 55.0%)
            "indicator":   1.4,   # baseline (acc=47.2%)
            "key_level":   1.0,   # baseline (acc=47.2%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 59.8% win rate (n=99)",
    },
    "USDJPY": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=36.5% < 45.0%)
            "running_tick":   1.0,   # baseline (samples=1<10)
            "pattern":   1.1,   # baseline (acc=53.1%)
            "indicator":   1.5,   # boost (acc=57.6% >= 55.0%)
            "key_level":   0.1,   # DISABLED (acc=42.6% < 45.0%)
            "trend_follow":   0.1,   # baseline (samples=0<10)
        },
        "description": "Calibrated: 47.0% win rate (n=95)",
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
    module_6_name="trend_follow",
    module_6_fn=_trend_follow_analyze,
    reliability=RELIABILITY,
    weight_adapter=weight_adapter,
    module_names=REAL_MODULES,
    engine_name="real",
)
