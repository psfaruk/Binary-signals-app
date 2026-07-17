"""
Per-pair module weight adaptation.

Different OTC pairs have different behaviors:
  - Exotic OTC (USDPKR, USDBDT, USDCOP) → mean-reverting → boost reversal engines
  - Major OTC (EURUSD, GBPUSD, USDJPY) → more trending → boost continuation engines
  - JPY pairs → session-dependent volatility → boost key_level
  - High-volatility pairs → dampen indicator (noise)

This module provides:
  1. Static per-pair configs (based on known OTC behavior)
  2. DB-based adaptation (NEW, Bug #5 fix, 2026-07-17): when ≥20 graded
     signals exist for a (asset, period), the per-module win rate is used
     to scale the static weights. Modules performing poorly (win_rate <
     0.45) get dampened; modules performing well (win_rate > 0.60) get
     boosted. Static config still provides the prior — adaptation is
     capped to ±30% to avoid overfitting on small samples.

Usage:
    weights = get_weights("EURUSD_otc")
    # weights = {"candle_reaction": 1.0, "running_tick": 0.8, ...}
"""
import os
import threading
import time

# Minimum graded samples per module before adaptation kicks in. Below
# this, the static config is used unchanged (small-sample noise dominates).
_ADAPT_MIN_SAMPLES = int(os.environ.get("ADAPT_MIN_SAMPLES", "20"))
# Static-config prior weight (0.7) vs DB-learned weight (0.3). Keeps
# adaptation from overreacting on small or noisy samples.
_ADAPT_PRIOR = float(os.environ.get("ADAPT_PRIOR", "0.7"))
# Cap on per-module adaptation magnitude (±30%).
_ADAPT_CAP = float(os.environ.get("ADAPT_CAP", "0.30"))
# In-memory cache TTL for per-pair DB lookups (seconds). Avoids re-querying
# signal_log on every prediction when 38 streams all close at once.
_ADAPT_CACHE_TTL = float(os.environ.get("ADAPT_CACHE_TTL", "60"))

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

    # ── CHF PAIRS (safe-haven, rangey) ──────────────────────────────────
    # CHF pairs tend to be more rangey with sharp news-driven moves.
    # FIX (Bug #11, 2026-07-17): these were missing from PAIR_CONFIGS,
    # silently falling through to DEFAULT_WEIGHTS.
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

    # ── EUR CROSS PAIRS (missing) ───────────────────────────────────────
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

    # ── GBP CROSS PAIRS (missing) ───────────────────────────────────────
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

    # ── AUD / CAD / JPY CROSS PAIRS (missing) ───────────────────────────
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

    # ── EXOTIC OTC PAIRS (missing) ──────────────────────────────────────
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


def get_weights(asset: str, period: int = 60, use_db: bool = True) -> dict:
    """Get module weights for a specific asset.

    Combines the static PAIR_CONFIGS prior with DB-learned per-module
    win-rate adaptation. When `use_db` is True (default) AND enough graded
    samples exist for this (asset, period), modules with win_rate > 0.60
    get boosted up to +30%, and modules with win_rate < 0.45 get dampened
    up to -30%. Adaptation is capped and prior-weighted (70% static /
    30% learned) to avoid overfitting on small or noisy samples.

    Args:
        asset: pair name (e.g. "EURUSD_otc", "USDPKR_otc")
        period: candle period in seconds (default 60 — only the 1m feed
            has graded history in practice, but the API supports any)
        use_db: set False to skip DB lookup (used by /api/stats and tests)

    Returns:
        dict mapping module name → weight multiplier
    """
    config = PAIR_CONFIGS.get(asset)
    base = config["weights"].copy() if config else DEFAULT_WEIGHTS.copy()

    if not use_db:
        return base

    # Cache lookup — the prediction path is hot (every candle close across
    # 38 streams), and per_module_accuracy does a SQL scan each call.
    now = time.time()
    cache_key = (asset, period)
    cached = _ADAPT_CACHE.get(cache_key)
    if cached and (now - cached["ts"]) < _ADAPT_CACHE_TTL:
        return cached["weights"]

    adapted = _adapt_from_db(base, asset, period)
    _ADAPT_CACHE[cache_key] = {"ts": now, "weights": adapted.copy()}
    return adapted


# In-process cache for DB-adapted weights. Keyed by (asset, period).
_ADAPT_CACHE: dict = {}
_ADAPT_CACHE_LOCK = threading.Lock()


def _adapt_from_db(static_weights: dict, asset: str, period: int) -> dict:
    """Blend static weights with DB-learned per-module win rates.

    Returns a new dict; the input is not mutated.
    """
    try:
        import db as _db
    except ImportError:
        # db module not importable (e.g. unit-test context) → static only.
        return static_weights.copy()

    try:
        stats = _db.per_module_accuracy(asset, period=period, n=200)
    except Exception:
        # DB read failed (locked, missing table, etc.) → static fallback.
        return static_weights.copy()

    adapted = {}
    for module, static_w in static_weights.items():
        s = stats.get(module, {})
        total = s.get("total", 0)
        win_rate = s.get("win_rate")
        if total < _ADAPT_MIN_SAMPLES or win_rate is None:
            adapted[module] = static_w
            continue

        # Map win_rate ∈ [0, 1] to a scaling factor centered at 1.0.
        # win_rate=0.50 → 1.0 (no change)
        # win_rate=0.70 → +0.30 (boosted to 1.30, capped)
        # win_rate=0.30 → -0.30 (dampened to 0.70, capped)
        deviation = win_rate - 0.50
        scale = max(-_ADAPT_CAP, min(_ADAPT_CAP, deviation * 1.5))
        learned_w = static_w * (1.0 + scale)
        # Prior-weighted blend: keep mostly the static config, layer in
        # a fraction of the learned value.
        blended = _ADAPT_PRIOR * static_w + (1.0 - _ADAPT_PRIOR) * learned_w
        # Round to 2 decimals for readable debug output.
        adapted[module] = round(blended, 2)

    return adapted


def invalidate_adaptation_cache(asset: str = None, period: int = None):
    """Clear the DB-adaptation cache. Called after a batch of new signal
    log writes so the next prediction reflects fresh accuracy data."""
    with _ADAPT_CACHE_LOCK:
        if asset is None:
            _ADAPT_CACHE.clear()
        else:
            keys_to_drop = [k for k in _ADAPT_CACHE
                            if k[0] == asset and (period is None or k[1] == period)]
            for k in keys_to_drop:
                _ADAPT_CACHE.pop(k, None)


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
