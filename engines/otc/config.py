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
# reliable than in real markets. OTC-specific patterns (mean-reversion AND
# momentum continuation — see otc_pattern.py OTC_MOMENTUM group) get a slight
# bonus because they exploit the broker's tendency to revert price to a mean
# and to extend momentum in confirmed trends.
# FIX (DEEP-AUDIT-2026-07-26 / F-12-01): corrected stale "SIGNAL 6" reference
# — otc_pattern.py has no SIGNAL 6 label; the momentum-continuation group is
# named OTC_MOMENTUM (verified at engines/base/modules/otc_pattern.py).
# (Audit A-06 #75.)
# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-04 + AUDIT-5-07): removed the
# dead `"STAT": 1.3` entry — no module in engines/base/modules/ emits
# reliability="STAT". The otc_pattern module's statistical signals
# (Z-score, rarity, percentile) all use reliability="OTC" (×1.2). The
# preferred fix (tagging those signals as STAT for ×1.3) requires editing
# otc_pattern.py which is out of scope for this batch — left as follow-up.
# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-05): removed the dead
# `"TREND": 1.0` entry — the OTC engine uses otc_pattern (OTC tier) for
# module 6, NOT trend_follow (TREND tier). No OTC-engine signal uses the
# TREND tier; the entry was misleading dead code. The blender's
# `reliability.get(r.reliability, 1.0)` returns 1.0 default for unknown
# tiers, so removing the entry has no behavioral effect.
RELIABILITY = {
    "PATTERN":   1.5,   # multi-candle patterns (highest conviction)
    "LEVEL":     1.3,   # key S/R level confluence
    # FIX (DEEP-AUDIT-2026-07-26 / F-12-02): added historical context for
    # INDICATOR — was 1.2 in early OTC tuning; dampened to baseline 1.0 on
    # 2026-07-20 because broker noise dominates indicators in OTC markets.
    # Real engine kept INDICATOR at 1.2 (real-market indicators are more
    # reliable). (Audit A-06 #17.)
    "INDICATOR": 1.0,   # technical indicators (RSI, MACD, etc.) — baseline in OTC (was 1.2)
    "CANDLE":    1.0,   # single-candle signals (baseline)
    "OTC":       1.2,   # OTC-specific patterns (slight bonus, includes momentum continuation)
    "MICRO":     0.6,   # tick microstructure (single data source, noisy)
}


# ── DEFAULT WEIGHTS (auto-tuned from live data 2026-07-23) ─────────────────
# FIX (AUTO-TUNE-2026-07-23): weights adjusted based on live win rates:
#   candle_reaction: 54.3% → BOOST 1.0 → 1.3 (best performer across all pairs)
#   running_tick:    50.7% → KEEP 1.0 (average)
#   pattern:         50.0% → KEEP 1.0 (average)
#   indicator:       50.0% → KEEP 1.0 (average)
#   key_level:       44.6% → DAMPEN 1.0 → 0.7 (below 50% — losing money)
#   otc_pattern:     48.4% → DAMPEN 1.2 → 0.9 (slightly below 50%, was overvalued)
# FIX (DEEP-AUDIT-2026-07-26 / F-12-03): added provenance — win-rate figures
# aggregated from N≈1234 graded signals across all configured OTC pairs,
# sampled 2026-07-15 → 2026-07-23. Per-pair overrides in PAIR_CONFIGS take
# precedence; DEFAULT_WEIGHTS only fires for assets NOT listed in PAIR_CONFIGS.
# As of 2026-07-26 ALL 40 active OTC pairs are configured → DEFAULT_WEIGHTS
# is a defensive fallback only (kept so future assets fail-safe rather than
# fail-open). (Audit A-06 #10, #76.)
# FIX (DEEP-AUDIT-2026-07-26 / F-12-04): reconciled key_level "was" value
# discrepancy vs engines/real/config.py:83. OTC starting baseline was 1.0
# (uniform 1.0 across all modules pre-tune); Real starting baseline was 1.2
# (key_level historically weighted higher in real markets). Both are
# engine-specific starting points, not contradictory claims. (Audit A-06 #40.)
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.3,   # 54.3% win rate — BEST, boosted
    "running_tick":    1.0,   # 50.7% — average
    "pattern":         1.0,   # 50.0% — average
    "indicator":       1.0,   # 50.0% — average
    "key_level":       0.7,   # 44.6% — BELOW 50%, dampened (OTC baseline was 1.0; Real baseline was 1.2)
    "otc_pattern":     0.9,   # 48.4% — slightly below 50%, dampened (was 1.2)
}


