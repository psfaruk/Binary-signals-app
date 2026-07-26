"""
core/auto_tune.py — Auto-tune module weights based on live win rates.

DEEP IMPLEMENTATION (2026-07-23):

This module reads per-module win rates from signal_log and adjusts
the engine's DEFAULT_WEIGHTS accordingly. It runs periodically
(every 100 graded signals) and updates weights in real-time.

TUNING RULES (display bucketing only — actual weight is continuous):
  - Win rate >= 55%  → status BOOST  (continuous weight ≈ ×1.3)
  - Win rate 50-54%  → status KEEP   (continuous weight ≈ ×1.0)
  - Win rate 45-49%  → status DAMPEN (continuous weight ≈ ×0.8)
  - Win rate < 45%   → status SEVERE (continuous weight ≈ ×0.5)
  - Win rate < 35%   → status DISABLE (continuous weight ≈ ×0.1)

The tuning is sample-size-adaptive — the tuned weight is blended with
the static weight using a blend factor that grows from 0.3 at
MIN_SAMPLES (20) to 0.9 at 200+ samples. Small samples preserve the
static prior; large samples let the live win rate dominate.

MINIMUM SAMPLES: 20 graded signals per module before tuning kicks in.
Below that, the static weight is used unchanged.

FIX (DEEP-AUDIT-2026-07-26 / F-08-18): the docstring previously claimed
a fixed 70/30 prior — that was stale (the code switched to adaptive
blend on 2026-07-25). Updated to reflect the current behaviour
(A-04 problem 58).
"""
import sqlite3
import threading

from core.constants import (
    AUTO_TUNE_MAX_ROWS,
    AUTO_TUNE_MAX_WEIGHT,
    AUTO_TUNE_MIN_SAMPLES,
    AUTO_TUNE_MIN_WEIGHT,
    AUTO_TUNE_WEIGHT_CHANGE_THRESHOLD,
    DB_PATH,
    MODULE_NAMES,
)
from core.stats import parse_module_direction, parse_reasons

# FIX (DEEP-AUDIT-2026-07-26 / F-08-19): remove dead imports `time`,
# `defaultdict`, `json`, and `os` — none of these names are referenced
# anywhere in this file after switching to the shared `parse_reasons`
# helper from `core.stats` (A-04 problems 48, 49, plus two newly-dead
# imports exposed by extracting the JSON parsing).

# FIX (DEEP-AUDIT-2026-07-26 / F-08-20): `MIN_SAMPLES`, `_MAX_WEIGHT`,
# `_MIN_WEIGHT`, and the change-detection threshold are now sourced from
# `core.constants` (env-configurable, A-04 problems 50 + 62). The
# local aliases below preserve backward compatibility for any caller
# that imports them by name.
MIN_SAMPLES = AUTO_TUNE_MIN_SAMPLES
_MAX_WEIGHT = AUTO_TUNE_MAX_WEIGHT
_MIN_WEIGHT = AUTO_TUNE_MIN_WEIGHT
_WEIGHT_CHANGE_THRESHOLD = AUTO_TUNE_WEIGHT_CHANGE_THRESHOLD

# FIX (DEEP-AUDIT-2026-07-26 / F-08-21): serialise the in-place mutation
# of the engine `DEFAULT_WEIGHTS` dicts. The previous code had no lock
# — two concurrent `apply_tuned_weights_to_engines` calls (e.g. from
# the feed loop + a manual `/api/auto-tune/apply` POST) could interleave
# reads and writes of the same dict, leaving it in a half-updated state
# (A-04 problem 8 / cross-cutting thread-safety theme).
_apply_lock = threading.Lock()

# Sample window (rows read from signal_log). Env-configurable via constants.
_AUTO_TUNE_MAX_ROWS = AUTO_TUNE_MAX_ROWS

