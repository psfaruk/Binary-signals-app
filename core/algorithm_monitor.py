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
# FIX (DEEP-AUDIT-2026-07-26 / F-07-25): removed dead `from contextlib import contextmanager`.
# FIX (DEEP-AUDIT-2026-07-26 / F-07-26): removed dead `from datetime import datetime, timezone`.

DB_PATH = os.environ.get("DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "signals.db"))

_lock = threading.Lock()
# FIX (DEEP-AUDIT-2026-07-26 / F-07-27): `record_candle` mutates `_WINDOWS[asset]`,
# `_LAST_PAYOUT[asset]`, `_LAST_ALGO_GUESS[asset]`, `_LAST_TICK_DENSITY[asset]`,
# and `_ALGO_GUESS_STREAK[asset]` without holding `_lock`. Concurrent calls
# from a watchdog restart or multi-threaded feed corrupt these structures.
# We use a separate RLock for state mutations so `record_candle` can call
# `_log_change` (which acquires `_lock` for DB-write serialization) without
# deadlocking — RLock is reentrant.
_state_lock = threading.RLock()


def _conn():
    # FIX (F-07-46): initialize `conn = None` before try so the `finally: conn.close()`
    # doesn't raise UnboundLocalError if `sqlite3.connect` itself fails.
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception as e:
        # FIX (F-07-28): log PRAGMA failures so operators can diagnose slow DB
        # performance (was silent `except Exception: pass` — masked WAL failures
        # on read-only filesystems or permission issues).
        print(f"[algo_monitor] WARN: PRAGMA setup failed: {e!r}")
        if conn is not None:
            # PRAGMA failed mid-setup; the connection is still usable with the
            # default journal mode.
            pass
    if conn is not None:
        conn.row_factory = sqlite3.Row
    return conn


def init_algorithm_monitor():
    """Create the algorithm_changes table if it doesn't exist."""
    conn = _conn()
    if conn is None:
        # FIX (F-07-46): _conn() can now return None if sqlite3.connect fails.
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
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-20): add ctime column so we
            # can dedup by (asset, ctime, change_type). The old UNIQUE(asset, ts,
            # change_type) used `ts = time.time()` (microsecond precision), so a
            # watchdog restart that re-processed the same candle inserted a new
            # row with a new ts — duplicate detection failed. Adding ctime
            # (candle-open timestamp, 1-minute granularity) lets the new UNIQUE
            # index catch true duplicates. We use ALTER TABLE for existing DBs.
            # FIX (F-07-29): catch `sqlite3.OperationalError` specifically (was
            # broad `except Exception` which swallowed DB-locked errors).
            try:
                cur.execute("ALTER TABLE algorithm_changes ADD COLUMN ctime INT")
            except sqlite3.OperationalError as _oe:
                # Expected: "duplicate column name: ctime" — column already exists.
                if "duplicate column" not in str(_oe).lower():
                    print(f"[algo_monitor] ALTER ctime failed: {_oe!r}")
            cur.execute("""CREATE INDEX IF NOT EXISTS ix_ac_asset_ts
                          ON algorithm_changes(asset, ts DESC)""")
            cur.execute("""CREATE INDEX IF NOT EXISTS ix_ac_ts
                          ON algorithm_changes(ts DESC)""")
            # FIX (AUDIT-DEEP #13, 2026-07-23): deduplication guard. The
            # previous schema had no UNIQUE constraint, so a watchdog restart
            # or a retry of record_candle could insert duplicate rows for
            # the same (asset, ts, change_type) tuple. Over time this inflated
            # the change count shown in /api/algorithm-changes. We use a
            # UNIQUE INDEX on (asset, ts, change_type) so duplicate inserts
            # are silently rejected (INSERT, not INSERT OR REPLACE — older
            # rows keep their original notes/confidence). First dedupe any
            # existing duplicates so the index creation succeeds.
            #
            # FIX (F-07-30): flattened the nested try/except (was 4 levels deep,
            # each swallowing with a print). Now each step is its own
            # try/except with a targeted error message.
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
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-20): second UNIQUE
            # index on (asset, ctime, change_type). The original index used
            # ts=time.time() which differs on every call — a watchdog
            # restart re-processing the same candle would get a new ts and
            # bypass the dedup. The ctime-based index catches true duplicates
            # (same asset, same candle, same change_type) regardless of
            # when the row was inserted.
            #
            # NOTE (F-07-44): the (asset, ts, change_type) UNIQUE index above
            # is effectively useless — `ts = time.time()` is unique per call,
            # so the index never catches real duplicates. Real dedup relies
            # on the ctime-based index below. Kept for schema compatibility
            # (dropping it would require a migration). TODO: drop in a future
            # schema migration.
            try:
                cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_ac_asset_ctime_type
                    ON algorithm_changes(asset, ctime, change_type)
                """)
            except sqlite3.Error as _ctime_idx_err:
                print(f"[algo_monitor] could not create ctime UNIQUE index: {_ctime_idx_err!r}")
            # ── per-pair rolling stats cache ────────────────────────────────
            # Stores the last N candles' summary stats so we can detect
            # regime shifts without re-querying signal_log every time.
            #
            # NOTE (F-07-04): this `algorithm_state` table is LEGACY / DEAD
            # SCHEMA. No code INSERTs, SELECTs, or UPDATEs it. The per-pair
            # rolling stats are kept in-memory in `_WINDOWS` / `_LAST_PAYOUT`
            # dicts instead. Kept for backward-compat (existing DBs already
            # have the table); do NOT add code that depends on it. TODO: drop
            # in a future schema migration.
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


# ── In-memory rolling window per asset ──────────────────────────────────────
# Keep the last 30 candles' summary so we can compute "before" vs "after"
# stats when a payout change is detected.
_WINDOWS: dict[str, deque] = {}
_WINDOW_SIZE = 30
_LAST_PAYOUT: dict[str, float] = {}
_LAST_ALGO_GUESS: dict[str, str] = {}
_LAST_TICK_DENSITY: dict[str, float] = {}
# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-18): hysteresis counters for
# regime-shift detection. A pair whose autocorrelation hovers near the 0.6
# trending threshold can flip-flop between "trending" and "random_walk" every
# candle, flooding algorithm_changes with regime_shift rows. We require the
# new guess to persist for _REGIME_CONFIRM_COUNT consecutive calls before
# logging a shift — this eliminates the flip-flop spam without missing real
# regime changes (which persist for many candles).
# FIX (DEEP-AUDIT-2026-07-26 / F-07-96): env-configurable confirm count
#   (was hardcoded 3).
_REGIME_CONFIRM_COUNT = int(os.environ.get("ALGO_REGIME_CONFIRM_COUNT", "3"))
_ALGO_GUESS_STREAK: dict[str, dict] = {}  # asset → {"guess": str, "count": int}

# FIX (DEEP-AUDIT-2026-07-26 / F-07-35): env-configurable payout-delta
#   threshold (was hardcoded 5.0pp).
_PAYOUT_DELTA_THRESHOLD = float(os.environ.get("ALGO_PAYOUT_DELTA_THRESHOLD", "5.0"))
# FIX (DEEP-AUDIT-2026-07-26 / F-07-36): env-configurable confidence scaling
#   — 50pp delta = 100% confidence was hardcoded.
_PAYOUT_CONFIDENCE_SCALE = float(os.environ.get("ALGO_PAYOUT_CONFIDENCE_SCALE", "50.0"))
# FIX (DEEP-AUDIT-2026-07-26 / F-07-47): env-configurable summary-row limit
#   (was hardcoded 1000).
_SUMMARY_LIMIT = int(os.environ.get("ALGO_SUMMARY_LIMIT", "1000"))

# FIX (DEEP-AUDIT-2026-07-26 / F-07-41): read env-configurable thresholds ONCE
#   at module load (was per-call inside `_guess_algorithm`, ~960 env lookups/min
#   at 240 predictions/min/asset). Trade-off: changes require a restart.
# FIX (DEEP-AUDIT-2026-07-26 / F-07-42): use inclusive `>=` / `<=` boundaries
#   so an autocorr exactly at the threshold is classified (was strict `>` / `<`,
#   which left edge values unclassified — fell through to `random_walk`).
_TREND_AUTOCORR = float(os.environ.get("ALGO_TREND_AUTOCORR", "0.6"))
_TREND_BODY = float(os.environ.get("ALGO_TREND_BODY", "45"))
_REVERSE_AUTOCORR = float(os.environ.get("ALGO_REVERSE_AUTOCORR", "0.4"))
_REVERSE_BODY = float(os.environ.get("ALGO_REVERSE_BODY", "35"))


def record_candle(asset: str, ctime: int, payout: float,
                  open_: float, high: float, low: float, close: float,
                  tick_count: int = None):
    """Called from feed.py after each candle closes.

    Maintains a rolling window of the last 30 candles' summary stats.
    Detects:
      - payout spikes/drops (immediate log)
      - regime shifts (deferred — logged when payout changes OR when
        stats drift significantly)

    FIX (DEEP-AUDIT-2026-07-26 / F-07-27): whole body wrapped in `_state_lock`
      so concurrent calls (watchdog restart, multi-threaded feed) can't corrupt
      `_WINDOWS` / `_LAST_PAYOUT` / `_LAST_ALGO_GUESS` / `_LAST_TICK_DENSITY` /
      `_ALGO_GUESS_STREAK`. Uses RLock so the nested `_log_change` call (which
      acquires `_lock` for DB-write serialization) doesn't deadlock.
    FIX (DEEP-AUDIT-2026-07-26 / F-07-31): reject None OHLC values (was only
      validating `asset` and `ctime`; None `open_/high/low/close` raised
      `TypeError` inside the body/range math).
    FIX (DEEP-AUDIT-2026-07-26 / F-07-32): reject `high < low` (data
      corruption) with a log instead of silently swapping in `1e-9`.
    FIX (DEEP-AUDIT-2026-07-26 / F-07-33): clamp `body_pct` to [0, 100]
      (was unbounded — degenerate candles with `rng=1e-9` produced 100M%
      bodies that corrupted rolling averages).
    FIX (DEEP-AUDIT-2026-07-26 / F-07-34): default `tick_count=None` so
      non-passing callers don't dilute the historical average with a 0.
      None values are filtered out in `_summarize_window`.
    FIX (DEEP-AUDIT-2026-07-26 / F-07-35): use env-configurable
      `_PAYOUT_DELTA_THRESHOLD` (was hardcoded 5.0).
    FIX (DEEP-AUDIT-2026-07-26 / F-07-36): use env-configurable
      `_PAYOUT_CONFIDENCE_SCALE` (was hardcoded 50.0).
    FIX (DEEP-AUDIT-2026-07-26 / F-07-37): use `_REGIME_CONFIRM_COUNT` for the
      `exclude_last` window in regime_shift detection (was hardcoded 5).
    FIX (DEEP-AUDIT-2026-07-26 / F-07-38): pass `last_payout` through unchanged
      (was `last_payout or 0` which collapsed None to 0, masking "no prior
      payout" vs "0% prior payout").
    FIX (DEEP-AUDIT-2026-07-26 / F-07-39): when `len(window) < 2`, return early
      from the tick-density check (was including the only candle in the
      "historical" average, making `tick_delta_pct == 0` always).
    FIX (DEEP-AUDIT-2026-07-26 / F-07-40): removed dead `if window else 0`
      ternary (`window` is guaranteed non-empty after the append above).
    FIX (DEEP-AUDIT-2026-07-26 / F-07-07): store `current_candle_ticks` in
      `_LAST_TICK_DENSITY` instead of `current_ticks` (rolling average) so
      the fallback path can detect single-candle shifts.
    FIX (DEEP-AUDIT-2026-07-26 / F-07-100): use `:.0f` format for both
      `hist_avg` and `current` in the tick_density notes (was `:.0f` for
      `hist_avg` but bare int for `current`).
    """
    if not asset or ctime is None:
        return
    # FIX (F-07-31): reject None OHLC up-front — previously the body/range
    # math raised `TypeError: unsupported operand type(s) for -: 'NoneType'
    # and 'NoneType'` deep inside the function.
    if None in (open_, high, low, close):
        print(f"[algo_monitor] record_candle: None OHLC for {asset!r}; skipping")
        return
    # FIX (F-07-32): reject inverted H/L (data corruption). Previously
    # `max(1e-9, high - low)` silently swapped in 1e-9 when `high < low`,
    # producing a candle with a fake range and no error.
    if high < low:
        print(f"[algo_monitor] record_candle: high<low for {asset!r} "
              f"(high={high}, low={low}); skipping")
        return

    with _state_lock:
        # Compute this candle's summary
        rng = high - low  # FIX (F-07-32): no longer need `max(1e-9, …)` — high<low rejected above.
        body = abs(close - open_)
        # FIX (F-07-33): clamp body_pct to [0, 100]. For degenerate candles
        # (rng≈0, body>0) the formula previously produced body_pct in the
        # millions, corrupting rolling averages.
        body_pct = min(100.0, max(0.0, (body / rng) * 100.0)) if rng > 0 else 0.0
        upper_wick = high - max(open_, close)
        lower_wick = min(open_, close) - low
        uw_pct = (upper_wick / rng) * 100.0 if rng > 0 else 0.0
        lw_pct = (lower_wick / rng) * 100.0 if rng > 0 else 0.0
        direction = "UP" if close > open_ else "DOWN" if close < open_ else "FLAT"

        # FIX (F-07-98): payout convention is percentage (0-100), as
        # evidenced by `:.0f` formatting in notes and the 50pp=100% confidence
        # scaling. Documented here to prevent future confusion.
        summary = {
            "ctime": ctime,
            "body_pct": round(body_pct, 1),
            "uw_pct": round(uw_pct, 1),
            "lw_pct": round(lw_pct, 1),
            "range": round(high - low, 6),
            "direction": direction,
            # FIX (F-07-34): store None if caller didn't pass tick_count
            #   (was 0, which diluted the historical average).
            "tick_count": tick_count,
        }

        # Maintain the rolling window
        if asset not in _WINDOWS:
            _WINDOWS[asset] = deque(maxlen=_WINDOW_SIZE)
        _WINDOWS[asset].append(summary)

        # ── Payout-change detection (immediate) ─────────────────────────────
        # The user's insight: when payout jumps from ~30% to ~85%+, the broker
        # has switched the candle-generation algorithm. Log this event.
        # FIX (ALGO-FIX-2026-07-22): lowered threshold from 10pp to 5pp so
        # smaller payout shifts are also caught.
        last_payout = _LAST_PAYOUT.get(asset)
        if last_payout is not None and payout is not None and last_payout != payout:
            delta = payout - last_payout
            if abs(delta) >= _PAYOUT_DELTA_THRESHOLD:
                change_type = "payout_spike" if delta > 0 else "payout_drop"
                confidence = min(1.0, abs(delta) / _PAYOUT_CONFIDENCE_SCALE)
                old_summary = _summarize_window(_WINDOWS[asset], exclude_last=1)
                # FIX (F-07-05 + F-07-06): use `_summarize_window` for new side too
                #   — previously `new_summary = summary` (the per-candle dict)
                #   had different keys (ctime/body_pct/uw_pct/...) than the
                #   `_summarize_window` schema (n/avg_body_pct/...). The
                #   `regime_shift` notes string then always printed `?` for the
                #   new-side `direction_autocorr` and `avg_body_pct` because
                #   the per-candle dict lacks those keys.
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
            # Track streak for hysteresis
            streak = _ALGO_GUESS_STREAK.get(asset, {"guess": current_guess, "count": 0})
            if current_guess == streak.get("guess"):
                streak["count"] = streak.get("count", 0) + 1
            else:
                streak = {"guess": current_guess, "count": 1}
            _ALGO_GUESS_STREAK[asset] = streak

            if (prev_guess is not None and prev_guess != current_guess
                    and prev_guess != "unknown" and current_guess != "unknown"
                    and streak["count"] >= _REGIME_CONFIRM_COUNT):
                # Significant transition PERSISTED for N candles.
                # FIX (F-07-37): use _REGIME_CONFIRM_COUNT (3) for exclude_last
                #   so the "before" window is the same size as the hysteresis
                #   window (was hardcoded 5 — unrelated to the 3-candle confirm).
                old_summary = _summarize_window(window, exclude_last=_REGIME_CONFIRM_COUNT)
                new_summary = current_summary
                confidence = 0.6
                notes = (f"algorithm {prev_guess}→{current_guess} "
                         f"(autocorr {old_summary.get('direction_autocorr','?')}→"
                         f"{new_summary.get('direction_autocorr','?')}, "
                         f"body {old_summary.get('avg_body_pct','?')}→"
                         f"{new_summary.get('avg_body_pct','?')}%, "
                         f"confirmed after {streak['count']} candles)")
                # FIX (F-07-38): pass `last_payout` / `payout` unchanged so the
                #   DB stores NULL for "no prior payout" (was `last_payout or 0`
                #   which collapsed None to 0, masking the distinction).
                _log_change(asset, "regime_shift",
                            last_payout, payout,
                            old_summary, new_summary, confidence, notes, ctime)
                _LAST_ALGO_GUESS[asset] = current_guess
            elif prev_guess is None:
                _LAST_ALGO_GUESS[asset] = current_guess

        if len(window) >= 15:
            current_summary = _summarize_window(window)
            current_ticks = current_summary.get("avg_tick_count", 0)
            # FIX (F-07-39): when len(window) < 2, return [] for hist_window so
            #   historical_avg=0 and the primary check is skipped (was including
            #   the only candle in the "historical" average, making the primary
            #   check always return tick_delta_pct==0).
            # FIX (F-07-40): `window` is guaranteed non-empty after the append
            #   above, so the `if window else 0` ternary was dead — removed.
            hist_window = list(window)[:-1] if len(window) > 1 else []
            hist_ticks_list = [x.get("tick_count") for x in hist_window
                              if x.get("tick_count") is not None]
            historical_avg = (sum(hist_ticks_list) / len(hist_ticks_list)
                              if hist_ticks_list else 0)
            current_candle_ticks = window[-1].get("tick_count")
            prev_ticks = _LAST_TICK_DENSITY.get(asset)
            # Primary check: single-candle spike vs historical average.
            if (current_candle_ticks is not None and historical_avg > 0):
                tick_delta_pct = abs(current_candle_ticks - historical_avg) / historical_avg * 100
                if tick_delta_pct >= 50:
                    old_summary = _summarize_window(window, exclude_last=_REGIME_CONFIRM_COUNT)
                    confidence = min(0.8, tick_delta_pct / 100)
                    # FIX (F-07-100): use `:.0f` format for both values.
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
            # FIX (F-07-07): store `current_candle_ticks` (single-candle value)
            #   instead of `current_ticks` (rolling average) so the fallback
            #   path can detect single-candle shifts.
            if current_candle_ticks is not None:
                _LAST_TICK_DENSITY[asset] = current_candle_ticks


def _summarize_window(window: deque, exclude_last: int = 0) -> dict:
    """Compute aggregate stats over the rolling window.

    FIX (DEEP-AUDIT-2026-07-26 / F-07-34): skip None tick_count values when
      computing avg_tick_count (was treating None as 0, diluting the average
      when callers don't pass tick_count).
    """
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
    # FIX (F-07-34): filter None tick_count values out of the average.
    ticks = [x.get("tick_count") for x in items if x.get("tick_count") is not None]
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

    FIX (AUDIT-DEEP-A8, 2026-07-23): thresholds were hardcoded. Now
    environment-configurable so operators can tune for different pairs
    or market regimes without code changes. Defaults are unchanged.

    FIX (DEEP-AUDIT-2026-07-26 / F-07-41): env vars are now read ONCE at module
      load (was per-call, ~960 env lookups/min at 240 predictions/min/asset).
      Trade-off: changes require a restart.
    FIX (DEEP-AUDIT-2026-07-26 / F-07-42): use inclusive `>=` / `<=` boundaries
      so an autocorr exactly at the threshold is classified (was strict `>` /
      `<`, leaving edge values unclassified — they fell through to random_walk).
    """
    if autocorr >= _TREND_AUTOCORR and avg_body >= _TREND_BODY:
        return "trending"
    if autocorr <= _REVERSE_AUTOCORR and avg_body <= _REVERSE_BODY:
        return "reversing"
    return "random_walk"


def _log_change(asset: str, change_type: str,
                old_payout: float, new_payout: float,
                old_summary: dict, new_summary: dict,
                confidence: float, notes: str, ctime: int = None):
    """Insert a row into algorithm_changes.

    FIX (AUDIT-DEEP #13, 2026-07-23): use INSERT OR IGNORE so the UNIQUE
    constraint on (asset, ts, change_type) doesn't crash the insert when a
    duplicate (e.g. from a watchdog restart) is attempted. The original
    row is preserved (its notes/confidence stay intact). Previously a
    duplicate insert would raise sqlite3.IntegrityError, which was caught
    by the broad `except Exception` below and logged — non-fatal but
    noisy, and the duplicate was silently dropped anyway. Now it's silent
    by design.

    FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-20): pass `ctime` (candle open
    time) so the new UNIQUE(asset, ctime, change_type) index catches true
    duplicates — same asset, same candle, same change_type — regardless of
    when the row is inserted (handles watchdog re-processing).

    FIX (DEEP-AUDIT-2026-07-26 / F-07-03): coalesce `ctime` to 0 when None.
      SQLite treats NULL as distinct in a UNIQUE index, so multiple rows with
      `ctime=NULL` for the same (asset, change_type) could coexist — defeating
      the dedup. Coalescing to 0 ensures NULL-ctime rows are dedup'd together
      at the (asset, 0, change_type) bucket. Callers passing real ctimes
      (always the case in `record_candle`) are unaffected.
    FIX (DEEP-AUDIT-2026-07-26 / F-07-43): documented INSERT OR IGNORE
      behavior — silently drops duplicates (e.g., from watchdog restart
      re-processing the same candle). The original row's notes/confidence
      are preserved. Operators should monitor `[algo_monitor] dedup_ignored`
      if duplicate detection becomes a concern.
    FIX (DEEP-AUDIT-2026-07-26 / F-07-45): catch `sqlite3.Error` specifically
      (was broad `except Exception`, which caught `MemoryError` / `SystemExit`).
    FIX (DEEP-AUDIT-2026-07-26 / F-07-46): `conn = None` before try so the
      `finally: conn.close()` doesn't raise UnboundLocalError if `_conn()`
      fails.
    """
    # FIX (F-07-03): NULL defeats UNIQUE(asset, ctime, change_type). Coalesce
    # to 0 so dedup works (callers without a candle timestamp get dedup'd
    # together at the (asset, 0, change_type) bucket).
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
        # FIX (F-07-45): catch sqlite3.Error specifically (was broad Exception
        #   which also caught MemoryError / SystemExit).
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
    """Aggregate: how many changes per asset, what types, etc.

    FIX (DEEP-AUDIT-2026-07-26 / F-07-47): use env-configurable `_SUMMARY_LIMIT`
      (was hardcoded 1000). Active pairs with frequent tick_density_shifts
      could exceed 1000 changes in 24h, undercounting the summary.
    """
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
    # FIX (REGIME-SHIFT-2026-07-22): include the full summary + human-readable
    # algorithm description so the frontend can show "Currently: trending
    # algorithm (high autocorrelation, large bodies)".
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
        # FIX (F-07-101): default to 0 instead of None so the frontend doesn't
        #   render "None%" for new pairs (was `_LAST_PAYOUT.get(asset)` which
        #   returns None when no payout has been recorded yet).
        "current_payout": _LAST_PAYOUT.get(asset, 0),
        "summary": summary,
        "algorithm_guess": algo,
        "algorithm_description": algo_desc,
    }
