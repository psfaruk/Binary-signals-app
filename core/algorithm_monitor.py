"""core/algorithm_monitor.py — Detects Quotex OTC algorithm changes."""
import json
import os
import sqlite3
import threading
import time
from collections import deque

DB_PATH = os.environ.get("DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "signals.db"))

_lock = threading.Lock()
_state_lock = threading.RLock()

def _conn():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception as e:
        print(f"[algo_monitor] WARN: PRAGMA setup failed: {e!r}")
        if conn is not None:
            pass
    if conn is not None:
        conn.row_factory = sqlite3.Row
    return conn
def init_algorithm_monitor():
    """Create the algorithm_changes table if it doesn't exist."""
    conn = _conn()
    if conn is None:
        print("[algo_monitor] init_algorithm_monitor: no DB connection")
        return
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
                notes TEXT,
                ctime INT
            )""")
            try:
                cur.execute("ALTER TABLE algorithm_changes ADD COLUMN ctime INT")
            except sqlite3.OperationalError as _oe:
                if "duplicate column" not in str(_oe).lower():
                    print(f"[algo_monitor] ALTER ctime failed: {_oe!r}")
            cur.execute("""CREATE INDEX IF NOT EXISTS ix_ac_asset_ts
                          ON algorithm_changes(asset, ts DESC)""")
            cur.execute("""CREATE INDEX IF NOT EXISTS ix_ac_ts
                          ON algorithm_changes(ts DESC)""")
            try:
                cur.execute("""
                    DELETE FROM algorithm_changes WHERE id IN (
                        SELECT a1.id FROM algorithm_changes a1
                        WHERE EXISTS (
                            SELECT 1 FROM algorithm_changes a2
                            WHERE a2.asset = a1.asset
                              AND a2.ts = a1.ts
                              AND a2.change_type = a1.change_type
                              AND a2.id > a1.id
                        )
                    )
                """)
            except sqlite3.Error as _dedup_err:
                print(f"[algo_monitor] dedup DELETE failed: {_dedup_err!r}")
            try:
                cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_ac_asset_ts_type
                    ON algorithm_changes(asset, ts, change_type)
                """)
            except sqlite3.Error as _ts_idx_err:
                print(f"[algo_monitor] could not create ts UNIQUE index: {_ts_idx_err!r}")
            try:
                cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_ac_asset_ctime_type
                    ON algorithm_changes(asset, ctime, change_type)
                """)
            except sqlite3.Error as _ctime_idx_err:
                print(f"[algo_monitor] could not create ctime UNIQUE index: {_ctime_idx_err!r}")
            cur.execute("""CREATE TABLE IF NOT EXISTS algorithm_state (
                asset TEXT PRIMARY KEY,
                last_payout REAL,
                last_regime_summary TEXT,
                last_update_ts REAL,
                candle_history TEXT
            )""")
            conn.commit()
    finally:
        if conn is not None:
            conn.close()
_WINDOWS: dict[str, deque] = {}
_WINDOW_SIZE = 30
_LAST_PAYOUT: dict[str, float] = {}
_LAST_ALGO_GUESS: dict[str, str] = {}
_LAST_TICK_DENSITY: dict[str, float] = {}
_REGIME_CONFIRM_COUNT = int(os.environ.get("ALGO_REGIME_CONFIRM_COUNT", "3"))
_ALGO_GUESS_STREAK: dict[str, dict] = {}  # asset → {"guess": str, "count": int}

_PAYOUT_DELTA_THRESHOLD = float(os.environ.get("ALGO_PAYOUT_DELTA_THRESHOLD", "5.0"))
_PAYOUT_CONFIDENCE_SCALE = float(os.environ.get("ALGO_PAYOUT_CONFIDENCE_SCALE", "50.0"))
_SUMMARY_LIMIT = int(os.environ.get("ALGO_SUMMARY_LIMIT", "1000"))

_TREND_AUTOCORR = float(os.environ.get("ALGO_TREND_AUTOCORR", "0.6"))
_TREND_BODY = float(os.environ.get("ALGO_TREND_BODY", "45"))
_REVERSE_AUTOCORR = float(os.environ.get("ALGO_REVERSE_AUTOCORR", "0.4"))
_REVERSE_BODY = float(os.environ.get("ALGO_REVERSE_BODY", "35"))

def record_candle(asset: str, ctime: int, payout: float,
                  open_: float, high: float, low: float, close: float,
                  tick_count: int = None):
    """Called from feed.py after each candle closes."""
    if not asset or ctime is None:
        return
    if None in (open_, high, low, close):
        print(f"[algo_monitor] record_candle: None OHLC for {asset!r}; skipping")
        return
    if high < low:
        print(f"[algo_monitor] record_candle: high<low for {asset!r} "
              f"(high={high}, low={low}); skipping")
        return
    with _state_lock:
        rng = high - low  # FIX (F-07-32): no longer need `max(1e-9, …)` — high<low rejected above.
        body = abs(close - open_)
        body_pct = min(100.0, max(0.0, (body / rng) * 100.0)) if rng > 0 else 0.0
        upper_wick = high - max(open_, close)
        lower_wick = min(open_, close) - low
        uw_pct = (upper_wick / rng) * 100.0 if rng > 0 else 0.0
        lw_pct = (lower_wick / rng) * 100.0 if rng > 0 else 0.0
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
        if asset not in _WINDOWS:
            _WINDOWS[asset] = deque(maxlen=_WINDOW_SIZE)
        _WINDOWS[asset].append(summary)
        last_payout = _LAST_PAYOUT.get(asset)
        if last_payout is not None and payout is not None and last_payout != payout:
            delta = payout - last_payout
            if abs(delta) >= _PAYOUT_DELTA_THRESHOLD:
                change_type = "payout_spike" if delta > 0 else "payout_drop"
                confidence = min(1.0, abs(delta) / _PAYOUT_CONFIDENCE_SCALE)
                old_summary = _summarize_window(_WINDOWS[asset], exclude_last=1)
                new_summary = _summarize_window(_WINDOWS[asset])
                notes = (f"payout {last_payout:.0f}%→{payout:.0f}% "
                         f"({'algorithm switch likely' if delta > 0 else 'algorithm revert likely'})")
                _log_change(asset, change_type, last_payout, payout,
                            old_summary, new_summary, confidence, notes, ctime)
        if payout is not None:
            _LAST_PAYOUT[asset] = payout
        window = _WINDOWS[asset]
        if len(window) >= 15:
            current_summary = _summarize_window(window)
            current_guess = current_summary.get("algorithm_guess", "unknown")
            prev_guess = _LAST_ALGO_GUESS.get(asset)
            streak = _ALGO_GUESS_STREAK.get(asset, {"guess": current_guess, "count": 0})
            if current_guess == streak.get("guess"):
                streak["count"] = streak.get("count", 0) + 1
            else:
                streak = {"guess": current_guess, "count": 1}
            _ALGO_GUESS_STREAK[asset] = streak
            if (prev_guess is not None and prev_guess != current_guess
                    and prev_guess != "unknown" and current_guess != "unknown"
                    and streak["count"] >= _REGIME_CONFIRM_COUNT):
                old_summary = _summarize_window(window, exclude_last=_REGIME_CONFIRM_COUNT)
                new_summary = current_summary
                confidence = 0.6
                notes = (f"algorithm {prev_guess}→{current_guess} "
                         f"(autocorr {old_summary.get('direction_autocorr','?')}→"
                         f"{new_summary.get('direction_autocorr','?')}, "
                         f"body {old_summary.get('avg_body_pct','?')}→"
                         f"{new_summary.get('avg_body_pct','?')}%, "
                         f"confirmed after {streak['count']} candles)")
                _log_change(asset, "regime_shift",
                            last_payout, payout,
                            old_summary, new_summary, confidence, notes, ctime)
                _LAST_ALGO_GUESS[asset] = current_guess
            elif prev_guess is None:
                _LAST_ALGO_GUESS[asset] = current_guess
        if len(window) >= 15:
            current_summary = _summarize_window(window)
            current_ticks = current_summary.get("avg_tick_count", 0)
            hist_window = list(window)[:-1] if len(window) > 1 else []
            hist_ticks_list = [x.get("tick_count") for x in hist_window
                              if x.get("tick_count") is not None]
            historical_avg = (sum(hist_ticks_list) / len(hist_ticks_list)
                              if hist_ticks_list else 0)
            current_candle_ticks = window[-1].get("tick_count")
            prev_ticks = _LAST_TICK_DENSITY.get(asset)
            if (current_candle_ticks is not None and historical_avg > 0):
                tick_delta_pct = abs(current_candle_ticks - historical_avg) / historical_avg * 100
                if tick_delta_pct >= 50:
                    old_summary = _summarize_window(window, exclude_last=_REGIME_CONFIRM_COUNT)
                    confidence = min(0.8, tick_delta_pct / 100)
                    notes = (f"tick_density hist_avg={historical_avg:.0f}→"
                             f"current={current_candle_ticks:.0f} "
                             f"({tick_delta_pct:.0f}% spike — possible feed switch)")
                    _log_change(asset, "tick_density_shift",
                                last_payout, payout,
                                old_summary, current_summary, confidence, notes, ctime)
            elif prev_ticks is not None and prev_ticks > 0:
                tick_delta_pct = abs(current_ticks - prev_ticks) / prev_ticks * 100
                if tick_delta_pct >= 30:
                    old_summary = _summarize_window(window, exclude_last=_REGIME_CONFIRM_COUNT)
                    confidence = min(0.8, tick_delta_pct / 100)
                    notes = (f"tick_density {prev_ticks:.0f}→{current_ticks:.0f} "
                             f"({tick_delta_pct:.0f}% change — possible feed switch)")
                    _log_change(asset, "tick_density_shift",
                                last_payout, payout,
                                old_summary, current_summary, confidence, notes, ctime)
            if current_candle_ticks is not None:
                _LAST_TICK_DENSITY[asset] = current_candle_ticks
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
    ticks = [x.get("tick_count") for x in items if x.get("tick_count") is not None]
    dirs = [1 if x["direction"] == "UP" else -1 if x["direction"] == "DOWN" else 0
            for x in items]
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
    """Heuristic: guess which algorithm the broker is using based on stats."""
    if autocorr >= _TREND_AUTOCORR and avg_body >= _TREND_BODY:
        return "trending"
    if autocorr <= _REVERSE_AUTOCORR and avg_body <= _REVERSE_BODY:
        return "reversing"
    return "random_walk"
def _log_change(asset: str, change_type: str,
                old_payout: float, new_payout: float,
                old_summary: dict, new_summary: dict,
                confidence: float, notes: str, ctime: int = None):
    """Insert a row into algorithm_changes."""
    ctime_value = int(ctime) if ctime is not None else 0
    conn = None  # FIX (F-07-46): guard against _conn() failure in finally.
    conn = _conn()
    if conn is None:
        print(f"[algo_monitor] _log_change: no DB connection for {asset!r}/{change_type}")
        return
    try:
        with _lock:
            cur = conn.cursor()
            cur.execute("""INSERT OR IGNORE INTO algorithm_changes
                (ts, asset, change_type, old_payout, new_payout,
                 old_regime_summary, new_regime_summary, confidence, notes, ctime)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (time.time(), asset, change_type,
                 float(old_payout) if old_payout is not None else None,
                 float(new_payout) if new_payout is not None else None,
                 json.dumps(old_summary) if old_summary else None,
                 json.dumps(new_summary) if new_summary else None,
                 float(confidence), notes, ctime_value))
            conn.commit()
    except sqlite3.Error as e:
        print(f"[algo_monitor] log_change sqlite error: {e!r}")
    finally:
        if conn is not None:
            conn.close()
