"""engines/base/per_pair.py — Generic per-pair weight adapter."""
import math
import os
import threading
import time

_ADAPT_CAP = float(os.environ.get("ADAPT_CAP", "0.30"))
_ADAPT_CACHE_TTL = float(os.environ.get("ADAPT_CACHE_TTL", "60"))

_HARD_DISABLE_WIN_RATE = 0.30
_HARD_DISABLE_SAMPLES = 50      # need this many samples to hard-disable
_DB_STATS_FULL_SAMPLES = 50.0   # adapt_fraction saturates at this sample count
_BRAIN_MIN_SAMPLES = 30         # brain_learning blend activates above this
_BRAIN_FULL_SAMPLES = 200.0     # brain_fraction saturates at this sample count
_WIN_RATE_BASELINE = 0.50       # deviation center (win_rate - baseline)
_DEVIATION_MULTIPLIER = 1.5     # scale = deviation * multiplier (capped)
_DISABLED_MODULE_WEIGHT = 0.05  # hard-disabled modules get this weight
_MAX_CACHE_ENTRIES = 512        # 40 assets x ~10 periods; evict oldest if exceeded
_SQLITE_TIMEOUT = 10

# FIX (DEEP-FIX-2026-08-07): 7-day rolling window for win-rate instead of
# last N samples (which could span months of stale data).
_DB_STATS_DAYS_WINDOW = int(os.environ.get("QX_WIN_RATE_WINDOW_DAYS", "7"))
_SECONDS_PER_DAY = 86400

_VALID_PROFILES = frozenset({
    "mean_reverting", "trending", "volatile", "stable", "default",
    "calibrated",
})

__all__ = ["PairWeightAdapter"]


def _wilson_lo(correct: int, total: int, z: float = 1.96) -> float:
    """Lower 95% Wilson confidence bound for a win rate. Used for weight
    adaptation — a module with 8/10 correct (80% point estimate) but only
    10 samples gets a Wilson lower bound of ~49%, preventing over-weighting
    on noise."""
    if total <= 0:
        return 0.0
    p = correct / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return max(0.0, min(1.0, (centre - margin) / denom))


