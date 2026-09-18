"""Supabase persistence bridge.

The application currently uses SQLite throughout the signal engine. This module
keeps that runtime intact while mirroring durable application tables to
Supabase/Postgres. A Supabase outage must not stop the live signal/feed engine.

Required Railway env vars:
  SUPABASE_URL
  SUPABASE_ANON_KEY (or SUPABASE_PUBLISHABLE_KEY)

SQLite remains the runtime source of truth until the full DB layer is migrated
to PostgreSQL. This bridge provides a durable Supabase mirror.
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
    url = f"{_URL}/rest/v1/{table}"
    with httpx.Client(timeout=25.0) as client:
        response = client.post(url, headers=_headers(), json=rows)
        response.raise_for_status()


def _sqlite_rows(cur, sql: str, params=()) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.execute(sql, params).fetchall()]


def _sync_table(cur, table: str, sql: str, params=()) -> int:
    rows = _sqlite_rows(cur, sql, params)
    total = 0
    for i in range(0, len(rows), _BATCH):
        batch = rows[i:i + _BATCH]
        _postgrest(table, batch)
        total += len(batch)
    return total


def sync_once() -> dict[str, int]:
    """Mirror durable SQLite tables into Supabase in bounded batches."""
    if not _ENABLED:
        return {}

    import db as _db

    counts = {
        "signal_log": 0,
        "otc_predictions": 0,
        "candle_micro": 0,
        "model_registry": 0,
    }
    with _db._read_cursor() as cur:
        counts["signal_log"] = _sync_table(
            cur,
            "signal_log",
            """SELECT id, asset, period, ctime, signal, score, confidence,
                       theories, actual, accuracy, strength, agree, right_codes,
                       wrong_codes, reasons, a_open, a_close, regime, zone, tags,
                       postmortem, category, ts, total, signal_quality, strategy
                  FROM signal_log ORDER BY id DESC LIMIT 5000""",
        )

        counts["otc_predictions"] = _sync_table(
            cur,
            "otc_predictions",
            """SELECT id, asset, period, signal_time, target_time, horizon,
                       prediction, probability, tier, score, emit, components,
                       regime, pa_agreed, quality, reason, model_version,
                       feature_json, close_i, created_at, actual_open,
                       actual_close, actual_result, win_loss, settled_at
                  FROM otc_predictions ORDER BY id DESC LIMIT 10000""",
        )

        counts["candle_micro"] = _sync_table(
            cur,
            "candle_micro",
            """SELECT asset, period, ctime, open, high, low, close, buy_pct,
                       sell_pct, pressure, is_fight, crosses, hold_price,
                       hold_visits, phases, reaction, net, tick_count,
                       last_react, round_near, round_str, gap_pct, gap_type,
                       key_levels, ticks_json
                  FROM candle_micro ORDER BY ctime DESC LIMIT 5000""",
        )

        counts["model_registry"] = _sync_table(
            cur,
            "model_registry",
            """SELECT id, name, version, scope, asset, trained_at, metrics,
                       path, active, created_at
                  FROM model_registry ORDER BY id""",
        )

    return counts


# ───────────────────── read-back (2026-09-18 backfill) ────────────────────
# core/retention.py prunes local SQLite (candle_micro > QX_RETENTION_OHLC_SECS,
# default 4h; everything else > QX_RETENTION_DATA_SECS, default 30min) to stop
# the disk-full crashes fixed in RAILWAY-500MB-FIX. That made the Supabase
# mirror above the only place older rows survive — but nothing ever read them
# back, so training/backtests silently saw only the last few hours even when
# they asked for a `days=` window. fetch_candles() closes that gap: it is the
# read counterpart to the write path above, used by
# core.otc_dataset.load_candles_from_db to backfill whatever local retention
# has already pruned. Best-effort like the sync above — never raises; callers
# must treat [] as "no Supabase data available" and keep going.
def _read_headers() -> dict[str, str]:
    return {"apikey": _KEY, "Authorization": f"Bearer {_KEY}"}


def fetch_candles(period: int = 60, since_ctime: int = 0,
                   until_ctime: int | None = None,
                   page_size: int = 1000) -> list[dict[str, Any]]:
    """Read candle_micro rows back from the Supabase mirror.

    Returns rows shaped like the local SQLite query in
    core.otc_dataset.load_candles_from_db (asset, ctime, open, high, low,
    close, buy_pct, sell_pct, tick_count, is_fight). Returns [] if the
    bridge is disabled or on any request failure — never raises.
    """
    if not _ENABLED:
        return []
    cols = "asset,ctime,open,high,low,close,buy_pct,sell_pct,tick_count,is_fight"
    out: list[dict[str, Any]] = []
    offset = 0
    try:
        with httpx.Client(timeout=25.0) as client:
            while True:
                params = [
                    ("select", cols),
                    ("period", f"eq.{period}"),
                    ("ctime", f"gt.{since_ctime}"),
                    ("order", "ctime.asc"),
                    ("limit", str(page_size)),
                    ("offset", str(offset)),
                ]
                if until_ctime is not None:
                    params.append(("ctime", f"lt.{until_ctime}"))
                resp = client.get(f"{_URL}/rest/v1/candle_micro",
                                   headers=_read_headers(), params=params)
                resp.raise_for_status()
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                out.extend(batch)
                if len(batch) < page_size:
                    break
                offset += page_size
    except Exception as exc:
        print(f"[supabase-sync] fetch_candles failed (non-fatal): "
              f"{type(exc).__name__}: {exc}")
        return []
    return out


def _loop() -> None:
    print(f"[supabase-sync] enabled → {_URL} every {_INTERVAL}s")
    time.sleep(20)
    while not _STOP.wait(_INTERVAL):
        try:
            counts = sync_once()
            print(f"[supabase-sync] synced {json.dumps(counts, separators=(',', ':'))}")
        except Exception as exc:
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
        thread = threading.Thread(target=_loop, name="supabase-sync", daemon=True)
        thread.start()
        _STARTED = True
    return True


def stop() -> None:
    _STOP.set()