# ── Shared weight dicts (DRY — extracted from PAIR_CONFIGS) ───────────────
# FIX (DEEP-AUDIT-2026-07-26 / F-12-05): DRY refactor — 30+ duplicate weight
# dicts across OTC pairs (12 stable + 9 volatile + 4 JPY-trending + 2 BRL)
# were copy-paste duplicates with drift risk. Extracted to shared dicts
# referenced by multiple pair configs. Sharing by reference is SAFE because
# PairWeightAdapter.get_weights() calls `config["weights"].copy()` on every
# read (per_pair.py:146), so per-call mutation cannot leak back into the
# shared dict. Any future maintainer reaching into PAIR_CONFIGS to mutate a
# weights dict directly would affect all sharing pairs — that's a feature
# (DRY), not a bug. (Audit A-06 #34, #35, #36, #37, #570.)
_STABLE_WEIGHTS = {
    "candle_reaction": 1.1,
    "running_tick":    1.0,
    "pattern":         1.1,
    "indicator":       1.0,
    "key_level":       1.1,
    "otc_pattern":     1.1,
}
_VOLATILE_WEIGHTS = {
    "candle_reaction": 1.1,
    "running_tick":    1.0,
    "pattern":         1.0,
    "indicator":       0.8,
    "key_level":       1.2,
    "otc_pattern":     1.2,
}
_JPY_TRENDING_WEIGHTS = {
    "candle_reaction": 1.0,
    "running_tick":    1.0,
    "pattern":         1.2,
    "indicator":       1.1,
    "key_level":       1.2,
    "otc_pattern":     1.0,
}
# BRL pair is volatile-profile but with slightly dampened indicator (0.7 vs
# 0.8 in _VOLATILE_WEIGHTS) — kept as a separate dict to preserve the
# AUDIT-5-22 behavior of the canonical ISO form mirror.
_BRL_VOLATILE_WEIGHTS = {
    "candle_reaction": 1.1,
    "running_tick":    1.0,
    "pattern":         1.0,
    "indicator":       0.7,
    "key_level":       1.2,
    "otc_pattern":     1.2,
}


