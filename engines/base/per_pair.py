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

        FIX (LIVE-DB-AUDIT-2026-07-25 / AUDIT-LIVE-1-03): also read
        brain_learning recommended_weight and blend it in. Previously
        brain_learning was computed but NEVER read by any code — the brain
        "learned" for 4-5 days and the engine ignored every recommendation.
        Now we blend 50% DB-stats-adapted + 50% brain-recommended when
        brain_learning has data (>= 30 samples).
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

        # FIX (AUDIT-LIVE-1-03): read brain_learning recommended_weight
        brain_recs = self._read_brain_learning(asset)

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

            # FIX (AUDIT-LIVE-1-03): also blend in brain_learning
            # recommended_weight if available. The brain uses more samples
            # and per-pair granularity, so give it significant weight.
            brain_rec = brain_recs.get(module)
            if brain_rec and brain_rec.get("total", 0) >= 30:
                brain_w = brain_rec["recommended_weight"]
                # 50/50 blend of DB-stats-adapted and brain-recommended
                blended = 0.5 * blended + 0.5 * brain_w

            adapted[module] = round(blended, 2)

        return adapted

    def _read_brain_learning(self, asset: str) -> dict:
        """Read brain_learning recommended_weight per module for an asset.

        FIX (AUDIT-LIVE-1-03): previously this method did not exist —
        brain_learning was write-only. Now per_pair adapter reads it.

        Returns:
            dict: {module_name: {recommended_weight, win_rate, total}}
            Empty dict if brain_learning table doesn't exist or read fails.
        """
        try:
            import sqlite3
            import os
            db_path = os.environ.get("DB_PATH",
                os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "signals.db"))
            if not os.path.exists(db_path):
                return {}
            conn = sqlite3.connect(db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """SELECT module_name, recommended_weight, win_rate, total
                       FROM brain_learning WHERE asset = ?""",
                    (asset,)
                ).fetchall()
                return {r["module_name"]: dict(r) for r in rows}
            except sqlite3.OperationalError:
                # brain_learning table doesn't exist
                return {}
            finally:
                conn.close()
        except Exception:
            return {}

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

    # FIX (WIN-RATE-BOOST #1, 2026-07-23): per-pair max_confidence cap.
    # Allows individual pairs (e.g., USDMXN_otc with 0% win rate) to be
    # capped at a very low confidence level so they emit NEUTRAL most of
    # the time. Returns None if no cap is set (use global calibration).
    def get_max_confidence(self, asset: str) -> int | None:
        """Get the per-pair max_confidence cap.

        Returns:
            int: the max confidence for this asset (e.g., 40 for USDMXN_otc)
            None: no per-pair cap — use the global calibration caps
        """
        config = self.pair_configs.get(asset)
        if config and "max_confidence" in config:
            return config["max_confidence"]
        return None
