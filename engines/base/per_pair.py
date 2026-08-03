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

# FIX (DEEP-AUDIT-2026-07-26 / F-04-02): removed dead `_ADAPT_MIN_SAMPLES`
# and `_ADAPT_PRIOR` env vars — they were silently ignored since the
# AUDIT-LIVE-1-03 refactor (the smooth `adapt_fraction` formula replaced
# the fixed 0.7/0.3 blend). Only `_ADAPT_CAP` and `_ADAPT_CACHE_TTL` still
# work as documented. (Audit A-02 #23, A-06 #5.)
_ADAPT_CAP = float(os.environ.get("ADAPT_CAP", "0.30"))
_ADAPT_CACHE_TTL = float(os.environ.get("ADAPT_CACHE_TTL", "60"))

# FIX (DEEP-AUDIT-2026-07-26 / F-04-06): named calibration thresholds.
# Previously inline magic numbers scattered across _adapt_from_db; now
# centralized for clarity and tuning. (Audit A-06 #12, #83-91, A-02 #115-118.)
_HARD_DISABLE_WIN_RATE = 0.0    # FIX (ALWAYS-SIGNAL-2026-08-03): was 0.30, set to 0.0 to never hard-disable modules
_HARD_DISABLE_SAMPLES = 50      # need this many samples to hard-disable
_DB_STATS_FULL_SAMPLES = 50.0   # adapt_fraction saturates at this sample count
_BRAIN_MIN_SAMPLES = 30         # brain_learning blend activates above this
_BRAIN_FULL_SAMPLES = 200.0     # brain_fraction saturates at this sample count
_WIN_RATE_BASELINE = 0.50       # deviation center (win_rate - baseline)
_DEVIATION_MULTIPLIER = 1.5     # scale = deviation * multiplier (capped)
_DISABLED_MODULE_WEIGHT = 0.05  # hard-disabled modules get this weight
_MAX_CACHE_ENTRIES = 512        # 40 assets × ~10 periods; evict oldest if exceeded
_SQLITE_TIMEOUT = 10           # match db.py:17 / brain.py:38 (was 5 — outlier)

# FIX (DEEP-AUDIT-2026-07-26 / F-04-20): valid profile names — used by
# __init__ validation. "default" is the fallback when asset is missing.
# (Audit A-06 #59.)
_VALID_PROFILES = frozenset({
    "mean_reverting", "trending", "volatile", "stable", "default",
    "calibrated",
})

