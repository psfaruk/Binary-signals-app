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


# FIX (DEAD-CODE-2026-07-21): removed upsert_pattern() and get_pattern() —
# never called. recompute_from_signal_log uses bulk_upsert_patterns(), and
# the API endpoints use get_all_patterns() / get_pattern_summary() /
# get_asset_patterns_detail().


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


def get_time_adjustment(asset, ctime):
    """Compute a multiplicative confidence adjustment based on stored patterns.

    Combines the per-(asset, hour), per-(asset, session), and per-(asset, dow)
    patterns into a single multiplier.

    FIX (PREDICTION-FIX-2026-07-21): the previous version used min_samples=5
    which is far too low for binary win/loss outcomes. With n=5, statistical
    variance alone produces observed win rates ≥80% or ≤20% ~37% of the time
    even when the true win rate is exactly 50%. The system was treating this
    noise as real "patterns" and scaling confidence ±30-50%.
    Now requires n >= 30 (statistical rule of thumb for binary outcomes).
    Also reduced the multiplier swing to match per_pair.py's conservative
    ~9% max (was up to ±50% here). The pattern adjustment is meant to be
    a nudge, not a swing.

    Logic:
      - For each dimension with data, compute deviation from 0.50 baseline.
      - Weight each dimension's deviation by sqrt(n) so larger samples
        contribute more (but not linearly — diminishing returns).
      - Average the weighted deviations.
      - Clamp to [-0.06, +0.06] — at most ±6% adjustment (matches
        per_pair.py's conservative adaptation range).
      - Multiplier: 1.0 + clamped_dev (final range [0.94, 1.06]).

    Returns (multiplier, debug_note).
    """
    dt = datetime.fromtimestamp(ctime, tz=timezone.utc)
    hour = dt.hour
    dow  = dt.weekday()  # 0=Mon
    session = session_for_hour(hour)

    patterns = get_all_patterns(asset)

    # FIX (PREDICTION-FIX-2026-07-21): raised from 5 to 30.
    # With n=5, binomial variance is so high that pure noise produces
    # extreme observed win rates. n=30 gives ~±18% confidence interval
    # at p=0.5, which is acceptable for a nudge.
    MIN_SAMPLES = 30

    weighted_devs = []   # [(dev, weight), ...]
    notes = []

    # Hour dimension
    hour_p = patterns.get("hour", {}).get(str(hour))
    if hour_p and hour_p["total"] >= MIN_SAMPLES:
        dev = hour_p["win_rate"] - 0.50
        # Weight by sqrt(n) — larger samples count more, but with diminishing returns.
        # Cap weight at sqrt(100) = 10 so a single huge sample doesn't dominate.
        weight = min(10.0, (hour_p["total"] ** 0.5))
        weighted_devs.append((dev, weight))
        notes.append(f"hour={hour}({hour_p['win_rate']:.0%},n={hour_p['total']}):{dev:+.2f}")

    # Session dimension
    sess_p = patterns.get("session", {}).get(session)
    if sess_p and sess_p["total"] >= MIN_SAMPLES:
        dev = sess_p["win_rate"] - 0.50
        weight = min(10.0, (sess_p["total"] ** 0.5))
        weighted_devs.append((dev, weight))
        notes.append(f"sess={session}({sess_p['win_rate']:.0%},n={sess_p['total']}):{dev:+.2f}")

    # Day-of-week dimension
    dow_p = patterns.get("dow", {}).get(str(dow))
    if dow_p and dow_p["total"] >= MIN_SAMPLES:
        dev = dow_p["win_rate"] - 0.50
        weight = min(10.0, (dow_p["total"] ** 0.5))
        weighted_devs.append((dev, weight))
        notes.append(f"dow={dow}({dow_p['win_rate']:.0%},n={dow_p['total']}):{dev:+.2f}")

    if not weighted_devs:
        return 1.0, ""

    # Weighted average of deviations (so a high-n dimension with a small
    # deviation can outweigh a low-n dimension with a large deviation).
    total_weight = sum(w for _, w in weighted_devs)
    weighted_avg_dev = sum(d * w for d, w in weighted_devs) / total_weight

    # FIX (PREDICTION-FIX-2026-07-21): clamp to ±6% (was ±30%).
    # The previous ±30% swing was 3x more aggressive than per_pair.py's
    # DB-adaptation (which uses a prior-weighted blend for max ~9% swing).
    # A nudge of ±6% is enough to break ties without overriding the
    # engine's actual prediction logic.
    clamped = max(-0.06, min(0.06, weighted_avg_dev))
    multiplier = 1.0 + clamped
    # Final safety clamp.
    multiplier = max(0.94, min(1.06, multiplier))

    note = "_TIME_PATTERN: " + " | ".join(notes) + f" → mult ×{multiplier:.3f}"
    return multiplier, note


