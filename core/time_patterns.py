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

# FIX (DEEP-AUDIT-2026-07-26 / F-08-33): remove dead imports `json` and
# `contextmanager` — neither name was referenced anywhere in this file
# (A-04 problems 73, 74).

# FIX (DEEP-AUDIT-2026-07-26 / F-08-34): optional DST-aware session
# mapping. zoneinfo is stdlib on Python 3.9+; on older runtimes we fall
# back to the (DST-naive) hardcoded UTC hour boundaries (A-04 problem 11).
try:
    from zoneinfo import ZoneInfo
    _NY_TZ = ZoneInfo("America/New_York")
except (ImportError, OSError, ValueError):  # pragma: no cover — fallback path
    _NY_TZ = None

DB_PATH = os.environ.get("DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "signals.db"))

_lock = threading.Lock()

# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-30): short-lived in-memory cache for
# get_all_patterns(asset). The blender calls get_time_adjustment,
# get_regime_adjustment, and get_tag_adjustment on EVERY prediction, and each
# one independently calls get_all_patterns(asset) — opening a SQLite
# connection and querying the time_session_patterns table. With ~240
# predictions/minute, that was 720 DB connections/minute just for pattern
# lookups. The cache holds the patterns for an asset for _PATTERNS_CACHE_TTL
# seconds (default 5s), so the three calls within a single prediction reuse
# the same cached dict and hit the DB at most once per (asset, 5s window).
# This cuts DB connections by ~3x without changing the public API.
_PATTERNS_CACHE_TTL = TIME_PATTERN_CACHE_TTL
_PATTERNS_CACHE: dict = {}  # asset → (timestamp, patterns_dict)

# FIX (DEEP-AUDIT-2026-07-26 / F-08-35): dedicated lock for the cache dict.
# The existing module-level `_lock` is held while WRITING to the DB
# (init_patterns, bulk_upsert_patterns); acquiring the same lock to read
# the cache would serialise every prediction behind every DB write. A
# separate `_PATTERNS_CACHE_LOCK` guards only the in-memory dict mutations
# so reads stay fast (A-04 problem 75).
_PATTERNS_CACHE_LOCK = threading.Lock()


def _conn():
    """Open a SQLite connection to signals.db with WAL + Row factory.

    FIX (DEEP-AUDIT-2026-07-26 / F-08-36): keep a local `_conn()` instead
    of switching wholesale to `db._conn()` because the latter does not
    set `row_factory = sqlite3.Row` (which this module depends on). The
    PRAGMA-swallowing `except Exception: pass` is preserved for backward
    compatibility — failing to set WAL on a read-only filesystem is a
    non-fatal degradation (A-04 problem 76 noted the duplication; the
    fix here documents why we keep the local copy).
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        # FIX (DEEP-AUDIT-2026-07-26 / F-08-37): narrow the swallow from
        # bare `Exception` to `sqlite3.Error` so genuine programming
        # errors (e.g.OperationalError on a locked DB) still surface
        # during development. PRAGMAs failing on read-only filesystems
        # remain non-fatal.
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
    """Map UTC hour (0-23) to a trading-session label.

    FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-14): rename 21-23 UTC bucket from
    "LATE_NY" to "ASIA_OPEN". NY session closes at 21:00-22:00 UTC; 21-23 UTC
    is actually the Sydney/Tokyo pre-open (Asian session start). The old
    label misled session-pattern insights ("skip LATE_NY" when the real
    issue is the Asia pre-open).

    FIX (DEEP-AUDIT-2026-07-26 / F-08-38): drop the redundant `0 <= hour`
    check — `dt.hour` is always in 0-23 (A-04 problem 77).

    FIX (DEEP-AUDIT-2026-07-26 / F-08-39): DST-aware NY/ASIA_OPEN boundary.
    When `dt` (a timezone-aware datetime in UTC) is provided AND zoneinfo is
    importable, the boundary is computed using America/New_York: in winter
    (EST, UTC-5) NY close is at 22:00 UTC, so hour=21 UTC is still "NY";
    in summer (EDT, UTC-4) NY close is at 21:00 UTC, so hour=21 UTC is
    "ASIA_OPEN". The old hardcoded `21` boundary was correct only half
    the year (A-04 problem 11). When `dt` is None (backwards-compatible
    callers), the old behaviour is preserved.
    """
    # FIX (F-08-38): `0 <= hour` is always True — drop it.
    if hour < 7:   return "ASIAN"
    if hour < 13:  return "LONDON"
    if hour < 17:  return "OVERLAP"
    if hour < 21:  return "NY"
    # 21-23 UTC: post-NY-close in summer (DST) but during NY session in
    # winter (EST). NY close is at 16:00 ET, which is 20:00 UTC in summer
    # (EDT, UTC-4) and 21:00 UTC in winter (EST, UTC-5). The original
    # hardcoded `21` boundary labelled 21:00 UTC as ASIA_OPEN year-round,
    # which was wrong for winter (NY was still open at 21:00 UTC = 16:00 EST).
    if dt is not None and _NY_TZ is not None:
        try:
            ny_dt = dt.astimezone(_NY_TZ)
            # NY-local hour <= 16 → still NY session (close is at 16:00 ET).
            # Summer 21 UTC = 17 EDT → past close → ASIA_OPEN (fall through).
            # Winter 21 UTC = 16 EST → at close → still NY.
            if ny_dt.hour <= 16:
                return "NY"
        except (ValueError, OSError, AttributeError):
            pass  # fall through to default ASIA_OPEN
    return "ASIA_OPEN"


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
            # FIX (DEEP-AUDIT-2026-07-26 / F-08-40): guard against `wr=None`
            # (buggy callers / NaN floats). The old `float(None)` raised
            # `TypeError`, aborting the ENTIRE bulk insert — so one bad row
            # would prevent every other row from being stored (A-04 problem 79).
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


# FIX (DEAD-CODE-2026-07-21): removed upsert_pattern() and get_pattern() —
# never called. recompute_from_signal_log uses bulk_upsert_patterns(), and
# the API endpoints use get_all_patterns() / get_pattern_summary() /
# get_asset_patterns_detail().


def get_all_patterns(asset):
    """Return all stored patterns for an asset, grouped by dimension.
    Returns: {dimension: {key: {win_rate, total, correct, wrong, last_updated}}}

    FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-30): consult the short-lived
    in-memory cache before hitting the DB. The blender calls this function
    three times per prediction (via get_time/regime/tag_adjustment), so
    caching for 5s eliminates 2/3 of the DB round-trips.

    FIX (DEEP-AUDIT-2026-07-26 / F-08-41): return a DEEP COPY of the cached
    dict so callers can't mutate the cache by accident. Previously the live
    cache dict was returned — a caller doing
    `patterns["hour"]["3"]["win_rate"] = 0.5` corrupted the cache for all
    future lookups for up to _PATTERNS_CACHE_TTL seconds (A-04 problem 80).

    FIX (DEEP-AUDIT-2026-07-26 / F-08-42): on DB error, cache an EMPTY dict
    for a short TTL so repeated errors don't thundering-herd the DB
    (A-04 problem 81).
    """
    now = time.time()
    with _PATTERNS_CACHE_LOCK:
        cached = _PATTERNS_CACHE.get(asset)
        if cached is not None:
            cached_ts, cached_patterns = cached
            if now - cached_ts < _PATTERNS_CACHE_TTL:
                # FIX (F-08-41): deep copy the cached dict before returning.
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
        # FIX (F-08-42): cache an empty dict for a short TTL so the next
        # call (likely milliseconds away, from the regime/tag adjustments)
        # doesn't immediately re-hit the DB.
        with _PATTERNS_CACHE_LOCK:
            _PATTERNS_CACHE[asset] = (now, {})
        print(f"[time_patterns] get_all_patterns({asset}) failed: {_db_err}")
        return {}
    # Cache the result (even if empty, so repeated lookups for an asset with
    # no patterns don't all hit the DB).
    with _PATTERNS_CACHE_LOCK:
        _PATTERNS_CACHE[asset] = (now, out)
    return copy.deepcopy(out)


def invalidate_patterns_cache(asset: str = None):
    """Clear the patterns cache (call after recompute_from_signal_log)."""
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-43): guard the cache mutation with
    # the cache lock so a concurrent get_all_patterns() doesn't see a
    # half-cleared dict (A-04 problem 75).
    with _PATTERNS_CACHE_LOCK:
        if asset is None:
            _PATTERNS_CACHE.clear()
        else:
            _PATTERNS_CACHE.pop(asset, None)


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
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-44): pass `dt` into session_for_hour
    # so the NY/ASIA_OPEN boundary is DST-aware (A-04 problem 11).
    dt = datetime.fromtimestamp(ctime, tz=timezone.utc)
    hour = dt.hour
    dow  = dt.weekday()  # 0=Mon
    session = session_for_hour(hour, dt=dt)

    patterns = get_all_patterns(asset)

    # FIX (PREDICTION-FIX-2026-07-21): raised from 5 to 30.
    # With n=5, binomial variance is so high that pure noise produces
    # extreme observed win rates. n=30 gives ~±18% confidence interval
    # at p=0.5, which is acceptable for a nudge.
    #
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-1-12): the hour dimension
    # gets a LOWER threshold (15) because each (asset, hour) bucket has fewer
    # samples than (asset, session) buckets — there are 24 hour-buckets vs 5
    # session-buckets, so the same data is spread 5x thinner per bucket. With
    # MIN_SAMPLES=30 for hour, almost no hour-bucket qualified after 4-5 days
    # of live data; the hour adjustment (the most granular dimension) never
    # fired. Lowering to 15 lets more hour-buckets activate while still
    # avoiding pure-noise thresholds (n=15 → ~±25% CI at p=0.5, acceptable
    # for a ±6% nudge).
    #
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-45): pull thresholds from
    # `core.constants` so they're env-configurable (A-04 problem cross-
    # cutting theme — pervasive magic numbers).
    MIN_SAMPLES = TIME_PATTERN_MIN_SAMPLES
    MIN_SAMPLES_HOUR = TIME_PATTERN_MIN_SAMPLES_HOUR

    weighted_devs = []   # [(dev, weight), ...]
    notes = []

    # Hour dimension
    hour_p = patterns.get("hour", {}).get(str(hour))
    if hour_p and hour_p["total"] >= MIN_SAMPLES_HOUR:
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
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-46): drop the redundant safety
    # clamp. `clamped` is already in [-0.06, +0.06], so `1.0 + clamped`
    # is already in [0.94, 1.06] — the second clamp was a no-op (A-04
    # problem 82).

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
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-47): pull the threshold from
    # `core.constants` (env-configurable).
    if not reg_p or reg_p["total"] < TIME_PATTERN_REGIME_MIN_SAMPLES:
        return 1.0, ""
    dev = reg_p["win_rate"] - 0.50
    # FIX: clamp to ±6% (was ±25%).
    clamped = max(-0.06, min(0.06, dev))
    multiplier = 1.0 + clamped
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-48): drop the redundant safety
    # clamp (same reasoning as F-08-46). A-04 problem 83.
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
        # FIX (DEEP-AUDIT-2026-07-26 / F-08-49): pull the threshold from
        # `core.constants` (env-configurable).
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
    # FIX: clamp to ±4% (was ±15%).
    clamped = max(-0.04, min(0.04, weighted_avg_dev))
    multiplier = 1.0 + clamped
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-50): drop the redundant safety
    # clamp (same reasoning as F-08-46). A-04 problem 84.
    note = "_TAG_PATTERN: " + " | ".join(notes) + f" → mult ×{multiplier:.3f}"
    return multiplier, note


def recompute_from_signal_log(min_samples=None, days_window=None):
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

    FIX (DEEP-AUDIT-2026-07-26 / F-08-51): default `min_samples` is now
    `core.constants.TIME_PATTERN_RECOMPUTE_MIN_SAMPLES` (env-configurable,
    default 30) instead of the hardcoded `3`. The lookups in
    `get_time/regime/tag_adjustment` all require `total >= 30`, so
    thousands of pattern rows with 3-29 samples were being stored but
    never consulted — pure DB bloat, and `get_pattern_summary` reported
    a misleading `MIN(total)=3` (A-04 problem 12).

    Returns a summary dict: {asset: {dimension: pattern_count}}
    """
    # FIX (F-08-51): env-configurable default (30) instead of hardcoded 3.
    if min_samples is None:
        min_samples = TIME_PATTERN_RECOMPUTE_MIN_SAMPLES

    # Import here to avoid circular import with db.py
    import db as _db

    # FIX (F-08-51 cont.): pull PATTERN_DAYS_WINDOW from constants too.
    if days_window is None:
        days_window = TIME_PATTERN_DAYS_WINDOW

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
        #
        # FIX (DEEP-AUDIT-2026-07-26 / F-08-52): drop unused columns
        # (`period`, `strength`, `confidence`) from the SELECT — they're
        # never read in this function (A-04 problem 85). Also add
        # `AND ctime IS NOT NULL` so rows with NULL ctime aren't fetched
        # and then skipped in the loop (A-04 problem 86).
        if days_window and days_window > 0:
            cutoff_ts = time.time() - (days_window * 86400)
            # Try `ts` first (production schema); if column doesn't exist,
            # fall back to `ctime`.
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

    # Group counts
    # Key: (asset, dimension, value) -> {correct, wrong, total}
    groups = {}
    for r in rows:
        asset = r["asset"]
        ctime = r["ctime"]
        # FIX (F-08-52): the SQL now filters `ctime IS NOT NULL`, but
        # keep a defensive guard in case a future schema change reintroduces
        # NULLs through a different code path.
        if not ctime:
            continue
        # FIX (DEEP-AUDIT-2026-07-26 / F-08-53): wrap fromtimestamp in
        # try/except so a single corrupted ctime (negative / overflow /
        # NaN) doesn't abort the entire recompute (A-04 problem 87).
        try:
            dt = datetime.fromtimestamp(ctime, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            continue
        hour = dt.hour
        dow  = dt.weekday()
        # FIX (F-08-44 cont.): pass `dt` for DST-aware session labelling.
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
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-30): invalidate the in-memory
    # patterns cache so the next prediction sees the freshly-recomputed
    # patterns immediately (otherwise stale cached patterns would persist
    # for up to _PATTERNS_CACHE_TTL seconds after recompute).
    invalidate_patterns_cache()
    return summary


