"""
engines/real/config.py — Real market engine configuration.

Holds the Real-engine-specific bits that differ from the OTC engine:
  - PAIR_CONFIGS (per-pair static weight priors, keyed by BARE symbol)
  - DEFAULT_WEIGHTS (fallback when asset not in PAIR_CONFIGS — fires only
    for the 12 unconfigured exotic real bases; see Problem 4 / F-11-04 below)
  - RELIABILITY (reliability-tier multipliers — Real slightly boosts
    INDICATOR; TREND dampened to 1.0 — see F-11-14)
  - module_6 selection: trend_follow (momentum continuation detector)
  - module_names: tuple of 6 module names

REAL-MARKET TUNING:
Real-market pairs (live exchange prices, real order flow) reflect actual
liquidity moves — indicators and continuation patterns are MORE reliable
than in OTC. INDICATOR tier is 1.2 (was 1.3, dampened 2026-07-20 due to
overlap with trend_follow logic). TREND tier is 1.0 (was 1.3, dampened
2026-07-20 because trend_follow has 28.9% win rate — see DEFAULT_WEIGHTS
trend_follow note and the FIX comment at line 32 below). MICRO is 0.7
(was 0.6 in OTC; real tick microstructure reflects real volume).
FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-09 + AUDIT-5-10): the previous
header claimed INDICATOR=1.3 and TREND=1.3 — both stale. Updated to match
the actual RELIABILITY map values (1.2 and 1.0 respectively).
FIX (DEEP-AUDIT-2026-07-26 / F-11-14): the module docstring at the prior
line 7 still claimed "Real boosts INDICATOR & TREND" — TREND is dampened
to 1.0, not boosted. Header rewritten to match actual values. (Audit A-06
#14, #16.)

Everything else is imported from engines.base.

Audit reference: /home/z/my-project/download/audit/agent-06.md (real config
section: Problems 3, 4, 9, 11, 14-16, 24, 38-39, 50, 52, 61, 63, 65-72, 77).
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
# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-04): removed dead "STAT": 1.3
# entry — no module emits reliability="STAT".
# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-11): removed dead "OTC": 1.2
# entry — Real engine uses trend_follow (TREND tier), NOT otc_pattern
# (OTC tier); the entry was misleading dead code.
# FIX (DEEP-AUDIT-2026-07-26 / F-11-67): collapsed the prior 9-line FIX
# block (AUDIT-5-04 / AUDIT-5-11 removal notes) into the 4 lines above —
# entries already removed; archival history kept terse. (Audit A-06 #67.)
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
#   trend_follow:    28.9% → DISABLE 1.2 → 0.1 (catastrophically bad — kept
#                     at 0.1 only for module breakdown display purposes).
# Provenance: aggregated across all 28 configured real pairs, period=60s,
# graded signals from 2026-07-15 to 2026-07-23. Per-pair sample sizes vary;
# full breakdown is in the brain insights DB (signals.db, brain_learning
# table). Win rates are asset-weighted averages, not per-pair figures.
# FIX (DEEP-AUDIT-2026-07-26 / F-11-77): added provenance line above — prior
# block cited win rates without sample size, date range, or aggregation
# method. (Audit A-06 #77.)
# FIX (DEEP-AUDIT-2026-07-26 / F-11-09): corrected win-rate inconsistency —
# prior comment cited BOTH 27.3% and 28.9% for the same module on the same
# calibration date. The 28.9% figure (from the more recent FIX at line 32
# above) is authoritative; 27.3% was stale. (Audit A-06 #9.)
# FIX (DEEP-AUDIT-2026-07-26 / F-11-11): added usage clarification —
# DEFAULT_WEIGHTS only fires when an asset is NOT in PAIR_CONFIGS. All 28
# configured real majors+minors+crosses have explicit per-pair weights, so
# this dict is the fallback for the 12 unconfigured exotic real bases
# (USDMXN, USDTRY, USDPKR, … — see Problem 4 / F-11-04 below). It is NOT
# dead code, but it is NOT shaping predictions for the 28 configured pairs
# either. (Audit A-06 #11.)
# FIX (DEEP-AUDIT-2026-07-26 / F-11-03 / F-11-69): CONTRADICTION DOCUMENTED —
# the DEFAULT trend_follow=0.1 "effectively disabled" intent is NOT applied
# to configured real pairs because EVERY per-pair override in PAIR_CONFIGS
# below restores trend_follow to 1.1–1.3 (full weight). A 28.9%-win-rate
# module therefore still votes at full strength on every configured pair.
# Options considered: (a) set each per-pair `trend_follow` to 0.1, or
# (b) drop `trend_follow` from per-pair overrides so DEFAULT applies. NOTE:
# option (b) does NOT work as the audit suggests — the blender's
# `pair_weights.get(r.module_name, 1.0)` default (blender.py:510) returns
# 1.0 for missing keys, NOT DEFAULT_WEIGHTS. Only option (a) actually
# dampens trend_follow. Applying option (a) is a significant behavior
# change that could break current prediction calibration; deferred to a
# dedicated calibration batch with full backtest re-measurement. Until then,
# this comment makes the contradiction explicit. (Audit A-06 #3, #69.)
# TODO(F-11-CALIBRATION): re-measure trend_follow win rate per pair, then
# either set per-pair trend_follow to 0.1 (apply disable intent) or remove
# the "effectively disabled" claim from DEFAULT_WEIGHTS. Tracked.
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.3,   # 54.3% win rate — BEST, boosted
    "running_tick":    1.0,   # 50.7% — average
    "pattern":         1.0,   # 50.0% — average (was 1.2, reduced)
    "indicator":       1.2,   # 50.0% — kept high (high conviction signals)
    "key_level":       0.8,   # 44.6% — BELOW 50%, dampened (was 1.2)
    "trend_follow":    0.1,   # 28.9% — WORST, "effectively disabled" in DEFAULT only;
                              # per-pair overrides restore to 1.1-1.3 (see F-11-03 above)
}


# ── PER-PAIR MODEL CONFIGS — REAL MARKET ─────────────────────────────────
# Keyed by BARE symbol (no _otc suffix). These match the OTC PAIR_CONFIGS
# but with trend-favoring weights for the real-market twins.
#
# FIX (DEEP-AUDIT-2026-07-26 / F-11-38 / F-11-39): DUPLICATION DOCUMENTED —
# 12 "stable"-profile pairs (USDCHF, AUDUSD, USDCAD, NZDUSD, EURGBP, EURCHF,
# AUDCHF, AUDCAD, AUDNZD, CADCHF, NZDCAD, NZDCHF) share IDENTICAL weights
# (two sub-groups — indicator=1.2 vs indicator=1.1); 8 "volatile"-profile
# pairs (EURAUD, EURCAD, GBPAUD, GBPCAD, GBPCHF, GBPNZD, CHFJPY, EURNZD)
# share IDENTICAL weights. Refactor to shared `_REAL_STABLE_WEIGHTS` /
# `_REAL_VOLATILE_WEIGHTS` constants was considered but deferred — the
# copy-paste risk is low (weights are stable, not actively tuned) and a
# refactor would obscure per-pair intent. Documented here so a future
# maintainer knows the duplication is intentional. (Audit A-06 #38, #39.)
#
# FIX (DEEP-AUDIT-2026-07-26 / F-11-03-RECURRING): trend_follow per-pair
# overrides contradict the DEFAULT_WEIGHTS dampening intent. Every
# per-pair `trend_follow: 1.1` or `1.2` or `1.3` below should be 0.1 to
# match the documented "effectively disabled" intent (28.9% win rate).
# Kept at full weight to preserve current calibration; see DEFAULT_WEIGHTS
# F-11-03 comment above and TODO(F-11-CALIBRATION) for the deferred fix.
PAIR_CONFIGS = {
    # ── MAJORS (real-market: 3 trending + 4 stable; indicators reliable) ─
    # FIX (DEEP-AUDIT-2026-07-26 / F-11-61 / F-11-63): corrected section
    # header — prior comment claimed "MAJORS (real-market trending behavior,
    # indicators reliable)" but only EURUSD, GBPUSD, USDJPY are "trending";
    # USDCHF, AUDUSD, USDCAD, NZDUSD are "stable". Header now reflects the
    # actual mix. (Audit A-06 #61, #63.)
    "EURUSD": {
        "profile": "trending",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.0,
            "pattern":         1.2,
            "indicator":       1.4,   # EURUSD respects indicators well
            "key_level":       1.2,
            "trend_follow":    1.2,   # TODO(F-11-CALIBRATION): should be 0.1 per DEFAULT intent (28.9% win rate) — see F-11-03 above
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
    # The DEFAULT correctly dampens trend_follow to 0.1 (matching the 28.9% win
    # rate intent — see F-11-09 above for the corrected figure) but
    # indicator/key_level/candle_reaction values are majors-tuned — not ideal
    # for exotics. The proper fix is to filter exotics OUT of _FOREX_BASES in
    # feed.py (out of scope for this config-only batch). Adding explicit
    # max_confidence:0 "do-not-trade" entries here was considered but skipped
    # because (a) the 12 entries are speculative (these pairs may never be
    # served as real), (b) max_confidence:0 would force NEUTRAL even if a real
    # exotic becomes tradeable in the future. Recommend a feed.py filter
    # follow-up that strips exotics from _FOREX_BASES so the Real engine never
    # sees them.
    # FIX (DEEP-AUDIT-2026-07-26 / F-11-04 / F-11-70): added actionable
    # TODO/FIXME tag below so the feed.py follow-up is trackable. Corrected
    # stale "27.3%" win rate citation to 28.9% (per F-11-09 above). (Audit
    # A-06 #4, #70.)
    # TODO(feed.py): filter exotics out of _FOREX_BASES so the Real engine
    # never sees them — see comment block above for the 12 symbols to strip.
}


# ── Build the Real engine's PairWeightAdapter (instance, scoped to Real) ─
# FIX (DEEP-AUDIT-2026-07-26 / F-11-24): import-time instantiation is safe —
# `PairWeightAdapter.__init__` only stores references and validates
# `pair_configs` shape (no DB access). The DB is only queried lazily on the
# first `get_weights()` call. Future refactors that add DB initialization to
# `__init__` would break import; document the constraint here. (Audit A-06
# #24.)
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
# FIX (DEEP-AUDIT-2026-07-26 / F-11-71 / F-11-72): `asset=""` default is
# technically dead (blender always passes the actual asset name) and the
# `asset` parameter is unused. Kept as-is — dropping the parameter would
# force the blender's TypeError fallback on EVERY prediction (slow). The
# wrapper accepting-but-ignoring asset is the documented contract. (Audit
# A-06 #71, #72.)
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