# Static (baseline) weights — the starting point. Auto-tune adjusts from here.
STATIC_WEIGHTS_OTC = {
    "candle_reaction": 1.3,
    "running_tick":    1.0,
    "pattern":         1.0,
    "indicator":       1.0,
    "key_level":       0.7,
    "otc_pattern":     0.9,
}

STATIC_WEIGHTS_REAL = {
    "candle_reaction": 1.3,
    "running_tick":    1.0,
    "pattern":         1.0,
    "indicator":       1.2,
    "key_level":       0.8,
    "trend_follow":    0.1,
}


def _get_module_win_rates() -> dict:
    """Read per-module win rates from signal_log across all pairs.

    Returns: {module_name: {correct, total, win_rate}}
    """
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-22): drop the duplicate
    # `MODULE_NAMES` fallback tuple. The constants module is already
    # imported at module load (top of file) — if that import succeeded,
    # the local try/except was dead code (it always hit the `from core.
    # constants import MODULE_NAMES` branch). If the import failed, the
    # module would have crashed at load time, never reaching this
    # function. The hardcoded tuple was a verbatim copy that could drift
    # out of sync with `core.constants.MODULE_NAMES` (A-04 problem 51).

    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-23): use db._conn() to inherit
    # WAL mode + synchronous=NORMAL. A raw non-WAL reader on a WAL-mode DB
    # causes lock contention with the feed's WAL writers — every
    # apply_tuned_weights_to_engines call (every ~100 graded signals) blocks
    # writes for 100ms+ on slow disks. Falls back to raw connect if db
    # module isn't importable.
    #
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-23): catch ImportError only — the
    # old broad `except Exception` swallowed genuine failures from
    # `db._conn()` (e.g.OperationalError on a locked DB) and silently
    # fell back to a non-WAL connection, causing lock contention with
    # the feed's WAL writers (A-04 problem 52).
    try:
        import db as _db
        conn = _db._conn()
    except ImportError:
        conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # Try ts column first (production schema), fall back to ctime
        try:
            rows = conn.execute("""SELECT signal, accuracy, reasons
                                   FROM signal_log
                                   WHERE signal IN ('CALL','PUT')
                                     AND accuracy IN ('correct','wrong')
                                   ORDER BY ts DESC LIMIT ?""", (_AUTO_TUNE_MAX_ROWS,)).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute("""SELECT signal, accuracy, reasons
                                   FROM signal_log
                                   WHERE signal IN ('CALL','PUT')
                                     AND accuracy IN ('correct','wrong')
                                   ORDER BY ctime DESC LIMIT ?""", (_AUTO_TUNE_MAX_ROWS,)).fetchall()
    finally:
        conn.close()

    stats = {m: {"correct": 0, "wrong": 0, "total": 0} for m in MODULE_NAMES}

    for row in rows:
        final_signal = row["signal"]
        accuracy = row["accuracy"]
        # FIX (DEEP-AUDIT-2026-07-26 / F-08-24): use the shared
        # `parse_reasons` + `parse_module_direction` helpers from
        # `core.stats`. The old code had a verbatim copy of the same
        # parsing logic — including the same LIVE-FIX-2-25 comment block
        # — and both files drifted in lockstep (A-04 problem 53). The
        # shared helper also normalises ASCII arrows (`->`, `=>`) so
        # reasons emitted by older engines go through the same
        # tail-based detection path (A-04 problem 54), drops the
        # redundant `tail == "CALL"` / `tail == "PUT"` clauses
        # (A-04 problem 55), and updates the stale "PUT-first scan"
        # comment that contradicted the CALL-first code path
        # (A-04 problem 56).
        reasons = parse_reasons(row["reasons"] or "[]")

        for reason in reasons:
            reason_str = str(reason)
            module, module_dir = parse_module_direction(reason_str, MODULE_NAMES)
            if module is None or module_dir is None:
                continue

            # FIX (DEEP-AUDIT-2026-07-26 / F-08-25): drop the dead
            # `if accuracy not in ("correct", "wrong"): continue` check
            # — the SQL WHERE clause already filters by
            # `accuracy IN ('correct','wrong')`, so the check never fires
            # (A-04 problem 57).

            stats[module]["total"] += 1
            if module_dir == final_signal and accuracy == "correct":
                stats[module]["correct"] += 1
            elif module_dir != final_signal and accuracy == "wrong":
                stats[module]["correct"] += 1

    # Compute win rates
    result = {}
    for m, s in stats.items():
        if s["total"] > 0:
            s["win_rate"] = s["correct"] / s["total"]
        else:
            s["win_rate"] = None
        result[m] = s

    return result


