"""
engines/real/config.py — DEEP CALIBRATED CONFIG v2 (2026-07-30)

Generated from 614 fresh signals sampled from Railway production.
Per-pair per-module weights calibrated against actual win rates.

Calibration methodology (DEEP_v2):
- Pairs with < 40% win rate (and >= 8 samples) → DISABLED entirely
- Per-module accuracy thresholds:
  - < 35% → weight = 0.1 (DISABLED)
  - 35-45% → weight = 0.5 (DAMPENED)
  - 45-55% → weight = 1.0 (BASELINE)
  - 55-65% → weight = 1.5 (BOOSTED)
  - >= 65% → weight = 1.8 (STRONG BOOST)
- Direction-specific patterns documented in comments

Source: scripts/deep_calibration_v2.json
Date: 2026-07-30
Total signals: 614 (OTC: 201, Real: 413)

KEY INSIGHT: Real engine modules perform WORSE than OTC:
  - pattern: 48.4% (vs OTC 55.8%)
  - indicator: 46.5% (vs OTC 50.0%)
  - key_level: 44.4% (vs OTC 52.9%)
  - candle_reaction: 48.9% (vs OTC 46.9%)
This is why Real signals have lower win rate (51.4% vs OTC 53.8%).
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from engines.base.modules.trend_follow import analyze as _trend_follow_analyze
from core.constants import REAL_MODULES


# ── Reliability tier multipliers (data-driven for Real) ─────────────────
# Real modules are weaker overall — dampened multipliers
RELIABILITY = {
    "PATTERN":   1.2,   # was 1.5 — Real pattern only 48.4%
    "LEVEL":     0.9,   # was 1.3 — Real key_level only 44.4%
    "TREND":     1.0,
    "INDICATOR": 0.9,   # was 1.4 — Real indicator only 46.5%
    "CANDLE":    0.9,
    "MICRO":     0.7,
}


# ── DEFAULT_WEIGHTS — for pairs with insufficient data ─────────────────
DEFAULT_WEIGHTS = {
    "candle_reaction": 0.9,
    "running_tick":    1.0,
    "pattern":         1.0,
    "indicator":       0.8,
    "key_level":       0.8,
    "trend_follow":    0.1,   # 0 graded signals — keep disabled
}


# ── DISABLED PAIRS — win rate < 40%, do not generate signals ────────────


# ── PER-PAIR CALIBRATED WEIGHTS ────────────────────────────────────────
PAIR_CONFIGS = {
    "AUDCAD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=26% < 35.0%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   0.5,   # dampened (acc=36% < 45.0%)
            "indicator":   1.5,   # boost (acc=56% >= 55.0%)
            "key_level":   1.0,   # baseline (acc=47%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 40.0% win (n=26)",
    },
    "AUDCHF": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.5,   # boost (acc=63% >= 55.0%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.8,   # STRONG (acc=67% >= 65.0%)
            "indicator":   0.5,   # dampened (acc=36% < 45.0%)
            "key_level":   1.0,   # baseline (acc=45%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 57.1% win (n=24)",
    },
    "AUDJPY": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (n=0<4)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.5,   # boost (acc=64% >= 55.0%)
            "indicator":   1.5,   # boost (acc=56% >= 55.0%)
            "key_level":   0.5,   # dampened (acc=44% < 45.0%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 54.5% win (n=22)",
    },
    "AUDUSD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (n=1<4)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.5,   # boost (acc=61% >= 55.0%) [PUT weak 29%, CALL strong 82%]
            "indicator":   0.5,   # dampened (acc=40% < 45.0%) [PUT weak 29%, CALL strong 67%]
            "key_level":   1.0,   # baseline (n=2<4)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 60.0% win (n=27)",
    },
    "CADJPY": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=48%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.0,   # baseline (acc=45%) [CALL weak 14%, PUT strong 62%]
            "indicator":   1.0,   # baseline (n=0<4)
            "key_level":   1.0,   # baseline (n=3<4)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 62.1% win (n=29)",
    },
    "CHFJPY": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.5,   # boost (acc=62% >= 55.0%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.5,   # boost (acc=60% >= 55.0%) [CALL weak 0%, PUT strong 75%]
            "indicator":   1.0,   # baseline (n=2<4)
            "key_level":   1.8,   # STRONG (acc=75% >= 65.0%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 55.6% win (n=9)",
    },
    "EURAUD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (n=0<4)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   0.5,   # dampened (acc=43% < 45.0%)
            "indicator":   1.0,   # baseline (acc=50%)
            "key_level":   0.5,   # dampened (acc=37% < 45.0%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 46.4% win (n=29)",
    },
    "EURCAD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (n=1<4)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   0.5,   # dampened (acc=39% < 45.0%)
            "indicator":   1.0,   # baseline (n=0<4)
            "key_level":   1.8,   # STRONG (acc=67% >= 65.0%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 43.5% win (n=25)",
    },
    "EURGBP": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=52%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.0,   # baseline (acc=50%)
            "indicator":   1.5,   # boost (acc=60% >= 55.0%)
            "key_level":   0.5,   # dampened (acc=39% < 45.0%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 50.0% win (n=35)",
    },
    "EURJPY": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (n=1<4)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.0,   # baseline (acc=55%) [CALL weak 20%, PUT strong 83%]
            "indicator":   0.1,   # DISABLED (acc=33% < 35.0%)
            "key_level":   0.1,   # DISABLED (acc=14% < 35.0%) [CALL weak 0%, PUT strong 100%]
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 43.8% win (n=18)",
    },
    "EURUSD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.5,   # dampened (acc=44% < 45.0%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   0.5,   # dampened (acc=38% < 45.0%) [PUT weak 17%, CALL strong 100%]
            "indicator":   0.1,   # DISABLED (acc=25% < 35.0%)
            "key_level":   1.0,   # baseline (acc=46%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 42.9% win (n=18)",
    },
    "GBPAUD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=53%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   0.1,   # DISABLED (acc=31% < 35.0%)
            "indicator":   0.5,   # dampened (acc=44% < 45.0%)
            "key_level":   0.5,   # dampened (acc=40% < 45.0%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 48.0% win (n=28)",
    },
    "GBPCAD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (n=0<4)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.0,   # baseline (acc=47%)
            "indicator":   1.0,   # baseline (n=0<4)
            "key_level":   0.5,   # dampened (acc=40% < 45.0%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 51.7% win (n=30)",
    },
    "GBPCHF": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=48%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.0,   # baseline (acc=54%)
            "indicator":   1.5,   # boost (acc=62% >= 55.0%)
            "key_level":   0.1,   # DISABLED (acc=29% < 35.0%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 45.8% win (n=24)",
    },
    "GBPJPY": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.5,   # boost (acc=56% >= 55.0%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   0.5,   # dampened (acc=43% < 45.0%)
            "indicator":   0.5,   # dampened (acc=40% < 45.0%)
            "key_level":   1.5,   # boost (acc=55% >= 55.0%)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 52.2% win (n=23)",
    },
    "GBPUSD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (n=1<4)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.0,   # baseline (acc=50%) [PUT weak 25%, CALL strong 67%]
            "indicator":   0.5,   # dampened (acc=38% < 45.0%)
            "key_level":   1.0,   # baseline (n=0<4)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 63.6% win (n=26)",
    },
    "USDJPY": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (n=0<4)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.5,   # boost (acc=62% >= 55.0%)
            "indicator":   0.5,   # dampened (acc=42% < 45.0%)
            "key_level":   1.0,   # baseline (n=0<4)
            "trend_follow":   1.0,   # baseline (n=0<4)
        },
        "description": "Calibrated: 55.0% win (n=20)",
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
    module_names=REAL_MODULES,
    engine_name="real",
)
