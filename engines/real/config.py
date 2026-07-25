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
than in OTC. INDICATOR tier is 1.2 (was 1.3, dampened 2026-07-20 due to
overlap with trend_follow logic). TREND tier is 1.0 (was 1.3, dampened
2026-07-20 because trend_follow has 27.3% win rate — see DEFAULT_WEIGHTS
trend_follow note and the FIX comment at line 27 below). MICRO is 0.7
(was 0.6 in OTC; real tick microstructure reflects real volume).
FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-09 + AUDIT-5-10): the previous
header claimed INDICATOR=1.3 and TREND=1.3 — both stale. Updated to match
the actual RELIABILITY map values (1.2 and 1.0 respectively).

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
# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-04): removed the dead
# `"STAT": 1.3` entry — no module in engines/base/modules/ emits
# reliability="STAT". The blender's `reliability.get(r.reliability, 1.0)`
# returns 1.0 default for unknown tiers, so removing the entry has no
# behavioral effect.
# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-11): removed the dead
# `"OTC": 1.2` entry — the Real engine uses trend_follow (TREND tier) for
# module 6, NOT otc_pattern (OTC tier). No Real-engine signal uses the OTC
# tier; the entry was misleading dead code.
RELIABILITY = {
    "PATTERN":   1.5,   # multi-candle patterns (highest conviction)
    "LEVEL":     1.3,   # key S/R level confluence
    "TREND":     1.0,   # was 1.3 — trend_follow underperforming, dampened
    "INDICATOR": 1.2,   # was 1.3 — slight dampen due to overlap with trend_follow
    "CANDLE":    1.0,   # single-candle signals (baseline)
    "MICRO":     0.7,   # REAL-MARKET: tick microstructure is more meaningful (real volume) — was 0.6
}


# ── DEFAULT WEIGHTS — REAL MARKET (auto-tuned from live data 2026-07-23) ───
# FIX (AUTO-TUNE-2026-07-23): weights adjusted based on live win rates:
#   candle_reaction: 54.3% → BOOST 1.0 → 1.3 (best performer)
#   running_tick:    50.7% → KEEP 1.0 (average)
#   pattern:         50.0% → KEEP 1.0 (average, was 1.2 — reduced)
#   indicator:       50.0% → KEEP 1.2 (average but high conviction when right)
#   key_level:       44.6% → DAMPEN 1.2 → 0.8 (below 50% — losing money)
#   trend_follow:    27.3% → DISABLE 1.2 → 0.1 ( catastrophically bad —
#                     nearly disabled, kept at 0.1 for module breakdown
#                     display, but effectively zero weight)
# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-08): the DEFAULT trend_follow=0.1
# value NEVER fires for any of the 28 configured real pairs — every per-pair
# entry in PAIR_CONFIGS_REAL overrides trend_follow back to 1.1-1.3 (full
# weight). So the "effectively disabled" intent documented above is NOT
# actually applied on configured pairs. The per-pair overrides reflect the
# original (pre-2026-07-20) calibration where trend_follow was full-weight;
# they were not updated when DEFAULT was dampened to 0.1. To preserve
# current behavior (no breakage), the per-pair overrides are kept. To
# actually disable trend_follow on real pairs, set every per-pair
# `trend_follow` value to 0.1 (recommend a separate calibration batch with
# full backtest re-measurement first). Tracked as a follow-up — for now,
# this comment block makes the contradiction explicit so a future maintainer
# is not misled.
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.3,   # 54.3% win rate — BEST, boosted
    "running_tick":    1.0,   # 50.7% — average
    "pattern":         1.0,   # 50.0% — average (was 1.2, reduced)
    "indicator":       1.2,   # 50.0% — kept high (high conviction signals)
    "key_level":       0.8,   # 44.6% — BELOW 50%, dampened (was 1.2)
    "trend_follow":    0.1,   # 27.3% — WORST, effectively disabled (was 1.2)
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

    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-12): real exotics NOT configured.
    # feed._FOREX_BASES (computed by stripping _otc from _FOREX_OTC) yields 40
    # base symbols including 12 exotics (USDMXN, USDTRY, USDPKR, USDCOP, USDBDT,
    # INRUSD, EURSGD, BRLUSD, USDARS, USDDZD, USDBRL, USDIDR). PAIR_CONFIGS_REAL
    # only has 28 majors+minors. If Quotex ever publishes any exotic as a real
    # instrument (rare but possible), that pair falls back to DEFAULT_WEIGHTS_REAL.
    # The DEFAULT correctly dampens trend_follow to 0.1 (matching the 27.3% win
    # rate intent) but indicator/key_level/candle_reaction values are majors-tuned
    # — not ideal for exotics. The proper fix is to filter exotics OUT of
    # _FOREX_BASES in feed.py (out of scope for this config-only batch). Adding
    # explicit max_confidence:0 "do-not-trade" entries here was considered but
    # skipped because (a) the 12 entries are speculative (these pairs may never
    # be served as real), (b) max_confidence:0 would force NEUTRAL even if a
    # real exotic becomes tradeable in the future. Recommend a feed.py filter
    # follow-up that strips exotics from _FOREX_BASES so the Real engine never
    # sees them.
}


# ── Build the Real engine's PairWeightAdapter (instance, scoped to Real) ─
weight_adapter = PairWeightAdapter(
    pair_configs=PAIR_CONFIGS,
    default_weights=DEFAULT_WEIGHTS,
    engine_name="real",
)

# Module 6 for Real: trend_follow (momentum continuation detector)
# FIX (OTC-DEEP Phase 1, 2026-07-23): wrapper accepts optional `asset`
# arg for signature compat with the blender's new 3-arg call. trend_follow
# doesn't use it currently but the wrapper must accept it so the
# TypeError fallback in blender.py doesn't trigger.
def _module_6(candles, ctx, asset=""):
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