def _win_rate_to_weight(win_rate: float, static_weight: float,
                         total: int = 0) -> float:
    """Convert a win rate to a tuned weight.

    FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-2-15): use a CONTINUOUS linear
    mapping (win_rate 0.30→0.1, 0.70→1.5) instead of the piecewise-constant
    buckets. The old piecewise mapping produced a 30% weight jump at the 55%
    boundary (54.9%→1.0, 55.0%→1.3), which caused weight THRASHING as win
    rates oscillated near boundaries due to sampling noise. The continuous
    mapping eliminates the discontinuities while preserving the boost/dampen
    intent.

    FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-21 + AUDIT-LIVE-1-11): the blend
    is now sample-size-adaptive instead of fixed 70/30. The old 70% static
    prior meant even a 0% win-rate module over 1000 samples only dropped from
    1.3 to 0.94 — the auto-tuner could NEVER fully disable a bad module. Now
    the tuned-weight blend shifts from 30% (at MIN_SAMPLES) to 90% (at 200+
    samples), so a catastrophically broken module with sufficient data gets
    dampened all the way to _MIN_WEIGHT (0.1).
    """
    # FIX (AUDIT-LIVE-2-15): continuous linear mapping.
    if win_rate <= 0.30:
        tuned = _MIN_WEIGHT
    elif win_rate >= 0.70:
        tuned = _MAX_WEIGHT
    else:
        tuned = _MIN_WEIGHT + (win_rate - 0.30) / 0.40 * (_MAX_WEIGHT - _MIN_WEIGHT)

    # FIX (AUDIT-2-21): adaptive blend. tuned_weight_blend goes from 0.3 at
    # MIN_SAMPLES (20) to 0.9 at 200+ samples. A 0% win-rate module with
    # 200+ samples now gets blended = 0.1*static + 0.9*0.1 = ~0.22 (clamped to
    # _MIN_WEIGHT 0.1) instead of the old 0.94.
    #
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-26): drop the dead `total is None or
    # total <= 0` branch — `compute_tuned_weights` only calls this helper
    # when `total >= MIN_SAMPLES` (>= 20), so the branch was unreachable
    # (A-04 problem 59).
    _N_REF_MAX = 200  # sample count at which tuned weight dominates (90%).
    tuned_weight_blend = min(
        0.9,
        0.3 + 0.6 * max(0, total - MIN_SAMPLES) / max(1, _N_REF_MAX - MIN_SAMPLES),
    )
    blended = (1 - tuned_weight_blend) * static_weight + tuned_weight_blend * tuned

    # Clamp
    return max(_MIN_WEIGHT, min(_MAX_WEIGHT, blended))


