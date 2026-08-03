"""engines/otc/config.py — AUTO-CALIBRATED CONFIG v3 (2026-08-03)

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
from engines.base.modules.otc_pattern import analyze as _otc_pattern_analyze
from core.constants import OTC_MODULES as _MODULES


# ── Reliability tier multipliers (data-driven) ──────────────────────────
# USER FIX #5 (2026-08-03): LEVEL reliability 0.8 -> 1.0.
# Live brain data shows key_level's S/R wick-rejection signals have 70%
# win rate — the highest of any module. But the previous double-dampen
# (RELIABILITY 0.8 × DEFAULT_WEIGHT 0.8 = 0.64 effective) was crushing
# its strong signals down to the same score as weak ones. Restoring to
# 1.0 lets the per-pair weight be the sole dampener/booster.
RELIABILITY = {
    "PATTERN":   1.3,
    "LEVEL":   1.0,   # was 0.8 — restored (key_level has 70% S/R win rate)
    "TREND":   1.0,
    "INDICATOR":   0.9,
    "CANDLE":   1.0,
    "MICRO":   0.7,
    "OTC":   1.4,
}


# ── DEFAULT_WEIGHTS — for pairs with insufficient data (< 8 samples) ──
DEFAULT_WEIGHTS = {
    "candle_reaction":   1.0,
    "running_tick":   1.0,
    "pattern":   1.0,
    "indicator":   0.9,
    "key_level":   0.8,
    "otc_pattern":   1.4,
}


# ── PER-PAIR CALIBRATED WEIGHTS (auto-generated from live data) ────────
PAIR_CONFIGS = {
    "AUDUSD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=54%) (n=80)
            "running_tick":   1.0,   # baseline (acc=54%) (n=90)
            "pattern":   0.5,   # dampened (acc=44% < 45.0%) (n=64)
            "indicator":   1.5,   # boost (acc=60% >= 55.0%) (n=35)
            "key_level":   1.0,   # baseline (acc=51%) (n=49)
            "otc_pattern":   1.0,   # baseline (acc=55%) (n=71) [CALL weak 41%, PUT strong 64%]
        },
        "description": "Calibrated: 52.7% win (n=389)",
    },
    "BRLUSD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.5,   # dampened (acc=45% < 45.0%) (n=58)
            "running_tick":   1.0,   # baseline (acc=46%) (n=71)
            "pattern":   0.5,   # dampened (acc=44% < 45.0%) (n=57)
            "indicator":   0.5,   # dampened (acc=42% < 45.0%) (n=38)
            "key_level":   1.0,   # baseline (acc=47%) (n=32)
            "otc_pattern":   0.5,   # dampened (acc=42% < 45.0%) (n=62)
        },
        "description": "Calibrated: 44.3% win (n=318)",
    },
    "EURUSD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=48%) (n=66)
            "running_tick":   1.0,   # baseline (acc=54%) (n=69)
            "pattern":   1.0,   # baseline (acc=48%) (n=56)
            "indicator":   0.5,   # dampened (acc=44% < 45.0%) (n=27)
            "key_level":   1.0,   # baseline (acc=49%) (n=41)
            "otc_pattern":   1.0,   # baseline (acc=51%) (n=55)
        },
        "description": "Calibrated: 49.7% win (n=314)",
    },
    "USDARS_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=50%) (n=114)
            "running_tick":   1.0,   # baseline (acc=53%) (n=123)
            "pattern":   1.0,   # baseline (acc=53%) (n=97)
            "indicator":   1.5,   # boost (acc=62% >= 55.0%) (n=48)
            "key_level":   0.5,   # dampened (acc=40% < 45.0%) (n=67)
            "otc_pattern":   1.5,   # boost (acc=55% >= 55.0%) (n=92)
        },
        "description": "Calibrated: 51.9% win (n=541)",
    },
    "USDBDT_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (no data)
            "running_tick":   1.0,   # baseline (acc=53%) (n=105)
            "pattern":   1.0,   # baseline (acc=49%) (n=87)
            "indicator":   0.1,   # DISABLED (acc=34% < 35.0%) (n=38) [CALL strong 64%, PUT weak 22%]
            "key_level":   0.5,   # dampened (acc=43% < 45.0%) (n=46)
            "otc_pattern":   0.1,   # DISABLED (acc=23% < 35.0%) (n=30) [CALL weak 0%, PUT strong 28%]
        },
        "description": "Calibrated: 45.4% win (n=306)",
    },
    "USDCHF_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=49%) (n=97)
            "running_tick":   1.0,   # baseline (acc=52%) (n=120)
            "pattern":   1.5,   # boost (acc=57% >= 55.0%) (n=87)
            "indicator":   1.5,   # boost (acc=56% >= 55.0%) (n=43) [CALL strong 73%, PUT weak 50%]
            "key_level":   0.5,   # dampened (acc=43% < 45.0%) (n=58)
            "otc_pattern":   1.5,   # boost (acc=58% >= 55.0%) (n=78)
        },
        "description": "Calibrated: 52.6% win (n=483)",
    },
    "USDCOP_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.5,   # dampened (acc=44% < 45.0%) (n=59)
            "running_tick":   0.5,   # dampened (acc=44% < 45.0%) (n=73)
            "pattern":   0.5,   # dampened (acc=40% < 45.0%) (n=20) [CALL weak 20%, PUT strong 47%]
            "indicator":   0.5,   # dampened (acc=45% < 45.0%) (n=38)
            "key_level":   1.0,   # baseline (acc=47%) (n=32) [CALL weak 31%, PUT strong 62%]
            "otc_pattern":   1.0,   # baseline (acc=47%) (n=66) [CALL weak 39%, PUT strong 64%]
        },
        "description": "Calibrated: 44.8% win (n=288)",
    },
    "USDDZD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.8,   # STRONG (acc=67% >= 65.0%) (n=87)
            "running_tick":   0.5,   # dampened (acc=44% < 45.0%) (n=109)
            "pattern":   0.5,   # dampened (acc=39% < 45.0%) (n=90)
            "indicator":   0.5,   # dampened (acc=44% < 45.0%) (n=41)
            "key_level":   1.5,   # boost (acc=65% >= 55.0%) (n=57)
            "otc_pattern":   1.5,   # boost (acc=58% >= 55.0%) (n=83)
        },
        "description": "Calibrated: 52.2% win (n=467)",
    },
    "USDIDR_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=50%) (n=68)
            "running_tick":   1.0,   # baseline (acc=55%) (n=88)
            "pattern":   1.0,   # baseline (acc=50%) (n=66)
            "indicator":   0.5,   # dampened (acc=37% < 45.0%) (n=38)
            "key_level":   1.0,   # baseline (acc=49%) (n=47)
            "otc_pattern":   0.5,   # dampened (acc=42% < 45.0%) (n=64)
        },
        "description": "Calibrated: 48.2% win (n=371)",
    },
    "USDINR_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=49%) (n=41)
            "running_tick":   1.0,   # baseline (acc=53%) (n=53)
            "pattern":   1.8,   # STRONG (acc=65% >= 65.0%) (n=40)
            "indicator":   1.5,   # boost (acc=64% >= 55.0%) (n=25)
            "key_level":   1.0,   # baseline (no data)
            "otc_pattern":   1.0,   # baseline (acc=54%) (n=37) [CALL strong 67%, PUT weak 45%]
        },
        "description": "Calibrated: 56.1% win (n=196)",
    },
    "USDJPY_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=51%) (n=96)
            "running_tick":   1.0,   # baseline (acc=54%) (n=114)
            "pattern":   1.5,   # boost (acc=57% >= 55.0%) (n=88)
            "indicator":   1.5,   # boost (acc=55% >= 55.0%) (n=49)
            "key_level":   1.0,   # baseline (acc=50%) (n=60) [CALL weak 41%, PUT strong 61%]
            "otc_pattern":   1.0,   # baseline (acc=55%) (n=84)
        },
        "description": "Calibrated: 53.6% win (n=491)",
    },
    "USDPKR_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.5,   # boost (acc=59% >= 55.0%) (n=46)
            "running_tick":   1.0,   # baseline (acc=47%) (n=49)
            "pattern":   1.0,   # baseline (acc=50%) (n=46)
            "indicator":   1.0,   # baseline (acc=50%) (n=32)
            "key_level":   1.5,   # boost (acc=59% >= 55.0%) (n=32)
            "otc_pattern":   1.5,   # boost (acc=63% >= 55.0%) (n=49)
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
    module_6_name="otc_pattern",
    module_6_fn=_otc_pattern_analyze,
    reliability=RELIABILITY,
    weight_adapter=weight_adapter,
    module_names=_MODULES,
    engine_name="otc",
)