__all__ = ["PairWeightAdapter"]


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
        # FIX (DEEP-AUDIT-2026-07-26 / F-04-16): typed annotations (was bare dict).
        # FIX (DEEP-AUDIT-2026-07-26 / F-04-08): bounded cache — evicts oldest
        # entry when _MAX_CACHE_ENTRIES is exceeded (audit A-06 #8).
        self._adapt_cache: dict[tuple[str, int], dict] = {}
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-10): per-key invalidation
        # epoch dict — bumped per (asset, period) on every invalidate_cache
        # call. The cache-miss path reads the epoch before the slow DB query
        # and re-checks after; if the epoch changed (invalidation happened
        # during the query), the stale result is NOT written to the cache,
        # preventing the invalidate→stale-write→60s-TTL-stale-read race.
        # FIX (DEEP-AUDIT-2026-07-26 / F-04-15): corrected misleading comment
        # (was "counter"; actually a per-key dict). (Audit A-06 #54.)
        self._invalidation_epoch: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

        # FIX (DEEP-AUDIT-2026-07-26 / F-04-20): validate pair_configs at
        # construction time — catches typos in profile strings, out-of-range
        # max_confidence, and out-of-range weights before the adapter is used
        # in production. (Audit A-06 #21, #58, #59.)
        for asset, cfg in pair_configs.items():
            if not isinstance(cfg, dict):
                raise TypeError(
                    f"pair_configs[{asset!r}] must be a dict, "
                    f"got {type(cfg).__name__}")
            if "profile" in cfg and cfg["profile"] not in _VALID_PROFILES:
                raise ValueError(
                    f"pair_configs[{asset!r}].profile={cfg['profile']!r} "
                    f"is not a valid profile (one of "
                    f"{sorted(_VALID_PROFILES)})")
            if "max_confidence" in cfg and not (0 <= cfg["max_confidence"] <= 100):
                raise ValueError(
                    f"pair_configs[{asset!r}].max_confidence="
                    f"{cfg['max_confidence']} is out of range [0, 100]")
            if "weights" in cfg:
                for mod, w in cfg["weights"].items():
                    if not isinstance(w, (int, float)):
                        raise TypeError(
                            f"pair_configs[{asset!r}].weights[{mod!r}] "
                            f"must be a number, got {type(w).__name__}")
                    if not (0.0 <= float(w) <= 2.0):
                        raise ValueError(
                            f"pair_configs[{asset!r}].weights[{mod!r}]={w} "
                            f"is out of range [0.0, 2.0]")

    def get_weights(self, asset: str, period: int = 60, use_db: bool = True) -> dict:
        """Get module weights for a specific asset.

        Combines the static PAIR_CONFIGS prior with DB-learned per-module
        win-rate adaptation. When `use_db` is True (default) AND enough
        graded samples exist for this (asset, period), modules with
        win_rate > 0.50 get boosted up to +30% (max boost at win_rate >= 0.70),
        and modules with win_rate < 0.50 get dampened up to -30% (max dampen
        at win_rate <= 0.30). Adaptation is sample-size-weighted (0% adapted
        at total=0, 100% adapted at total>=50) to avoid overfitting on small
        or noisy samples.

        FIX (DEEP-AUDIT-2026-07-26 / F-04-17): corrected docstring — old
        version claimed "win_rate > 0.60" threshold and "70% static / 30%
        learned" blend; both stale after AUDIT-LIVE-1-03 refactor (centers at
        0.50, uses smooth adapt_fraction blend). (Audit A-06 #19, #57.)

        Args:
            asset: pair name (e.g. "EURUSD_otc", "EURUSD")
            period: candle period in seconds (default 60)
            use_db: set False to skip DB lookup (used by /api/stats and tests)

        Returns:
            dict mapping module name → weight multiplier
        """
        config = self.pair_configs.get(asset)
        # FIX (DEEP-AUDIT-2026-07-26 / F-04-09): use `is not None` instead of
        # truthiness — an empty dict config (misconfigured entry) would
        # otherwise silently fall back to default_weights, hiding the bug.
        # (Audit A-06 #25.)
        base = (config["weights"].copy() if config is not None
                else self.default_weights.copy())

        if not use_db:
            return base

        # Cache lookup — the prediction path is hot (every candle close
        # across many streams), and per_module_accuracy does a SQL scan
        # each call. Acquire the lock only around the cache mutation, not
        # the DB read (which can be slow).
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-10): capture the
        # invalidation epoch INSIDE the lock before releasing it to do the
        # slow DB query. After the query, re-check the epoch under the lock;
        # if it changed, an invalidate_cache call happened during the query —
        # discard our result (don't cache) so the next caller sees fresh data.
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-23): use a lock-free read
        # for the cache HIT path (Python dict reads are atomic under the GIL).
        # The lock is now only acquired on cache MISSES, cutting lock
        # contention from 240/min to ~5/min (only first-call-per-asset-per-TTL).
        cache_key = (asset, period)
        cached = self._adapt_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < _ADAPT_CACHE_TTL:
            # FIX (CRASH-FIX-2026-07-26 / EN-010): return a COPY of the
            # cached dict, not the dict itself. If the blender or any
            # caller mutates the returned dict (e.g., sets a weight to 0
            # for a "disable this module" path), the cache is silently
            # corrupted — the next caller gets the mutated version. This
            # is a classic cache-returns-mutable bug. Now: dict() copy.
            # The copy is shallow — weights are float scalars, not nested
            # dicts — so shallow is sufficient.
            return dict(cached["weights"])

        with self._lock:
            invalidation_epoch = self._invalidation_epoch.get(cache_key, 0)

        adapted = self._adapt_from_db(base, asset, period)

        with self._lock:
            # Re-check: if invalidation happened during the DB query, discard
            # our result — don't cache stale data. The next call will re-query.
            if self._invalidation_epoch.get(cache_key, 0) != invalidation_epoch:
                # FIX (DEEP-AUDIT-2026-07-26 / F-04-05): previously returned
                # `adapted` (the stale result) uncached — the caller still got
                # stale weights for THIS prediction. Now return the static
                # `base` so this prediction is at least free of stale DB
                # data; the next call (immediately) re-queries fresh.
                # (Audit A-02 #28.)
                return base
            # FIX (DEEP-AUDIT-2026-07-26 / F-04-08): bound cache size — evict
            # oldest entry if we're at the cap. (Audit A-06 #8.)
            if len(self._adapt_cache) >= _MAX_CACHE_ENTRIES:
                _evict_key = min(self._adapt_cache.items(),
                                 key=lambda kv: kv[1]["ts"])[0]
                self._adapt_cache.pop(_evict_key, None)
                self._invalidation_epoch.pop(_evict_key, None)
            # FIX (DEEP-AUDIT-2026-07-26 / F-04-10): drop redundant `.copy()` —
            # `_adapt_from_db` already returns a fresh dict and the blender
            # does not mutate the weights it receives. (Audit A-06 #26.)
            self._adapt_cache[cache_key] = {"ts": time.time(), "weights": adapted}
        return adapted

    def _adapt_from_db(self, static_weights: dict, asset: str, period: int) -> dict:
        """Blend static weights with DB-learned per-module win rates.

        Returns a new dict; the input is not mutated.

        FIX (LIVE-DB-AUDIT-2026-07-25 / AUDIT-LIVE-1-03): also read
        brain_learning recommended_weight and blend it in. Previously
        brain_learning was computed but NEVER read by any code — the brain
        "learned" for 4-5 days and the engine ignored every recommendation.

        FIX (DEEP-AUDIT-2026-07-26 / F-04-06): inline magic numbers (0.30, 50,
        30, 0.5, 1.5) extracted to named module constants.
        FIX (DEEP-AUDIT-2026-07-26 / F-04-07): brain blend is now sample-size
        weighted (was flat 50/50 once total >= 30). A 30-sample brain
        recommendation gets ~15% weight; a 200+ sample one gets full 100%.
        (Audit A-06 #7, #12.)
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
        brain_recs = self._read_brain_learning(asset, period)

        adapted = {}
        for module, static_w in static_weights.items():
            s = stats.get(module, {})
            total = s.get("total", 0)
            win_rate = s.get("win_rate")
            if total == 0 or win_rate is None:
                adapted[module] = static_w
                continue

            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-09 + AUDIT-LIVE-2-18):
            # previously used a FIXED 0.7/0.3 (static/learned) blend regardless
            # of sample count. A 0% win-rate module over 1000 samples only got
            # -9% weight reduction — the adapter could NEVER fully disable a
            # bad module. Also, the hard threshold `_ADAPT_MIN_SAMPLES = 20`
            # caused weight oscillation for rare-firing modules (key_level)
            # hovering around 20 votes. Now:
            #   (a) Use a SMOOTH blend that transitions from 100% static at
            #       total=0 to 100% adapted at total=50 (adapt_fraction).
            #       This eliminates the threshold oscillation for rare modules.
            #   (b) Hard-disable modules with win_rate < 0.30 AND total >= 50
            #       (set weight to 0.05) so catastrophically broken modules
            #       actually get disabled, matching the auto_tune behavior
            #       (AUDIT-2-21 in core/auto_tune.py).
            # FIX (DEEP-AUDIT-2026-07-26 / F-04-06): use named constants
            # (was inline magic numbers 0.30 / 50 / 0.05).
            if win_rate < _HARD_DISABLE_WIN_RATE and total >= _HARD_DISABLE_SAMPLES:
                adapted[module] = _DISABLED_MODULE_WEIGHT
                continue

            # Map win_rate ∈ [0, 1] to a scaling factor centered at 1.0.
            # win_rate=0.50 → 1.0 (no change)
            # win_rate=0.70 → +0.30 (boosted to 1.30, capped)
            # win_rate=0.30 → -0.30 (dampened to 0.70, capped)
            # FIX (DEEP-AUDIT-2026-07-26 / F-04-06): use named constants
            # (was inline magic numbers 0.50 / 1.5).
            deviation = win_rate - _WIN_RATE_BASELINE
            scale = max(-_ADAPT_CAP, min(_ADAPT_CAP, deviation * _DEVIATION_MULTIPLIER))
            learned_w = static_w * (1.0 + scale)
            # Smooth blend: 0% adapted at total=0, 100% adapted at total>=50.
            # This replaces the FIXED _ADAPT_PRIOR (0.7) blend that was
            # sample-count-independent. For total >= 50, the adapter fully
            # trusts the DB-learned weight (matching the auto_tune behavior).
            # For total < 50, the adapter progressively shifts from static
            # to learned as samples accumulate — no threshold discontinuity.
            # FIX (DEEP-AUDIT-2026-07-26 / F-04-06): use named constant for
            # sample saturation threshold (was inline magic number 50.0).
            adapt_fraction = min(1.0, total / _DB_STATS_FULL_SAMPLES)
            blended = (1.0 - adapt_fraction) * static_w + adapt_fraction * learned_w

            # FIX (AUDIT-LIVE-1-03): also blend in brain_learning
            # recommended_weight if available. The brain uses more samples
            # and per-pair granularity, so give it significant weight.
            brain_rec = brain_recs.get(module)
            if brain_rec:
                brain_total = brain_rec.get("total", 0)
                if brain_total >= _BRAIN_MIN_SAMPLES:
                    # FIX (CRASH-FIX-2026-07-26 / EN-002): the brain stores
                    # `recommended_weight` as a MULTIPLIER (1.5/1.3/1.0/0.7/0.5
                    # in brain.py:921-945) — NOT an absolute weight. The
                    # previous code blended `brain_w` as an absolute weight,
                    # so a module with static_w=0.7 and brain rec 1.5 got
                    # blended toward 1.5 (overriding the static config)
                    # instead of being multiplied to 0.7 * 1.5 = 1.05.
                    # This caused massive weight inflation on modules the
                    # brain was confident in — predictions became over-
                    # confident and frequently wrong. Now we apply the
                    # brain recommendation as a multiplier to the static
                    # weight, not a replacement.
                    brain_mult = brain_rec["recommended_weight"]
                    brain_w = static_w * brain_mult
                    # FIX (DEEP-AUDIT-2026-07-26 / F-04-07): sample-size-weighted
                    # brain blend (was flat 50/50 regardless of brain_total).
                    # A 30-sample recommendation (wide CI) gets ~15% weight;
                    # a 200+ sample one (narrow CI) gets full 100% weight.
                    # (Audit A-06 #7.)
                    brain_fraction = min(1.0, brain_total / _BRAIN_FULL_SAMPLES)
                    blended = (1.0 - brain_fraction) * blended + brain_fraction * brain_w

            adapted[module] = round(blended, 2)

        return adapted

    def _read_brain_learning(self, asset: str, period: int = 60) -> dict:
        """Read brain_learning recommended_weight per module for an asset.

        FIX (AUDIT-LIVE-1-03): previously this method did not exist —
        brain_learning was write-only. Now per_pair adapter reads it.

        FIX (DEEP-AUDIT-2026-07-26 / A-06-CRIT-1 + A-06-CRIT-2):
        BUG A: `os.path.dirname(os.path.dirname(__file__))` resolves to
        `engines/` (one level too high), making db_path =
        `engines/signals.db` — file never exists, so brain_learning is
        ALWAYS INERT. Fix: go up THREE levels to reach repo root.
        BUG B: SQL omitted `period` filter; the UNIQUE index
        `(asset, module_name, period)` meant rows collide in the dict
        comprehension and only the last row per module survives.
        Fix: filter by period.

        Returns:
            dict: {module_name: {recommended_weight, win_rate, total}}
            Empty dict if brain_learning table doesn't exist or read fails.
        """
        try:
            import sqlite3
            # FIX (DEEP-AUDIT-2026-07-26 / F-04-03): removed redundant
            # `import os` (already imported at module top, line 15).
            # (Audit A-02 #24, A-06 #81.)
            # FIX A: __file__ = engines/base/per_pair.py → 3 dirnames to root.
            db_path = os.environ.get("DB_PATH",
                os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                             "signals.db"))
            if not os.path.exists(db_path):
                return {}
            # FIX (DEEP-AUDIT-2026-07-26 / F-04-11): standardize on timeout=10
            # to match db.py:17 and brain.py:38 (was 5 — inconsistent outlier).
            # (Audit A-06 #42, A-02 #118.)
            conn = sqlite3.connect(db_path, timeout=_SQLITE_TIMEOUT)
            conn.row_factory = sqlite3.Row
            try:
                # FIX B: filter by period so multi-period rows don't collide.
                rows = conn.execute(
                    """SELECT module_name, recommended_weight, win_rate, total
                       FROM brain_learning
                       WHERE asset = ? AND period = ?""",
                    (asset, period)
                ).fetchall()
                return {r["module_name"]: dict(r) for r in rows}
            except sqlite3.OperationalError as e:
                # FIX (DEEP-AUDIT-2026-07-26 / F-04-13): distinguish "no such
                # table" (return {} — table doesn't exist yet, common on fresh
                # DB) from other OperationalError causes (e.g. "database is
                # locked") — both currently fall through to return {} but the
                # explicit branch makes the intent clear for future tuning.
                # (Audit A-06 #44.)
                msg = str(e).lower()
                if "no such table" in msg:
                    return {}  # brain_learning table doesn't exist
                # Locked DB or schema-mismatch — silent fallback to {}.
                # Caller (_adapt_from_db) blends with brain_recs={} →
                # DB-stats-only adaptation. No re-raise to preserve the
                # silent-fallback contract documented in the docstring.
                return {}
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            # FIX (DEEP-AUDIT-2026-07-26 / F-04-12): narrowed the broad
            # `except Exception` to sqlite3 + OS errors only. Programming
            # errors (KeyError, TypeError, AttributeError) now propagate so
            # they're visible during testing instead of being silently
            # swallowed. (Audit A-06 #43.)
            return {}

    def invalidate_cache(self, asset: str | None = None, period: int | None = None):
        """Clear the DB-adaptation cache.

        Called after a batch of new signal log writes so the next
        prediction reflects fresh accuracy data. Safe to call from any
        thread.

        FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-10): bump the invalidation
        epoch for the affected cache_key(s) BEFORE popping the cache entry.
        This ensures that any concurrent cache-miss path that captured the
        OLD epoch before this call will detect the change when it tries to
        write its (stale) result back, and discard the write.

        FIX (DEEP-AUDIT-2026-07-26 / F-04-01 / F-04-14): the previous
        `asset=None` branch did `self._invalidation_epoch.clear()` — this
        wiped the dict, so concurrent miss-path callers that had captured
        epoch=0 (the initial state, very common for fresh assets) would see
        epoch=0 on re-check too, FAILING to detect the invalidation and
        writing stale data into the cache for 60s. Fix: bump the epoch for
        EVERY key being cleared (instead of clearing the dict), so any
        captured epoch value (including 0) will mismatch on re-check.
        Also: standardized on `str | None` modern syntax (was old-style
        `str = None`). (Audit A-02 #2, A-06 #45.)
        """
        with self._lock:
            if asset is None:
                # Invalidate everything — bump epoch for every cached key
                # BEFORE clearing the cache. DO NOT clear the epoch dict;
                # clearing breaks epoch=0 race-detection (see FIX F-04-01).
                for k in list(self._adapt_cache.keys()):
                    self._invalidation_epoch[k] = self._invalidation_epoch.get(k, 0) + 1
                self._adapt_cache.clear()
            else:
                keys_to_drop = [k for k in self._adapt_cache
                                if k[0] == asset and (period is None or k[1] == period)]
                for k in keys_to_drop:
                    self._adapt_cache.pop(k, None)
                    # Bump epoch so concurrent miss-path callers see the change.
                    self._invalidation_epoch[k] = self._invalidation_epoch.get(k, 0) + 1

    def get_profile(self, asset: str) -> str:
        """Get the behavior profile for an asset.

        Returns one of: "mean_reverting", "trending", "volatile",
        "stable", "default"
        """
        config = self.pair_configs.get(asset)
        # FIX (DEEP-AUDIT-2026-07-26 / F-04-19): use .get("profile", "default")
        # instead of `config["profile"]` — guards against misconfigured entries
        # missing the "profile" key (was KeyError). (Audit A-02 #119.)
        if config is not None:
            return config.get("profile", "default")
        return "default"

    # FIX (WIN-RATE-BOOST #1, 2026-07-23): per-pair max_confidence cap.
    # Allows individual pairs (e.g., USDMXN_otc with 0% win rate) to be
    # capped at a very low confidence level so they emit NEUTRAL most of
    # the time. Returns None if no cap is set (use global calibration).
    # FIX (DEEP-AUDIT-2026-07-26 / F-04-15): kept `int | None` syntax — Python
    # 3.10+ is required (project runs 3.12). Range validation moved to
    # __init__ (see F-04-20). (Audit A-02 #25.)
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