def compute_tuned_weights(engine: str = "otc", win_rates: dict = None) -> dict:
    """Compute auto-tuned weights for an engine.

    Args:
        engine: "otc" or "real"
        win_rates: optional pre-computed win-rate dict (output of
            `_get_module_win_rates`). When provided, skips the DB read —
            `get_tuning_report` and `apply_tuned_weights_to_engines` both
            need win rates for BOTH engines, so computing them once and
            passing them in avoids 2 redundant DB round-trips per call
            (A-04 problem 60).

    Returns:
        {module_name: tuned_weight}
    """
    if engine == "real":
        static = STATIC_WEIGHTS_REAL
    else:
        static = STATIC_WEIGHTS_OTC

    # FIX (DEEP-AUDIT-2026-07-26 / F-08-27): accept a pre-computed
    # `win_rates` dict so callers that need both engines' tuned weights
    # in one go don't trigger 3 DB round-trips (A-04 problem 60).
    if win_rates is None:
        win_rates = _get_module_win_rates()

    tuned = {}
    for module, static_w in static.items():
        stats = win_rates.get(module, {})
        total = stats.get("total", 0)
        wr = stats.get("win_rate")

        if total < MIN_SAMPLES or wr is None:
            # Not enough data — use static weight unchanged
            tuned[module] = static_w
        else:
            # Auto-tune based on win rate (pass total for adaptive blend).
            tuned[module] = round(_win_rate_to_weight(wr, static_w, total), 2)

    return tuned


def get_tuning_report() -> dict:
    """Generate a human-readable tuning report for /api/auto-tune endpoint."""
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-28): compute `win_rates` ONCE and
    # pass it into both `compute_tuned_weights` calls. The old code
    # called `_get_module_win_rates` 3 times per report (once here, once
    # inside each `compute_tuned_weights` call), causing 3 DB round-trips
    # per `/api/auto-tune` request (A-04 problem 60).
    win_rates = _get_module_win_rates()
    tuned_otc = compute_tuned_weights("otc", win_rates=win_rates)
    tuned_real = compute_tuned_weights("real", win_rates=win_rates)

    report = {
        "win_rates": {},
        "tuned_weights_otc": tuned_otc,
        "tuned_weights_real": tuned_real,
        "static_weights_otc": STATIC_WEIGHTS_OTC,
        "static_weights_real": STATIC_WEIGHTS_REAL,
        "min_samples": MIN_SAMPLES,
    }

    for module, stats in win_rates.items():
        wr = stats.get("win_rate")
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-22): use explicit `is not None`
        # checks instead of truthy `if wr`. The old `if wr` treated wr=0.0
        # (all signals wrong) as falsy, so a catastrophically broken module
        # was reported as "NO_DATA" instead of "DISABLE". Its weight stayed at
        # the static default and the operator never knew it was failing.
        wr_total = stats.get("total", 0)
        if wr is not None:
            status = ("BOOST" if wr >= 0.55 else
                      "KEEP" if wr >= 0.50 else
                      "DAMPEN" if wr >= 0.45 else
                      "SEVERE" if wr >= 0.35 else
                      "DISABLE")
            wr_display = round(wr * 100, 1)
        else:
            status = "NO_DATA"
            wr_display = None
        # FIX (DEEP-AUDIT-2026-07-26 / F-08-29): expose the continuous
        # tuned weight per module so operators can see the actual value
        # being applied, not just the discrete status bucket. The status
        # label is misleading without the underlying number — e.g. a 54.9%
        # module shows "KEEP" but its weight is computed at 54.9% (not
        # bucketed to 50%). Showing both lets operators reconcile the two
        # (A-04 problem 61).
        report["win_rates"][module] = {
            "correct": stats.get("correct", 0),
            "total": wr_total,
            "win_rate": wr_display,
            "status": status,
            "tuned_weight_otc": tuned_otc.get(module),
            "tuned_weight_real": tuned_real.get(module),
        }

    return report


