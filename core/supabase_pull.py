"""core/supabase_pull.py — Supabase → SQLite durable-history read-back.

VERCEL-DEPLOY (2026-11). On a serverless platform every function instance
starts with an empty /tmp, so the SQLite read endpoints would always see a
fresh ledger. This module is the read counterpart of supabase_sync.py: it
pulls the mirrored tables back into the local SQLite so that

    • /api/share-signals, history, win-rate, stats read real rows again
    • the burst worker (api/cron/refresh.py) re-arms the history-gate,
      pair-health and pattern learning from the durable ledger before it
      runs the live feed

Best-effort by design — identical error policy to supabase_sync.py:
never raise, never block the caller for long, disabled without env vars.
The write side (SQLite → Supabase) remains supabase_sync.sync_once().
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any

import httpx

_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
_KEY = (os.environ.get("SUPABASE_ANON_KEY", "").strip()
        or os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip())
_ENABLED = bool(_URL and _KEY)
# Warm instances re-pull at most this often (cold starts always pull).
_INTERVAL = max(30, int(os.environ.get("QX_PULL_INTERVAL_SECS", "120")))
_LOCK = threading.Lock()
_LAST_PULL = 0.0

# Mirror of supabase_sync.sync_once()'s write set, minus candle_micro
# (high volume — the dedicated read-back for candles already exists as
# supabase_sync.fetch_candles, used by core.otc_dataset).
_TABLES: dict[str, dict[str, Any]] = {
    "signal_log": {"order": "id.desc", "limit": 5000, "pk": "id"},
    "otc_predictions": {"order": "id.desc", "limit": 10000, "pk": "id"},
    "model_registry": {"order": "id", "limit": 1000, "pk": "id"},
}


def enabled() -> bool:
    return _ENABLED


def _headers() -> dict[str, str]:
    return {"apikey": _KEY, "Authorization": f"Bearer {_KEY}"}


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in
            conn.execute(f"PRAGMA table_info({table})").fetchall()]


def pull(force: bool = False) -> dict[str, int]:
    """Pull mirrored tables from Supabase into local SQLite.

    Throttled to QX_PULL_INTERVAL_SECS (default 120 s) unless force=True.
    Rows are INSERT OR REPLACEd by primary key, so a pull is idempotent and
    never duplicates history. Returns per-table counts; {} when disabled or
    throttled. Never raises.
    """
    global _LAST_PULL
    if not _ENABLED:
        return {}
    with _LOCK:
        now = time.time()
        if not force and (now - _LAST_PULL) < _INTERVAL:
            return {}
        _LAST_PULL = now

    import db as _db

    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(_db.DB_PATH, timeout=10.0)
        try:
            with httpx.Client(timeout=25.0) as client:
                for table, spec in _TABLES.items():
                    try:
                        resp = client.get(
                            f"{_URL}/rest/v1/{table}",
                            headers=_headers(),
                            params={"select": "*",
                                    "order": spec["order"],
                                    "limit": str(spec["limit"])},
                        )
                        resp.raise_for_status()
                        rows = resp.json()
                    except Exception as _exc:
                        print(f"[supabase-pull] {table} failed (non-fatal): "
                              f"{type(_exc).__name__}: {_exc}")
                        continue
                    if not isinstance(rows, list) or not rows:
                        counts[table] = 0
                        continue
                    cols = _table_columns(conn, table)
                    # Only insert columns that exist locally — forward
                    # compatibility when either schema drifts.
                    usable = [c for c in cols if c in rows[0]]
                    if not usable:
                        counts[table] = 0
                        continue
                    placeholders = ",".join("?" for _ in usable)
                    sql = (f"INSERT OR REPLACE INTO {table} "
                           f"({','.join(usable)}) VALUES ({placeholders})")
                    n = 0
                    for row in rows:
                        try:
                            conn.execute(sql, [row.get(c) for c in usable])
                            n += 1
                        except sqlite3.Error:
                            continue
                    conn.commit()
                    counts[table] = n
        finally:
            conn.close()
    except Exception as _exc:
        print(f"[supabase-pull] failed (non-fatal): "
              f"{type(_exc).__name__}: {_exc}")
        return counts
    if any(counts.values()):
        print(f"[supabase-pull] pulled {sum(counts.values())} rows: "
              f"{counts}")
    return counts