def get_recent_changes(asset: str = None, hours: int = 24, limit: int = 50):
    """Return recent algorithm changes, optionally filtered by asset."""
    conn = _conn()
    if conn is None:
        return []
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
    changes = get_recent_changes(asset=asset, hours=hours, limit=_SUMMARY_LIMIT)
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
    algo = summary.get("algorithm_guess", "unknown")
    algo_desc = {
        "trending":    "Trending — strong directional bias, large bodies, high autocorrelation. Best for trend-following signals.",
        "reversing":   "Reversing — choppy, small bodies, low autocorrelation. Best for mean-reversion signals.",
        "random_walk": "Random Walk — no clear directional bias. Coin-flip territory, signals less reliable.",
        "unknown":     "Insufficient data — need at least 15 candles to guess the algorithm.",
    }.get(algo, "Unknown algorithm type.")
    return {
        "asset": asset,
        "samples": len(window),
        "current_payout": _LAST_PAYOUT.get(asset, 0),
        "summary": summary,
        "algorithm_guess": algo,
        "algorithm_description": algo_desc,
    }


# ═══════════════════════════════════════════════════════════════════════════
# MONITORING PIPELINE EXTENSIONS (DEEP-FIX-2026-08-07)
# ═══════════════════════════════════════════════════════════════════════════

