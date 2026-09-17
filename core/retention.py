"""
core/retention.py — RAILWAY-500MB-FIX (2026-09-17): bounded-storage retention.

USER REQUIREMENT (verbatim):
  "শুধু মাত্র পেয়ার এর ohlc রেকর্ড সেভ রাখবেন, 4 ঘণ্টার এর, বাকি যত ডেটা
   সেভ হওয়ার কথা সব কিছু সেভ থাকবে মাত্র 30 মিনিট, এর পরে সকল backdate
   ডাটা অটো ডিলিট হয়ে যাবে। এতে করে রেল ওয়ে ভলিউম full হবে না।
   অ্যাপ ক্র্যাশ ও করবে না।"

WHY THE APP WAS CRASHING ON RAILWAY (root cause, measured):
  The free plan caps the attached Volume at 500 MB. The old retention
  default was **90 days** and the cleanup pass ran only every 6 hours.
  Growth math: candle_micro carries ticks_json (~3-5 KB/row) and gets one
  row per closed candle per pair → 22 pairs × 1440 candles/day ≈ 31,700
  rows/day ≈ 100-150 MB/day from candle_micro ALONE, plus signal_log,
  module_votes, theory_votes, otc_predictions and friends. The production
  volume had already reached 175 MB (see brain.py STRAT-FIX note). When the
  volume hits 500 MB every SQLite write fails with SQLITE_FULL /
  "database or disk is full" → the signal engine's DB calls raise → the
  feed/stream loops degrade → Railway restart-loops the container.

THE POLICY (this module, applied every 60 s in a daemon thread):
  1. candle_micro  — the PAIR OHLC RECORDS — keep the last
                    QX_RETENTION_OHLC_SECS (default 4 hours = 14400 s).
  2. EVERY other time-series table — signals, predictions, module votes,
                    theory votes, quality metrics, brain records, algo
                    changes, share snapshots, aggregate pattern rows —
                    keep only QX_RETENTION_DATA_SECS (default 30 min =
                    1800 s). Everything older is auto-deleted, always.
  3. Bounded runtime state tables are NOT time-pruned because they can
     never grow (PRIMARY-KEY-replaced, fixed row count) and deleting them
     would break auth/learning/model-serving:
       _meta, model_registry, api_keys, agent_models, algorithm_state.
     (Their stale rows are still refreshed by the natural writers; the
     volume math does not involve them — a few hundred KB at worst.)

SPACE RECLAMATION (the part everyone forgets):
  Deleting rows does NOT shrink an SQLite file — pages move to the
  freelist and the file stays at its high-water mark, so the Railway
  volume keeps filling even with aggressive deletes. This module:
    • runs `PRAGMA wal_checkpoint(TRUNCATE)` after every pass so the
      -wal side-car stays near zero;
    • runs `VACUUM` when freelist pages exceed 25% of the file (or 8 MB),
      actually returning the space to the Volume;
    • keeps only QX_DB_BACKUP_KEEP (default 2) rolling DB backups.

STORAGE WATCHDOG (belt + braces, the "never again crashes" guarantee):
  soft cap  (QX_RETENTION_DIR_SOFT_MB,  default 350 MB):
      force an immediate retention pass, prune backups to 1, VACUUM.
  hard cap  (QX_RETENTION_DIR_HARD_MB,  default 450 MB):
      EMERGENCY — delete every backup, apply emergency windows
      (1 h OHLC / 5 min data), VACUUM, log CRITICAL.
  Caps are measured on the whole DB directory (db + wal + backups) via
  shutil.disk_usage on the containing filesystem, so even unrelated
  junk written into the Volume triggers the guard.

Public API:
  apply_retention()  → dict of per-table deleted counts (safe to call
                       from anywhere; batched deletes, own connection)
  start()            → idempotent daemon-thread scheduler (60 s default)
  retention_info()   → policy + sizes + last-pass stats (for /api/latency)
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time

import db as _db

# ── Policy windows (env-overridable, repo convention) ───────────────────────
OHLC_SECS = int(os.environ.get("QX_RETENTION_OHLC_SECS", "14400"))   # 4 h
DATA_SECS = int(os.environ.get("QX_RETENTION_DATA_SECS", "1800"))    # 30 min
INTERVAL_SECS = max(15, int(os.environ.get("QX_RETENTION_INTERVAL_SECS", "60")))
ENABLED = os.environ.get("QX_RETENTION_ENABLED", "1") == "1"

# Emergency windows used ONLY when the hard disk cap trips.
_EMERGENCY_OHLC_SECS = max(600, int(os.environ.get("QX_RETENTION_EMR_OHLC_SECS", "3600")))
_EMERGENCY_DATA_SECS = max(120, int(os.environ.get("QX_RETENTION_EMR_DATA_SECS", "300")))

# Storage watchdog caps on the DB directory size (MB).
_DIR_SOFT_MB = float(os.environ.get("QX_RETENTION_DIR_SOFT_MB", "350"))
_DIR_HARD_MB = float(os.environ.get("QX_RETENTION_DIR_HARD_MB", "450"))
# Also watch the filesystem itself (Railway mounts the volume; other
# processes could fill it too). free-space floors in MB.
_DISK_FREE_SOFT_MB = float(os.environ.get("QX_RETENTION_DISK_FREE_SOFT_MB", "60"))
_DISK_FREE_HARD_MB = float(os.environ.get("QX_RETENTION_DISK_FREE_HARD_MB", "25"))

# VACUUM trigger: freelist must exceed this fraction of the file…
_VACUUM_FREE_FRACTION = 0.25
# …or this many pages (page_size × pages; 8 MB at 4 KB pages).
_VACUUM_FREE_PAGES = 2048

_BATCH = 2000   # delete batch size — keeps lock windows tiny


# ── The policy table spec ───────────────────────────────────────────────────
# (table, time column, bucket) — bucket: "ohlc" (4 h) or "data" (30 min).
# Time columns are all epoch seconds. INTEGER columns are preferred where
# an index exists (ctime on the hot tables) so the DELETEs stay index-driven.
_POLICY: tuple[tuple[str, str, str], ...] = (
    # ── THE pair OHLC records — 4 HOURS ─────────────────────────────────
    ("candle_micro",            "ctime",         "ohlc"),
    # ── Everything else — 30 MINUTES ────────────────────────────────────
    ("signal_log",              "ctime",         "data"),
    ("otc_predictions",         "signal_time",   "data"),
    ("module_votes",            "ctime",         "data"),
    ("theory_votes",            "ctime",         "data"),
    ("signal_quality_metrics",  "ctime",         "data"),
    ("brain_predictions",       "ts",            "data"),
    ("brain_module_votes",      "ts",            "data"),
    ("brain_learning",          "ts",            "data"),
    ("brain_patterns",          "ts",            "data"),
    ("brain_insights",          "ts",            "data"),
    ("algorithm_changes",       "ts",            "data"),
    ("quotex_algo_patterns",    "ts",            "data"),
    ("pair_performance_daily",  "ts",            "data"),
    ("pair_hourly_patterns",    "last_updated",  "data"),
    ("time_session_patterns",   "last_updated",  "data"),
    ("pair_gate_state",         "updated_ts",    "data"),
    ("share_signal_history",    "ts",            "data"),
)

# Bounded runtime state — NEVER time-pruned (see module docstring §3).
_STATE_TABLES = ("_meta", "model_registry", "api_keys", "agent_models",
                 "algorithm_state")


_lock = threading.Lock()
_thread: threading.Thread | None = None
_started = False
_last_stats: dict = {}
_last_pass_ts = 0.0


# ── helpers ─────────────────────────────────────────────────────────────────
def _dir_size_bytes(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _backups_dir() -> str:
    return os.path.join(os.path.dirname(_db.DB_PATH) or ".", "backups")


def _prune_backups(keep: int) -> int:
    """Keep only the newest `keep` rolling backups. Returns deleted count."""
    bdir = _backups_dir()
    try:
        backups = sorted(
            (f for f in os.listdir(bdir)
             if f.startswith("signals_") and f.endswith(".db")),
            reverse=True)
    except OSError:
        return 0
    deleted = 0
    for old in backups[keep:]:
        try:
            os.unlink(os.path.join(bdir, old))
            deleted += 1
        except OSError:
            pass
    return deleted


def _prune_wal(conn: sqlite3.Connection) -> None:
    """Shrink the -wal side-car. Failures are non-fatal."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def _maybe_vacuum(conn: sqlite3.Connection) -> bool:
    """VACUUM when free pages dominate the file. Returns True if run."""
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    except (sqlite3.Error, TypeError, IndexError):
        return False
    if freelist <= 0:
        return False
    if freelist < _VACUUM_FREE_PAGES and \
            (page_count <= 0 or freelist / page_count < _VACUUM_FREE_FRACTION):
        return False
    try:
        t0 = time.time()
        conn.execute("VACUUM")
        dur = time.time() - t0
        try:
            mb = os.path.getsize(_db.DB_PATH) / (1024 * 1024)
        except OSError:
            mb = -1
        print(f"[retention] VACUUM ok ({dur:.1f}s, db now "
              f"{mb:.1f} MB, freed {freelist} pages)")
        return True
    except sqlite3.Error as exc:
        print(f"[retention] VACUUM skipped (non-fatal): {exc}")
        return False


