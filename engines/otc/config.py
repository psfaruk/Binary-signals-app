"""engines/otc/config.py — DATA-DRIVEN CONFIG (USER-AUG-2026 V3).

FIX (USER-AUG-2026): Activated 7 previously-dead modules + added 4 new
strategy modules with pair-specific weights based on web research
(Task 3 findings).

Previous DEEP-FIX-2026-08-07 V2 had ONLY pattern (2.5) + wickwall (1.5)
active — all 7 other modules at weight 0.0. This caused:
  1. Wasted compute (modules ran but votes were suppressed)
  2. No multi-strategy confirmation (single-module signals are ~47% WR)
  3. Forced reliance on coin-flip fallback for ~60% of candles

New approach: ALL modules active with calibrated weights. Pair-specific
tuning based on Task 3 research findings (which strategies work best on
which OTC pairs).

NOTE on OTC pairs: Quotex OTC pairs are USD-against-exotic-currency pairs
(USDZAR, USDMXN, USDIDR etc.) plus NZDUSD. They exhibit high volatility
and frequent mean-reversion. The research findings (BB+RSI+Engulfing,
S/R bounce, Stochastic) apply directly to these pairs.

Module weight guide:
  3.0 = primary strategy for this pair (proven edge, ~60%+ WR)
  2.0 = strong secondary strategy
  1.5 = supporting confirmation
  1.0 = weak contributor
  0.0 = disabled (proven to lose money on this pair)
"""
from engines.base.blender import BlenderConfig
from engines.base.per_pair import PairWeightAdapter
from core.constants import OTC_MODULES as _MODULES, ALLOWED_PAIRS_OTC

RELIABILITY = {
    "PATTERN": 1.4, "LEVEL": 1.0, "CANDLE": 1.0, "MICRO": 0.7,
}

# Default weights — all modules active with balanced weights.
# Pair-specific overrides below tune these per asset.
DEFAULT_WEIGHTS = {
    "candle_reaction": 1.0,   # body reaction (was 0.0 — now active as baseline)
    "pattern":         3.0,   # proven 63.5% WR — strongest module
    "key_level":       1.5,   # S/R levels (was 0.0 — now active)
    "market_state":    0.8,   # regime awareness (was 0.0)
    "wickwall":        2.0,   # proven 55.6% WR — secondary anchor
    "divergence":      1.0,   # RSI/MACD divergence (was 0.0)
    "tickrun":         0.8,   # tick momentum (was 0.0)
    "multi_tf":        1.2,   # HTF confirmation (was 0.0)
    "momentum":        1.5,   # RSI + MACD (was 0.0 — was 0.0!)
    # FIX (USER-AUG-2026 / STRATEGY-EXPANSION): 4 new modules
    "bollinger_rsi":   2.0,   # BB+RSI+Engulfing — 60-70% expected WR (highest)
    "stochastic":      1.5,   # Stoch crossover — 55-60% expected WR
    "ema_ribbon":      1.2,   # EMA(5/8/13) trend — 55-62% expected WR
    "sr_bounce":       1.8,   # S/R + candle confirm — 60-68% expected WR
}

# Pair-specific strategy weights based on Task 3 research.
# Each pair has its strongest strategies boosted and weakest dampened.
PAIR_CONFIGS = {
    # ─── High-volatility exotic OTC pairs (S/R + BB dominant) ───────────
    "USDZAR_otc": {
        "profile": "volatile", "description": "USDZAR-OTC: high volatility — S/R + BB",
        "weights": {**DEFAULT_WEIGHTS,
                    "sr_bounce": 2.5,       # S/R bounces strong on volatile pairs
                    "bollinger_rsi": 2.2,
                    "stochastic": 1.8,
                    "momentum": 1.0},       # MACD laggy on volatile pairs
    },
    "BRLUSD_otc": {
        "profile": "calibrated", "description": "BRLUSD-OTC: live 59.1% WR — keep balanced",
        "weights": {**DEFAULT_WEIGHTS,
                    "bollinger_rsi": 2.2,
                    "sr_bounce": 2.0,
                    "stochastic": 1.5},
    },
    "USDIDR_otc": {
        "profile": "calibrated", "description": "USDIDR-OTC: live 58.8% WR — keep balanced",
        "weights": {**DEFAULT_WEIGHTS,
                    "bollinger_rsi": 2.0,
                    "sr_bounce": 1.8,
                    "stochastic": 1.5},
    },
    "NZDUSD_otc": {
        "profile": "calibrated", "description": "NZDUSD-OTC: most liquid OTC — BB+RSI primary",
        "weights": {**DEFAULT_WEIGHTS,
                    "bollinger_rsi": 2.5,
                    "sr_bounce": 2.2,
                    "pattern": 3.0,
                    "ema_ribbon": 1.0},
    },
    "USDCOP_otc": {
        "profile": "volatile", "description": "USDCOP-OTC: live 33.3% WR — try aggressive mean-reversion",
        "weights": {**DEFAULT_WEIGHTS,
                    "bollinger_rsi": 2.5,
                    "stochastic": 2.0,
                    "sr_bounce": 2.0,
                    "ema_ribbon": 0.5,      # trend-following losing here
                    "pattern": 2.0,
                    "wickwall": 1.5},
    },
    "USDBDT_otc": {
        "profile": "default", "description": "USDBDT-OTC: default balanced",
        "weights": DEFAULT_WEIGHTS,
    },
    "USDPKR_otc": {
        "profile": "default", "description": "USDPKR-OTC: default balanced",
        "weights": DEFAULT_WEIGHTS,
    },
    "USDMXN_otc": {
        "profile": "volatile", "description": "USDMXN-OTC: high volatility — S/R + BB",
        "weights": {**DEFAULT_WEIGHTS,
                    "sr_bounce": 2.5,
                    "bollinger_rsi": 2.2,
                    "stochastic": 1.8,
                    "momentum": 1.0},
    },
    "USDDZD_otc": {
        "profile": "default", "description": "USDDZD-OTC: default balanced",
        "weights": DEFAULT_WEIGHTS,
    },
    "USDINR_otc": {
        "profile": "calibrated", "description": "USDINR-OTC: live 60.7% WR — keep balanced",
        "weights": {**DEFAULT_WEIGHTS,
                    "bollinger_rsi": 2.0,
                    "sr_bounce": 1.8,
                    "stochastic": 1.5},
    },
    "USDPHP_otc": {
        "profile": "default", "description": "USDPHP-OTC: default balanced",
        "weights": DEFAULT_WEIGHTS,
    },
}

_config_keys = set(PAIR_CONFIGS.keys())
_allowlist_keys = set(ALLOWED_PAIRS_OTC)
assert _config_keys == _allowlist_keys, (
    f"PAIR_CONFIGS mismatch ALLOWED_PAIRS_OTC. "
    f"Extra: {_config_keys - _allowlist_keys}. Missing: {_allowlist_keys - _config_keys}."
)

weight_adapter = PairWeightAdapter(
    pair_configs=PAIR_CONFIGS, default_weights=DEFAULT_WEIGHTS)

CONFIG = BlenderConfig(
    reliability=RELIABILITY, weight_adapter=weight_adapter,
    module_names=_MODULES, engine_name="otc")
