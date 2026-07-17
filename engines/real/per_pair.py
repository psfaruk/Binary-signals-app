"""
Real-market per-pair module weight adaptation.

REAL MARKET TUNING (2026-07-17):
This is the REAL engine — used for live exchange pairs (EURUSD, GBPUSD,
USDJPY — no "_otc" suffix). Real market pairs reflect actual order flow,
so:

  - Indicators (RSI, MACD, EMA, Bollinger, Stochastic) are MORE reliable
    than in OTC — boost them.
  - Continuation patterns (3 soldiers, inside bar breakout) work better
    — boost pattern module.
  - Mean-reversion is WEAKER (real markets trend harder) — dampen
    otc_pattern module.
  - Key levels are respected more (real institutional S/R) — boost
    key_level.

Static configs are keyed by the BARE symbol (EURUSD, not EURUSD_otc).
DB-adaptation uses the same lookup, so the (asset="EURUSD", period=60)
bucket is separate from the OTC ("EURUSD_otc", period=60) bucket —
no cross-contamination.

Usage:
    weights = get_weights("EURUSD", period=60)
"""
import os
import threading
import time

# Tunables — same meaning as the OTC engine's, but scoped to REAL pairs.
_ADAPT_MIN_SAMPLES = int(os.environ.get("ADAPT_MIN_SAMPLES", "20"))
_ADAPT_PRIOR = float(os.environ.get("ADAPT_PRIOR", "0.7"))
_ADAPT_CAP = float(os.environ.get("ADAPT_CAP", "0.30"))
_ADAPT_CACHE_TTL = float(os.environ.get("ADAPT_CACHE_TTL", "60"))

# ═══════════════════════════════════════════════════════════════════════════════
#  DEFAULT WEIGHTS — REAL MARKET (trend-following bias)
# ═══════════════════════════════════════════════════════════════════════════════
# Real-market default weights favor continuation/trend modules over
# reversal modules. Compare to OTC DEFAULT_WEIGHTS which gives otc_pattern
# a 1.2 bonus — here we instead give indicator +0.3 and pattern +0.2,
# and dampen otc_pattern to 0.7.
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.0,
    "running_tick":    1.0,
    "pattern":         1.2,   # continuation patterns (3 soldiers, inside bar breakout) work well on real
    "indicator":       1.3,   # indicators reflect real order flow → boost (was 1.0 in OTC)
    "key_level":       1.2,   # institutional S/R respected → boost
    "otc_pattern":     0.7,   # mean-reversion is weaker on trending real pairs → dampen (was 1.2 in OTC)
}

# ═══════════════════════════════════════════════════════════════════════════════
#  PER-PAIR MODEL CONFIGS — REAL MARKET
# ═══════════════════════════════════════════════════════════════════════════════
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
            "otc_pattern":     0.7,
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
            "otc_pattern":     0.7,
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
            "otc_pattern":     0.7,
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
            "otc_pattern":     0.8,
        },
        "description": "USD/CHF real — safe-haven, rangey with news spikes",
    },
    "AUDUSD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "AUD/USD real — commodity-correlated, stable",
    },
    "NZDUSD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "NZD/USD real — commodity-correlated, stable",
    },
    "USDCAD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "USD/CAD real — oil-correlated, stable",
    },

    # ── EUR CROSSES ─────────────────────────────────────────────────────
    "EURGBP": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "EUR/GBP real — rangey, key levels important",
    },
    "EURJPY": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.3,
            "indicator":       1.3,
            "key_level":       1.3,
            "otc_pattern":     0.7,
        },
        "description": "EUR/JPY real — trending, round levels important",
    },
    "EURAUD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "EUR/AUD real — volatile cross",
    },
    "EURCHF": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "EUR/CHF real — rangey, key levels important",
    },
    "EURCAD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "EUR/CAD real — volatile, oil-correlated",
    },

    # ── GBP CROSSES ─────────────────────────────────────────────────────
    "GBPJPY": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.2,
            "key_level":       1.3,
            "otc_pattern":     0.7,
        },
        "description": "GBP/JPY real — high-volatility carry trade",
    },
    "GBPAUD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "GBP/AUD real — volatile cross",
    },
    "GBPCAD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "GBP/CAD real — volatile, oil-correlated",
    },
    "GBPCHF": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "GBP/CHF real — volatile cross, key levels important",
    },

    # ── AUD / CAD / NZD / CHF / JPY CROSSES ─────────────────────────────
    "AUDJPY": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.3,
            "indicator":       1.3,
            "key_level":       1.3,
            "otc_pattern":     0.7,
        },
        "description": "AUD/JPY real — carry trade, trending",
    },
    "AUDCAD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "AUD/CAD real — stable, balanced",
    },
    "AUDNZD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "AUD/NZD real — stable, balanced",
    },
    "AUDCHF": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "AUD/CHF real — carry-trade, rangey",
    },
    "CADJPY": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.3,
            "indicator":       1.3,
            "key_level":       1.3,
            "otc_pattern":     0.7,
        },
        "description": "CAD/JPY real — oil-correlated, trending",
    },
    "CADCHF": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "CAD/CHF real — stable, balanced",
    },
    "NZDJPY": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.3,
            "indicator":       1.3,
            "key_level":       1.3,
            "otc_pattern":     0.7,
        },
        "description": "NZD/JPY real — trending, round levels",
    },
    "NZDCAD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "NZD/CAD real — stable, balanced",
    },
    "NZDCHF": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "NZD/CHF real — stable, balanced",
    },
    "CHFJPY": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "CHF/JPY real — safe-haven cross, volatile",
    },
    "EURNZD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "EUR/NZD real — volatile, key levels important",
    },
    "GBPNZD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "GBP/NZD real — volatile, key levels important",
    },

    # ── EXOTICS (real-market — usually available only during bank hours) ──
    "USDMXN": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "USD/MXN real — momentum works, continuation biased",
    },
    "USDTRY": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "USD/TRY real — emerging-market, strong trends",
    },
    "USDPKR": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "USD/PKR real — pegged, low volatility",
    },
    "USDCOP": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "USD/COP real — emerging-market, trending",
    },
    "USDARS": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "USD/ARS real — emerging-market, strong trends",
    },
    "USDBDT": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "USD/BDT real — pegged, low volatility",
    },
    "USDDZD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "USD/DZD real — pegged, low volatility",
    },
    "BRLUSD": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.2,
            "otc_pattern":     0.8,
        },
        "description": "BRL/USD real — volatile, key levels important",
    },
    "INRUSD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "INR/USD real — pegged, low volatility",
    },
    "EURSGD": {
        "profile": "stable",
        "weights": {
            "candle_reaction": 1.1,
            "running_tick":    1.0,
            "pattern":         1.1,
            "indicator":       1.2,
            "key_level":       1.1,
            "otc_pattern":     0.8,
        },
        "description": "EUR/SGD real — managed float, rangey",
    },
}


