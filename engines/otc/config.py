"""engines/otc/config.py — AUTO-CALIBRATED CONFIG v4 (2026-08-03)

Generated from live Railway brain data (~1280 graded signals).
Per-pair per-module weights calibrated against actual win rates.

FIX (MODULE-PRUNE-2026-08-03): removed `indicator` and `otc_pattern`
modules entirely. Both engines now run the same shared modules.

FIX (RUNNING-TICK-REMOVE-2026-08-05): removed `running_tick` entirely
(module deleted, weight entries removed below). Live theory_votes data
(12 sub-signals + composite, n=460-7742) confirmed no measurable edge —
every one sat at 48.7-51.4% win rate with Wilson 95% lower bound below
break-even. See core/constants.py MODULE_NAMES comment for the full
rationale; the module was first weighted to 0.0 (commit b54ada0) and
confirmed live to have zero effect, then physically removed.

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
# FIX (POST-PRUNE-TUNE-2026-08-03): boosted PATTERN 1.3 → 1.4 to compensate
# for the lost OTC ×1.4 tier (otc_pattern module). This restores the
# effective confidence of OTC signals to pre-prune levels.
# NOTE: "MICRO" tier is still used by tickrun and market_state — do not
# remove it just because running_tick (its original user) was deleted.
RELIABILITY = {
    "PATTERN":   1.4,
    "LEVEL":     1.0,
    "CANDLE":    1.0,
    "MICRO":     0.7,
}


# ── DEFAULT_WEIGHTS — for pairs with insufficient data (< 8 samples) ──
# FIX (MODULE-PRUNE-2026-08-03): removed indicator + otc_pattern entries.
# FIX (RUNNING-TICK-REMOVE-2026-08-05): removed running_tick entry.
DEFAULT_WEIGHTS = {
    "candle_reaction":   1.0,
    "pattern":           1.0,
    "key_level":         0.8,
    # FIX (PROD-BACKTEST-2026-08-05 / NEW-MODULES): 4 new modules ported
    # from analyze_eoc.py. Starting at LOW weight (0.5) so they don't
    # dominate consensus until the per-pair adapter validates them
    # against new production data over the next 24h.
    "market_state":      0.8,   # main predictor — slightly higher default
    "wickwall":          0.5,
    "divergence":        0.5,
    "tickrun":           0.5,
}


# ── PER-PAIR CALIBRATED WEIGHTS (auto-generated from live data) ────────
# FIX (MODULE-PRUNE-2026-08-03): removed indicator + otc_pattern from
# every pair's weights dict. Comments for those entries are also removed.
# FIX (RUNNING-TICK-REMOVE-2026-08-05): removed running_tick from every
# pair's weights dict.
PAIR_CONFIGS = {
    "AUDUSD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=54%) (n=80)
            "pattern":           0.5,   # dampened (acc=44% < 45.0%) (n=64)
            "key_level":         1.0,   # baseline (acc=51%) (n=49)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 52.7% win (n=389)",
    },
    "BRLUSD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.5,   # dampened (acc=45% < 45.0%) (n=58)
            "pattern":           0.5,   # dampened (acc=44% < 45.0%) (n=57)
            "key_level":         1.0,   # baseline (acc=47%) (n=32)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 44.3% win (n=318)",
    },
    "EURUSD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=48%) (n=66)
            "pattern":           1.0,   # baseline (acc=48%) (n=56)
            "key_level":         1.0,   # baseline (acc=49%) (n=41)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 49.7% win (n=314)",
    },
    "USDARS_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=50%) (n=114)
            "pattern":           1.0,   # baseline (acc=53%) (n=97)
            "key_level":         0.5,   # dampened (acc=40% < 45.0%) (n=67)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 51.9% win (n=541)",
    },
    "USDBDT_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (no data)
            "pattern":           1.0,   # baseline (acc=49%) (n=87)
            "key_level":         0.5,   # dampened (acc=43% < 45.0%) (n=46)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 45.4% win (n=306)",
    },
    "USDCHF_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=49%) (n=97)
            "pattern":           1.5,   # boost (acc=57% >= 55.0%) (n=87)
            "key_level":         0.5,   # dampened (acc=43% < 45.0%) (n=58)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 52.6% win (n=483)",
    },
    "USDCOP_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   0.5,   # dampened (acc=44% < 45.0%) (n=59)
            "pattern":           0.5,   # dampened (acc=40% < 45.0%) (n=20)
            "key_level":         1.0,   # baseline (acc=47%) (n=32)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 44.8% win (n=288)",
    },
    "USDDZD_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.8,   # STRONG (acc=67% >= 65.0%) (n=87)
            "pattern":           0.5,   # dampened (acc=39% < 45.0%) (n=90)
            "key_level":         1.5,   # boost (acc=65% >= 55.0%) (n=57)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 52.2% win (n=467)",
    },
    "USDIDR_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=50%) (n=68)
            "pattern":           1.0,   # baseline (acc=50%) (n=66)
            "key_level":         1.0,   # baseline (acc=49%) (n=47)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 48.2% win (n=371)",
    },
    "USDINR_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=49%) (n=41)
            "pattern":           1.8,   # STRONG (acc=65% >= 65.0%) (n=40)
            "key_level":         1.0,   # baseline (no data)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 56.1% win (n=196)",
    },
    "USDJPY_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=51%) (n=96)
            "pattern":           1.5,   # boost (acc=57% >= 55.0%) (n=88)
            "key_level":         1.0,   # baseline (acc=50%) (n=60)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 53.6% win (n=491)",
    },
    "USDPKR_otc": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.5,   # boost (acc=59% >= 55.0%) (n=46)
            "pattern":           1.0,   # baseline (acc=50%) (n=46)
            "key_level":         1.5,   # boost (acc=59% >= 55.0%) (n=32)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
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