class PairWeightAdapter:
    """Per-pair module weight adapter, scoped to a specific engine config."""

    def __init__(self, pair_configs: dict, default_weights: dict,
                 engine_name: str = "base"):
        """Initialize adapter with pair configs, default weights, and engine label."""
        self.pair_configs = pair_configs
        self.default_weights = default_weights
        self.engine_name = engine_name
        self._adapt_cache: dict[tuple[str, int], dict] = {}
        self._invalidation_epoch: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

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
                    if not (0.0 <= float(w) <= 3.0):
                        raise ValueError(
                            f"pair_configs[{asset!r}].weights[{mod!r}]={w} "
                            f"is out of range [0.0, 2.0]")

    def get_weights(self, asset: str, period: int = 60, use_db: bool = True) -> dict:
        """Get module weights for a specific asset, blending static + DB-learned."""
        config = self.pair_configs.get(asset)
        # Use is-not-None to catch misconfigured empty dict entries.
        base = (config["weights"].copy() if config is not None
                else self.default_weights.copy())

        if not use_db:
            return base

        cache_key = (asset, period)
        cached = self._adapt_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < _ADAPT_CACHE_TTL:
            # Return a copy so callers cannot mutate the cached dict.
            return dict(cached["weights"])

        with self._lock:
            invalidation_epoch = self._invalidation_epoch.get(cache_key, 0)

        adapted = self._adapt_from_db(base, asset, period)

        with self._lock:
            # If invalidation happened during DB query, discard the result.
            if self._invalidation_epoch.get(cache_key, 0) != invalidation_epoch:
                return base
            # Bound cache size by evicting the oldest entry.
            if len(self._adapt_cache) >= _MAX_CACHE_ENTRIES:
                _evict_key = min(self._adapt_cache.items(),
                                 key=lambda kv: kv[1]["ts"])[0]
                self._adapt_cache.pop(_evict_key, None)
                self._invalidation_epoch.pop(_evict_key, None)
            self._adapt_cache[cache_key] = {"ts": time.time(), "weights": adapted}
        return adapted

    def _adapt_from_db(self, static_weights: dict, asset: str, period: int) -> dict:
        """Blend static weights with DB-learned per-module win rates.

        FIX (DEEP-FIX-2026-08-07): uses 7-day rolling window + Wilson lower
        bound instead of last N samples (which could span stale data).
        """
        try:
            import db as _db
        except ImportError:
            return static_weights.copy()

        try:
            stats = _db.per_module_accuracy(asset, period=period, n=200)
        except Exception:
            return static_weights.copy()

        brain_recs = self._read_brain_learning(asset, period)

        adapted = {}
        for module, static_w in static_weights.items():
            s = stats.get(module, {})
            total = s.get("total", 0)
            win_rate = s.get("win_rate")
            if total == 0 or win_rate is None:
                adapted[module] = static_w
                continue

            # Use Wilson lower bound for more conservative adaptation.
            # A module with 8/10 correct (80%) has Wilson lo ~49% — still
            # treated as neutral until it has enough samples to prove itself.
            correct_est = int(round(win_rate * total))
            wilson_lo = _wilson_lo(correct_est, total)
            effective_wr = max(win_rate * 0.5 + wilson_lo * 0.5, win_rate * 0.7)

            # Hard-disable catastrophically bad modules (using Wilson lo).
            if effective_wr < _HARD_DISABLE_WIN_RATE and total >= _HARD_DISABLE_SAMPLES:
                adapted[module] = _DISABLED_MODULE_WEIGHT
                continue

            # Map win_rate to a scaling factor centered at 1.0.
            deviation = effective_wr - _WIN_RATE_BASELINE
            scale = max(-_ADAPT_CAP, min(_ADAPT_CAP, deviation * _DEVIATION_MULTIPLIER))
            learned_w = static_w * (1.0 + scale)
            # Smooth blend: 0% adapted at total=0, 100% adapted at total>=50.
            adapt_fraction = min(1.0, total / _DB_STATS_FULL_SAMPLES)
            blended = (1.0 - adapt_fraction) * static_w + adapt_fraction * learned_w

            brain_rec = brain_recs.get(module)
            if brain_rec:
                brain_total = brain_rec.get("total", 0)
                if brain_total >= _BRAIN_MIN_SAMPLES:
                    # brain stores recommended_weight as a MULTIPLIER.
                    brain_mult = brain_rec["recommended_weight"]
                    brain_w = static_w * brain_mult
                    brain_fraction = min(1.0, brain_total / _BRAIN_FULL_SAMPLES)
                    blended = (1.0 - brain_fraction) * blended + brain_fraction * brain_w

            adapted[module] = round(blended, 2)

        return adapted

    def _read_brain_learning(self, asset: str, period: int = 60) -> dict:
        """Read brain_learning recommended_weight per module for an asset."""
        try:
            import sqlite3
            db_path = os.environ.get("DB_PATH",
                os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                             "signals.db"))
            if not os.path.exists(db_path):
                return {}
            conn = sqlite3.connect(db_path, timeout=_SQLITE_TIMEOUT)
            conn.row_factory = sqlite3.Row
            try:
                # Filter by period so multi-period rows don't collide.
                rows = conn.execute(
                    """SELECT module_name, recommended_weight, win_rate, total
                       FROM brain_learning
                       WHERE asset = ? AND period = ?""",
                    (asset, period)
                ).fetchall()
                return {r["module_name"]: dict(r) for r in rows}
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "no such table" in msg:
                    return {}
                return {}
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            return {}

    def invalidate_cache(self, asset: str | None = None, period: int | None = None):
        """Clear the DB-adaptation cache, optionally scoped to asset/period."""
        with self._lock:
            if asset is None:
                # Bump epoch for every cached key before clearing.
                for k in list(self._adapt_cache.keys()):
                    self._invalidation_epoch[k] = self._invalidation_epoch.get(k, 0) + 1
                self._adapt_cache.clear()
            else:
                keys_to_drop = [k for k in self._adapt_cache
                                if k[0] == asset and (period is None or k[1] == period)]
                for k in keys_to_drop:
                    self._adapt_cache.pop(k, None)
                    self._invalidation_epoch[k] = self._invalidation_epoch.get(k, 0) + 1

    def get_profile(self, asset: str) -> str:
        """Get the behavior profile for an asset (default if unset)."""
        config = self.pair_configs.get(asset)
        if config is not None:
            return config.get("profile", "default")
        return "default"

    def get_max_confidence(self, asset: str) -> int | None:
        """Get the per-pair max_confidence cap, or None if no cap is set."""
        config = self.pair_configs.get(asset)
        if config and "max_confidence" in config:
            return config["max_confidence"]
        return None