# Track degradation of signal quality tiers over time.
_quality_snapshot: dict = {}  # "HIGH"|"MEDIUM"|"LOW" → {"wr": float, "n": int, "ts": float}
_quality_lock = threading.Lock()


def check_signal_quality_degradation() -> dict:
    """Compare current signal quality tier win rates against last snapshot.
    Returns degradation alerts if any tier dropped significantly.
    """
    global _quality_snapshot
    try:
        import db as _db
        conn = _db._conn()
        try:
            rows = conn.execute("""
                SELECT COALESCE(signal_quality, 'UNLABELED') as tier,
                       SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) as correct,
                       SUM(CASE WHEN accuracy='wrong' THEN 1 ELSE 0 END) as wrong
                FROM signal_log
                WHERE period=60 AND accuracy IN ('correct','wrong')
                GROUP BY tier
            """).fetchall()
        finally:
            conn.close()

        current = {}
        for r in rows:
            total = (r["correct"] or 0) + (r["wrong"] or 0)
            wr = round(100.0 * r["correct"] / total, 1) if total > 0 else 0.0
            current[r["tier"]] = {"wr": wr, "n": total, "ts": time.time()}

        alerts = []
        with _quality_lock:
            # Capture the previous snapshot's age BEFORE it gets overwritten
            # below — reading it after the overwrite always yields ~0 sec
            # since it would be reading the just-computed `current` snapshot.
            prev_ts = (next(iter(_quality_snapshot.values()))["ts"]
                       if _quality_snapshot else None)
            for tier, cur in current.items():
                prev = _quality_snapshot.get(tier)
                if prev and prev["n"] >= 30 and cur["n"] >= 30:
                    delta = cur["wr"] - prev["wr"]
                    if abs(delta) >= 10:
                        direction = "⬆️ improved" if delta > 0 else "⬇️ declined"
                        alerts.append({
                            "tier": tier,
                            "old_wr": prev["wr"],
                            "new_wr": cur["wr"],
                            "delta_pp": round(delta, 1),
                            "direction": direction,
                            "samples": cur["n"],
                        })
            _quality_snapshot = current

        return {"alerts": alerts, "current": current,
                "previous_snapshot_ago_sec":
                    round(time.time() - prev_ts, 0) if prev_ts else None}
    except Exception as e:
        return {"error": str(e)}