def get_weights(asset: str, period: int = 60, use_db: bool = True) -> dict:
    """Get module weights for a REAL-market asset (no _otc suffix).

    Combines the static PAIR_CONFIGS prior with DB-learned per-module
    win-rate adaptation. Same algorithm as the OTC engine's get_weights,
    but uses REAL signal_log rows (asset name has no _otc suffix →
    different DB bucket → no cross-contamination).

    Args:
        asset: pair name (e.g. "EURUSD", "USDJPY") — no _otc suffix
        period: candle period in seconds (default 60)
        use_db: set False to skip DB lookup (used by /api/stats and tests)

    Returns:
        dict mapping module name → weight multiplier
    """
    config = PAIR_CONFIGS.get(asset)
    base = config["weights"].copy() if config else DEFAULT_WEIGHTS.copy()

    if not use_db:
        return base

    now = time.time()
    cache_key = (asset, period)
    cached = _ADAPT_CACHE.get(cache_key)
    if cached and (now - cached["ts"]) < _ADAPT_CACHE_TTL:
        return cached["weights"]

    adapted = _adapt_from_db(base, asset, period)
    _ADAPT_CACHE[cache_key] = {"ts": now, "weights": adapted.copy()}
    return adapted


# In-process cache for DB-adapted weights. Keyed by (asset, period).
# Separate from the OTC engine's cache — assets never collide (real names
# have no _otc suffix, OTC names do).
_ADAPT_CACHE: dict = {}
_ADAPT_CACHE_LOCK = threading.Lock()


def _adapt_from_db(static_weights: dict, asset: str, period: int) -> dict:
    """Blend static weights with DB-learned per-module win rates.

    Returns a new dict; the input is not mutated.
    """
    try:
        import db as _db
    except ImportError:
        return static_weights.copy()

    try:
        stats = _db.per_module_accuracy(asset, period=period, n=200)
    except Exception:
        return static_weights.copy()

    adapted = {}
    for module, static_w in static_weights.items():
        s = stats.get(module, {})
        total = s.get("total", 0)
        win_rate = s.get("win_rate")
        if total < _ADAPT_MIN_SAMPLES or win_rate is None:
            adapted[module] = static_w
            continue

        deviation = win_rate - 0.50
        scale = max(-_ADAPT_CAP, min(_ADAPT_CAP, deviation * 1.5))
        learned_w = static_w * (1.0 + scale)
        blended = _ADAPT_PRIOR * static_w + (1.0 - _ADAPT_PRIOR) * learned_w
        adapted[module] = round(blended, 2)

    return adapted


def invalidate_adaptation_cache(asset: str = None, period: int = None):
    """Clear the DB-adaptation cache for real-market pairs."""
    with _ADAPT_CACHE_LOCK:
        if asset is None:
            _ADAPT_CACHE.clear()
        else:
            keys_to_drop = [k for k in _ADAPT_CACHE
                            if k[0] == asset and (period is None or k[1] == period)]
            for k in keys_to_drop:
                _ADAPT_CACHE.pop(k, None)


def get_profile(asset: str) -> str:
    """Get the behavior profile for a real-market asset.

    Returns one of: "trending", "volatile", "stable", "default"
    (No "mean_reverting" — real markets trend more than they revert.)
    """
    config = PAIR_CONFIGS.get(asset)
    if config:
        return config["profile"]
    return "default"


def get_description(asset: str) -> str:
    """Get human-readable description for a real-market asset's behavior profile."""
    config = PAIR_CONFIGS.get(asset)
    if config:
        return config.get("description", "")
    return "Unknown pair — using REAL default trend-following weights"


def list_configured_pairs() -> list:
    """Return list of all real-market pairs with custom configs."""
    return sorted(PAIR_CONFIGS.keys())
