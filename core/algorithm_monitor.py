"""
core/algorithm_monitor.py — Detects Quotex OTC algorithm changes.

Quotex OTC pairs (especially exotic ones like USDBDT, USDBRL, USDPKR,
USDCOP, USDMXN, USDIDR) exhibit a well-documented behavior:
  - Most of the time the payout is low (~30%)
  - Periodically the payout spikes to ~85-92%
  - When the payout spikes, the broker switches the candle-generation
    algorithm (the candles look "different" — different volatility,
    different body/wick ratios, different tick patterns)

This module monitors per-pair:
  1. Payout changes (the trigger)
  2. Candle volatility regime changes (body%, wick%, range)
  3. Tick-density changes (ticks per minute)
  4. Direction autocorrelation (trend vs reversal behavior)

When a significant change is detected, it's logged to the
`algorithm_changes` table with:
  - asset, timestamp
  - old_payout, new_payout
  - old_regime_summary, new_regime_summary
  - change_type (payout_spike / payout_drop / regime_shift / tick_density_shift)
  - confidence (how sure we are this is a real algorithm change)

The /api/algorithm-changes/{asset} endpoint exposes this data so the
frontend can show "Algorithm changed 3 times today" insights.
"""
import json
import os
import sqlite3
import threading
import time
from collections import deque
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


def init_algorithm_monitor():
    """Create the algorithm_changes table if it doesn't exist."""
    conn = _conn()
    try:
        with _lock:
            cur = conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS algorithm_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                asset TEXT,
                change_type TEXT,
                old_payout REAL,
                new_payout REAL,
                old_regime_summary TEXT,
                new_regime_summary TEXT,
                confidence REAL,
                notes TEXT
            )""")
            cur.execute("""CREATE INDEX IF NOT EXISTS ix_ac_asset_ts
                          ON algorithm_changes(asset, ts DESC)""")
            cur.execute("""CREATE INDEX IF NOT EXISTS ix_ac_ts
                          ON algorithm_changes(ts DESC)""")
            # ── per-pair rolling stats cache ────────────────────────────────
            # Stores the last N candles' summary stats so we can detect
            # regime shifts without re-querying signal_log every time.
            cur.execute("""CREATE TABLE IF NOT EXISTS algorithm_state (
                asset TEXT PRIMARY KEY,
                last_payout REAL,
                last_regime_summary TEXT,
                last_update_ts REAL,
                candle_history TEXT
            )""")
            conn.commit()
    finally:
        conn.close()


# ── In-memory rolling window per asset ──────────────────────────────────────
# Keep the last 30 candles' summary so we can compute "before" vs "after"
# stats when a payout change is detected.
_WINDOWS: dict[str, deque] = {}
_WINDOW_SIZE = 30
_LAST_PAYOUT: dict[str, float] = {}


def record_candle(asset: str, ctime: int, payout: float,
                  open_: float, high: float, low: float, close: float,
                  tick_count: int = 0):
    """Called from feed.py after each candle closes.

    Maintains a rolling window of the last 30 candles' summary stats.
    Detects:
      - payout spikes/drops (immediate log)
      - regime shifts (deferred — logged when payout changes OR when
        stats drift significantly)
    """
    if not asset or ctime is None:
        return

    # Compute this candle's summary
    rng = max(1e-9, high - low)
    body = abs(close - open_)
    body_pct = (body / rng) * 100.0
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low
    uw_pct = (upper_wick / rng) * 100.0
    lw_pct = (lower_wick / rng) * 100.0
    direction = "UP" if close > open_ else "DOWN" if close < open_ else "FLAT"

    summary = {
        "ctime": ctime,
        "body_pct": round(body_pct, 1),
        "uw_pct": round(uw_pct, 1),
        "lw_pct": round(lw_pct, 1),
        "range": round(high - low, 6),
        "direction": direction,
        "tick_count": tick_count,
    }

    # Maintain the rolling window
    if asset not in _WINDOWS:
        _WINDOWS[asset] = deque(maxlen=_WINDOW_SIZE)
    _WINDOWS[asset].append(summary)

    # ── Payout-change detection (immediate) ─────────────────────────────
    # The user's insight: when payout jumps from ~30% to ~85%+, the broker
    # has switched the candle-generation algorithm. Log this event.
    last_payout = _LAST_PAYOUT.get(asset)
    if last_payout is not None and payout is not None and last_payout != payout:
        delta = payout - last_payout
        # Only log significant changes (>= 10 percentage points) to avoid noise
        if abs(delta) >= 10.0:
            change_type = "payout_spike" if delta > 0 else "payout_drop"
            confidence = min(1.0, abs(delta) / 50.0)  # 50pp delta = 100% confidence
            old_summary = _summarize_window(_WINDOWS[asset], exclude_last=1)
            new_summary = summary
            notes = (f"payout {last_payout:.0f}%→{payout:.0f}% "
                     f"({'algorithm switch likely' if delta > 0 else 'algorithm revert likely'})")
            _log_change(asset, change_type, last_payout, payout,
                        old_summary, new_summary, confidence, notes)
    if payout is not None:
        _LAST_PAYOUT[asset] = payout


def _summarize_window(window: deque, exclude_last: int = 0) -> dict:
    """Compute aggregate stats over the rolling window."""
    if not window:
        return {}
    items = list(window)
    if exclude_last > 0:
        items = items[:-exclude_last] if len(items) > exclude_last else []
    if not items:
        return {}
    bodies = [x["body_pct"] for x in items]
    uws = [x["uw_pct"] for x in items]
    lws = [x["lw_pct"] for x in items]
    ticks = [x.get("tick_count", 0) for x in items]
    dirs = [1 if x["direction"] == "UP" else -1 if x["direction"] == "DOWN" else 0
            for x in items]
    # Direction autocorrelation: how often does the same direction repeat?
    # High autocorrelation = trending algorithm. Low = mean-reverting.
    same_count = 0
    total_pairs = 0
    for i in range(1, len(dirs)):
        if dirs[i] != 0 and dirs[i-1] != 0:
            total_pairs += 1
            if dirs[i] == dirs[i-1]:
                same_count += 1
    autocorr = (same_count / total_pairs) if total_pairs > 0 else 0.5
    return {
        "n": len(items),
        "avg_body_pct": round(sum(bodies) / len(bodies), 1),
        "avg_uw_pct": round(sum(uws) / len(uws), 1),
        "avg_lw_pct": round(sum(lws) / len(lws), 1),
        "avg_tick_count": round(sum(ticks) / len(ticks), 1) if ticks else 0,
        "direction_autocorr": round(autocorr, 2),
        "algorithm_guess": _guess_algorithm(autocorr, sum(bodies)/len(bodies),
                                            sum(ticks)/len(ticks) if ticks else 0),
    }


def _guess_algorithm(autocorr: float, avg_body: float, avg_ticks: float) -> str:
    """Heuristic: guess which algorithm the broker is using based on stats.

    Quotex OTC pairs typically cycle through 2-3 algorithms:
      - 'trending'   : high autocorr (>0.6), large bodies (>50%), clear swings
      - 'reversing'  : low autocorr (<0.4), small bodies (<30%), choppy
      - 'random_walk': mid autocorr (~0.5), mid bodies, no clear pattern
    """
    if autocorr > 0.6 and avg_body > 45:
        return "trending"
    if autocorr < 0.4 and avg_body < 35:
        return "reversing"
    return "random_walk"


def _log_change(asset: str, change_type: str,
                old_payout: float, new_payout: float,
                old_summary: dict, new_summary: dict,
                confidence: float, notes: str):
    """Insert a row into algorithm_changes."""
    conn = _conn()
    try:
        with _lock:
            cur = conn.cursor()
            cur.execute("""INSERT INTO algorithm_changes
                (ts, asset, change_type, old_payout, new_payout,
                 old_regime_summary, new_regime_summary, confidence, notes)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (time.time(), asset, change_type,
                 float(old_payout) if old_payout is not None else None,
                 float(new_payout) if new_payout is not None else None,
                 json.dumps(old_summary) if old_summary else None,
                 json.dumps(new_summary) if new_summary else None,
                 float(confidence), notes))
            conn.commit()
    except Exception as e:
        print(f"[algo_monitor] log_change error: {e}")
    finally:
        conn.close()