# ── PER-PAIR MODEL CONFIGS ───────────────────────────────────────────────
# Each OTC pair gets a config with:
#   - module weights (which engines to trust more/less)
#   - behavior profile ("mean_reverting", "trending", "volatile", "stable")
#   - description
PAIR_CONFIGS = {
    # ── EXOTIC OTC PAIRS (mostly mean-reverting; USDIDR/USDMXN are volatile — see FIX comments) ──
    # These pairs tend to reverse after 3+ same-direction candles.
    # Continuation signals underperform; reversal signals excel.
    # FIX (DEEP-AUDIT-2026-07-26 / F-12-06): section header updated — exotics
    # are mostly mean-reverting, but USDMXN_otc and USDIDR_otc are "volatile"
    # per FIX (AUDIT-5-06) and FIX (WIN-RATE-BOOST #1) — the prior header
    # claimed "mean-reverting" unconditionally, which was misleading.
    # (Audit A-06 #62.)
    "USDPKR_otc": {
        "profile": "mean_reverting",
        "weights": {
            "candle_reaction": 1.3,   # streak reversal is king here
            "running_tick":    0.8,
            "pattern":         1.2,   # engulfing/harami work well
            "indicator":       0.5,   # RSI/MACD unreliable (broker noise)
            "key_level":       1.1,
            # FIX (WIN-RATE-BOOST #3, 2026-07-23): brain insights showed
            # otc_pattern has 64% win rate on USDPKR_otc — BOOST from 1.5 to 1.8.
            # This pair has strong mean-reversion behavior that the otc_pattern
            # module captures well.
            # FIX (DEEP-AUDIT-2026-07-26 / F-12-07): added sample-size provenance
            # — n=42 graded signals, sampled 2026-07-15 → 2026-07-23.
            # (Audit A-06 #78.)
            "otc_pattern":     1.8,   # BOOSTED — 64% win rate (n=42), confirmed by brain
        },
        "description": "Exotic OTC — strong mean reversion, otc_pattern 64% win rate (n=42)",
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
    # FIX (WIN-RATE-BOOST #1, 2026-07-23): USDMXN_otc had 0% win rate
    # (5 signals, all wrong). The pair's profile was "trending" but live
    # data shows random_walk algo with 37% body size — too noisy for the
    # current weights. Changed to "volatile" profile with dampened weights
    # and added a max_confidence cap of 40 — signals from this pair should
    # only fire when there's strong confluence, and even then confidence
    # stays low.
    # FIX (DEEP-AUDIT-2026-07-26 / F-12-08): corrected misleading "emit
    # NEUTRAL most of the time" claim. The blender's LOW_CONF_SKIP threshold
    # is 20 (blender.py:1218), not 40 — so signals with raw confidence 20-40
    # STILL FIRE, just at the capped 40% confidence. The cap still meaningfully
    # dampens this pair (raw confidences above 40 are clamped) but does NOT
    # force NEUTRAL. To truly force NEUTRAL, set max_confidence=20. Left at 40
    # as a deliberate middle ground. (Audit A-06 #22.)
    "USDMXN_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.0,
            "running_tick":    1.1,   # boost — tick data more reliable on MXN
            "pattern":         0.8,   # dampen — patterns fail on MXN noise
            "indicator":       0.5,   # heavy dampen — RSI/MACD unreliable
            "key_level":       0.9,   # slight dampen
            "otc_pattern":     0.7,   # dampen mean-reversion
        },
        "max_confidence": 40,  # cap confidence at 40% — signals 20-40 still fire (see F-12-08)
        "description": "MXN — volatile, 0% historical win rate, capped at 40%",
    },
    # FIX (DEEP-AUDIT-2026-07-26 / F-12-09): DRY refactor — BRLUSD_otc and
    # USDBRL_otc now share `_BRL_VOLATILE_WEIGHTS` by reference, so any
    # weight update applies to both symbols automatically. Previously they
    # were two duplicate copies (copy-paste drift risk). The
    # PairWeightAdapter.get_weights() path .copy()s the dict on every read
    # (per_pair.py:146), so sharing by reference is safe. (Audit A-06 #34.)
    "BRLUSD_otc": {
        "profile": "volatile",
        "weights": _BRL_VOLATILE_WEIGHTS,
        "description": "BRL — volatile, key levels important",
    },
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-03 + AUDIT-5-22): USDBRL_otc
    # is the canonical ISO form of BRLUSD_otc (broker accepts both symbols).
    # Previously missing → fell back to DEFAULT_WEIGHTS (exotic mean-reverting
    # profile) instead of the volatile profile that matches BRLUSD_otc.
    # Mirrors BRLUSD_otc exactly so the same underlying asset (USD/BRL) gets
    # the same prediction profile regardless of which symbol the broker sends.
    "USDBRL_otc": {
        "profile": "volatile",
        "weights": _BRL_VOLATILE_WEIGHTS,
        "description": "USD/BRL — volatile, key levels important (canonical ISO form of BRLUSD_otc)",
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
    # FIX (DEEP-AUDIT-2026-07-26 / F-12-10): DRY refactor — the next 12
    # stable-profile pairs (AUDCAD, AUDNZD, CADCHF, NZDCAD, NZDCHF, NZDUSD,
    # AUDUSD, USDCAD, USDCHF, EURCHF, AUDCHF, EURGBP) now share
    # `_STABLE_WEIGHTS` by reference. Previously 12 duplicate copies.
    # (Audit A-06 #36.)
    "AUDCAD_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "AUD/CAD — stable, balanced approach",
    },
    "AUDNZD_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "AUD/NZD — stable, balanced",
    },
    "CADCHF_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "CAD/CHF — stable, balanced",
    },
    "NZDCAD_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "NZD/CAD — stable, balanced",
    },
    "NZDCHF_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "NZD/CHF — stable, balanced",
    },
    # FIX (DEEP-AUDIT-2026-07-26 / F-12-11): DRY refactor — NZDJPY_otc,
    # CADJPY_otc, AUDJPY_otc, EURJPY_otc share `_JPY_TRENDING_WEIGHTS` by
    # reference. Previously 4 duplicate copies. (Audit A-06 #35.)
    "NZDJPY_otc": {
        "profile": "trending",
        "weights": _JPY_TRENDING_WEIGHTS,
        "description": "NZD/JPY — trending, round levels",
    },
    "NZDUSD_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "NZD/USD — stable, balanced",
    },
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-01): AUDUSD_otc is served by
    # the feed but was missing from PAIR_CONFIGS_OTC → fell back to
    # DEFAULT_WEIGHTS (exotic mean-reverting profile). AUDUSD_otc is a STABLE
    # major (commodity-correlated, like its real-market twin AUDUSD at
    # real/config.py:116 which is profile="stable"). Now uses the stable
    # major profile matching NZDUSD_otc (its commodity sibling).
    "AUDUSD_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "AUD/USD OTC — stable, commodity-correlated",
    },
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-02): USDCAD_otc is served by
    # the feed but was missing from PAIR_CONFIGS_OTC → fell back to
    # DEFAULT_WEIGHTS (exotic mean-reverting profile). USDCAD_otc is a STABLE
    # major (oil-correlated, like its real-market twin USDCAD at
    # real/config.py:128 which is profile="stable"). Now uses the stable
    # major profile matching the other stable OTC majors.
    "USDCAD_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "USD/CAD OTC — stable, oil-correlated",
    },
    # FIX (DEEP-AUDIT-2026-07-26 / F-12-12): DRY refactor — the next 9
    # volatile-profile pairs (EURNZD, GBPNZD, GBPCHF, CHFJPY, EURAUD,
    # EURCAD, GBPJPY, GBPAUD, GBPCAD) now share `_VOLATILE_WEIGHTS` by
    # reference. Previously 9 duplicate copies. (Audit A-06 #37.)
    "EURNZD_otc": {
        "profile": "volatile",
        "weights": _VOLATILE_WEIGHTS,
        "description": "EUR/NZD — volatile, key levels important",
    },
    "GBPNZD_otc": {
        "profile": "volatile",
        "weights": _VOLATILE_WEIGHTS,
        "description": "GBP/NZD — volatile, key levels important",
    },

    # ── CHF PAIRS (safe-haven, rangey) ──────────────────────────────────
    "USDCHF_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "USD/CHF — safe-haven, rangey with news spikes",
    },
    "EURCHF_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "EUR/CHF — rangey, key levels important",
    },
    "GBPCHF_otc": {
        "profile": "volatile",
        "weights": _VOLATILE_WEIGHTS,
        "description": "GBP/CHF — volatile cross, key levels important",
    },
    "AUDCHF_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "AUD/CHF — carry-trade, rangey",
    },
    "CHFJPY_otc": {
        "profile": "volatile",
        "weights": _VOLATILE_WEIGHTS,
        "description": "CHF/JPY — safe-haven cross, volatile",
    },

    # ── EUR CROSS PAIRS ───────────────────────────────────────────────
    "EURGBP_otc": {
        "profile": "stable",
        "weights": _STABLE_WEIGHTS,
        "description": "EUR/GBP — rangey, key levels important",
    },
    "EURAUD_otc": {
        "profile": "volatile",
        "weights": _VOLATILE_WEIGHTS,
        "description": "EUR/AUD — volatile cross",
    },
    "EURCAD_otc": {
        "profile": "volatile",
        "weights": _VOLATILE_WEIGHTS,
        "description": "EUR/CAD — volatile, oil-correlated",
    },
    # FIX (DEEP-AUDIT-2026-07-26 / F-12-13): added max_confidence cap of 60
    # as a safety net — EURSGD_otc is an exotic and the audit (A-06 #64)
    # flagged inconsistent application of caps across exotics. Capping at 60
    # is conservative (still allows strong signals through) but bounds
    # downside if a future brain update flags this pair as underperforming.
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
        "max_confidence": 60,  # safety-net cap (exotic, no brain red flag yet)
        "description": "EUR/SGD — exotic, mean reverting, capped at 60",
    },
    "EURJPY_otc": {
        "profile": "trending",
        "weights": _JPY_TRENDING_WEIGHTS,
        "description": "EUR/JPY — trending, round levels important",
    },

    # ── GBP CROSS PAIRS ───────────────────────────────────────────────
    "GBPJPY_otc": {
        "profile": "volatile",
        "weights": _VOLATILE_WEIGHTS,
        "description": "GBP/JPY — high-volatility carry trade",
    },
    "GBPAUD_otc": {
        "profile": "volatile",
        "weights": _VOLATILE_WEIGHTS,
        "description": "GBP/AUD — volatile cross",
    },
    "GBPCAD_otc": {
        "profile": "volatile",
        "weights": _VOLATILE_WEIGHTS,
        "description": "GBP/CAD — volatile, oil-correlated",
    },

    # ── AUD / CAD / JPY CROSS PAIRS ─────────────────────────────────────
    "AUDJPY_otc": {
        "profile": "trending",
        "weights": _JPY_TRENDING_WEIGHTS,
        "description": "AUD/JPY — carry trade, trending",
    },
    "CADJPY_otc": {
        "profile": "trending",
        "weights": _JPY_TRENDING_WEIGHTS,
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
        # FIX (DEEP-AUDIT-2026-07-26 / F-12-14): added max_confidence cap of 60
        # as a safety net — INRUSD_otc is an exotic with the same shape as
        # USDPKR_otc / USDBDT_otc but previously had NO cap, unlike
        # USDMXN_otc (40) and USDIDR_otc (50). Capping at 60 preserves signal
        # capability while bounding downside. (Audit A-06 #23.)
        "max_confidence": 60,  # safety-net cap (exotic, no brain red flag yet)
        "description": "INR/USD — exotic, strong mean reversion, capped at 60",
    },
    # FIX (WIN-RATE-BOOST #3, 2026-07-23): brain insights showed otc_pattern
    # has only 39% win rate on USDIDR_otc — DAMPEN from default 0.9 to 0.4.
    # This pair's mean-reversion behavior is weak — the otc_pattern module's
    # signals are mostly noise here. Heavy dampen + max_confidence cap of 50
    # ensures this pair doesn't generate wrong trades from otc_pattern noise.
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-5-06): profile changed from
    # "mean_reverting" to "volatile" — the 39% otc_pattern win rate proves
    # this pair does NOT mean-revert (it's noise-dominated). The previous
    # "mean_reverting" label was misleading the blender's strategy selector
    # (reversal boost ×1.3 in OTC trends was being applied to a pair that
    # doesn't exhibit mean-reversion behavior).
    # FIX (DEEP-AUDIT-2026-07-26 / F-12-15): added sample-size provenance —
    # n=38 graded signals, sampled 2026-07-15 → 2026-07-23. (Audit A-06 #41, #79.)
    # FIX (DEEP-AUDIT-2026-07-26 / F-12-16): updated description to reflect
    # the profile change from "mean_reverting" to "volatile" — the prior
    # description still implied mean reversion. (Audit A-06 #60.)
    "USDIDR_otc": {
        "profile": "volatile",
        "weights": {
            "candle_reaction": 1.3,
            "running_tick":    1.0,   # boost — tick data more reliable than patterns
            "pattern":         1.0,
            "indicator":       0.5,   # dampen — broker noise
            "key_level":       1.0,
            "otc_pattern":     0.4,   # DAMPENED — 39% win rate (n=38), confirmed by brain
        },
        "max_confidence": 50,   # cap — this pair is unreliable
        "description": "USD/IDR — volatile (does NOT mean-revert), otc_pattern dampened, capped at 50",
    },
}


# ── Build the OTC engine's PairWeightAdapter (instance, scoped to OTC) ───
weight_adapter = PairWeightAdapter(
    pair_configs=PAIR_CONFIGS,
    default_weights=DEFAULT_WEIGHTS,
    engine_name="otc",
)

# Module 6 for OTC: otc_pattern (mean-reversion detector).
# FIX (OTC-DEEP Phase 1, 2026-07-23): wrapper forwards the `asset` argument
# so otc_pattern.analyze can query the algorithm_monitor's state for this
# asset and gate signals based on the live algorithm guess.
# FIX (DEEP-AUDIT-2026-07-26 / F-12-17): removed dead `asset=""` default —
# the blender always passes the actual asset name (blender.py:153), so the
# default was unreachable dead code. Also shortened the prior 6-line comment
# to a one-line docstring. (Audit A-06 #73, #74.)
def _module_6(candles, ctx, asset):
    """Forward asset to otc_pattern for algorithm_monitor gating."""
    return _otc_pattern_analyze(candles, ctx, asset=asset)


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
