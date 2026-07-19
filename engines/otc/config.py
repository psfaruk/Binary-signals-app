"""
engines/otc/config.py — OTC engine configuration.

Holds the OTC-specific bits that differ from the Real engine:
  - PAIR_CONFIGS (per-pair static weight priors)
  - DEFAULT_WEIGHTS (fallback when asset not in PAIR_CONFIGS)
  - RELIABILITY (reliability-tier multipliers)
  - module_6 selection: otc_pattern (mean-reversion detector)
  - module_names: tuple of 6 module names

Everything else (blender algorithm, context, 5 shared modules, types) is
imported from engines.base — eliminating the previous 95% duplication
between engines/otc/ and engines/real/.

The BlenderConfig instance is built once at import time and reused for
every prediction.
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from engines.base.modules.otc_pattern import analyze as _otc_pattern_analyze
from core.constants import OTC_MODULES

# ── Reliability tier multipliers (OTC tuning) ────────────────────────────
# OTC markets are broker-generated — indicators and patterns are less
# reliable than in real markets. OTC-specific patterns (mean-reversion)
# get a slight bonus because they exploit the broker's tendency to
# revert price to a mean.
RELIABILITY = {
    "PATTERN":   1.5,   # multi-candle patterns (highest conviction)
    "STAT":      1.3,   # statistical edge (Z-score, rarity)
    "LEVEL":     1.3,   # key S/R level confluence
    "INDICATOR": 1.0,   # technical indicators (RSI, MACD, etc.) — baseline in OTC
    "CANDLE":    1.0,   # single-candle signals (baseline)
    "OTC":       1.2,   # OTC-specific patterns (slight bonus)
    "MICRO":     0.6,   # tick microstructure (single data source, noisy)
    "TREND":     1.0,   # not used by OTC engine (kept for dict compat)
}


# ── DEFAULT WEIGHTS (all modules equal, OTC gets slight bonus) ───────────
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.0,
    "running_tick":    1.0,
    "pattern":         1.0,
    "indicator":       1.0,
    "key_level":       1.0,
    "otc_pattern":     1.2,   # OTC-specific gets bonus (most relevant for OTC markets)
}


# ── PER-PAIR MODEL CONFIGS ───────────────────────────────────────────────
# Each OTC pair gets a config with:
#   - module weights (which engines to trust more/less)
#   - behavior profile ("mean_reverting", "trending", "volatile", "stable")
#   - description
PAIR_CONFIGS = {
    # ── EXOTIC OTC PAIRS (mean-reverting) ──────────────────────────────
    # These pairs tend to reverse after 3+ same-direction candles.
    # Continuation signals underperform; reversal signals excel.
    "USDPKR_otc": {
        "profile": "mean_reverting",
        "weights": {
            "candle_reaction": 1.3,   # streak reversal is king here
            "running_tick":    0.8,
            "pattern":         1.2,   # engulfing/harami work well
            "indicator":       0.5,   # RSI/MACD unreliable (broker noise)
            "key_level":       1.1,
            "otc_pattern":     1.5,   # OTC mean-rev bias is strong
        },
        "description": "Exotic OTC — strong mean reversion, weak trends",
    },
    "USDBDT_otc": {
        "profile": "mean_reverting",
        "weights": {
            "candle_reaction": 1.3,
            "running_tick":    0.8,
            "pattern":         1.2,
            "indicator":       0.5,
            "key_level":       1.1,
            "otc_pattern":     1.5,
        },
        "description": "Exotic OTC — strong mean reversion",
    },
    "USDCOP_otc": {
        "profile": "mean_reverting",
        "weights": {
            "candle_reaction": 1.2,
            "running_tick":    0.9,
            "pattern":         1.1,
            "indicator":       0.6,
            "key_level":       1.0,
            "otc_pattern":     1.4,
        },
        "description": "Exotic OTC — mean reverting with occasional trends",
    },
    "USDARS_otc": {
        "profile": "mean_reverting",
        "weights": {
            "candle_reaction": 1.3,
            "running_tick":    0.8,
            "pattern":         1.2,
            "indicator":       0.5,
            "key_level":       1.1,
            "otc_pattern":     1.5,
        },
        "description": "Exotic OTC — strong mean reversion",
    },
    "USDDZD_otc": {
        "profile": "mean_reverting",
        "weights": {
            "candle_reaction": 1.2,
            "running_tick":    0.8,
            "pattern":         1.1,
            "indicator":       0.5,
            "key_level":       1.1,
            "otc_pattern":     1.4,
        },
        "description": "Exotic OTC — mean reverting",
    },
    "USDMXN_otc": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.1,
            "key_level":       1.0,
            "otc_pattern":     1.1,
        },
        "description": "MXN — momentum works, continuation biased",
    },
    "BRLUSD_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.0,
            "indicator":       0.7,
            "key_level":       1.2,
            "otc_pattern":     1.2,
        },
        "description": "BRL — volatile, key levels important",
    },

    # ── MAJOR OTC PAIRS (more trending) ────────────────────────────────
    "EURUSD_otc": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     1.0,
        },
        "description": "Major OTC — trending, indicators reliable",
    },
    "GBPUSD_otc": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     1.0,
        },
        "description": "Major OTC — trending, indicators reliable",
    },
    # FIX (Bug 20, deep audit 2026-07-19): removed the phantom "EURJPY"
    # entry (without _otc suffix) — it was dead code that never matched
    # any real asset (OTC asset is "EURJPY_otc", already defined below).
    # The duplicate was misleading maintainers and could mask typos.
    "USDJPY_otc": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.1,
            "key_level":       1.2,
            "otc_pattern":     1.0,
        },
        "description": "JPY OTC — trending, round levels important",
    },

    # ── COMMODITY OTC PAIRS ────────────────────────────────────────────
    "AUDCAD_otc": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.1,
            "otc_pattern":     1.1,
        },
        "description": "AUD/CAD — stable, balanced approach",
    },
    "AUDNZD_otc": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.1,
            "otc_pattern":     1.1,
        },
        "description": "AUD/NZD — stable, balanced",
    },
    "CADCHF_otc": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.1,
            "otc_pattern":     1.1,
        },
        "description": "CAD/CHF — stable, balanced",
    },
    "NZDCAD_otc": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.1,
            "otc_pattern":     1.1,
        },
        "description": "NZD/CAD — stable, balanced",
    },
    "NZDCHF_otc": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.1,
            "otc_pattern":     1.1,
        },
        "description": "NZD/CHF — stable, balanced",
    },
    "NZDJPY_otc": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.1,
            "key_level":       1.2,
            "otc_pattern":     1.0,
        },
        "description": "NZD/JPY — trending, round levels",
    },
    "NZDUSD_otc": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.1,
            "otc_pattern":     1.1,
        },
        "description": "NZD/USD — stable, balanced",
    },
    "EURNZD_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.0,
            "indicator":       0.8,
            "key_level":       1.2,
            "otc_pattern":     1.2,
        },
        "description": "EUR/NZD — volatile, key levels important",
    },
    "GBPNZD_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.0,
            "indicator":       0.8,
            "key_level":       1.2,
            "otc_pattern":     1.2,
        },
        "description": "GBP/NZD — volatile, key levels important",
    },

    # ── CHF PAIRS (safe-haven, rangey) ──────────────────────────────────
    "USDCHF_otc": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.1,
            "otc_pattern":     1.1,
        },
        "description": "USD/CHF — safe-haven, rangey with news spikes",
    },
    "EURCHF_otc": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.1,
            "otc_pattern":     1.1,
        },
        "description": "EUR/CHF — rangey, key levels important",
    },
    "GBPCHF_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.0,
            "indicator":       0.8,
            "key_level":       1.2,
            "otc_pattern":     1.2,
        },
        "description": "GBP/CHF — volatile cross, key levels important",
    },
    "AUDCHF_otc": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.1,
            "otc_pattern":     1.1,
        },
        "description": "AUD/CHF — carry-trade, rangey",
    },
    "CHFJPY_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.0,
            "indicator":       0.8,
            "key_level":       1.2,
            "otc_pattern":     1.2,
        },
        "description": "CHF/JPY — safe-haven cross, volatile",
    },

    # ── EUR CROSS PAIRS ───────────────────────────────────────────────
    "EURGBP_otc": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.0,
            "key_level":       1.1,
            "otc_pattern":     1.1,
        },
        "description": "EUR/GBP — rangey, key levels important",
    },
    "EURAUD_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.0,
            "indicator":       0.8,
            "key_level":       1.2,
            "otc_pattern":     1.2,
        },
        "description": "EUR/AUD — volatile cross",
    },
    "EURCAD_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.0,
            "indicator":       0.8,
            "key_level":       1.2,
            "otc_pattern":     1.2,
        },
        "description": "EUR/CAD — volatile, oil-correlated",
    },
    "EURSGD_otc": {
        "profile": "mean_reverting",
        "weights": {
            "candle_reaction": 1.2,
            "running_tick":    0.9,
            "pattern":         1.1,
            "indicator":       0.6,
            "key_level":       1.1,
            "otc_pattern":     1.4,
        },
        "description": "EUR/SGD — exotic, mean reverting",
    },
    "EURJPY_otc": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.1,
            "key_level":       1.2,
            "otc_pattern":     1.0,
        },
        "description": "EUR/JPY — trending, round levels important",
    },

    # ── GBP CROSS PAIRS ───────────────────────────────────────────────
    "GBPJPY_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.0,
            "indicator":       0.8,
            "key_level":       1.2,
            "otc_pattern":     1.2,
        },
        "description": "GBP/JPY — high-volatility carry trade",
    },
    "GBPAUD_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.0,
            "indicator":       0.8,
            "key_level":       1.2,
            "otc_pattern":     1.2,
        },
        "description": "GBP/AUD — volatile cross",
    },
    "GBPCAD_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.0,
            "indicator":       0.8,
            "key_level":       1.2,
            "otc_pattern":     1.2,
        },
        "description": "GBP/CAD — volatile, oil-correlated",
    },

    # ── AUD / CAD / JPY CROSS PAIRS ─────────────────────────────────────
    "AUDJPY_otc": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.1,
            "key_level":       1.2,
            "otc_pattern":     1.0,
        },
        "description": "AUD/JPY — carry trade, trending",
    },
    "CADJPY_otc": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.1,
            "key_level":       1.2,
            "otc_pattern":     1.0,
        },
        "description": "CAD/JPY — oil-correlated, trending",
    },

    # ── EXOTIC OTC PAIRS ──────────────────────────────────────────────
    "USDTRY_otc": {
        "profile": "mean_reverting",
        "weights": {
            "candle_reaction": 1.3,
            "running_tick":    0.8,
            "pattern":         1.2,
            "indicator":       0.5,
            "key_level":       1.1,
            "otc_pattern":     1.5,
        },
        "description": "USD/TRY — exotic, strong mean reversion",
    },
    "INRUSD_otc": {
        "profile": "mean_reverting",
        "weights": {
            "candle_reaction": 1.3,
            "running_tick":    0.8,
            "pattern":         1.2,
            "indicator":       0.5,
            "key_level":       1.1,
            "otc_pattern":     1.5,
        },
        "description": "INR/USD — exotic, strong mean reversion",
    },
}


# ── Build the OTC engine's PairWeightAdapter (instance, scoped to OTC) ───
weight_adapter = PairWeightAdapter(
    pair_configs=PAIR_CONFIGS,
    default_weights=DEFAULT_WEIGHTS,
    engine_name="otc",
)

# Module 6 for OTC: otc_pattern (mean-reversion detector)
# Wraps the analyze function so it matches the (candles, ctx) signature.
def _module_6(candles, ctx):
    return _otc_pattern_analyze(candles, ctx)


# ── Build the OTC engine's BlenderConfig ─────────────────────────────────
# This is the SINGLE object that captures all OTC-specific behavior. The
# shared engines.base.blender.predict() takes this config and runs the
# generic 6-module pipeline.
CONFIG = BlenderConfig(
    module_6_name="otc_pattern",
    module_6_fn=_module_6,
    reliability=RELIABILITY,
    weight_adapter=weight_adapter,
    module_names=OTC_MODULES,
    engine_name="otc",
)
