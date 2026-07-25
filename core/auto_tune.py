"""
core/auto_tune.py — Auto-tune module weights based on live win rates.

DEEP IMPLEMENTATION (2026-07-23):

This module reads per-module win rates from signal_log and adjusts
the engine's DEFAULT_WEIGHTS accordingly. It runs periodically
(every 100 graded signals) and updates weights in real-time.

TUNING RULES:
  - Win rate >= 55%  → weight × 1.3 (BOOST)
  - Win rate 50-54%  → weight × 1.0 (KEEP)
  - Win rate 45-49%  → weight × 0.8 (DAMPEN)
  - Win rate < 45%   → weight × 0.5 (SEVERE DAMPEN)
  - Win rate < 35%   → weight × 0.1 (EFFECTIVELY DISABLED)

The tuning is CONSERVATIVE — it blends the tuned weight with the
static weight using a 70/30 prior (70% static, 30% tuned) to avoid
overreacting to small sample sizes. As sample count grows, the blend
shifts toward the tuned weight.

MINIMUM SAMPLES: 20 graded signals per module before tuning kicks in.
Below that, the static weight is used unchanged.
"""
import json
import os
import sqlite3
import time
from collections import defaultdict

DB_PATH = os.environ.get("DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "signals.db"))

# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-24): make the win-rate query LIMIT
# env-configurable so operators can widen the sample window on large DBs.
# The old hardcoded 2000 over-weighted recent performance after the DB
# grew past ~100k signals (only the latest 2% was sampled).
_AUTO_TUNE_MAX_ROWS = int(os.environ.get("AUTO_TUNE_MAX_ROWS", "5000"))

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

MIN_SAMPLES = 20  # need at least 20 graded signals per module to tune
_MAX_WEIGHT = 1.5  # never boost above this
_MIN_WEIGHT = 0.1  # never dampen below this (keep module alive for display)


def _get_module_win_rates() -> dict:
    """Read per-module win rates from signal_log across all pairs.

    Returns: {module_name: {correct, total, win_rate}}
    """
    try:
        from core.constants import MODULE_NAMES
    except ImportError:
        MODULE_NAMES = (
            "candle_reaction", "running_tick", "pattern",
            "indicator", "key_level", "otc_pattern", "trend_follow",
        )

    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-23): use db._conn() to inherit
    # WAL mode + synchronous=NORMAL. A raw non-WAL reader on a WAL-mode DB
    # causes lock contention with the feed's WAL writers — every
    # apply_tuned_weights_to_engines call (every ~100 graded signals) blocks
    # writes for 100ms+ on slow disks. Falls back to raw connect if db
    # module isn't importable.
    try:
        import db as _db
        conn = _db._conn()
    except Exception:
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
        reasons_raw = row["reasons"] or "[]"
        try:
            reasons = json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw
        except (ValueError, TypeError):
            reasons = []
        if not isinstance(reasons, list):
            reasons = []

        for reason in reasons:
            reason_str = str(reason)
            if not reason_str.startswith("["):
                continue
            end_bracket = reason_str.find("]")
            if end_bracket == -1:
                continue
            module = reason_str[1:end_bracket].strip()
            if module not in MODULE_NAMES:
                continue
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-25): parse direction
            # from the END of the reason (after the final "→") rather than
            # substring-scanning the whole string. The old logic checked
            # PUT/BEAR/SELLER BEFORE CALL/BULL/BUYER, so a reason mentioning
            # BOTH (e.g. "[running_tick] seller pressure easing, buyer taking
            # over → CALL") was misclassified as PUT. This corrupted per-
            # module win rates for modules whose reasons naturally mention
            # both directions.
            #
            # Strategy: find the final arrow in the reason and look at the
            # trailing token. If neither matches, fall back to the legacy
            # scan (still PUT-first, but only for reasons with no arrow).
            tail = reason_str.rsplit("→", 1)[-1].strip().upper() if "→" in reason_str else ""
            if tail.endswith("CALL") or " CALL" in tail or tail == "CALL":
                module_dir = "CALL"
            elif tail.endswith("PUT") or " PUT" in tail or tail == "PUT":
                module_dir = "PUT"
            else:
                # Legacy fallback: PUT-first scan (only used when no arrow present).
                upper = reason_str.upper()
                if "CALL" in upper or "BULL" in upper or "BUYER" in upper:
                    module_dir = "CALL"
                elif "PUT" in upper or "BEAR" in upper or "SELLER" in upper:
                    module_dir = "PUT"
                else:
                    continue

            if accuracy not in ("correct", "wrong"):
                continue

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
        tuned = 0.1
    elif win_rate >= 0.70:
        tuned = 1.5
    else:
        tuned = 0.1 + (win_rate - 0.30) / 0.40 * (1.5 - 0.1)

    # FIX (AUDIT-2-21): adaptive blend. tuned_weight_blend goes from 0.3 at
    # MIN_SAMPLES (20) to 0.9 at 200+ samples. A 0% win-rate module with
    # 200+ samples now gets blended = 0.1*static + 0.9*0.1 = ~0.22 (clamped to
    # _MIN_WEIGHT 0.1) instead of the old 0.94.
    if total is None or total <= 0:
        tuned_weight_blend = 0.3
    else:
        _N_REF_MAX = 200  # sample count at which tuned weight dominates (90%).
        tuned_weight_blend = min(0.9, 0.3 + 0.6 * max(0, total - MIN_SAMPLES) / max(1, _N_REF_MAX - MIN_SAMPLES))
    blended = (1 - tuned_weight_blend) * static_weight + tuned_weight_blend * tuned

    # Clamp
    return max(_MIN_WEIGHT, min(_MAX_WEIGHT, blended))


