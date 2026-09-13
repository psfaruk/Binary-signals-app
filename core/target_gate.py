"""core/target_gate.py — per-pair, per-direction adaptive TARGET-75 gate.

USER DIRECTIVE (2026-09-09)
===========================
"আরও deeply Backtest করেন, যেনো প্রত্যেক পেয়ার এর উইন রেট call put
signals 75 এর উপরে থাকে। সব গুলো রেকর্ড backtest করেন। তার পর লাইন বাই
লাইন ফিক্স করেন।"

→ Every pair's CALL/PUT win rate must stay ABOVE 75%.

THEORY OF CHANGE (why a gate is the only honest path)
=====================================================
The every-candle mode forces a tradeable CALL/PUT on every candle. An
emission set that ignores conviction is mathematically pinned near the
coin-flip band (breakeven at 85% payout = 54.05%): on a near-random walk
no direction logic can lift an ALL-candles stream to 75%. The ONLY honest
mechanism that reaches the target is SELECTIVITY:

    emit CALL/PUT  only when engine conviction for THAT pair+direction
                   clears a bar that is actively tuned to hold the rolling
                   win rate at/above the target;
    show WAIT      otherwise (NEUTRAL is never graded by feed._accuracy,
                   so WAIT candles can NEVER pollute the win rate).

This module is the feedback controller for that bar:

    rolling WR (last N graded, per asset+direction, from signal_log — the
    SAME rows the Result tab shows) drives the gate:

        WR < target            → gate += max(1, round((target-WR) * KP))
                                 (harder to emit — cut the losses)
        WR ≥ target + margin   → gate -= STEP_DOWN (ease up — keep volume)
        starved ≥ S candles    → gate -= 1 for BOTH directions of the pair
                                 (anti-silence relief; floor-capped)

    gate is clamped to [GATE_FLOOR, GATE_CAP] and persisted per
    (asset, direction) in the pair_gate_state table (survives restarts,
    included in DB backups).

GRADING-SAFETY (critical invariant)
===================================
feed._grade_and_log "recovers" a NEUTRAL back into CALL/PUT when its
reason text contains "opposed original CALL|PUT" (the WEAK→NEUTRAL marker).
A target-gate WAIT is a GENUINE no-trade and must never be recovered, so
the reason string below deliberately avoids that phrase. It also avoids
"_RECOVERED_CONFIDENCE" (the confidence-recovery marker).

FAIL-OPEN
=========
Any DB/logic error → the gate allows the signal unchanged. The gate may
never crash or silently block the live pipeline; a broken gate degrades
to the pre-gate behaviour (every-candle), never to a dead app.

Env switches:
    QX_TARGET_GATE           "0" (default since FREQ-FIRST-FIX 2026-09-09 —
                             the user re-affirmed every-candle emission:
                             "প্রত্যেক ক্যান্ডেল এ সিগন্যাল লাগবে। যে কোনো
                             একটি স্ট্রাটেজি একমত হলেই সিগন্যাল আসবে।"),
                             "1" = opt-in selective mode
    QX_TARGET_WR             75   target win rate % per pair+direction
    QX_TARGET_GATE_INIT      68   initial confidence bar (confluence-pass
                                  signals start at 65; fallback-capped
                                  signals are ≤63, so INIT=68 means only
                                  real confluence trades at boot)
    QX_TARGET_GATE_FLOOR     62   never ease below this
    QX_TARGET_GATE_CAP       88   never raise above this (MAX_CONFIDENCE=92)
    QX_TARGET_GATE_ROLLING_N 30   rolling graded window per direction
    QX_TARGET_GATE_MIN_N     12   min graded samples before controller acts
    QX_TARGET_GATE_EASE_MARGIN 3  ease only when WR ≥ target + this
    QX_TARGET_GATE_KP        0.5  proportional gain for the raise step
    QX_TARGET_GATE_STARVATION 60  WAIT-candles before anti-silence relief
"""
import os
import threading
import time

TARGET_WR = float(os.environ.get("QX_TARGET_WR", "75"))
GATE_INIT = int(os.environ.get("QX_TARGET_GATE_INIT", "68"))
GATE_FLOOR = int(os.environ.get("QX_TARGET_GATE_FLOOR", "62"))
GATE_CAP = int(os.environ.get("QX_TARGET_GATE_CAP", "88"))
ROLLING_N = int(os.environ.get("QX_TARGET_GATE_ROLLING_N", "30"))
MIN_N = int(os.environ.get("QX_TARGET_GATE_MIN_N", "12"))
EASE_MARGIN = float(os.environ.get("QX_TARGET_GATE_EASE_MARGIN", "3"))
KP_UP = float(os.environ.get("QX_TARGET_GATE_KP", "0.5"))
STARVATION_CANDLES = int(os.environ.get("QX_TARGET_GATE_STARVATION", "40"))

_LOCK = threading.Lock()
_CACHE_TTL = 10.0                       # seconds
_cache = {}                             # (asset, direction) -> (ts, gate, wr, n)
# in-memory anti-silence state: asset -> {"waits": int, "last_emit": ts}
_starve = {}
_table_ready = set()                    # db pathnames already ensured

