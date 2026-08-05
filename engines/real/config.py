"""engines/real/config.py — AUTO-CALIBRATED CONFIG v4 (2026-08-03)

Generated from live Railway brain data (~1280 graded signals).
Per-pair per-module weights calibrated against actual win rates.

FIX (MODULE-PRUNE-2026-08-03): removed `indicator` and `trend_follow`
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
from core.constants import REAL_MODULES as _MODULES


# ── Reliability tier multipliers (data-driven) ──────────────────────────
# FIX (MODULE-PRUNE-2026-08-03): removed "INDICATOR" and "TREND" tiers —
# the corresponding modules have been deleted.
# FIX (POST-PRUNE-TUNE-2026-08-03): boosted PATTERN 1.0 → 1.2 to compensate
# for the lost INDICATOR + TREND tiers. Pattern module is now the primary
# trend-continuation detector in Real markets.
RELIABILITY = {
    "PATTERN":   1.2,
    "LEVEL":     1.0,
    "CANDLE":    1.0,
    "MICRO":     0.7,
}


# ── DEFAULT_WEIGHTS — for pairs with insufficient data (< 8 samples) ──
# FIX (MODULE-PRUNE-2026-08-03): removed indicator + trend_follow entries.
DEFAULT_WEIGHTS = {
    "candle_reaction":   1.0,
    "running_tick":      1.0,
    "pattern":           1.0,
    "key_level":         0.8,
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
}


# ── PER-PAIR CALIBRATED WEIGHTS (auto-generated from live data) ────────
# FIX (MODULE-PRUNE-2026-08-03): removed indicator + trend_follow from
# every pair's weights dict. Comments for those entries are also removed.
PAIR_CONFIGS = {
    "AUDUSD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=46%) (n=28)
            "running_tick":      1.5,   # boost (acc=65% >= 55.0%) (n=34)
            "pattern":           1.0,   # baseline (acc=55%) (n=22)
            "key_level":         1.0,   # baseline (no data)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
        },
        "description": "Calibrated: 56.0% win (n=84)",
    },
    "EURUSD": {
        "profile": "calibrated",
        "weights": {
            "candle_reaction":   1.0,   # baseline (acc=51%) (n=49)
            "running_tick":      1.0,   # baseline (acc=53%) (n=60)
            "pattern":           1.0,   # baseline (acc=48%) (n=46)
            "key_level":         1.0,   # baseline (acc=50%) (n=34)
            "market_state":      0.8,
            "wickwall":          0.5,
            "divergence":        0.5,
            "tickrun":           0.5,
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
    reliability=RELIABILITY,
    weight_adapter=weight_adapter,
    module_names=_MODULES,
    engine_name="real",
)