def compute_tuned_weights(engine: str = "otc") -> dict:
    """Compute auto-tuned weights for an engine.

    Args:
        engine: "otc" or "real"

    Returns:
        {module_name: tuned_weight}
    """
    if engine == "real":
        static = STATIC_WEIGHTS_REAL
    else:
        static = STATIC_WEIGHTS_OTC

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
    win_rates = _get_module_win_rates()
    tuned_otc = compute_tuned_weights("otc")
    tuned_real = compute_tuned_weights("real")

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
        report["win_rates"][module] = {
            "correct": stats.get("correct", 0),
            "total": wr_total,
            "win_rate": wr_display,
            "status": status,
        }

    return report


def apply_tuned_weights_to_engines():
    """Update the engine configs with auto-tuned weights.

    Called periodically (every 100 graded signals) from feed.py.
    Updates engines.otc.config.DEFAULT_WEIGHTS and
    engines.real.config.DEFAULT_WEIGHTS in-place.
    """
    try:
        tuned_otc = compute_tuned_weights("otc")
        tuned_real = compute_tuned_weights("real")

        from engines.otc.config import DEFAULT_WEIGHTS as _otc_w
        from engines.real.config import DEFAULT_WEIGHTS as _real_w

        changed_otc = False
        for m, w in tuned_otc.items():
            if m in _otc_w and abs(_otc_w[m] - w) > 0.01:
                _otc_w[m] = w
                changed_otc = True

        changed_real = False
        for m, w in tuned_real.items():
            if m in _real_w and abs(_real_w[m] - w) > 0.01:
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
            try:
                from engines.otc.config import weight_adapter as _otc_adapter
                from engines.real.config import weight_adapter as _real_adapter
                _otc_adapter.invalidate_cache()  # FIX: was invalidate_cache_all()
                _real_adapter.invalidate_cache()  # FIX: was invalidate_cache_all()
            except Exception as _cache_err:
                print(f"[auto_tune] cache invalidation failed: {_cache_err}")

        return {"otc": tuned_otc, "real": tuned_real}
    except Exception as e:
        print(f"[auto_tune] error: {e}")
        return None