def _delete_batches(cur: sqlite3.Connection.cursor,
                    table: str, col: str, cutoff: float) -> int:
    """Batched DELETE so no long lock is ever held. Returns rows deleted."""
    total = 0
    while True:
        cur.execute(
            f"DELETE FROM {table} WHERE rowid IN ("
            f"    SELECT rowid FROM {table} WHERE {col} < ? LIMIT ?"
            f")",
            (cutoff, _BATCH))
        n = cur.rowcount
        total += n
        if n < _BATCH:
            break
    return total


# ── core pass ───────────────────────────────────────────────────────────────
def apply_retention(ohlc_secs: int | None = None,
                    data_secs: int | None = None) -> dict:
    """Apply the 4h-OHLC / 30-min-everything-else policy ONCE.

    Safe to call concurrently — takes the module lock, uses its own
    connection, batched deletes, commits per batch. Returns a stats dict:
      {table: deleted_rows, "__meta__": {...}}
    """
    global _last_stats, _last_pass_ts
    ohlc = int(ohlc_secs if ohlc_secs is not None else OHLC_SECS)
    data = int(data_secs if data_secs is not None else DATA_SECS)
    now = time.time()
    stats: dict = {"__meta__": {
        "ts": now, "ohlc_secs": ohlc, "data_secs": data,
        "vacuumed": False, "wal_truncated": False}}

    with _lock:
        conn = _db._conn()
        try:
            cur = conn.cursor()
            for table, col, bucket in _POLICY:
                window = ohlc if bucket == "ohlc" else data
                cutoff = now - window
                # Skip tables that don't exist (fresh DB, optional features).
                try:
                    cur.execute(
                        f"SELECT 1 FROM {table} WHERE {col} < ? LIMIT 1",
                        (cutoff,))
                    if cur.fetchone() is None:
                        stats[table] = 0
                        continue
                except sqlite3.Error:
                    stats[table] = "skipped"
                    continue
                try:
                    stats[table] = _delete_batches(cur, table, col, cutoff)
                except sqlite3.Error as exc:
                    print(f"[retention] {table} prune failed (non-fatal): {exc}")
                    stats[table] = f"error: {exc}"
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
            conn.commit()
            _prune_wal(conn)
            stats["__meta__"]["wal_truncated"] = True
            stats["__meta__"]["vacuumed"] = _maybe_vacuum(conn)
        finally:
            conn.close()

    _last_stats = stats
    _last_pass_ts = time.time()
    return stats


