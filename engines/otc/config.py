"""
engines/otc/config.py — DEEP CALIBRATED CONFIG v2 (2026-07-30)

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
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from engines.base.modules.otc_pattern import analyze as _otc_pattern_analyze
from core.constants import OTC_MODULES


# ── Reliability tier multipliers (data-driven) ──────────────────────────
# Based on per-module accuracy across all OTC pairs:
#   otc_pattern: 52.1% → 1.3 (best OTC module, boosted)
#   pattern:     55.8% → 1.5 (strong on OTC)
#   key_level:   52.9% → 1.2
#   indicator:   50.0% → 1.0
#   candle_reaction: 46.9% → 0.9 (worst, dampened)
RELIABILITY = {
    "PATTERN":   1.5,
    "LEVEL":     1.2,
    "TREND":     1.0,
    "INDICATOR": 1.0,
    "CANDLE":    0.9,
    "MICRO":     0.6,
    "OTC":       1.3,
}


# ── DEFAULT_WEIGHTS — for pairs with insufficient data (< 8 samples) ──
DEFAULT_WEIGHTS = {
    "candle_reaction": 0.9,
    "running_tick":    1.0,
    "pattern":         1.3,
    "indicator":       1.0,
    "key_level":       1.1,
    "otc_pattern":     1.3,
}


# ── DISABLED PAIRS — win rate < 40%, do not generate signals ────────────
#   GBPNZD_otc: 36.4% win rate (n=11) — win rate 36.4% < 40.0% threshold
#   USDPKR_otc: 27.3% win rate (n=11) — win rate 27.3% < 40.0% threshold
#   BRLUSD_otc: 38.5% win rate (n=13) — win rate 38.5% < 40.0% threshold


# ── PER-PAIR CALIBRATED WEIGHTS ────────────────────────────────────────
PAIR_CONFIGS = {
    "CADCHF_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.5,   # boost (acc=60% >= 55.0%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.0,   # baseline (n=3<4)
            "indicator":   1.8,   # STRONG (acc=67% >= 65.0%)
            "key_level":   1.0,   # baseline (acc=52%)
            "otc_pattern":   1.5,   # boost (acc=63% >= 55.0%)
        },
        "description": "Calibrated: 62.5% win (n=24)",
    },
    "EURNZD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=52%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.0,   # baseline (acc=52%)
            "indicator":   1.5,   # boost (acc=60% >= 55.0%)
            "key_level":   1.0,   # baseline (acc=50%)
            "otc_pattern":   1.5,   # boost (acc=55% >= 55.0%)
        },
        "description": "Calibrated: 66.7% win (n=34)",
    },
    "NZDUSD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.5,   # dampened (acc=43% < 45.0%) [CALL weak 20%, PUT strong 100%]
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.0,   # baseline (n=3<4)
            "indicator":   1.0,   # baseline (n=2<4)
            "key_level":   1.8,   # STRONG (acc=75% >= 65.0%)
            "otc_pattern":   0.5,   # dampened (acc=40% < 45.0%) [CALL weak 0%, PUT strong 100%]
        },
        "description": "Calibrated: 62.5% win (n=8)",
    },
    "USDBDT_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=29% < 35.0%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.8,   # STRONG (acc=79% >= 65.0%)
            "indicator":   1.0,   # baseline (acc=50%)
            "key_level":   1.5,   # boost (acc=60% >= 55.0%)
            "otc_pattern":   1.0,   # baseline (acc=48%)
        },
        "description": "Calibrated: 65.5% win (n=29)",
    },
    "USDCOP_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.5,   # boost (acc=60% >= 55.0%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   0.1,   # DISABLED (acc=33% < 35.0%)
            "indicator":   1.0,   # baseline (n=1<4)
            "key_level":   1.0,   # baseline (acc=54%)
            "otc_pattern":   1.8,   # STRONG (acc=68% >= 65.0%)
        },
        "description": "Calibrated: 55.2% win (n=29)",
    },
    "USDIDR_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=46%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   1.0,   # baseline (acc=50%)
            "indicator":   1.0,   # baseline (n=3<4)
            "key_level":   1.0,   # baseline (acc=53%) [CALL weak 17%, PUT strong 78%]
            "otc_pattern":   0.5,   # dampened (acc=43% < 45.0%) [CALL weak 25%, PUT strong 67%]
        },
        "description": "Calibrated: 53.8% win (n=13)",
    },
    "USDMXN_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.1,   # DISABLED (acc=22% < 35.0%)
            "running_tick":   1.0,   # baseline (n=0<4)
            "pattern":   0.5,   # dampened (acc=40% < 45.0%)
            "indicator":   1.0,   # baseline (n=2<4)
            "key_level":   1.5,   # boost (acc=57% >= 55.0%)
            "otc_pattern":   0.1,   # DISABLED (acc=25% < 35.0%) [PUT weak 0%, CALL strong 100%]
        },
        "description": "Calibrated: 40.0% win (n=10)",
    },
}


# ── BlenderConfig assembly ─────────────────────────────────────────────
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
