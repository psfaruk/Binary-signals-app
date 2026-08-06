"""core/auto_tune.py — Auto-tune module weights based on live win rates."""
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

MIN_SAMPLES = AUTO_TUNE_MIN_SAMPLES
_MAX_WEIGHT = AUTO_TUNE_MAX_WEIGHT
_MIN_WEIGHT = AUTO_TUNE_MIN_WEIGHT
_WEIGHT_CHANGE_THRESHOLD = AUTO_TUNE_WEIGHT_CHANGE_THRESHOLD

_apply_lock = threading.Lock()

_AUTO_TUNE_MAX_ROWS = AUTO_TUNE_MAX_ROWS

STATIC_WEIGHTS_OTC = {
    "candle_reaction": 1.3,
    "pattern":         1.0,
    "key_level":       0.7,
}

STATIC_WEIGHTS_REAL = {
    "candle_reaction": 1.3,
    "pattern":         1.0,
    "key_level":       0.8,
}

def _get_module_win_rates() -> dict:
    """Read per-module win rates from signal_log across all pairs."""
    try:
        import db as _db
        conn = _db._conn()
    except ImportError:
        conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
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
        reasons = parse_reasons(row["reasons"] or "[]")
        for reason in reasons:
            reason_str = str(reason)
            module, module_dir = parse_module_direction(reason_str, MODULE_NAMES)
            if module is None or module_dir is None:
                continue
            stats[module]["total"] += 1
            if module_dir == final_signal and accuracy == "correct":
                stats[module]["correct"] += 1
            elif module_dir != final_signal and accuracy == "wrong":
                stats[module]["correct"] += 1
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
    """Convert a win rate to a tuned weight."""
    if win_rate <= 0.30:
        tuned = _MIN_WEIGHT
    elif win_rate >= 0.70:
        tuned = _MAX_WEIGHT
    else:
        tuned = _MIN_WEIGHT + (win_rate - 0.30) / 0.40 * (_MAX_WEIGHT - _MIN_WEIGHT)
    _N_REF_MAX = 200  # sample count at which tuned weight dominates (90%).
    tuned_weight_blend = min(
        0.9,
        0.3 + 0.6 * max(0, total - MIN_SAMPLES) / max(1, _N_REF_MAX - MIN_SAMPLES),
    )
    blended = (1 - tuned_weight_blend) * static_weight + tuned_weight_blend * tuned
    return max(_MIN_WEIGHT, min(_MAX_WEIGHT, blended))
def compute_tuned_weights(engine: str = "otc", win_rates: dict = None) -> dict:
    """Compute auto-tuned weights for an engine."""
    if engine == "real":
        static = STATIC_WEIGHTS_REAL
    else:
        static = STATIC_WEIGHTS_OTC
    if win_rates is None:
        win_rates = _get_module_win_rates()
    tuned = {}
    for module, static_w in static.items():
        stats = win_rates.get(module, {})
        total = stats.get("total", 0)
        wr = stats.get("win_rate")
        if total < MIN_SAMPLES or wr is None:
            tuned[module] = static_w
        else:
            tuned[module] = round(_win_rate_to_weight(wr, static_w, total), 2)
    return tuned
def get_tuning_report() -> dict:
    """Generate a human-readable tuning report for /api/auto-tune endpoint."""
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
            "tuned_weight_otc": tuned_otc.get(module),
            "tuned_weight_real": tuned_real.get(module),
        }
    return report
def apply_tuned_weights_to_engines():
    """Update the engine configs with auto-tuned weights."""
    try:
        win_rates = _get_module_win_rates()
        tuned_otc = compute_tuned_weights("otc", win_rates=win_rates)
        tuned_real = compute_tuned_weights("real", win_rates=win_rates)
        from engines.otc.config import DEFAULT_WEIGHTS as _otc_w
        from engines.real.config import DEFAULT_WEIGHTS as _real_w
        cache_invalidated = True
        cache_error = None
        with _apply_lock:
            changed_otc = False
            for m, w in tuned_otc.items():
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
