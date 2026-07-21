"""
core/time_patterns.py — Per-pair time/session/regime pattern storage + lookup.

Stores backtest-derived patterns in the `time_session_patterns` SQLite table:
  - per (asset, hour_utc)   → win_rate, total
  - per (asset, session)    → win_rate, total
  - per (asset, dow_utc)    → win_rate, total
  - per (asset, regime)     → win_rate, total

The blender consults this table at prediction time to apply a confidence
boost/dampener based on the current (asset, hour, session, regime). The brain
refreshes the table periodically from signal_log.

Tables:
  time_session_patterns:
    asset TEXT, dimension TEXT, key TEXT,
    win_rate REAL, total INTEGER, correct INTEGER, wrong INTEGER,
    last_updated REAL,
    PRIMARY KEY (asset, dimension, key)

  dimension ∈ {'hour','session','dow','regime','tag'}
  key       = the value within that dimension (e.g. '3' for hour=3, 'ASIAN' for session)
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "signals.db"))

_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
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


def session_for_hour(hour):
    """Map UTC hour (0-23) to a trading-session label."""
    if 0 <= hour < 7:   return "ASIAN"
    if 7 <= hour < 13:  return "LONDON"
    if 13 <= hour < 17: return "OVERLAP"
    if 17 <= hour < 21: return "NY"
    return "LATE_NY"


def upsert_pattern(asset, dimension, key, win_rate, total, correct, wrong):
    """Insert or update a single pattern row."""
    conn = _conn()
    try:
        with _lock:
            cur = conn.cursor()
            cur.execute("""INSERT OR REPLACE INTO time_session_patterns
                (asset, dimension, key, win_rate, total, correct, wrong, last_updated)
                VALUES (?,?,?,?,?,?,?,?)""",
                (asset, dimension, str(key), float(win_rate),
                 int(total), int(correct), int(wrong), time.time()))
            conn.commit()
    finally:
        conn.close()


def bulk_upsert_patterns(rows):
    """Bulk insert/replace many pattern rows.
    rows = list of (asset, dimension, key, win_rate, total, correct, wrong)
    """
    if not rows:
        return
    conn = _conn()
    try:
        with _lock:
            cur = conn.cursor()
            cur.executemany("""INSERT OR REPLACE INTO time_session_patterns
                (asset, dimension, key, win_rate, total, correct, wrong, last_updated)
                VALUES (?,?,?,?,?,?,?,?)""",
                [(a, d, str(k), float(wr), int(t), int(c), int(wg), time.time())
                 for (a, d, k, wr, t, c, wg) in rows])
            conn.commit()
    finally:
        conn.close()


def get_all_patterns(asset):
    """Return all stored patterns for an asset, grouped by dimension.
    Returns: {dimension: {key: {win_rate, total, correct, wrong, last_updated}}}
    """
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
        return out
    finally:
        conn.close()


def get_pattern(asset, dimension, key):
    """Return a single pattern's stats, or None."""
    conn = _conn()
    try:
        cur = conn.cursor()
        row = cur.execute("""SELECT win_rate, total, correct, wrong FROM time_session_patterns
                             WHERE asset=? AND dimension=? AND key=?""",
                          (asset, dimension, str(key))).fetchone()
        if not row:
            return None
        return {"win_rate": row["win_rate"], "total": row["total"],
                "correct": row["correct"], "wrong": row["wrong"]}
    finally:
        conn.close()


def get_time_adjustment(asset, ctime):
    """Compute a multiplicative confidence adjustment based on stored patterns.

    Combines the per-(asset, hour), per-(asset, session), and per-(asset, dow)
    patterns into a single multiplier in [0.5, 1.5].

    Logic:
      - For each dimension with data, compute deviation from 0.50 baseline.
      - Sum the deviations, clamp to [-0.30, +0.30].
      - Convert to multiplier: 1.0 + deviation.
      - Skip a dimension if total < 5 (not enough data).

    Returns (multiplier, debug_note).
    """
    dt = datetime.fromtimestamp(ctime, tz=timezone.utc)
    hour = dt.hour
    dow  = dt.weekday()  # 0=Mon
    session = session_for_hour(hour)

    patterns = get_all_patterns(asset)

    deviations = []
    notes = []

    # Hour dimension
    hour_p = patterns.get("hour", {}).get(str(hour))
    if hour_p and hour_p["total"] >= 5:
        dev = hour_p["win_rate"] - 0.50
        deviations.append(dev)
        notes.append(f"hour={hour}({hour_p['win_rate']:.0%},n={hour_p['total']}):{dev:+.2f}")

    # Session dimension
    sess_p = patterns.get("session", {}).get(session)
    if sess_p and sess_p["total"] >= 5:
        dev = sess_p["win_rate"] - 0.50
        deviations.append(dev)
        notes.append(f"sess={session}({sess_p['win_rate']:.0%},n={sess_p['total']}):{dev:+.2f}")

    # Day-of-week dimension
    dow_p = patterns.get("dow", {}).get(str(dow))
    if dow_p and dow_p["total"] >= 5:
        dev = dow_p["win_rate"] - 0.50
        deviations.append(dev)
        notes.append(f"dow={dow}({dow_p['win_rate']:.0%},n={dow_p['total']}):{dev:+.2f}")

    if not deviations:
        return 1.0, ""

    # Average the deviations (so one bad hour doesn't dominate if session+dow are neutral)
    avg_dev = sum(deviations) / len(deviations)
    # Clamp to [-0.30, +0.30] — at most ±30% adjustment.
    clamped = max(-0.30, min(0.30, avg_dev))
    # Slightly amplify the effect since averaging dilutes.
    multiplier = 1.0 + clamped * 1.2  # 1.2x amplification, but clamped input keeps it bounded
    # Final clamp to [0.5, 1.5]
    multiplier = max(0.5, min(1.5, multiplier))

    note = "_TIME_PATTERN: " + " | ".join(notes) + f" → mult ×{multiplier:.2f}"
    return multiplier, note