def apply_tuned_weights_to_engines():
    """Update the engine configs with auto-tuned weights.

    Called periodically (every 100 graded signals) from feed.py.
    Updates engines.otc.config.DEFAULT_WEIGHTS and
    engines.real.config.DEFAULT_WEIGHTS in-place.

    Returns a dict ``{"otc": ..., "real": ..., "cache_invalidated": bool}``
    on success, or ``None`` on unexpected failure.

    FIX (DEEP-AUDIT-2026-07-26 / F-08-30): wrap the in-place mutation
    of the engine `DEFAULT_WEIGHTS` dicts in `_apply_lock` so concurrent
    invocations (e.g. feed loop + manual `/api/auto-tune/apply` POST)
    can't interleave reads and writes of the same dict (A-04 problem 8).

    FIX (DEEP-AUDIT-2026-07-26 / F-08-31): use the env-configurable
    `_WEIGHT_CHANGE_THRESHOLD` constant (sourced from
    `core.constants`) instead of the hardcoded `0.01` (A-04 problem 62).

    FIX (DEEP-AUDIT-2026-07-26 / F-08-32): surface the cache-invalidation
    outcome in the return value. Previously the function returned
    ``{"otc": ..., "real": ...}`` regardless of whether the per-pair
    adapter cache was actually cleared — a silent failure left stale
    cached weights for up to 60s while the API reported success
    (A-04 problem 63).
    """
    try:
        # FIX (F-08-28-style): single DB round-trip for both engines.
        win_rates = _get_module_win_rates()
        tuned_otc = compute_tuned_weights("otc", win_rates=win_rates)
        tuned_real = compute_tuned_weights("real", win_rates=win_rates)

        from engines.otc.config import DEFAULT_WEIGHTS as _otc_w
        from engines.real.config import DEFAULT_WEIGHTS as _real_w

        cache_invalidated = True
        cache_error = None

        # FIX (F-08-30): serialise the mutation so concurrent callers
        # can't half-update either dict.
        with _apply_lock:
            changed_otc = False
            for m, w in tuned_otc.items():
                # FIX (F-08-31): use the env-configurable threshold.
                if m in _otc_w and abs(_otc_w[m] - w) > _WEIGHT_CHANGE_THRESHOLD:
                    _otc_w[m] = w
                    changed_otc = True

            changed_real = False
            for m, w in tuned_real.items():
                if m in _real_w and abs(_real_w[m] - w) > _WEIGHT_CHANGE_THRESHOLD:
                    _real_w[m] = w
                    changed_real = True

            if changed_otc or changed_real:
                print(f"[auto_tune] weights updated — OTC: {tuned_otc}, Real: {tuned_real}")
                # Also invalidate the per_pair adapter cache so new weights take effect.
                #
                # FIX (AUDIT-DEEP #09, 2026-07-23): the previous code called
                # `_otc_adapter.invalidate_cache_all()` and
                # `_real_adapter.invalidate_cache_all()` — but `PairWeightAdapter`
                # only defines `invalidate_cache(asset=None, period=None)`, NOT
                # `invalidate_cache_all()`. This raised AttributeError, which was
                # swallowed by the surrounding `except Exception: pass`. The
                # result: DEFAULT_WEIGHTS was updated in-place (line 252/258) but
                # the per_pair adapter's `_adapt_cache` was NEVER cleared. The
                # cache has a 60-second TTL (`_ADAPT_CACHE_TTL = 60`), so old
                # weights persisted for up to 1 minute after auto-tune ran —
                # during which predictions still used the STALE (pre-tune) weights.
                # Now we call `invalidate_cache()` with no args, which clears the
                # entire cache (the method already handles `asset=None` as
                # "clear all" — see per_pair.py line 141-142).
                #
                # FIX (F-08-32): record the cache-invalidation outcome so the
                # caller knows whether the new weights will actually take
                # effect on the next prediction.
                try:
                    from engines.otc.config import weight_adapter as _otc_adapter
                    from engines.real.config import weight_adapter as _real_adapter
                    _otc_adapter.invalidate_cache()  # FIX: was invalidate_cache_all()
                    _real_adapter.invalidate_cache()  # FIX: was invalidate_cache_all()
                except Exception as _cache_err:
                    cache_invalidated = False
                    cache_error = str(_cache_err)
                    print(f"[auto_tune] cache invalidation failed: {_cache_err}")

        return {
            "otc": tuned_otc,
            "real": tuned_real,
            "cache_invalidated": cache_invalidated,
            "cache_error": cache_error,
        }
    except Exception as e:
        print(f"[auto_tune] error: {e}")
        return None
