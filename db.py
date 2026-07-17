"""
Lightweight SQLite persistence layer.
Tables: candle_micro, signal_log
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
            category TEXT,        -- FIX (2026-07-17): track which engine produced this signal
            ts REAL DEFAULT (strftime('%s','now')))""")
        # Indexes — added ctime composite for faster recent_accuracy queries
        c.execute("CREATE INDEX IF NOT EXISTS ix_sl_asset_period ON signal_log(asset, period)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sl_ctime ON signal_log(asset, period, ctime DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sl_ts ON signal_log(ts)")
        # FIX (2026-07-17): index for per-category accuracy queries.
        c.execute("CREATE INDEX IF NOT EXISTS ix_sl_category ON signal_log(category, asset, period)")

        # FIX (2026-07-17): schema migration. Older deployments created
        # signal_log WITHOUT the `category` column. Detect & add it here
        # so existing DBs upgrade transparently on next startup.
        try:
            cols = [row["name"] for row in c.execute("PRAGMA table_info(signal_log)").fetchall()]
            if "category" not in cols:
                c.execute("ALTER TABLE signal_log ADD COLUMN category TEXT")
                print("[db] migrated signal_log: added `category` column")
                # Backfill existing rows from asset name (OTC if ends with _otc)
                c.execute("UPDATE signal_log SET category = 'otc' WHERE asset LIKE '%_otc'")
                c.execute("UPDATE signal_log SET category = 'real' WHERE category IS NULL")
        except Exception as _e:
            print(f"[db] signal_log migration skipped: {_e}")

        # Refactor (2026-07-14): the `theory_votes` table is no longer
        # populated. The old theory engine was replaced by the
        # candle_reaction / advanced_analysis prediction path which doesn't
        # emit per-theory votes. Drop the table + its indexes if they still
        # exist from an older schema, and clean up orphaned rows.
        try:
            c.execute("DROP INDEX IF EXISTS ix_tv_theory")
            c.execute("DROP INDEX IF EXISTS ix_tv_ts")
            c.execute("DROP TABLE IF EXISTS theory_votes")
        except Exception:
            pass


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
                # FIX (2026-07-17): persist `category` so per-engine accuracy
                # can be tracked separately. Defaults to auto-detected from
                # asset name if caller doesn't pass it.
                category = kw.get("category")
                if category is None:
                    category = "otc" if asset.endswith("_otc") else "real"
                c.execute("""INSERT INTO signal_log
                    (asset,period,ctime,signal,score,confidence,theories,
                     actual,accuracy,strength,agree,right_codes,wrong_codes,
                     reasons,a_open,a_close,regime,zone,tags,postmortem,category)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (asset, period, ctime, signal, score, confidence, _as_text(theories),
                     actual, accuracy,
                     kw.get("strength"), kw.get("agree"),
                     _as_text(kw.get("right_codes")), _as_text(kw.get("wrong_codes")),
                     _as_text(kw.get("reasons")),
                     kw.get("a_open"), kw.get("a_close"),
                     kw.get("regime"), kw.get("zone"),
                     _as_text(kw.get("tags")), kw.get("postmortem"),
                     category))
        except Exception as e:
            print(f"[db] log_signal error: {e}")


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


def get_recent_signals(asset, period, limit=20):
    """Return recent signals with full details for frontend history display.
    Includes postmortem (win/loss reason), tags."""
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
    gating on sample size (prediction engine requires recent_n >= 8 before flipping).
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


# ── Per-module per-pair accuracy (added Bug #5 fix, 2026-07-17) ─────────────
# The `reasons` column in signal_log stores reason strings prefixed with
# [module_name]. We parse those to extract per-module win rates per asset,
# which lets per_pair.get_weights() adapt from historical accuracy instead
# of relying on the hardcoded PAIR_CONFIGS alone.

_MODULE_NAMES = (
    "candle_reaction", "running_tick", "pattern",
    "indicator", "key_level", "otc_pattern", "trend_follow",
)


def per_module_accuracy(asset, period=60, n=200):
    """Return per-module accuracy for a given (asset, period).

    Parses the `reasons` JSON array from signal_log and, for each module,
    counts how often its votes aligned with the final graded outcome.

    Returns:
        dict[module_name] = {
            "correct": int, "wrong": int, "total": int,
            "win_rate": float (0..1) or None if no graded rows
        }

    A module is credited `correct` when its own vote direction matched the
    final signal direction AND that final signal was graded `correct`. If
    the module's vote opposed the final signal and the final was `wrong`,
    the module is also credited `correct` (it was right, against the
    majority). Symmetric logic for `wrong`.

    This matches the attribution already used by /api/stats in server.py.
    """
    out = {m: {"correct": 0, "wrong": 0, "total": 0, "win_rate": None}
           for m in _MODULE_NAMES}

    with _cursor() as c:
        rows = c.execute("""SELECT signal, accuracy, reasons
                   FROM signal_log
                   WHERE asset=? AND period=? AND signal IN ('CALL','PUT')
                     AND accuracy IN ('correct','wrong')
                   ORDER BY ctime DESC LIMIT ?""",
                   (asset, period, n)).fetchall()

    if not rows:
        return out

    for row in rows:
        final_signal = row["signal"]
        accuracy = row["accuracy"]
        reasons_raw = row["reasons"] or "[]"
        try:
            reasons = json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw
        except (ValueError, TypeError):
            reasons = []
        if not isinstance(reasons, list):
            reasons = []

        for reason in reasons:
            reason_str = str(reason)
            if not reason_str.startswith("["):
                continue
            end_bracket = reason_str.find("]")
            if end_bracket == -1:
                continue
            module = reason_str[1:end_bracket].strip()
            if module not in _MODULE_NAMES:
                continue
            upper = reason_str.upper()
            # Determine the module's vote direction from the reason text.
            if "PUT" in upper or "BEAR" in upper or "SELLER" in upper:
                module_dir = "PUT"
            elif "CALL" in upper or "BULL" in upper or "BUYER" in upper:
                module_dir = "CALL"
            else:
                continue

            out[module]["total"] += 1
            # Aligned with final AND final was correct → module was right.
            # Opposed final AND final was wrong → module was also right.
            if module_dir == final_signal and accuracy == "correct":
                out[module]["correct"] += 1
            elif module_dir != final_signal and accuracy == "wrong":
                out[module]["correct"] += 1
            else:
                out[module]["wrong"] += 1

    for m in _MODULE_NAMES:
        s = out[m]
        if s["total"] > 0:
            s["win_rate"] = s["correct"] / s["total"]

    return out


def cleanup(days=7):
    # NOTE (2026-07-13): this is a synchronous blocking sqlite3 call.
    # feed.py's run() calls it once at startup (acceptable — one-time prune
    # before the event loop is busy), while periodic 6-hour cleanups go
    # through asyncio.to_thread(_db.cleanup) to avoid blocking the loop.
    cutoff = time.time() - days * 86400
    with _cursor() as c:
        c.execute("DELETE FROM candle_micro WHERE ctime < ?", (int(cutoff),))
        c.execute("DELETE FROM signal_log WHERE ts < ?", (cutoff,))
