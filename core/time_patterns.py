"""core/time_patterns.py — Per-pair time/session/regime pattern storage + lookup."""
import copy
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from core.constants import (
    TIME_PATTERN_CACHE_TTL,
    TIME_PATTERN_DAYS_WINDOW,
    TIME_PATTERN_MIN_SAMPLES,
    TIME_PATTERN_MIN_SAMPLES_HOUR,
    TIME_PATTERN_RECOMPUTE_MIN_SAMPLES,
    TIME_PATTERN_REGIME_MIN_SAMPLES,
    TIME_PATTERN_TAG_MIN_SAMPLES,
)

try:
    from zoneinfo import ZoneInfo
    _NY_TZ = ZoneInfo("America/New_York")
except (ImportError, OSError, ValueError):  # pragma: no cover — fallback path
    _NY_TZ = None

DB_PATH = os.environ.get("DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "signals.db"))

_lock = threading.Lock()

_PATTERNS_CACHE_TTL = TIME_PATTERN_CACHE_TTL
_PATTERNS_CACHE: dict = {}  # asset → (timestamp, patterns_dict)

_PATTERNS_CACHE_LOCK = threading.Lock()

def _conn():
    """Open a SQLite connection to signals.db with WAL + Row factory."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    conn.row_factory = sqlite3.Row
    return conn
def init_patterns():
    """Create the time_session_patterns table if it doesn't exist."""
    conn = _conn()
    try:
        with _lock:
            cur = conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS time_session_patterns (
                asset TEXT, dimension TEXT, key TEXT,
                win_rate REAL, total INTEGER, correct INTEGER, wrong INTEGER,
                last_updated REAL,
                PRIMARY KEY (asset, dimension, key)
            )""")
            cur.execute("""CREATE INDEX IF NOT EXISTS ix_tsp_asset_dim
                          ON time_session_patterns(asset, dimension)""")
            conn.commit()
    finally:
        conn.close()
def session_for_hour(hour, dt=None):
    """Map UTC hour (0-23) to a trading-session label."""
    if hour < 7:   return "ASIAN"
    if hour < 13:  return "LONDON"
    if hour < 17:  return "OVERLAP"
    if hour < 21:  return "NY"
    if dt is not None and _NY_TZ is not None:
        try:
            ny_dt = dt.astimezone(_NY_TZ)
            if ny_dt.hour <= 16:
                return "NY"
        except (ValueError, OSError, AttributeError) as _e:
            print(f"[silent-except] core/time_patterns.py:165 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
            pass  # fall through to default ASIA_OPEN
    return "ASIA_OPEN"
def bulk_upsert_patterns(rows):
    """Bulk insert/replace many pattern rows."""
    if not rows:
        return
    conn = _conn()
    try:
        with _lock:
            cur = conn.cursor()
            clean_rows = []
            for (a, d, k, wr, t, c, wg) in rows:
                if wr is None:
                    continue
                try:
                    clean_rows.append((
                        a, d, str(k),
                        float(wr), int(t), int(c), int(wg),
                        time.time(),
                    ))
                except (TypeError, ValueError):
                    continue
            if not clean_rows:
                return
            cur.executemany("""INSERT OR REPLACE INTO time_session_patterns
                (asset, dimension, key, win_rate, total, correct, wrong, last_updated)
                VALUES (?,?,?,?,?,?,?,?)""", clean_rows)
            conn.commit()
    finally:
        conn.close()
def get_all_patterns(asset):
    """Return all stored patterns for an asset, grouped by dimension."""
    now = time.time()
    with _PATTERNS_CACHE_LOCK:
        cached = _PATTERNS_CACHE.get(asset)
        if cached is not None:
            cached_ts, cached_patterns = cached
            if now - cached_ts < _PATTERNS_CACHE_TTL:
                return copy.deepcopy(cached_patterns)
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            rows = cur.execute("""SELECT dimension, key, win_rate, total, correct, wrong, last_updated
                                  FROM time_session_patterns WHERE asset=?""",
                               (asset,)).fetchall()
            out = {}
            for r in rows:
                dim = r["dimension"]
                if dim not in out:
                    out[dim] = {}
                out[dim][r["key"]] = {
                    "win_rate": r["win_rate"],
                    "total":    r["total"],
                    "correct":  r["correct"],
                    "wrong":    r["wrong"],
                    "last_updated": r["last_updated"],
                }
        finally:
            conn.close()
    except sqlite3.Error as _db_err:
        with _PATTERNS_CACHE_LOCK:
            _PATTERNS_CACHE[asset] = (now, {})
        print(f"[time_patterns] get_all_patterns({asset}) failed: {_db_err}")
        return {}
    with _PATTERNS_CACHE_LOCK:
        _PATTERNS_CACHE[asset] = (now, out)
    return copy.deepcopy(out)
def invalidate_patterns_cache(asset: str = None):
    """Clear the patterns cache (call after recompute_from_signal_log)."""
    with _PATTERNS_CACHE_LOCK:
        if asset is None:
            _PATTERNS_CACHE.clear()
        else:
            _PATTERNS_CACHE.pop(asset, None)
def get_time_adjustment(asset, ctime):
    """Compute a multiplicative confidence adjustment based on stored patterns."""
    dt = datetime.fromtimestamp(ctime, tz=timezone.utc)
    hour = dt.hour
    dow  = dt.weekday()  # 0=Mon
    session = session_for_hour(hour, dt=dt)
    patterns = get_all_patterns(asset)
    MIN_SAMPLES = TIME_PATTERN_MIN_SAMPLES
    MIN_SAMPLES_HOUR = TIME_PATTERN_MIN_SAMPLES_HOUR
    weighted_devs = []   # [(dev, weight), ...]
    notes = []
    hour_p = patterns.get("hour", {}).get(str(hour))
    if hour_p and hour_p["total"] >= MIN_SAMPLES_HOUR:
        dev = hour_p["win_rate"] - 0.50
        weight = min(10.0, (hour_p["total"] ** 0.5))
        weighted_devs.append((dev, weight))
        notes.append(f"hour={hour}({hour_p['win_rate']:.0%},n={hour_p['total']}):{dev:+.2f}")
    sess_p = patterns.get("session", {}).get(session)
    if sess_p and sess_p["total"] >= MIN_SAMPLES:
        dev = sess_p["win_rate"] - 0.50
        weight = min(10.0, (sess_p["total"] ** 0.5))
        weighted_devs.append((dev, weight))
        notes.append(f"sess={session}({sess_p['win_rate']:.0%},n={sess_p['total']}):{dev:+.2f}")
    dow_p = patterns.get("dow", {}).get(str(dow))
    if dow_p and dow_p["total"] >= MIN_SAMPLES:
        dev = dow_p["win_rate"] - 0.50
        weight = min(10.0, (dow_p["total"] ** 0.5))
        weighted_devs.append((dev, weight))
        notes.append(f"dow={dow}({dow_p['win_rate']:.0%},n={dow_p['total']}):{dev:+.2f}")
    if not weighted_devs:
        return 1.0, ""
    total_weight = sum(w for _, w in weighted_devs)
    weighted_avg_dev = sum(d * w for d, w in weighted_devs) / total_weight
    clamped = max(-0.06, min(0.06, weighted_avg_dev))
    multiplier = 1.0 + clamped
    note = "_TIME_PATTERN: " + " | ".join(notes) + f" → mult ×{multiplier:.3f}"
    return multiplier, note
def get_regime_adjustment(asset, regime_name):
    """Compute a multiplicative confidence adjustment based on regime pattern."""
    if not regime_name:
        return 1.0, ""
    patterns = get_all_patterns(asset)
    reg_p = patterns.get("regime", {}).get(regime_name)
    if not reg_p or reg_p["total"] < TIME_PATTERN_REGIME_MIN_SAMPLES:
        return 1.0, ""
    dev = reg_p["win_rate"] - 0.50
    clamped = max(-0.06, min(0.06, dev))
    multiplier = 1.0 + clamped
    note = (f"_REGIME_PATTERN: {regime_name}({reg_p['win_rate']:.0%},n={reg_p['total']}) "
            f"→ mult ×{multiplier:.3f}")
    return multiplier, note
def get_tag_adjustment(asset, tags):
    """Compute adjustment based on tags (COUNTER_REGIME, WITH_REGIME, etc.)."""
    if not tags:
        return 1.0, ""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if isinstance(tags, str) else tags
    if not tag_list:
        return 1.0, ""
    patterns = get_all_patterns(asset)
    tag_dim = patterns.get("tag", {})
    weighted_devs = []
    notes = []
    for t in tag_list:
        tp = tag_dim.get(t)
        if tp and tp["total"] >= TIME_PATTERN_TAG_MIN_SAMPLES:
            dev = tp["win_rate"] - 0.50
            if abs(dev) >= 0.05:  # only count meaningful deviations
                weight = min(10.0, (tp["total"] ** 0.5))
                weighted_devs.append((dev, weight))
                notes.append(f"tag={t}({tp['win_rate']:.0%},n={tp['total']}):{dev:+.2f}")
    if not weighted_devs:
        return 1.0, ""
    total_weight = sum(w for _, w in weighted_devs)
    weighted_avg_dev = sum(d * w for d, w in weighted_devs) / total_weight
    clamped = max(-0.04, min(0.04, weighted_avg_dev))
    multiplier = 1.0 + clamped
    note = "_TAG_PATTERN: " + " | ".join(notes) + f" → mult ×{multiplier:.3f}"
    return multiplier, note
def recompute_from_signal_log(min_samples=None, days_window=None):
    """Recompute ALL patterns from signal_log."""
    if min_samples is None:
        min_samples = TIME_PATTERN_RECOMPUTE_MIN_SAMPLES
    import db as _db
    if days_window is None:
        days_window = TIME_PATTERN_DAYS_WINDOW
    conn = _db._conn()
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        if days_window and days_window > 0:
            cutoff_ts = time.time() - (days_window * 86400)
            try:
                rows = cur.execute("""SELECT asset, ctime, signal, accuracy, regime, tags
                                      FROM signal_log
                                      WHERE signal IN ('CALL','PUT')
                                        AND accuracy IN ('correct','wrong')
                                        AND ctime IS NOT NULL
                                        AND ts >= ?""", (cutoff_ts,)).fetchall()
            except sqlite3.OperationalError:
                rows = cur.execute("""SELECT asset, ctime, signal, accuracy, regime, tags
                                      FROM signal_log
                                      WHERE signal IN ('CALL','PUT')
                                        AND accuracy IN ('correct','wrong')
                                        AND ctime IS NOT NULL
                                        AND ctime >= ?""", (cutoff_ts,)).fetchall()
        else:
            rows = cur.execute("""SELECT asset, ctime, signal, accuracy, regime, tags
                                  FROM signal_log
                                  WHERE signal IN ('CALL','PUT')
                                    AND accuracy IN ('correct','wrong')
                                    AND ctime IS NOT NULL""").fetchall()
    finally:
        conn.close()
    if not rows:
        return {}
    groups = {}
    for r in rows:
        asset = r["asset"]
        ctime = r["ctime"]
        if not ctime:
            continue
        try:
            dt = datetime.fromtimestamp(ctime, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            continue
        hour = dt.hour
        dow  = dt.weekday()
        session = session_for_hour(hour, dt=dt)
        regime = r["regime"] or "UNKNOWN"
        tags_raw = r["tags"] or ""
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        is_correct = r["accuracy"] == "correct"
        for dim, val in [("hour", str(hour)), ("session", session),
                          ("dow", str(dow)), ("regime", regime)]:
            key = (asset, dim, val)
            if key not in groups:
                groups[key] = {"correct": 0, "wrong": 0, "total": 0}
            groups[key]["total"] += 1
            if is_correct:
                groups[key]["correct"] += 1
            else:
                groups[key]["wrong"] += 1
        for t in tags:
            key = (asset, "tag", t)
            if key not in groups:
                groups[key] = {"correct": 0, "wrong": 0, "total": 0}
            groups[key]["total"] += 1
            if is_correct:
                groups[key]["correct"] += 1
            else:
                groups[key]["wrong"] += 1
    upsert_rows = []
    summary = {}
    for (asset, dim, val), counts in groups.items():
        if counts["total"] < min_samples:
            continue
        wr = counts["correct"] / counts["total"]
        upsert_rows.append((asset, dim, val, wr, counts["total"],
                            counts["correct"], counts["wrong"]))
        summary.setdefault(asset, {}).setdefault(dim, 0)
        summary[asset][dim] += 1
    bulk_upsert_patterns(upsert_rows)
    invalidate_patterns_cache()
    return summary
def get_pattern_summary():
    """Return a summary of all stored patterns for /api/patterns endpoint."""
    conn = _conn()
    try:
        cur = conn.cursor()
        rows = cur.execute("""SELECT asset, dimension, COUNT(*) as n,
                              AVG(win_rate) as avg_wr_simple,
                              SUM(correct) as sum_correct,
                              SUM(wrong) as sum_wrong,
                              SUM(total) as sum_total,
                              MIN(total) as min_n, MAX(total) as max_n,
                              MAX(last_updated) as last_upd
                              FROM time_session_patterns
                              GROUP BY asset, dimension
                              ORDER BY asset, dimension""").fetchall()
        out = []
        for r in rows:
            sum_total = r["sum_total"] or 0
            sum_correct = r["sum_correct"] or 0
            avg_wr_weighted = (sum_correct / sum_total) if sum_total else None
            out.append({
                "asset": r["asset"],
                "dimension": r["dimension"],
                "pattern_count": r["n"],
                "avg_win_rate": avg_wr_weighted,
                "avg_win_rate_simple": r["avg_wr_simple"],
                "min_samples": r["min_n"],
                "max_samples": r["max_n"],
                "last_updated": r["last_upd"],
            })
        return out
    finally:
        conn.close()
def get_asset_patterns_detail(asset):
    """Return the full pattern detail for one asset (for /api/patterns/{asset})."""
    return get_all_patterns(asset)