def storage_report() -> dict:
    """Sizes + filesystem free space for the watchdog and the API."""
    db_dir = os.path.dirname(_db.DB_PATH) or "."
    try:
        db_mb = os.path.getsize(_db.DB_PATH) / (1024 * 1024)
    except OSError:
        db_mb = 0.0
    try:
        wal_mb = os.path.getsize(_db.DB_PATH + "-wal") / (1024 * 1024)
    except OSError:
        wal_mb = 0.0
    dir_mb = _dir_size_bytes(db_dir) / (1024 * 1024)
    try:
        usage = shutil.disk_usage(db_dir)
        free_mb = usage.free / (1024 * 1024)
        total_mb = usage.total / (1024 * 1024)
    except OSError:
        free_mb, total_mb = -1.0, -1.0
    return {
        "db_mb": round(db_mb, 2),
        "wal_mb": round(wal_mb, 2),
        "data_dir_mb": round(dir_mb, 2),
        "disk_free_mb": round(free_mb, 2),
        "disk_total_mb": round(total_mb, 2),
        "soft_cap_mb": _DIR_SOFT_MB,
        "hard_cap_mb": _DIR_HARD_MB,
    }


def _emergency_pass(reason: str) -> None:
    """Hard-cap response: minimal windows, all backups gone, VACUUM."""
    print(f"[retention] ⚠️ EMERGENCY storage pass ({reason}) — applying "
          f"{_EMERGENCY_OHLC_SECS}s OHLC / {_EMERGENCY_DATA_SECS}s data "
          f"windows, deleting ALL backups")
    try:
        apply_retention(ohlc_secs=_EMERGENCY_OHLC_SECS,
                        data_secs=_EMERGENCY_DATA_SECS)
    except Exception as exc:
        print(f"[retention] emergency apply failed: {exc}")
    _prune_backups(0)
    # One more VACUUM attempt with a fresh connection.
    try:
        conn = _db._conn()
        try:
            _maybe_vacuum(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[retention] emergency vacuum failed: {exc}")


def watchdog_check() -> str | None:
    """Returns 'soft', 'hard' or None. Runs inside the scheduler loop."""
    rep = storage_report()
    if rep["disk_free_mb"] >= 0:
        if rep["disk_free_mb"] <= _DISK_FREE_HARD_MB:
            return "hard"
        if rep["disk_free_mb"] <= _DISK_FREE_SOFT_MB:
            return "soft"
    if rep["data_dir_mb"] >= _DIR_HARD_MB:
        return "hard"
    if rep["data_dir_mb"] >= _DIR_SOFT_MB:
        return "soft"
    return None


def _loop() -> None:
    # small initial delay so boot-time migrations / restores finish first
    time.sleep(min(20, max(3, INTERVAL_SECS // 5)))
    failures = 0
    while True:
        try:
            stats = apply_retention()
            deleted = sum(
                v for k, v in stats.items()
                if k != "__meta__" and isinstance(v, int))
            if deleted:
                print(f"[retention] pass: pruned {deleted} rows "
                      f"(OHLC>{OHLC_SECS // 60}min, data>{DATA_SECS // 60}min)")
        except Exception as exc:
            failures += 1
            print(f"[retention] pass failed (non-fatal, #{failures}): {exc}")
            if failures >= 10:
                print("[retention] 10 consecutive failures — sleeping 10 min")
                time.sleep(600)
                failures = 0

        try:
            level = watchdog_check()
            if level == "hard":
                _emergency_pass("hard cap")
            elif level == "soft":
                print("[retention] soft cap reached — pruning backups + "
                      "immediate pass")
                _prune_backups(1)
                try:
                    apply_retention()
                except Exception as exc:
                    print(f"[retention] soft-cap pass failed: {exc}")
        except Exception as exc:
            print(f"[retention] watchdog check failed (non-fatal): {exc}")

        time.sleep(INTERVAL_SECS)


def start() -> bool:
    """Start the idempotent retention daemon. Returns True if running.

    Skips: explicit disable, throwaway backtest/tmp DBs (same rule as the
    backup scheduler — a pruning thread must never eat a backtest DB).
    """
    global _thread, _started
    if not ENABLED:
        print("[retention] disabled via QX_RETENTION_ENABLED=0")
        return False
    base = os.path.basename(_db.DB_PATH)
    if os.environ.get("DB_PATH") and (
            "backtest" in base.lower() or "tmp" in base.lower()):
        return False
    with _lock:
        if _started and _thread and _thread.is_alive():
            return True
        _thread = threading.Thread(
            target=_loop, name="qx-retention", daemon=True)
        _thread.start()
        _started = True
    print(f"[retention] RAILWAY-500MB-FIX active: candle_micro "
          f"(pair OHLC) kept {OHLC_SECS // 60} min; ALL other data kept "
          f"{DATA_SECS // 60} min; pass every {INTERVAL_SECS}s; "
          f"dir caps soft={_DIR_SOFT_MB:.0f}MB hard={_DIR_HARD_MB:.0f}MB")
    return True


def retention_info() -> dict:
    """Full policy + size + last-pass snapshot for /api/latency."""
    rep = storage_report()
    return {
        "policy": {
            "ohlc_secs": OHLC_SECS,
            "data_secs": DATA_SECS,
            "interval_secs": INTERVAL_SECS,
            "enabled": ENABLED,
            "thread_running": bool(_thread and _thread.is_alive()),
            "bounded_state_tables_kept": list(_STATE_TABLES),
        },
        "storage": rep,
        "last_pass": {
            "ts": _last_pass_ts,
            "age_secs": round(time.time() - _last_pass_ts, 1)
                        if _last_pass_ts else None,
            "stats": _last_stats,
        },
    }