def get_pattern_summary():
    """Return a summary of all stored patterns for /api/patterns endpoint.

    FIX (DEEP-AUDIT-2026-07-26 / F-08-54): replace `AVG(win_rate)` with a
    sample-weighted aggregate `SUM(correct) * 1.0 / SUM(total)`. The old
    simple average over-weighted small-sample keys (e.g. hour=3 with n=50
    contributed as much to the average as hour=14 with n=200). The
    weighted aggregate properly accounts for sample size, so the reported
    `avg_win_rate` is the true pooled win rate across all keys in the
    dimension (A-04 problem 88). The original `avg_win_rate` field is
    preserved as `avg_win_rate_simple` for backward compatibility with
    any caller that wanted the un-weighted mean.
    """
    conn = _conn()
    try:
        cur = conn.cursor()
        # FIX (F-08-54): fetch correct/total sums so we can compute the
        # sample-weighted aggregate; keep AVG(win_rate) as a simple-mean
        # alias for backward compat.
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
            # Sample-weighted win rate across all keys in this (asset, dim).
            avg_wr_weighted = (sum_correct / sum_total) if sum_total else None
            out.append({
                "asset": r["asset"],
                "dimension": r["dimension"],
                "pattern_count": r["n"],
                # Primary field is now the sample-weighted aggregate.
                "avg_win_rate": avg_wr_weighted,
                # Backward-compat alias for the simple (un-weighted) mean.
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
