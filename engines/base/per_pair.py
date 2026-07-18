"""
engines/base/per_pair.py — Generic per-pair weight adapter.

Takes a `pair_configs` dict and `default_weights` dict (provided by the
engine's config.py) and blends them with DB-learned per-module win rates.

This eliminates the duplication between engines/otc/per_pair.py and
engines/real/per_pair.py — they were 95% identical, only differing in
the PAIR_CONFIGS and DEFAULT_WEIGHTS data (and the dead
`invalidate_adaptation_cache()` function which was never called).

The engine-specific config.py files import `PairWeightAdapter` from here
and instantiate it with their own PAIR_CONFIGS / DEFAULT_WEIGHTS.
"""
import os
import threading
import time

# Tunables (env-overridable). Same meaning as before — kept identical so
# existing Railway env vars continue to work without changes.
_ADAPT_MIN_SAMPLES = int(os.environ.get("ADAPT_MIN_SAMPLES", "20"))
_ADAPT_PRIOR = float(os.environ.get("ADAPT_PRIOR", "0.7"))
_ADAPT_CAP = float(os.environ.get("ADAPT_CAP", "0.30"))
_ADAPT_CACHE_TTL = float(os.environ.get("ADAPT_CACHE_TTL", "60"))


class PairWeightAdapter:
    """Per-pair module weight adapter, scoped to a specific engine config.

    One instance per engine (OTC, Real). Holds its own PAIR_CONFIGS,
    DEFAULT_WEIGHTS, and DB-adaptation cache — so OTC and Real adaptation
    never collide (they query different (asset, period) buckets anyway,
    but the cache isolation is extra safety).
    """

    def __init__(self, pair_configs: dict, default_weights: dict,
                 engine_name: str = "base"):
        """
        Args:
            pair_configs: dict[asset] = {"profile": str, "weights": dict, "description": str}
            default_weights: dict[module_name] = float (fallback when asset not in pair_configs)
            engine_name: short label for debug logs (e.g. "otc", "real")
        """
        self.pair_configs = pair_configs
        self.default_weights = default_weights
        self.engine_name = engine_name
        # Per-instance DB-adaptation cache. Keyed by (asset, period).
        self._adapt_cache: dict = {}
        self._lock = threading.Lock()

    def get_weights(self, asset: str, period: int = 60, use_db: bool = True) -> dict:
        """Get module weights for a specific asset.

        Combines the static PAIR_CONFIGS prior with DB-learned per-module
        win-rate adaptation. When `use_db` is True (default) AND enough
        graded samples exist for this (asset, period), modules with
        win_rate > 0.60 get boosted up to +30%, and modules with
        win_rate < 0.45 get dampened up to -30%. Adaptation is capped
        and prior-weighted (70% static / 30% learned) to avoid
        overfitting on small or noisy samples.

        Args:
            asset: pair name (e.g. "EURUSD_otc", "EURUSD")
            period: candle period in seconds (default 60)
            use_db: set False to skip DB lookup (used by /api/stats and tests)

        Returns:
            dict mapping module name → weight multiplier
        """
        config = self.pair_configs.get(asset)
        base = config["weights"].copy() if config else self.default_weights.copy()

        if not use_db:
            return base

        # Cache lookup — the prediction path is hot (every candle close
        # across many streams), and per_module_accuracy does a SQL scan
        # each call. Acquire the lock only around the cache mutation, not
        # the DB read (which can be slow).
        now = time.time()
        cache_key = (asset, period)
        with self._lock:
            cached = self._adapt_cache.get(cache_key)
            if cached and (now - cached["ts"]) < _ADAPT_CACHE_TTL:
                return cached["weights"]

        adapted = self._adapt_from_db(base, asset, period)

        with self._lock:
            self._adapt_cache[cache_key] = {"ts": now, "weights": adapted.copy()}
        return adapted

    def _adapt_from_db(self, static_weights: dict, asset: str, period: int) -> dict:
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
            adapted[module] = round(blended, 2)

        return adapted

    def invalidate_cache(self, asset: str = None, period: int = None):
        """Clear the DB-adaptation cache.

        Called after a batch of new signal log writes so the next
        prediction reflects fresh accuracy data. Safe to call from any
        thread.
        """
        with self._lock:
            if asset is None:
                self._adapt_cache.clear()
            else:
                keys_to_drop = [k for k in self._adapt_cache
                                if k[0] == asset and (period is None or k[1] == period)]
                for k in keys_to_drop:
                    self._adapt_cache.pop(k, None)

    def get_profile(self, asset: str) -> str:
        """Get the behavior profile for an asset.

        Returns one of: "mean_reverting", "trending", "volatile",
        "stable", "default"
        """
        config = self.pair_configs.get(asset)
        if config:
            return config["profile"]
        return "default"