def get_monitoring_snapshot() -> dict:
    """Generate a comprehensive monitoring snapshot for the dashboard.

    Aggregates: algorithm changes, current state per asset, quality tier
    health, and Quotex trap/boost hour summary.
    """
    import time as _time
    now = _time.time()

    # Algorithm state per asset
    assets_state = {}
    for asset in list(_WINDOWS.keys()):
        assets_state[asset] = get_current_state(asset)

    # Recent algorithm changes (last 24h)
    changes = get_recent_changes(hours=24, limit=100)

    # Quality tier degradation
    quality = check_signal_quality_degradation()

    # Active alerts summary
    active_alerts = []
    for c in changes[:10]:
        active_alerts.append({
            "asset": c["asset"],
            "type": c["change_type"],
            "confidence": c["confidence"],
            "notes": c["notes"],
            "ago_sec": round(now - c["ts"], 0),
        })

    # Quotex algo patterns summary
    try:
        from core.time_patterns import get_pattern_summary
        patterns = get_pattern_summary()
    except Exception:
        patterns = {}

    return {
        "timestamp": now,
        "assets_monitored": len(assets_state),
        "algorithm_guesses": {
            asset: s.get("algorithm_guess", "unknown")
            for asset, s in assets_state.items()
        },
        "payout_changes_24h": sum(
            1 for c in changes if c["change_type"] in ("payout_spike", "payout_drop")),
        "regime_shifts_24h": sum(
            1 for c in changes if c["change_type"] == "regime_shift"),
        "quality_alerts": quality.get("alerts", []),
        "active_alerts": active_alerts,
        "pair_patterns_count": len(patterns) if patterns else 0,
    }