def get_regime_adjustment(asset, regime_name):
    """Compute a multiplicative confidence adjustment based on regime pattern.

    Returns (multiplier, debug_note).
    """
    if not regime_name:
        return 1.0, ""
    patterns = get_all_patterns(asset)
    reg_p = patterns.get("regime", {}).get(regime_name)
    if not reg_p or reg_p["total"] < 5:
        return 1.0, ""
    dev = reg_p["win_rate"] - 0.50
    # Regime is a stronger signal — allow up to ±25% adjustment.
    clamped = max(-0.25, min(0.25, dev))
    multiplier = 1.0 + clamped * 1.5  # 1.5x amplification
    multiplier = max(0.6, min(1.4, multiplier))
    note = (f"_REGIME_PATTERN: {regime_name}({reg_p['win_rate']:.0%},n={reg_p['total']}) "
            f"→ mult ×{multiplier:.2f}")
    return multiplier, note


def get_tag_adjustment(asset, tags):
    """Compute adjustment based on tags (COUNTER_REGIME, WITH_REGIME, etc.).

    If any tag has a known strong pattern (win_rate >= 0.55 or <= 0.45 with n >= 5),
    apply a small boost/dampener.

    Returns (multiplier, debug_note).
    """
    if not tags:
        return 1.0, ""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if isinstance(tags, str) else tags
    if not tag_list:
        return 1.0, ""
    patterns = get_all_patterns(asset)
    tag_dim = patterns.get("tag", {})

    deviations = []
    notes = []
    for t in tag_list:
        tp = tag_dim.get(t)
        if tp and tp["total"] >= 5:
            dev = tp["win_rate"] - 0.50
            if abs(dev) >= 0.05:  # only count meaningful deviations
                deviations.append(dev)
                notes.append(f"tag={t}({tp['win_rate']:.0%},n={tp['total']}):{dev:+.2f}")

    if not deviations:
        return 1.0, ""

    avg_dev = sum(deviations) / len(deviations)
    clamped = max(-0.15, min(0.15, avg_dev))  # tags are weaker signal — max ±15%
    multiplier = 1.0 + clamped
    multiplier = max(0.85, min(1.15, multiplier))
    note = "_TAG_PATTERN: " + " | ".join(notes) + f" → mult ×{multiplier:.2f}"
    return multiplier, note


def recompute_from_signal_log(min_samples=3):
    """Recompute ALL patterns from signal_log.

    Called by the brain on a periodic schedule (every ~100 graded signals)
    or via /api/patterns/refresh HTTP endpoint.

    Reads all graded CALL/PUT signals from signal_log, groups by
    (asset, hour, session, dow, regime, tag), computes win_rate per group,
    and bulk-upserts into time_session_patterns.

    Returns a summary dict: {asset: {dimension: pattern_count}}
    """
    # Import here to avoid circular import with db.py
    import db as _db
    conn = _db._conn()
    # FIX (BACKTEST-2026-07-21): db._conn() doesn't set row_factory, so
    # rows come back as tuples. Set it here so r["asset"] works.
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        # Pull all graded signals with category breakdown
        rows = cur.execute("""SELECT asset, period, ctime, signal, accuracy, regime, tags,
                                      strength, confidence
                              FROM signal_log
                              WHERE signal IN ('CALL','PUT')
                                AND accuracy IN ('correct','wrong')""").fetchall()
    finally:
        conn.close()

    if not rows:
        return {}

    # Group counts
    # Key: (asset, dimension, value) -> {correct, wrong, total}
    groups = {}
    for r in rows:
        asset = r["asset"]
        ctime = r["ctime"]
        if not ctime:
            continue
        dt = datetime.fromtimestamp(ctime, tz=timezone.utc)
        hour = dt.hour
        dow  = dt.weekday()
        session = session_for_hour(hour)
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

    # Build bulk upsert rows
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
    return summary


def get_pattern_summary():
    """Return a summary of all stored patterns for /api/patterns endpoint."""
    conn = _conn()
    try:
        cur = conn.cursor()
        rows = cur.execute("""SELECT asset, dimension, COUNT(*) as n,
                              AVG(win_rate) as avg_wr, MIN(total) as min_n, MAX(total) as max_n,
                              MAX(last_updated) as last_upd
                              FROM time_session_patterns
                              GROUP BY asset, dimension
                              ORDER BY asset, dimension""").fetchall()
        out = []
        for r in rows:
            out.append({
                "asset": r["asset"],
                "dimension": r["dimension"],
                "pattern_count": r["n"],
                "avg_win_rate": r["avg_wr"],
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