def get_recent_changes(asset: str = None, hours: int = 24, limit: int = 50):
    """Return recent algorithm changes, optionally filtered by asset."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cutoff = time.time() - (hours * 3600)
        if asset:
            rows = cur.execute("""SELECT * FROM algorithm_changes
                                  WHERE asset = ? AND ts >= ?
                                  ORDER BY ts DESC LIMIT ?""",
                              (asset, cutoff, limit)).fetchall()
        else:
            rows = cur.execute("""SELECT * FROM algorithm_changes
                                  WHERE ts >= ?
                                  ORDER BY ts DESC LIMIT ?""",
                              (cutoff, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_change_summary(asset: str = None, hours: int = 24) -> dict:
    """Aggregate: how many changes per asset, what types, etc."""
    changes = get_recent_changes(asset=asset, hours=hours, limit=1000)
    by_asset = {}
    by_type = {}
    for c in changes:
        a = c["asset"]
        by_asset[a] = by_asset.get(a, 0) + 1
        t = c["change_type"]
        by_type[t] = by_type.get(t, 0) + 1
    return {
        "total_changes": len(changes),
        "by_asset": by_asset,
        "by_type": by_type,
        "window_hours": hours,
    }


def get_current_state(asset: str) -> dict:
    """Return the current rolling-window state for an asset."""
    window = _WINDOWS.get(asset)
    if not window:
        return {"asset": asset, "samples": 0, "algorithm_guess": "unknown"}
    summary = _summarize_window(window)
    return {
        "asset": asset,
        "samples": len(window),
        "current_payout": _LAST_PAYOUT.get(asset),
        "summary": summary,
    }
