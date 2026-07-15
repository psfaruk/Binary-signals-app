"""
Per-pair module weight adaptation.

Different OTC pairs have different behaviors:
  - Exotic OTC (USDPKR, USDBDT, USDCOP) → mean-reverting → boost reversal engines
  - Major OTC (EURUSD, GBPUSD, USDJPY) → more trending → boost continuation engines
  - JPY pairs → session-dependent volatility → boost key_level
  - High-volatility pairs → dampen indicator (noise)

This module provides:
  1. Static per-pair configs (based on known OTC behavior)
  2. DB-based adaptation (adjusts weights from historical accuracy)

Usage:
    weights = get_weights("EURUSD_otc")
    # weights = {"candle_reaction": 1.0, "running_tick": 0.8, ...}
"""
import os

# ═══════════════════════════════════════════════════════════════════════════════
#  DEFAULT WEIGHTS (all modules equal, OTC gets slight bonus)
# ═══════════════════════════════════════════════════════════════════════════════
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.0,
    "running_tick":    1.0,
    "pattern":         1.0,
    "indicator":       1.0,
    "key_level":       1.0,
    "otc_pattern":     1.2,   # OTC-specific gets bonus (most relevant for OTC markets)
}

# ═══════════════════════════════════════════════════════════════════════════════
#  PER-PAIR MODEL CONFIGS
# ═══════════════════════════════════════════════════════════════════════════════
# Each pair gets a config with:
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
            "indicator":       0.7,   # noisy, dampen indicators
            "key_level":       1.2,
            "otc_pattern":     1.2,
        },
        "description": "BRL — volatile, key levels important",
    },

    # ── MAJOR OTC PAIRS (more trending) ────────────────────────────────
    # These pairs follow trends better. Continuation signals work.
    # Indicators (EMA, MACD) are more reliable here.
    "EURUSD_otc": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,   # indicators work on major pairs
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
    "EURJPY": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.1,
            "key_level":       1.2,   # JPY pairs respect round numbers
            "otc_pattern":     1.0,
        },
        "description": "JPY cross — trending, round levels important",
    },
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
}


def get_weights(asset: str) -> dict:
    """Get module weights for a specific asset.

    Falls back to DEFAULT_WEIGHTS if the asset is not in PAIR_CONFIGS.

    Args:
        asset: pair name (e.g. "EURUSD_otc", "USDPKR_otc")

    Returns:
        dict mapping module name → weight multiplier
    """
    config = PAIR_CONFIGS.get(asset)
    if config:
        return config["weights"].copy()
    return DEFAULT_WEIGHTS.copy()


def get_profile(asset: str) -> str:
    """Get the behavior profile for an asset.

    Returns one of: "mean_reverting", "trending", "volatile", "stable", "default"
    """
    config = PAIR_CONFIGS.get(asset)
    if config:
        return config["profile"]
    return "default"


def get_description(asset: str) -> str:
    """Get human-readable description for an asset's behavior profile."""
    config = PAIR_CONFIGS.get(asset)
    if config:
        return config.get("description", "")
    return "Unknown pair — using default balanced weights"


def list_configured_pairs() -> list:
    """Return list of all pairs with custom configs."""
    return sorted(PAIR_CONFIGS.keys())
