"""
engines/real/config.py — Real market engine configuration.

Holds the Real-engine-specific bits that differ from the OTC engine:
  - PAIR_CONFIGS (per-pair static weight priors, keyed by BARE symbol)
  - DEFAULT_WEIGHTS (fallback when asset not in PAIR_CONFIGS)
  - RELIABILITY (reliability-tier multipliers — Real boosts INDICATOR & TREND)
  - module_6 selection: trend_follow (momentum continuation detector)
  - module_names: tuple of 6 module names

REAL-MARKET TUNING:
Real-market pairs (live exchange prices, real order flow) reflect actual
liquidity moves — indicators and continuation patterns are MORE reliable
than in OTC. INDICATOR tier is bumped 1.0 → 1.3. A new TREND tier (1.3)
is added for the trend_follow module's signals. MICRO is bumped 0.6 → 0.7
because real tick microstructure reflects real volume.

Everything else is imported from engines.base.
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from engines.base.modules.trend_follow import analyze as _trend_follow_analyze
from core.constants import REAL_MODULES


# ── Reliability tier multipliers (Real-market tuning) ────────────────────
# FIX (trend_follow calibration, 2026-07-20): backtest showed trend_follow
# module has 28.9% win rate — dampened TREND tier from 1.3 to 1.0 until the
# module improves. INDICATOR also dampened slightly (1.3→1.2) since
# indicators overlap with trend_follow logic.
RELIABILITY = {
    "PATTERN":   1.5,   # multi-candle patterns (highest conviction)
    "STAT":      1.3,   # statistical edge (Z-score, rarity)
    "LEVEL":     1.3,   # key S/R level confluence
    "TREND":     1.0,   # was 1.3 — trend_follow underperforming, dampened
    "INDICATOR": 1.2,   # was 1.3 — slight dampen due to overlap with trend_follow
    "CANDLE":    1.0,   # single-candle signals (baseline)
    "OTC":       1.2,   # kept for dict compat (Real engine uses TREND module instead)
    "MICRO":     0.7,   # REAL-MARKET: tick microstructure is more meaningful (real volume) — was 0.6
}


# ── DEFAULT WEIGHTS — REAL MARKET (trend-following bias) ─────────────────
# Real-market default weights favor continuation/trend modules over
# reversal modules. Module 6 is `trend_follow` (trend-continuation
# detector) — NOT `otc_pattern` (mean-reversion detector).
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.0,
    "running_tick":    1.0,
    "pattern":         1.2,   # continuation patterns (3 soldiers, inside bar breakout) work well on real
    "indicator":       1.3,   # indicators reflect real order flow → boost (was 1.0 in OTC)
    "key_level":       1.2,   # institutional S/R respected → boost
    "trend_follow":    1.2,   # REAL engine's trend-continuation module (replaces otc_pattern)
}


# ── PER-PAIR MODEL CONFIGS — REAL MARKET ─────────────────────────────────
# Keyed by BARE symbol (no _otc suffix). These match the OTC PAIR_CONFIGS
# but with trend-favoring weights for the real-market twins.
PAIR_CONFIGS = {
    # ── MAJORS (real-market trending behavior, indicators reliable) ────
    "EURUSD": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.4,   # EURUSD respects indicators well
            "key_level":       1.2,
            "trend_follow":    1.2,
        },
        "description": "EUR/USD real — trending, indicators very reliable",
    },
    "GBPUSD": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.4,
            "key_level":       1.2,
            "trend_follow":    1.2,
        },
        "description": "GBP/USD real — trending, indicators reliable",
    },
    "USDJPY": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.3,
            "indicator":       1.3,
            "key_level":       1.3,   # JPY pairs respect round numbers
            "trend_follow":    1.2,
        },
        "description": "USD/JPY real — trending, round levels important",
    },
    "USDCHF": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "USD/CHF real — safe-haven, rangey",
    },
    "AUDUSD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "AUD/USD real — commodity-correlated, stable",
    },
    "USDCAD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "USD/CAD real — oil-correlated, stable",
    },
    "NZDUSD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "NZD/USD real — commodity-correlated, stable",
    },

    # ── EUR CROSSES ──────────────────────────────────────────────────────
    "EURJPY": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.3,
            "indicator":       1.3,
            "key_level":       1.3,
            "trend_follow":    1.3,
        },
        "description": "EUR/JPY real — trending, round levels",
    },
    "EURGBP": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "EUR/GBP real — rangey, key levels",
    },
    "EURCHF": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "EUR/CHF real — rangey, key levels",
    },
    "EURAUD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.2,
            "trend_follow":    1.1,
        },
        "description": "EUR/AUD real — volatile cross",
    },
    "EURCAD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.2,
            "trend_follow":    1.1,
        },
        "description": "EUR/CAD real — volatile, oil-correlated",
    },

    # ── GBP CROSSES ──────────────────────────────────────────────────────
    "GBPJPY": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.1,
            "key_level":       1.3,
            "trend_follow":    1.2,
        },
        "description": "GBP/JPY real — high-volatility carry trade",
    },
    "GBPAUD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.2,
            "trend_follow":    1.1,
        },
        "description": "GBP/AUD real — volatile cross",
    },
    "GBPCAD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.2,
            "trend_follow":    1.1,
        },
        "description": "GBP/CAD real — volatile, oil-correlated",
    },
    "GBPCHF": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.2,
            "trend_follow":    1.1,
        },
        "description": "GBP/CHF real — volatile cross",
    },
    "GBPNZD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.2,
            "trend_follow":    1.1,
        },
        "description": "GBP/NZD real — volatile cross",
    },

    # ── AUD CROSSES ──────────────────────────────────────────────────────
    "AUDJPY": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.2,
            "key_level":       1.2,
            "trend_follow":    1.3,
        },
        "description": "AUD/JPY real — carry trade, trending",
    },
    "AUDCHF": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.1,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "AUD/CHF real — carry-trade, rangey",
    },
    "AUDCAD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.1,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "AUD/CAD real — stable, balanced",
    },
    "AUDNZD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.1,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "AUD/NZD real — stable, balanced",
    },

    # ── CAD / CHF / NZD CROSSES ─────────────────────────────────────────
    "CADJPY": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.2,
            "key_level":       1.2,
            "trend_follow":    1.3,
        },
        "description": "CAD/JPY real — oil-correlated, trending",
    },
    "CADCHF": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.1,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "CAD/CHF real — stable, balanced",
    },
    "CHFJPY": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.2,
            "trend_follow":    1.1,
        },
        "description": "CHF/JPY real — safe-haven cross, volatile",
    },
    "NZDJPY": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.2,
            "key_level":       1.2,
            "trend_follow":    1.3,
        },
        "description": "NZD/JPY real — trending, round levels",
    },
    "NZDCAD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.1,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "NZD/CAD real — stable, balanced",
    },
    "NZDCHF": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.1,
            "key_level":       1.1,
            "trend_follow":    1.1,
        },
        "description": "NZD/CHF real — stable, balanced",
    },
    "EURNZD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.2,
            "trend_follow":    1.1,
        },
        "description": "EUR/NZD real — volatile, key levels",
    },
}


# ── Build the Real engine's PairWeightAdapter (instance, scoped to Real) ─
weight_adapter = PairWeightAdapter(
    pair_configs=PAIR_CONFIGS,
    default_weights=DEFAULT_WEIGHTS,
    engine_name="real",
)

# Module 6 for Real: trend_follow (momentum continuation detector)
def _module_6(candles, ctx):
    return _trend_follow_analyze(candles, ctx)


# ── Build the Real engine's BlenderConfig ────────────────────────────────
CONFIG = BlenderConfig(
    module_6_name="trend_follow",
    module_6_fn=_module_6,
    reliability=RELIABILITY,
    weight_adapter=weight_adapter,
    module_names=REAL_MODULES,
    engine_name="real",
)
