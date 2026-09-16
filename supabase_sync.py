"""Supabase persistence bridge.

The application currently uses SQLite throughout the signal engine. This module
keeps that runtime intact while mirroring the durable application tables to
Supabase/Postgres. It is deliberately best-effort: a Supabase outage must not
stop the live signal/feed engine.

Required Railway env vars:
  SUPABASE_URL
  SUPABASE_ANON_KEY (or SUPABASE_PUBLISHABLE_KEY)

The local SQLite database remains the source of truth until the full DB layer
is migrated to PostgreSQL. This bridge prevents data loss across Railway
redeploys once the local DB is also placed on a persistent Volume.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import httpx


_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
_KEY = (os.environ.get("SUPABASE_ANON_KEY", "").strip()
        or os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip())
_ENABLED = bool(_URL and _KEY)
_INTERVAL = max(30, int(os.environ.get("SUPABASE_SYNC_SECS", "60")))
_BATCH = max(100, min(1000, int(os.environ.get("SUPABASE_SYNC_BATCH", "500"))))
_STOP = threading.Event()
_STARTED = False
_LOCK = threading.Lock()


def enabled() -> bool:
    return _ENABLED


def _headers() -> dict[str, str]:
    return {
        "apikey": _KEY,
        "Authorization": f"Bearer {_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


def _postgrest(table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    # Supabase PostgREST accepts a JSON array for bulk upsert.
    url = f"{_URL}/rest/v1/{table}"
    with httpx.Client(timeout=25.0) as client:
        r = client.post(url, headers=_headers(), json=rows)
        r.raise_for_status()


def _sqlite_rows(cur, sql: str, params=()) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.execute(sql, params).fetchall()]


def _sync_table(cur, table: str, sql: str, params=()) -> int:
    rows = _sqlite_rows(cur, sql, params)
    total = 0
    for i in range(0, len(rows), _BATCH):
        _postgrest(table, rows[i:i + _BATCH])
        total += len(rows[i:i + _BATCH])
    return total


def sync_once() -> dict[str, int]:
    """Mirror durable SQLite tables into Supabase in bounded batches."""
    if not _ENABLED:
        return {}

    import db as _db

    counts = {"signal_log": 0, "otc_predictions": 0,
              "candle_micro": 0, "model_registry": 0}
    with _db._read_cursor() as cur:
        # Signal history: newest-first bounded bootstrap. Once mirrored, the
        # periodic run uses the newest window to also catch grading updates.
        counts["signal_log"] = _sync_table(
            cur, "signal_log",
            "SELECT id, asset, period, ctime, signal, score, confidence,
                    theories, actual, accuracy, strength, agree, right_codes,
                    wrong_codes, reasons, a_open, a_close, regime, zone, tags,
                    postmortem, category, ts, total, signal_quality, strategy
             FROM signal_log ORDER BY id DESC LIMIT 5000")

        counts["otc_predictions"] = _sync_table(
            cur, "otc_predictions",
            "SELECT id, asset, period, signal_time, target_time, horizon,
                    prediction, probability, tier, score, emit, components,
                    regime, pa_agreed, quality, reason, model_version,
                    feature_json, close_i, created_at, actual_open,
                    actual_close, actual_result, win_loss, settled_at
             FROM otc_predictions ORDER BY id DESC LIMIT 10000")

        counts["candle_micro"] = _sync_table(
            cur, "candle_micro",
            "SELECT asset, period, ctime, open, high, low, close, buy_pct,
                    sell_pct, pressure, is_fight, crosses, hold_price,
                    hold_visits, phases, reaction, net, tick_count,
                    last_react, round_near, round_str, gap_pct, gap_type,
                    key_levels, ticks_json
             FROM candle_micro ORDER BY ctime DESC LIMIT 5000")

        # The registry is small and needs exact current state (active flag,
        # metrics/path changes), so mirror the whole table each cycle.
        counts["model_registry"] = _sync_table(
            cur, "model_registry",
            "SELECT id, name, version, scope, asset, trained_at, metrics,
                    path, active, created_at
             FROM model_registry ORDER BY id")

    return counts


def _loop() -> None:
    print(f"[supabase-sync] enabled → {_URL} every {_INTERVAL}s")
    # Let normal SQLite startup/migrations finish first.
    time.sleep(20)
    while not _STOP.wait(_INTERVAL):
        try:
            counts = sync_once()
            print(f"[supabase-sync] synced {json.dumps(counts, separators=(',', ':'))}")
        except Exception as exc:
            # Never take down the trading/feed process because Supabase is
            # unavailable or a schema/API policy needs attention.
            print(f"[supabase-sync] non-fatal sync error: {type(exc).__name__}: {exc}")


def start() -> bool:
    """Start the idempotent background sync thread."""
    global _STARTED
    if not _ENABLED:
        print("[supabase-sync] disabled: SUPABASE_URL or API key missing")
        return False
    with _LOCK:
        if _STARTED:
            return True
        t = threading.Thread(target=_loop, name="supabase-sync", daemon=True)
        t.start()
        _STARTED = True
    return True


def stop() -> None:
    _STOP.set()