_SQL_ROLLING = (
    "SELECT accuracy FROM signal_log "
    "WHERE asset=? AND signal=? AND accuracy IN ('correct','wrong') "
    "AND period=? "
    "ORDER BY ctime DESC LIMIT ?"
)


def _ensure_table(conn):
    """CREATE TABLE IF NOT EXISTS pair_gate_state (per-connection-safe)."""
    key = getattr(conn, "id_repr", None) or "ok"
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pair_gate_state (
            asset       TEXT NOT NULL,
            direction   TEXT NOT NULL,
            gate        REAL NOT NULL,
            last_wr     REAL,
            last_n      INTEGER,
            updated_ts  REAL,
            PRIMARY KEY (asset, direction)
        )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_signal_log_dir_lookup "
        "ON signal_log(asset, signal, accuracy, ctime)")


def _db():
    """Open a short-lived connection via db._conn (fail-open helper)."""
    import db as _db_mod
    return _db_mod._conn()


def _clamp(gate: float) -> int:
    return int(max(GATE_FLOOR, min(GATE_CAP, round(gate))))


def _rolling(asset: str, direction: str, period: int = 60):
    """(win_pct, n) over the last ROLLING_N graded rows — or (None, 0).
    Period-filtered so the controller optimises the SAME numbers the
    Result tab shows (get_directional_winrate filters period too)."""
    conn = _db()
    try:
        _ensure_table(conn)
        rows = conn.execute(_SQL_ROLLING,
                            (asset, direction, period, ROLLING_N)).fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    n = len(rows or [])
    if not n:
        return None, 0
    wins = sum(1 for r in rows if r[0] == "correct")
    return 100.0 * wins / n, n


def get_gate(asset: str, direction: str, period: int = 60):
    """Return (gate_conf, rolling_wr_pct, rolling_n) for (asset, direction).

    Never raises: on any failure returns (GATE_INIT, None, 0) so the caller
    can degrade to the initial bar instead of crashing the pipeline.
    """
    key = (asset, direction)
    now = time.time()
    with _LOCK:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1], hit[2], hit[3]
    try:
        conn = _db()
        try:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT gate FROM pair_gate_state WHERE asset=? AND direction=?",
                (asset, direction)).fetchone()
        finally:
            conn.close()
        if row:
            gate = int(round(row[0]))
        else:
            # BOOT-TIME CALIBRATION (2026-09-09): no stored bar yet, but the
            # pair already has measured history in signal_log (persistence
            # fix keeps that history across deploys). Seed the bar from the
            # measured rolling WR instead of the naive INIT — a pair that
            # has been losing starts STRICT, a proven winner starts LOOSE:
            #   WR ≥ 78% → INIT-4   (proven pocket — encourage volume)
            #   70–78%   → INIT
            #   60–70%   → INIT+6
            #   <60%     → INIT+12  (chronic loser — probe rarely)
            # Without this, every redeploy would re-learn the same losses
            # from scratch at the naive INIT bar.
            try:
                wr0, n0 = _rolling(asset, direction, period)
            except Exception:
                wr0, n0 = None, 0
            if wr0 is not None and n0 >= MIN_N:
                if wr0 >= TARGET_WR + 3:
                    gate = GATE_INIT - 4
                elif wr0 >= TARGET_WR - 5:
                    gate = GATE_INIT
                elif wr0 >= 60.0:
                    gate = GATE_INIT + 6
                else:
                    gate = GATE_INIT + 12
                try:
                    _persist_gate(asset, direction, _clamp(gate), wr0, n0)
                    print(f"[target-gate] {asset} {direction} boot-calibrated "
                          f"bar={_clamp(gate)} from measured WR {wr0:.1f}% n={n0}")
                except Exception:
                    pass
            else:
                gate = GATE_INIT
        gate = _clamp(gate)
    except Exception as exc:                      # fail-open
        print(f"[target-gate] get_gate({asset},{direction}) failed: {exc}")
        gate = GATE_INIT
    try:
        wr, n = _rolling(asset, direction, period)
    except Exception:
        wr, n = None, 0
    with _LOCK:
        _cache[key] = (now, gate, wr, n)
    return gate, wr, n