def get_regime_adjustment(asset, regime_name):
    """Compute a multiplicative confidence adjustment based on regime pattern.

    FIX (PREDICTION-FIX-2026-07-21): raised min_samples from 5 to 30 and
    reduced multiplier swing from ±25% to ±6% (matching the time-adjustment
    conservative range). The previous ±25% swing was 2.5x more aggressive
    than per_pair.py's adaptation.

    Returns (multiplier, debug_note).
    """
    if not regime_name:
        return 1.0, ""
    patterns = get_all_patterns(asset)
    reg_p = patterns.get("regime", {}).get(regime_name)
    # FIX: raised from 5 to 30.
    if not reg_p or reg_p["total"] < 30:
        return 1.0, ""
    dev = reg_p["win_rate"] - 0.50
    # FIX: clamp to ±6% (was ±25%).
    clamped = max(-0.06, min(0.06, dev))
    multiplier = 1.0 + clamped
    multiplier = max(0.94, min(1.06, multiplier))
    note = (f"_REGIME_PATTERN: {regime_name}({reg_p['win_rate']:.0%},n={reg_p['total']}) "
            f"→ mult ×{multiplier:.3f}")
    return multiplier, note


def get_tag_adjustment(asset, tags):
    """Compute adjustment based on tags (COUNTER_REGIME, WITH_REGIME, etc.).

    FIX (PREDICTION-FIX-2026-07-21): raised min_samples from 5 to 30 and
    reduced multiplier swing from ±15% to ±4%. Tags are the weakest signal
    (often correlating with regime/condition that's already accounted for),
    so they get the smallest adjustment range.

    Returns (multiplier, debug_note).
    """
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
        # FIX: raised from 5 to 30.
        if tp and tp["total"] >= 30:
            dev = tp["win_rate"] - 0.50
            if abs(dev) >= 0.05:  # only count meaningful deviations
                weight = min(10.0, (tp["total"] ** 0.5))
                weighted_devs.append((dev, weight))
                notes.append(f"tag={t}({tp['win_rate']:.0%},n={tp['total']}):{dev:+.2f}")

    if not weighted_devs:
        return 1.0, ""

    total_weight = sum(w for _, w in weighted_devs)
    weighted_avg_dev = sum(d * w for d, w in weighted_devs) / total_weight
    # FIX: clamp to ±4% (was ±15%).
    clamped = max(-0.04, min(0.04, weighted_avg_dev))
    multiplier = 1.0 + clamped
    multiplier = max(0.96, min(1.04, multiplier))
    note = "_TAG_PATTERN: " + " | ".join(notes) + f" → mult ×{multiplier:.3f}"
    return multiplier, note


def recompute_from_signal_log(min_samples=3, days_window=None):
    """Recompute ALL patterns from signal_log.

    Called by the brain on a periodic schedule (every ~100 graded signals)
    or via /api/patterns/refresh HTTP endpoint.

    Reads all graded CALL/PUT signals from signal_log, groups by
    (asset, hour, session, dow, regime, tag), computes win_rate per group,
    and bulk-upserts into time_session_patterns.

    FIX (PREDICTION-FIX-2026-07-21): added `days_window` parameter (default
    reads DAYS_WINDOW env var, falls back to 14). Only signals from the
    last N days are used. This prevents patterns from being contaminated
    by data produced under older (buggy) engine versions. The engine has
    undergone multiple structural fixes (structural reversal bias, HTF
    cold-start, chop-guard conversion, etc.) — pre-fix data doesn't
    represent the current engine's behavior, so including it would
    bias the pattern adjustments.
    Set days_window=0 or None to use ALL data (for backward compat or
    offline analysis).

    Returns a summary dict: {asset: {dimension: pattern_count}}
    """
    # Import here to avoid circular import with db.py
    import db as _db

    # FIX (PREDICTION-FIX-2026-07-21): default to last 14 days.
    if days_window is None:
        try:
            days_window = int(os.environ.get("PATTERN_DAYS_WINDOW", "14"))
        except (TypeError, ValueError):
            days_window = 14

    conn = _db._conn()
    # FIX (BACKTEST-2026-07-21): db._conn() doesn't set row_factory, so
    # rows come back as tuples. Set it here so r["asset"] works.
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        # FIX (PREDICTION-FIX-2026-07-21): add time filter — only use
        # recent data so patterns reflect the CURRENT engine's behavior.
        # Old data was produced by previous (buggy) engine versions and
        # would contaminate the pattern adjustments.
        # FIX: prefer `ts` column (insert-time); fall back to `ctime`
        # (candle-open time) for older schemas / backtest DBs that don't
        # have `ts`. Both are unix timestamps so the comparison is valid.
        if days_window and days_window > 0:
            cutoff_ts = time.time() - (days_window * 86400)
            # Try `ts` first (production schema); if column doesn't exist,
            # fall back to `ctime`.
            try:
                rows = cur.execute("""SELECT asset, period, ctime, signal, accuracy, regime, tags,
                                              strength, confidence
                                      FROM signal_log
                                      WHERE signal IN ('CALL','PUT')
                                        AND accuracy IN ('correct','wrong')
                                        AND ts >= ?""", (cutoff_ts,)).fetchall()
            except sqlite3.OperationalError:
                rows = cur.execute("""SELECT asset, period, ctime, signal, accuracy, regime, tags,
                                              strength, confidence
                                      FROM signal_log
                                      WHERE signal IN ('CALL','PUT')
                                        AND accuracy IN ('correct','wrong')
                                        AND ctime >= ?""", (cutoff_ts,)).fetchall()
        else:
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
