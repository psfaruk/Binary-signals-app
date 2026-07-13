"""
Lightweight SQLite persistence layer.
Tables: candle_micro, signal_log, theory_votes
"""
import json
import sqlite3
import os
import time
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "signals.db"))
_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # WAL mode: allows concurrent reads during writes — critical for
    # avoiding lock contention when 38 streams close simultaneously.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


@contextmanager
def _cursor():
    conn = _conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    finally:
        conn.close()


def init():
    with _cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS candle_micro (
            asset TEXT, period INT, ctime INT,
            open REAL, high REAL, low REAL, close REAL,
            buy_pct REAL, sell_pct REAL, pressure TEXT,
            is_fight INT, crosses INT, hold_price REAL, hold_visits INT,
            phases TEXT, reaction TEXT, net REAL, tick_count INT,
            last_react TEXT,
            round_near REAL, round_str TEXT,
            gap_pct REAL, gap_type TEXT, key_levels TEXT,
            ticks_json TEXT,
            PRIMARY KEY (asset, period, ctime))""")
        c.execute("""CREATE TABLE IF NOT EXISTS signal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT, period INT, ctime INT,
            signal TEXT, score INT, confidence REAL,
            theories TEXT, actual TEXT, accuracy TEXT,
            strength TEXT, agree INT,
            right_codes TEXT, wrong_codes TEXT,
            reasons TEXT,
            a_open REAL, a_close REAL,
            regime TEXT, zone TEXT,
            tags TEXT, postmortem TEXT,
            ts REAL DEFAULT (strftime('%s','now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS theory_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT, period INT, ctime INT,
            theory TEXT, vote TEXT, mag REAL,
            outcome TEXT, ts REAL DEFAULT (strftime('%s','now')))""")
        # Indexes — added ctime composite for faster recent_accuracy queries
        c.execute("CREATE INDEX IF NOT EXISTS ix_sl_asset_period ON signal_log(asset, period)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sl_ctime ON signal_log(asset, period, ctime DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sl_ts ON signal_log(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_tv_theory ON theory_votes(theory)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_tv_ts ON theory_votes(ts)")


def _as_text(v):
    """SQLite can't bind lists/dicts — store them as JSON text."""
    if v is None or isinstance(v, (str, int, float)):
        return v
    return json.dumps(v)


def save(asset, period, closed, micro):
    with _lock:
        try:
            with _cursor() as c:
                c.execute("""INSERT OR REPLACE INTO candle_micro
                    (asset,period,ctime,open,high,low,close,
                     buy_pct,sell_pct,pressure,is_fight,crosses,
                     hold_price,hold_visits,phases,reaction,net,
                     tick_count,last_react,round_near,round_str,
                     gap_pct,gap_type,key_levels,ticks_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (asset, period, closed["time"],
                     closed["open"], closed["high"], closed["low"], closed["close"],
                     micro.get("buy_pct"), micro.get("sell_pct"), micro.get("pressure"),
                     int(micro.get("is_fight", False)), micro.get("crosses"),
                     micro.get("hold_price"), micro.get("hold_visits"),
                     ",".join(micro.get("phases", [])), micro.get("reaction"),
                     micro.get("net"), micro.get("tick_count"),
                     micro.get("last_react"),
                     micro.get("round", {}).get("near_level"),
                     micro.get("round", {}).get("near_strength"),
                     micro.get("gap_pct"), micro.get("gap_type"),
                     _as_text(micro.get("key_levels")), _as_text(micro.get("ticks_json"))))
        except Exception as e:
            print(f"[db] save error: {e}")


def log_signal(asset, period, ctime, signal, score, confidence,
               theories, actual, accuracy, **kw):
    with _lock:
        try:
            with _cursor() as c:
                c.execute("""INSERT INTO signal_log
                    (asset,period,ctime,signal,score,confidence,theories,
                     actual,accuracy,strength,agree,right_codes,wrong_codes,
                     reasons,a_open,a_close,regime,zone,tags,postmortem)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (asset, period, ctime, signal, score, confidence, _as_text(theories),
                     actual, accuracy,
                     kw.get("strength"), kw.get("agree"),
                     _as_text(kw.get("right_codes")), _as_text(kw.get("wrong_codes")),
                     _as_text(kw.get("reasons")),
                     kw.get("a_open"), kw.get("a_close"),
                     kw.get("regime"), kw.get("zone"),
                     _as_text(kw.get("tags")), kw.get("postmortem")))
        except Exception as e:
            print(f"[db] log_signal error: {e}")


def log_theory_votes(asset, period, ctime, votes):
    """votes: list of (theory, CALL/PUT, mag, right/wrong/draw)"""
    if not votes:
        return
    with _lock:
        try:
            with _cursor() as c:
                for theory, vote, mag, outcome in votes:
                    c.execute("""INSERT INTO theory_votes
                        (asset,period,ctime,theory,vote,mag,outcome)
                        VALUES (?,?,?,?,?,?,?)""",
                        (asset, period, ctime, theory, vote, mag, outcome))
        except Exception as e:
            print(f"[db] log_theory_votes error: {e}")


def get_micro_history(asset, period, n=5, before_ctime=None):
    with _cursor() as c:
        q = """SELECT * FROM candle_micro
               WHERE asset=? AND period=?
               ORDER BY ctime DESC LIMIT ?"""
        params = [asset, period, n]
        if before_ctime is not None:
            q = q.replace("ORDER BY ctime DESC",
                          "AND ctime < ? ORDER BY ctime DESC")
            params.insert(2, before_ctime)
        rows = c.execute(q, params).fetchall()
        return [dict(r) for r in reversed(rows)]


def theory_perf(asset=None, period=None, days=7, min_n=30):
    """Return per-theory accuracy over last N days."""
    cutoff = time.time() - days * 86400
    with _cursor() as c:
        rows = c.execute("""SELECT theory,
                   SUM(CASE WHEN outcome='right' THEN 1 ELSE 0 END) as right_n,
                   COUNT(*) as total_n
                   FROM theory_votes
                   WHERE ts > ?
                   GROUP BY theory""", (cutoff,)).fetchall()
        out = {}
        for r in rows:
            theory = r["theory"]
            right_n, total_n = r["right_n"], r["total_n"]
            if total_n < min_n:
                continue
            out[theory] = {"rate": (right_n / total_n * 100) if total_n else 0,
                           "n": total_n}
        return out


def get_recent_signals(asset, period, limit=20):
    """Return recent signals with full details for frontend history display.
    Includes postmortem (win/loss reason), tags, right/wrong theories."""
    with _cursor() as c:
        rows = c.execute("""SELECT ctime, signal, accuracy, score, confidence,
                   strength, agree, theories, actual, regime, zone,
                   tags, postmortem, right_codes, wrong_codes,
                   a_open, a_close, reasons
                   FROM signal_log
                   WHERE asset=? AND period=? AND signal IN ('CALL','PUT')
                   ORDER BY ctime DESC LIMIT ?""",
                   (asset, period, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]


def get_signal_detail(asset, period, ctime):
    """Return a single signal's full detail (for the reason modal)."""
    with _cursor() as c:
        row = c.execute("""SELECT * FROM signal_log
                   WHERE asset=? AND period=? AND ctime=?
                   LIMIT 1""", (asset, period, ctime)).fetchone()
        return dict(row) if row else None


def recent_accuracy(asset, period, n=20):
    """Return (accuracy_float, sample_count) over the last N graded signals.

    accuracy_float = correct / (correct + wrong)   — draws excluded.
    Returns (None, 0) when no graded rows exist.
    A single graded row returns (1.0 or 0.0, 1) — caller is responsible for
    gating on sample size (analyze_eoc requires recent_n >= 8 before flipping).
    """
    with _cursor() as c:
        rows = c.execute("""SELECT accuracy
                   FROM signal_log
                   WHERE asset=? AND period=? AND signal IN ('CALL','PUT')
                     AND accuracy IN ('correct','wrong')
                   ORDER BY ctime DESC LIMIT ?""",
                   (asset, period, n)).fetchall()
    if not rows:
        return None, 0
    correct = sum(1 for r in rows if r["accuracy"] == "correct")
    total = len(rows)
    return correct / total, total


def cleanup(days=7):
    # NOTE (2026-07-13): this is a synchronous blocking sqlite3 call.
    # feed.py's run() calls it once at startup (acceptable — one-time prune
    # before the event loop is busy), while periodic 6-hour cleanups go
    # through asyncio.to_thread(_db.cleanup) to avoid blocking the loop.
    cutoff = time.time() - days * 86400
    with _cursor() as c:
        c.execute("DELETE FROM candle_micro WHERE ctime < ?", (int(cutoff),))
        c.execute("DELETE FROM signal_log WHERE ts < ?", (cutoff,))
        c.execute("DELETE FROM theory_votes WHERE ts < ?", (cutoff,))