def _persist_gate(asset: str, direction: str, gate: int, wr, n: int) -> None:
    conn = _db()
    try:
        _ensure_table(conn)
        conn.execute(
            """INSERT INTO pair_gate_state(asset, direction, gate, last_wr, last_n, updated_ts)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(asset, direction) DO UPDATE SET
                 gate=excluded.gate, last_wr=excluded.last_wr,
                 last_n=excluded.last_n, updated_ts=excluded.updated_ts""",
            (asset, direction, gate, wr, n, time.time()))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def note_graded(asset: str, direction: str, correct: bool, period: int = 60) -> None:
    """Controller update after a graded outcome (call from the feed grade path).

    correct=True/False → the rolling window moves → the gate may move.
    Never raises.
    """
    try:
        with _LOCK:
            _cache.pop((asset, direction), None)      # force re-read
        wr, n = _rolling(asset, direction, period)
        gate, _, _ = get_gate(asset, direction, period)
        new_gate = gate
        why = ""
        if wr is not None and n >= MIN_N:
            if wr < TARGET_WR:
                step = max(1, int(round((TARGET_WR - wr) * KP_UP)))
                new_gate = gate + step
                why = f"WR {wr:.1f}% n={n} < target {TARGET_WR:.0f}% → +{step}"
            elif wr >= TARGET_WR + EASE_MARGIN and gate > GATE_FLOOR:
                new_gate = gate - 1
                why = (f"WR {wr:.1f}% n={n} ≥ target+{EASE_MARGIN:.0f} "
                       f"→ ease −1 (keep volume)")
        new_gate = _clamp(new_gate)
        if new_gate != gate or why:
            _persist_gate(asset, direction, new_gate, wr, n)
            if new_gate != gate:
                print(f"[target-gate] {asset} {direction} gate {gate} → "
                      f"{new_gate} ({why})")
        else:
            # refresh stored diagnostics even when the bar didn't move
            _persist_gate(asset, direction, gate, wr, n)
        # CACHE BUGFIX (2026-09-09): get_gate() re-populates the 10s cache
        # with the OLD gate while this function runs (it reads before we
        # write). Invalidate AFTER persisting so the next caller sees the
        # new bar immediately — otherwise live trading runs up to 10s on a
        # stale bar and unit tests read the pre-move value.
        with _LOCK:
            _cache.pop((asset, direction), None)
    except Exception as exc:
        print(f"[target-gate] note_graded({asset},{direction}) failed: {exc}")


def _starve_note(asset: str) -> None:
    """Anti-silence relief: after STARVATION_CANDLES consecutive WAIT candles
    for a pair, ease BOTH direction gates by 1 (floor-capped) so a pair that
    drifted to permanent silence gets another chance to trade."""
    st = _starve.setdefault(asset, {"waits": 0, "last_emit": 0.0})
    st["waits"] += 1
    if st["waits"] < STARVATION_CANDLES:
        return
    st["waits"] = 0
    for direction in ("CALL", "PUT"):
        try:
            gate, wr, n = get_gate(asset, direction)
            if gate > GATE_FLOOR:
                new_gate = _clamp(gate - 1)
                _persist_gate(asset, direction, new_gate, wr, n)
                with _LOCK:
                    _cache.pop((asset, direction), None)
                print(f"[target-gate] {asset} {direction} starved ≥"
                      f"{STARVATION_CANDLES} candles → gate {gate} → {new_gate}")
        except Exception as exc:
            print(f"[target-gate] starvation relief failed {asset}: {exc}")


def apply_gate(result: dict, asset: str, period: int = 60) -> dict:
    """Convert a CALL/PUT below the pair+direction bar into an honest WAIT.

    Signals at/above the bar pass through untouched. Fail-open: any error
    returns the original result.
    """
    try:
        sig = result.get("signal")
        if sig not in ("CALL", "PUT"):
            return result
        gate, wr, n = get_gate(asset, sig, period)
        conf = int(result.get("confidence") or 0)
        if conf >= gate:
            st = _starve.setdefault(asset, {"waits": 0, "last_emit": 0.0})
            st["waits"] = 0
            st["last_emit"] = time.time()
            result["target_gate_bar"] = gate
            return result
        # ── WAIT conversion (see module docstring: grading-safe reason) ──
        gated = dict(result)
        gated["signal"] = "NEUTRAL"
        gated["strength"] = "NEUTRAL"
        gated["confidence"] = 0
        gated["score"] = 0.0
        gated["target_gate"] = True
        gated["gated_from"] = sig
        gated["gated_conf"] = conf
        gated["gated_bar"] = gate
        wr_txt = f"{wr:.1f}%" if wr is not None else "n/a"
        gated["reasons"] = list(result.get("reasons") or []) + [
            f"[TARGET-75] {sig} conf {conf} below bar {gate} → WAIT "
            f"(pair {sig} rolling WR {wr_txt} n={n}, target "
            f"{TARGET_WR:.0f}%; WAIT is never graded)"]
        print(f"[target-gate] {asset} {sig} conf {conf} < bar {gate} → WAIT")
        _starve_note(asset)
        return gated
    except Exception as exc:
        print(f"[target-gate] apply_gate({asset}) failed (fail-open): {exc}")
        return result


def gate_report() -> list:
    """Rows for /api/target-gate — full transparency of the controller."""
    out = []
    try:
        conn = _db()
        try:
            _ensure_table(conn)
            rows = conn.execute(
                "SELECT asset, direction, gate, last_wr, last_n, updated_ts "
                "FROM pair_gate_state ORDER BY asset, direction").fetchall()
        finally:
            conn.close()
        for r in rows or []:
            out.append({"asset": r[0], "direction": r[1], "gate": r[2],
                        "rolling_wr": r[3], "rolling_n": r[4],
                        "updated_ts": r[5]})
    except Exception as exc:
        print(f"[target-gate] gate_report failed: {exc}")
    return out
