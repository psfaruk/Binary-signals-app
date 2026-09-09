"""
Lightweight SQLite persistence layer.
Tables: candle_micro, signal_log
"""
import json
import re
import shutil
import sqlite3
import os
import time
from datetime import timedelta
import threading
from contextlib import contextmanager

def _resolve_db_path() -> str:
    """PERSISTENCE-FIX (2026-09-09): choose the DB location.

    Order:
      1. explicit DB_PATH env (backtests, custom deploys) — untouched.
      2. Railway Volume mount points (/app/data, /data) when they exist and
         are writable. Railway containers are EPHEMERAL: without a Volume
         every redeploy WIPES the repo-local signals.db — the 2026-09-09
         incident destroyed 14k+ real signal rows and 79k learned votes
         exactly this way (fresh 288KB signals.db after redeploy).
      3. legacy repo-local signals.db (local dev).
    """
    env = os.environ.get("DB_PATH")
    if env:
        return env
    for _d in ("/app/data", "/data"):
        try:
            if os.path.isdir(_d) and os.access(_d, os.W_OK):
                return os.path.join(_d, "signals.db")
        except Exception:
            pass
    return os.path.abspath(os.path.join(os.path.dirname(__file__) or ".", "signals.db"))


DB_PATH = _resolve_db_path()

try:
    _db_dir = os.path.dirname(DB_PATH)
    if _db_dir:
        os.makedirs(_db_dir, exist_ok=True)
except Exception as _mkdir_exc:
    print(f"[db] WARNING: could not create DB_PATH directory {DB_PATH!r}: {_mkdir_exc}")


_BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH) or ".", "backups")
_BACKUP_KEEP = int(os.environ.get("QX_DB_BACKUP_KEEP", "8"))
_BACKUP_INTERVAL = int(os.environ.get("QX_DB_BACKUP_SECS", "900"))   # 15 min; 0 = off


def _db_is_empty(path: str) -> bool:
    """True when the file is missing / not a DB / has no signal_log rows."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return True
        c = sqlite3.connect(path, timeout=5)
        try:
            try:
                n = c.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
            except sqlite3.OperationalError:
                return True          # table missing → fresh schema file
            return n == 0
        finally:
            c.close()
    except Exception:
        return True


def _restore_from_backup() -> None:
    """PERSISTENCE-FIX: on boot, if the live DB has NO graded history but a
    newer backup exists in <db_dir>/backups/, restore it. This rescues the
    history when a Volume is mounted but the DB file itself was lost/corrupted
    (or a deploy reset the file while backups survived on the Volume).
    Skipped entirely when DB_PATH was set explicitly (backtests etc.)."""
    if os.environ.get("DB_PATH"):
        return
    try:
        if not os.path.isdir(_BACKUP_DIR):
            return
        backups = sorted(
            (f for f in os.listdir(_BACKUP_DIR)
             if f.startswith("signals_") and f.endswith(".db")),
            reverse=True)
        if not backups:
            return
        newest = os.path.join(_BACKUP_DIR, backups[0])
        if _db_is_empty(DB_PATH) and not _db_is_empty(newest):
            shutil.copy2(newest, DB_PATH)
            print(f"[db] PERSISTENCE-RESTORE: {DB_PATH!r} was empty → restored "
                  f"history from backup {newest!r} ({os.path.getsize(DB_PATH)} bytes)")
    except Exception as exc:
        print(f"[db] backup-restore check failed (non-fatal): {exc}")


def _migrate_legacy_into_volume() -> None:
    """One-time copy of the legacy repo-local signals.db into the Volume DB
    when the Volume DB is still empty. Preserves history for users who attach
    a Railway Volume AFTER signals already accumulated repo-locally."""
    try:
        if os.environ.get("DB_PATH"):
            return                                   # explicit path → respect it
        legacy = os.path.abspath(os.path.join(os.path.dirname(__file__), "signals.db"))
        if os.path.abspath(DB_PATH) == legacy or not os.path.exists(legacy):
            return
        if _db_is_empty(DB_PATH) and not _db_is_empty(legacy):
            shutil.copy2(legacy, DB_PATH)
            print(f"[db] PERSISTENCE-MIGRATE: copied legacy {legacy!r} → {DB_PATH!r}")
    except Exception as exc:
        print(f"[db] legacy migration check failed (non-fatal): {exc}")


_restore_from_backup()
_migrate_legacy_into_volume()


def _backup_once() -> str | None:
    """SQLite-safe online backup → <db_dir>/backups/signals_YYYYmmdd_HHMMSS.db.
    Keeps the newest _BACKUP_KEEP files. Returns the backup path or None."""
    try:
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        dest_path = os.path.join(_BACKUP_DIR, f"signals_{stamp}.db")
        src = sqlite3.connect(DB_PATH, timeout=10)
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)                          # online API, no locks held
        finally:
            dst.close()
            src.close()
        olds = sorted(
            (f for f in os.listdir(_BACKUP_DIR)
             if f.startswith("signals_") and f.endswith(".db")),
            reverse=True)
        for old in olds[_BACKUP_KEEP:]:
            try:
                os.unlink(os.path.join(_BACKUP_DIR, old))
            except Exception:
                pass
        return dest_path
    except Exception as exc:
        print(f"[db] backup failed (non-fatal): {exc}")
        return None


_backup_thread_started = False


def start_backup_scheduler(interval_sec: int | None = None) -> None:
    """Start the periodic DB backup daemon thread (idempotent)."""
    global _backup_thread_started
    interval = int(interval_sec if interval_sec is not None else _BACKUP_INTERVAL)
    if _backup_thread_started or interval <= 0:
        return
    # Never schedule backups for throwaway backtest DBs.
    base = os.path.basename(DB_PATH)
    if os.environ.get("DB_PATH") and ("backtest" in base or "tmp" in base):
        return

    def _loop():
        # small initial delay so boot-time migrations finish first
        time.sleep(min(120, max(30, interval // 10)))
        while True:
            path = _backup_once()
            if path:
                print(f"[db] backup → {path}")
            time.sleep(interval)

    try:
        _t = threading.Thread(target=_loop, name="db-backup", daemon=True)
        _t.start()
        _backup_thread_started = True
        print(f"[db] backup scheduler ON: every {interval}s → {_BACKUP_DIR} "
              f"(keep {_BACKUP_KEEP})")
    except Exception as exc:
        print(f"[db] backup scheduler failed to start: {exc}")


# Ephemeral-filesystem loud warning (Railway without a Volume).
if not os.environ.get("DB_PATH") and not any(
        os.path.isdir(_d) for _d in ("/app/data", "/data")) \
        and os.environ.get("RAILWAY_ENVIRONMENT"):
    print("[db] ⚠️ NO PERSISTENT VOLUME DETECTED — signals.db lives on an "
          "ephemeral Railway filesystem and WILL BE WIPED on the next deploy. "
          "Attach a Railway Volume mounted at /app/data to keep signal "
          "history, learned weights and TARGET-75 gate state. "
          "See DEPLOYMENT_V2.md § Persistence.")


def _log_persistence_status() -> None:
    """Log a boot counter next to signals.db to verify persistence across redeploys."""
    marker_path = os.path.join(os.path.dirname(DB_PATH) or ".", ".persistence_marker.json")
    boot_count = 1
    first_seen = None
    try:
        if os.path.exists(marker_path):
            with open(marker_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            boot_count = int(data.get("boot_count", 0)) + 1
            first_seen = data.get("first_seen")
        else:
            first_seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump({"boot_count": boot_count, "first_seen": first_seen,
                       "last_boot": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
    except Exception as exc:
        print(f"[db] persistence marker check failed (non-fatal): {exc}")
        return
    if boot_count == 1:
        print(f"[db] persistence marker: boot_count=1 at {DB_PATH!r} — "
              f"if this ALSO reads 1 after your next redeploy, the Railway "
              f"Volume is not actually mounted here (data is being wiped).")
    else:
        print(f"[db] persistence marker: boot_count={boot_count} "
              f"(first_seen={first_seen}) at {DB_PATH!r} — data directory "
              f"is surviving restarts.")

_migration_lock = threading.Lock()

_VALID_SIGNALS = ("CALL", "PUT", "DRAW", "PENDING")
_VALID_ACCURACY = ("correct", "wrong", "draw", "pending", None)
_SECONDS_PER_DAY = 86400


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except sqlite3.Error as e:
        print(f"[db] PRAGMA setup failed (falling back to defaults): {e}")
    return conn


@contextmanager
def _read_cursor():
    """Read-only cursor — no commit, no fsync overhead."""
    conn = _conn()
    cur = conn.cursor()
    try:
        yield cur
    finally:
        conn.close()


@contextmanager
def _write_cursor():
    """Write cursor — commits on success, rolls back on exception."""
    conn = _conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception as _e:
            print(f"[silent-except] db.py:102 {type(_e).__name__}: {_e}")
        raise
    finally:
        conn.close()


_cursor = _write_cursor


def init():
    _log_persistence_status()
    with _cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS _meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
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
            category TEXT,        -- track which engine produced this signal
            ts REAL)""")
        try:
            cols = [row["name"] for row in c.execute("PRAGMA table_info(signal_log)").fetchall()]
            if "total" not in cols:
                c.execute("ALTER TABLE signal_log ADD COLUMN total INT")
                print("[db] migrated signal_log: added `total` column")
        except Exception as _e:
            print(f"[db] signal_log `total` column migration skipped: {_e}")
        try:
            cols = [row["name"] for row in c.execute("PRAGMA table_info(signal_log)").fetchall()]
            if "signal_quality" not in cols:
                c.execute("ALTER TABLE signal_log ADD COLUMN signal_quality TEXT")
                print("[db] migrated signal_log: added `signal_quality` column")
        except Exception as _e:
            print(f"[db] signal_log `signal_quality` column migration skipped: {_e}")
        # CONFLUENCE-V1 (2026-09-02): persist which strategy produced each
        # signal so history can prove exactly what fired (confluence_v1).
        try:
            cols = [row["name"] for row in c.execute("PRAGMA table_info(signal_log)").fetchall()]
            if "strategy" not in cols:
                c.execute("ALTER TABLE signal_log ADD COLUMN strategy TEXT")
                print("[db] migrated signal_log: added `strategy` column")
        except Exception as _e:
            print(f"[db] signal_log `strategy` column migration skipped: {_e}")
        c.execute("DROP INDEX IF EXISTS ix_sl_asset_period")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sl_ctime ON signal_log(asset, period, ctime DESC)")
        c.execute("DROP INDEX IF EXISTS ix_sl_ts")
        try:
            _cols = [row["name"] for row in c.execute("PRAGMA table_info(signal_log)").fetchall()]
            if "category" not in _cols:
                c.execute("ALTER TABLE signal_log ADD COLUMN category TEXT")
                print("[db] migrated signal_log: added `category` column")
                c.execute(
                    "UPDATE signal_log SET category = 'otc' "
                    "WHERE asset LIKE '%\\_otc' ESCAPE '\\'"
                )
                c.execute("UPDATE signal_log SET category = 'real' WHERE category IS NULL")
        except Exception as _e:
            print(f"[db] signal_log `category` column migration skipped: {_e}")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sl_category ON signal_log(category, asset, period)")
        try:
            c.execute("CREATE INDEX IF NOT EXISTS ix_sl_quality ON signal_log(signal_quality, accuracy)")
        except Exception as _e:
            print(f"[db] ix_sl_quality index creation skipped: {_e}")

        # FIX (TRUE-WR-MIGRATION-2026-08-31): pair_hourly_patterns historically
        # stored call/put win rates as an EMA approximation (old*0.8 + x*0.2)
        # which never converges to the real ratio and is skewed by recency.
        # Add true counter columns; call_win_pct / put_win_pct are now exact
        # ratios computed from them (see _update_hourly_pattern).
        # NOTE (CONFLUENCE-V1): these ALTERs run AFTER the CREATE TABLE block
        # further down in init() — moving them earlier silently skipped them
        # on fresh DBs (the table did not exist yet → `call_total` missing →
        # every _update_hourly_pattern call failed with "no such column").

        try:
            done_row = c.execute(
                "SELECT value FROM _meta WHERE key='signal_log_dedup_done'"
            ).fetchone()
            already_done = bool(done_row and done_row["value"])
        except Exception:
            already_done = False

        if not already_done:
            try:
                # Step 1+2: dedupe existing rows.
                dup_count = c.execute("""
                    SELECT COUNT(*) AS n FROM signal_log s1
                    WHERE EXISTS (
                        SELECT 1 FROM signal_log s2
                        WHERE s2.asset = s1.asset
                          AND s2.period = s1.period
                          AND s2.ctime  = s1.ctime
                          AND s2.id     > s1.id
                    )
                """).fetchone()
                dup_n = dup_count[0] if dup_count else 0
                if dup_n > 0:
                    print(f"[db] dedup signal_log: removing {dup_n} duplicate rows "
                          f"(keeping latest id per (asset,period,ctime))")
                    c.execute("""
                        DELETE FROM signal_log
                        WHERE id IN (
                            SELECT s1.id FROM signal_log s1
                            WHERE EXISTS (
                                SELECT 1 FROM signal_log s2
                                WHERE s2.asset = s1.asset
                                  AND s2.period = s1.period
                                  AND s2.ctime  = s1.ctime
                                  AND s2.id     > s1.id
                            )
                        )
                    """)
            except Exception as _e:
                print(f"[db] signal_log dedup skipped: {_e}")

        # Step 3: drop legacy UNIQUE indexes (best-effort).
        try:
            legacy_indexes = [
                "ux_sl_asset_period_ctime",
                "ux_sl_legacy_asset_period_ctime",
                "uq_sl_asset_period_ctime",
                "unique_sl_asset_period_ctime",
            ]
            for idx_name in legacy_indexes:
                c.execute(f"DROP INDEX IF EXISTS {idx_name}")
        except Exception as _e:
            print(f"[silent-except] db.py:295 {type(_e).__name__}: {_e}")

        # Step 4: create the canonical UNIQUE index.
        try:
            c.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_sl_asset_period_ctime
                ON signal_log(asset, period, ctime)
            """)
            try:
                c.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("signal_log_dedup_done", "1"))
            except Exception as _e:
                print(f"[db] could not record dedup-done flag: {_e}")
        except sqlite3.Error as _e:
            print(f"[db] WARNING: could not create UNIQUE index on signal_log: {_e}")
            print("[db] Falling back to non-unique index — duplicate-row bug may recur.")
            print("[db] Manual dedup required: see init() Step 1+2.")
            try:
                c.execute("""
                    CREATE INDEX IF NOT EXISTS ix_sl_asset_period_ctime_nonunique
                    ON signal_log(asset, period, ctime)
                """)
            except sqlite3.Error as _e:
                print(f"[silent-except] db.py:331 {type(_e).__name__}: {_e}")

        try:
            tv_done = c.execute(
                "SELECT value FROM _meta WHERE key='theory_votes_dropped'"
            ).fetchone()
            if not (tv_done and tv_done["value"]):
                c.execute("DROP INDEX IF EXISTS ix_tv_theory")
                c.execute("DROP INDEX IF EXISTS ix_tv_ts")
                c.execute("DROP TABLE IF EXISTS theory_votes")
                c.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("theory_votes_dropped", "1"),
                )
        except Exception as _e:
            print(f"[db] theory_votes cleanup skipped: {_e}")

        try:
            c.execute("""CREATE TABLE IF NOT EXISTS module_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INT,           -- FK to signal_log.id (nullable for legacy)
                asset TEXT, period INT, ctime INT,
                module_name TEXT,        -- candle_reaction, pattern, etc.
                direction TEXT,          -- CALL or PUT
                vote_correct INT,        -- 1=correct, 0=wrong, NULL=ungraded
                score REAL,              -- module's effective score
                confidence REAL,         -- module's confidence contribution
                signal_group TEXT,       -- BODY, WICK, PATTERN, etc.
                engine TEXT,             -- otc or real
                regime TEXT,             -- RANGE, TREND_UP, etc.
                strength TEXT,           -- WEAK, MEDIUM, STRONG
                ts REAL)""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_mv_asset_module ON module_votes(asset, module_name, vote_correct)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_mv_module_correct ON module_votes(module_name, vote_correct)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_mv_asset_dir ON module_votes(asset, direction, vote_correct)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_mv_ctime ON module_votes(ctime DESC)")
        except sqlite3.Error as _e:
            print(f"[db] module_votes table creation skipped: {_e}")

        try:
            c.execute("""CREATE TABLE IF NOT EXISTS theory_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INT,
                asset TEXT, period INT, ctime INT,
                module_name TEXT,
                theory_name TEXT,
                theory_group TEXT,
                direction TEXT,
                signal_type TEXT,
                score INT,
                confidence INT,
                effective_score INT,
                vote_correct INT,
                engine TEXT, regime TEXT, strength TEXT,
                ts REAL)""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_tv_module_theory ON theory_votes(module_name, theory_name, vote_correct)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_tv_asset_theory ON theory_votes(asset, theory_name, vote_correct)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_tv_theory ON theory_votes(theory_name, vote_correct)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_tv_ctime ON theory_votes(ctime DESC)")
        except sqlite3.Error as _e:
            print(f"[db] theory_votes table creation skipped: {_e}")

        try:
            c.execute("""CREATE TABLE IF NOT EXISTS signal_quality_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INT,
                asset TEXT, period INT, ctime INT,
                move_atr_pct REAL,       -- |close-open|/ATR * 100
                move_direction TEXT,     -- UP, DOWN, FLAT
                tick_count INT,          -- ticks in the candle
                buy_pct REAL, sell_pct REAL,
                pressure TEXT,
                session_hour INT,        -- 0-23 UTC
                session_name TEXT,       -- asian, london, ny, off
                agree_count INT,         -- modules agreeing
                total_modules INT,
                confidence_at_close REAL,
                confidence_final REAL,
                confidence_changed INT,  -- 1 if LIVE re-eval modified it
                tags TEXT,
                ts REAL)""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_sqm_asset ON signal_quality_metrics(asset, ctime DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_sqm_session ON signal_quality_metrics(session_name, move_direction)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_sqm_move ON signal_quality_metrics(move_atr_pct)")
        except sqlite3.Error as _e:
            print(f"[db] signal_quality_metrics table creation skipped: {_e}")

        try:
            c.execute("""CREATE TABLE IF NOT EXISTS pair_performance_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT,
                date TEXT,               -- YYYY-MM-DD
                engine TEXT,
                total_signals INT,
                correct INT,
                wrong INT,
                draw INT,
                win_pct REAL,
                avg_confidence REAL,
                best_module TEXT,
                worst_module TEXT,
                ts REAL)""")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_ppd_asset_date ON pair_performance_daily(asset, date)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_ppd_date ON pair_performance_daily(date DESC)")
        except sqlite3.Error as _e:
            print(f"[db] pair_performance_daily table creation skipped: {_e}")

        try:
            c.execute("""CREATE TABLE IF NOT EXISTS pair_hourly_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT,
                hour_utc INT,             -- 0-23
                session TEXT,             -- asian, london, ny, overlap, off
                total_signals INT,
                correct INT,
                wrong INT,
                win_pct REAL,
                avg_confidence REAL,
                avg_move_atr REAL,        -- average move size for this hour
                best_direction TEXT,      -- CALL or PUT (which wins more)
                call_win_pct REAL,        -- win rate when signal is CALL
                put_win_pct REAL,         -- win rate when signal is PUT
                last_updated REAL,
                ts REAL)""")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_php_asset_hour ON pair_hourly_patterns(asset, hour_utc)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_php_session ON pair_hourly_patterns(session, win_pct)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_php_win_pct ON pair_hourly_patterns(win_pct)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_php_asset ON pair_hourly_patterns(asset)")
        except sqlite3.Error as _e:
            print(f"[db] pair_hourly_patterns table creation skipped: {_e}")

        # CONFLUENCE-V1 (2026-09-02) + TRUE-WR migration: pair_hourly_patterns
        # column migrations — run AFTER the CREATE TABLE above so they work on
        # BOTH fresh DBs (table just created) and existing DBs.
        try:
            _php_cols = [row["name"] for row in c.execute(
                "PRAGMA table_info(pair_hourly_patterns)").fetchall()]
            if _php_cols:
                for _new_col in ("call_total INT", "call_correct INT",
                                 "put_total INT", "put_correct INT",
                                 "last_ctime INT",
                                 # FIX (HOURLY-DEDUP-2026-09-07, MEDIUM):
                                 # the dedup ledger keyed (asset, hour_utc) on a
                                 # single last_ctime — a 300s candle graded at the
                                 # same ctime as a 60s candle was silently skipped,
                                 # and out-of-order re-grades double-counted.
                                 # counted_keys is a JSON map {"<period>": ctime}
                                 # so every (period, ctime) is deduped exactly.
                                 "counted_keys TEXT DEFAULT '{}'"):
                    _col_name = _new_col.split()[0]
                    if _col_name not in _php_cols:
                        c.execute(f"ALTER TABLE pair_hourly_patterns ADD COLUMN {_new_col}")
                        print(f"[db] migrated pair_hourly_patterns: added `{_col_name}` column")
        except Exception as _e:
            print(f"[db] pair_hourly_patterns column migration skipped: {_e}")

        try:
            c.execute("""CREATE TABLE IF NOT EXISTS quotex_algo_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT,
                pattern_type TEXT,        -- trap_hour, boost_hour, reversal_hour, direction_bias
                hour_utc INT,
                session TEXT,
                description TEXT,
                evidence TEXT,            -- JSON: {win_pct, sample_count, confidence}
                severity TEXT,            -- info, warning, critical
                detected_at REAL,
                ts REAL)""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_qap_asset_type ON quotex_algo_patterns(asset, pattern_type)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_qap_hour ON quotex_algo_patterns(hour_utc)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_qap_severity ON quotex_algo_patterns(severity)")
        except sqlite3.Error as _e:
            print(f"[db] quotex_algo_patterns table creation skipped: {_e}")


def _as_text(v):
    """SQLite can't bind lists/dicts — store them as JSON text."""
    if v is None or isinstance(v, (str, int, float)):
        return v
    return json.dumps(v)


def save(asset, period, closed, micro):
    conn = _conn()
    try:
        try:
            cur = conn.cursor()
            cur.execute("""INSERT OR REPLACE INTO candle_micro
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
                 (micro.get("round") or {}).get("near_level"),
                 (micro.get("round") or {}).get("near_strength"),
                 micro.get("gap_pct"), micro.get("gap_type"),
                 _as_text(micro.get("key_levels")), _as_text(micro.get("ticks_json"))))
            conn.commit()
        except (sqlite3.Error, KeyError, TypeError, ValueError, AttributeError) as e:
            print(f"[db] save {type(e).__name__}: {e}")
            try:
                conn.rollback()
            except Exception as _e:
                print(f"[silent-except] db.py:425 {type(_e).__name__}: {_e}")
    finally:
        conn.close()


def _category_for_asset(asset):
    """Detect engine category from asset name. Single source of truth."""
    return "otc" if asset.endswith("_otc") else "real"


import re as _re_module

try:
    from core.constants import MODULE_NAMES as _MODULE_NAMES
except ImportError:
    _MODULE_NAMES = (
        "candle_reaction", "pattern", "key_level",
        "market_state", "wickwall", "divergence", "tickrun",
    )

_MODULE_TAG_RE = _re_module.compile(
    r'\[(' + '|'.join(_re_module.escape(m) for m in _MODULE_NAMES) + r')\]')

_THEORY_PATTERNS = {
    'candle_reaction': [
        (r'(\d+)\+\s*(UP|DOWN)\s+streak', 'Streak reversal'),
        (r'Big\s+(UP|DOWN)\s+body', 'Big body reversal'),
        (r'Upper wick rejection', 'Upper wick rejection'),
        (r'Lower wick rejection', 'Lower wick rejection'),
        (r'Close at range top', 'Close at range top'),
        (r'Close at range bottom', 'Close at range bottom'),
        (r'Rising closes momentum', 'Rising closes momentum'),
        (r'Falling closes momentum', 'Falling closes momentum'),
    ],
    'pattern': [
        (r'Bullish Engulfing', 'Bullish Engulfing'),
        (r'Bearish Engulfing', 'Bearish Engulfing'),
        (r'Morning Star', 'Morning Star'),
        (r'Evening Star', 'Evening Star'),
        (r'Tweezer Top', 'Tweezer Top'),
        (r'Tweezer Bottom', 'Tweezer Bottom'),
        (r'Three White Soldiers|3_SOLDIERS', 'Three White Soldiers'),
        (r'Three Black Crows|3_CROWS', 'Three Black Crows'),
        (r'3_SOLDIERS_EXHAUST|Three Soldiers Exhaust', '3 Soldiers Exhaust'),
        (r'3_CROWS_EXHAUST|Three Crows Exhaust', '3 Crows Exhaust'),
        (r'Piercing Line', 'Piercing Line'),
        (r'Dark Cloud', 'Dark Cloud Cover'),
        (r'Bull Harami|BULL_HARAMI', 'Bull Harami'),
        (r'Bear Harami|BEAR_HARAMI', 'Bear Harami'),
        (r'Hammer|BULL_PIN_BAR', 'Hammer'),
        (r'Shooting Star|BEAR_PIN_BAR', 'Shooting Star'),
        (r'Bullish Pin Bar|BULL_PIN_BAR', 'Bullish Pin Bar'),
        (r'Bearish Pin Bar|BEAR_PIN_BAR', 'Bearish Pin Bar'),
        (r'Bullish Two-Bar Reversal|BULL_TWO_BAR_REV', 'Bullish Two-Bar Reversal'),
        (r'Bearish Two-Bar Reversal|BEAR_TWO_BAR_REV', 'Bearish Two-Bar Reversal'),
        (r'Doji after uptrend|DOJI_BEARISH', 'Doji Bearish'),
        (r'Doji after downtrend|DOJI_BULLISH', 'Doji Bullish'),
    ],
    'key_level': [
        (r'Support wick rejection', 'Support wick rejection'),
        (r'Resistance wick rejection', 'Resistance wick rejection'),
        (r'Key support bounce', 'Key support bounce'),
        (r'Key resistance bounce', 'Key resistance bounce'),
        (r'Close near prev high', 'Close near prev high'),
        (r'Close above prev high', 'Close above prev high (breakout)'),
        (r'Close near prev low', 'Close near prev low'),
        (r'Close below prev low', 'Close below prev low (breakdown)'),
        (r'Fibonacci\s+(\d+\.?\d*)%', 'Fibonacci retracement'),
        (r'Broken resistance now support', 'S/R flip (resistance→support)'),
        (r'Broken support now resistance', 'S/R flip (support→resistance)'),
        (r'Trendline breakout above', 'Trendline breakout (bullish)'),
        (r'Trendline breakdown below', 'Trendline breakdown (bearish)'),
    ],
    'market_state': [
        (r'MARKET_STATE\s+CONTINUATION', 'Market state: continuation'),
        (r'MARKET_STATE\s+EXHAUSTION', 'Market state: exhaustion'),
        (r'MARKET_STATE\s+REVERSAL', 'Market state: reversal'),
        (r'MARKET_STATE\s+TRAP', 'Market state: trap'),
        (r'MARKET_STATE\s+RANGE', 'Market state: range fade'),
    ],
    'wickwall': [
        (r'Lower-wick cluster', 'Lower-wick cluster (support)'),
        (r'Upper-wick cluster', 'Upper-wick cluster (resistance)'),
    ],
    'divergence': [
        (r'DIVERGENCE\s+Bearish', 'Bearish divergence'),
        (r'DIVERGENCE\s+Bullish', 'Bullish divergence'),
    ],
    'tickrun': [
        (r'TICKSWEEP\s+Upper stop-hunt', 'Tick sweep: upper stop-hunt'),
        (r'TICKSWEEP\s+Lower stop-hunt', 'Tick sweep: lower stop-hunt'),
        (r'ABSORBWALL.*upper band', 'Absorb wall: upper band'),
        (r'ABSORBWALL.*lower band', 'Absorb wall: lower band'),
        (r'LATEFLIP\s+Control transfer', 'Late flip: control transfer'),
    ],
    # FIX (DEEP-FIX-2026-08-07): new modules
    'multi_tf': [
        (r'HTF CONFIRM.*strong confirmation', 'HTF strong confirm'),
        (r'HTF CONFIRM.*moderate confirmation', 'HTF moderate confirm'),
        (r'HTF COUNTER.*strong counter', 'HTF strong counter'),
        (r'HTF WEAKEN.*mild counter', 'HTF mild counter'),
    ],
    'momentum': [
        (r'RSI\s+\d+\.?\d*\s*overbought', 'RSI overbought reversal'),
        (r'RSI\s+\d+\.?\d*\s*oversold', 'RSI oversold reversal'),
        (r'RSI\s+\d+\.?\d*\s*bullish momentum', 'RSI bullish continuation'),
        (r'RSI\s+\d+\.?\d*\s*bearish momentum', 'RSI bearish continuation'),
        (r'MACD bullish crossover', 'MACD bullish crossover'),
        (r'MACD bearish crossover', 'MACD bearish crossover'),
        (r'MACD histogram bullish', 'MACD histogram bullish'),
        (r'MACD histogram bearish', 'MACD histogram bearish'),
    ],
}

_THEORY_GROUPS = {
    'Streak reversal': 'BODY',
    'Big body reversal': 'BODY',
    'Upper wick rejection': 'WICK',
    'Lower wick rejection': 'WICK',
    'Close at range top': 'BODY',
    'Close at range bottom': 'BODY',
    'Rising closes momentum': 'BODY_CONT',
    'Falling closes momentum': 'BODY_CONT',
    'Micro composite': 'MICRO',
    'Bullish Engulfing': 'PATTERN',
    'Bearish Engulfing': 'PATTERN',
    'Morning Star': 'PATTERN',
    'Evening Star': 'PATTERN',
    'Tweezer Top': 'PATTERN',
    'Tweezer Bottom': 'PATTERN',
    'Three White Soldiers': 'PATTERN',
    'Three Black Crows': 'PATTERN',
    '3 Soldiers Exhaust': 'PATTERN',
    '3 Crows Exhaust': 'PATTERN',
    'Piercing Line': 'PATTERN',
    'Dark Cloud Cover': 'PATTERN',
    'Bull Harami': 'PATTERN',
    'Bear Harami': 'PATTERN',
    'Hammer': 'PATTERN',
    'Shooting Star': 'PATTERN',
    'Bullish Pin Bar': 'PATTERN',
    'Bearish Pin Bar': 'PATTERN',
    'Bullish Two-Bar Reversal': 'PATTERN',
    'Bearish Two-Bar Reversal': 'PATTERN',
    'Doji Bearish': 'PATTERN',
    'Doji Bullish': 'PATTERN',
    'Support wick rejection': 'LEVEL',
    'Resistance wick rejection': 'LEVEL',
    'Key support bounce': 'LEVEL',
    'Key resistance bounce': 'LEVEL',
    'Close near prev high': 'MICRO_SR',
    'Close above prev high (breakout)': 'MICRO_SR',
    'Close near prev low': 'MICRO_SR',
    'Close below prev low (breakdown)': 'MICRO_SR',
    'Fibonacci retracement': 'FIB',
    'S/R flip (resistance→support)': 'SR_FLIP',
    'S/R flip (support→resistance)': 'SR_FLIP',
    'Trendline breakout (bullish)': 'TRENDLINE',
    'Trendline breakdown (bearish)': 'TRENDLINE',
    'Market state: continuation': 'MARKET_STATE',
    'Market state: exhaustion': 'MARKET_STATE',
    'Market state: reversal': 'MARKET_STATE',
    'Market state: trap': 'MARKET_STATE',
    'Market state: range fade': 'MARKET_STATE',
    'Lower-wick cluster (support)': 'WICKWALL',
    'Upper-wick cluster (resistance)': 'WICKWALL',
    'Bearish divergence': 'DIVERGENCE',
    'Bullish divergence': 'DIVERGENCE',
    'Tick sweep: upper stop-hunt': 'TICKRUN_SWEEP',
    'Tick sweep: lower stop-hunt': 'TICKRUN_SWEEP',
    'Absorb wall: upper band': 'TICKRUN_ABSORB',
    'Absorb wall: lower band': 'TICKRUN_ABSORB',
    'Late flip: control transfer': 'TICKRUN_FLIP',
    # FIX (DEEP-FIX-2026-08-07): new module theory groups
    'HTF strong confirm': 'MULTI_TF',
    'HTF moderate confirm': 'MULTI_TF',
    'HTF strong counter': 'MULTI_TF',
    'HTF mild counter': 'MULTI_TF',
    'RSI overbought reversal': 'MOMENTUM_RSI',
    'RSI oversold reversal': 'MOMENTUM_RSI',
    'RSI bullish continuation': 'MOMENTUM_RSI',
    'RSI bearish continuation': 'MOMENTUM_RSI',
    'MACD bullish crossover': 'MOMENTUM_MACD',
    'MACD bearish crossover': 'MOMENTUM_MACD',
    'MACD histogram bullish': 'MOMENTUM_MACD',
    'MACD histogram bearish': 'MOMENTUM_MACD',
}


def _vote_correct(direction, actual):
    """1 if `direction` matched `actual`, 0 if not, None if the candle drew."""
    if actual not in ('UP', 'DOWN'):
        return None
    return 1 if ((direction == 'CALL' and actual == 'UP') or
                 (direction == 'PUT' and actual == 'DOWN')) else 0


def _extract_theory_votes(reasons_list, asset, period, ctime, actual, category, regime, strength, ts_val):
    """Parse reason strings and extract per-theory vote rows for theory_votes."""
    rows = []
    if not reasons_list:
        return rows

    for reason_str in reasons_list:
        if not isinstance(reason_str, str):
            reason_str = str(reason_str)
        # Extract module name from [module_name] prefix
        if not reason_str.startswith('['):
            continue
        end_bracket = reason_str.find(']')
        if end_bracket == -1:
            continue
        module = reason_str[1:end_bracket].strip()
        if module not in _MODULE_NAMES:
            continue

        # Extract direction
        dir_match = _re_module.search(r'(?:→|->)\s*(CALL|PUT)\b', reason_str)
        if not dir_match:
            continue
        direction = dir_match.group(1)

        # Determine signal_type from keywords
        reason_lower = reason_str.lower()
        if 'continuation' in reason_lower or 'breakout' in reason_lower or 'breakdown' in reason_lower:
            signal_type = 'CONTINUATION'
        elif 'reversal' in reason_lower or 'bounce' in reason_lower or 'rejection' in reason_lower or 'flip' in reason_lower:
            signal_type = 'REVERSAL'
        else:
            signal_type = 'REVERSAL'  # default

        # Extract effective score from (eff=N) suffix
        eff_match = _re_module.search(r'\(eff=(\d+)\)', reason_str)
        effective_score = int(eff_match.group(1)) if eff_match else None

        # Extract theory name using module-specific patterns.
        theory_name = None
        for pattern, name in _THEORY_PATTERNS.get(module, ()):
            if _re_module.search(pattern, reason_str, _re_module.IGNORECASE):
                theory_name = name
                break

        if not theory_name:
            content = reason_str[end_bracket + 1:].split('→')[0].strip()[:40]
            theory_name = content or 'Unknown'

        theory_group = _THEORY_GROUPS.get(theory_name, 'UNKNOWN')

        rows.append((
            None, asset, period, ctime, module,
            theory_name, theory_group, direction, signal_type,
            None, None, effective_score,
            _vote_correct(direction, actual),
            category, regime, strength, ts_val
        ))

    return rows


def log_signal(asset, period, ctime, signal, score, confidence,
               theories, actual, accuracy, **kw):
    if signal not in _VALID_SIGNALS:
        print(f"[db] log_signal: invalid signal={signal!r} "
              f"(allowed: {_VALID_SIGNALS})")
        return
    if accuracy not in _VALID_ACCURACY:
        print(f"[db] log_signal: invalid accuracy={accuracy!r} "
              f"(allowed: {_VALID_ACCURACY})")
        return

    category = kw.get("category") or _category_for_asset(asset)
    total_val = kw.get("total")
    if total_val is None:
        total_val = kw.get("agree") or 0
    ts_val = time.time()

    conn = _conn()
    try:
        strategy_val = kw.get("strategy")
        try:
            cur = conn.cursor()
            try:
                cur.execute("""
                    INSERT INTO signal_log
                        (asset,period,ctime,signal,score,confidence,theories,
                         actual,accuracy,strength,agree,right_codes,wrong_codes,
                         reasons,a_open,a_close,regime,zone,tags,postmortem,
                         category,total,ts,signal_quality,strategy)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(asset, period, ctime) DO UPDATE SET
                        signal=excluded.signal,
                        score=excluded.score,
                        confidence=excluded.confidence,
                        theories=excluded.theories,
                        actual=excluded.actual,
                        accuracy=excluded.accuracy,
                        strength=excluded.strength,
                        agree=excluded.agree,
                        right_codes=excluded.right_codes,
                        wrong_codes=excluded.wrong_codes,
                        reasons=excluded.reasons,
                        a_open=excluded.a_open,
                        a_close=excluded.a_close,
                        regime=excluded.regime,
                        zone=excluded.zone,
                        tags=excluded.tags,
                        postmortem=excluded.postmortem,
                        category=excluded.category,
                        total=excluded.total,
                        ts=excluded.ts,
                        signal_quality=excluded.signal_quality,
                        strategy=excluded.strategy
                    """,
                    (asset, period, ctime, signal, score, confidence, _as_text(theories),
                     actual, accuracy,
                     kw.get("strength"), kw.get("agree"),
                     _as_text(kw.get("right_codes")), _as_text(kw.get("wrong_codes")),
                     _as_text(kw.get("reasons")),
                     kw.get("a_open"), kw.get("a_close"),
                     kw.get("regime"), kw.get("zone"),
                     _as_text(kw.get("tags")), kw.get("postmortem"),
                     category, total_val, ts_val, kw.get("signal_quality"),
                     strategy_val))
            except sqlite3.Error as _conflict_err:
                if "ON CONFLICT" in str(_conflict_err) and "UNIQUE" in str(_conflict_err).upper():
                    # Fallback: no unique constraint, use INSERT OR REPLACE.
                    cur.execute("""
                        INSERT OR REPLACE INTO signal_log
                            (asset,period,ctime,signal,score,confidence,theories,
                             actual,accuracy,strength,agree,right_codes,wrong_codes,
                             reasons,a_open,a_close,regime,zone,tags,postmortem,
                             category,total,ts,signal_quality,strategy)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (asset, period, ctime, signal, score, confidence, _as_text(theories),
                         actual, accuracy,
                         kw.get("strength"), kw.get("agree"),
                         _as_text(kw.get("right_codes")), _as_text(kw.get("wrong_codes")),
                         _as_text(kw.get("reasons")),
                         kw.get("a_open"), kw.get("a_close"),
                         kw.get("regime"), kw.get("zone"),
                         _as_text(kw.get("tags")), kw.get("postmortem"),
                         category, total_val, ts_val, kw.get("signal_quality"),
                         strategy_val))
                else:
                    raise
            conn.commit()

            try:
                reasons_text = kw.get("reasons", "")
                if reasons_text:
                    import re as _re
                    if isinstance(reasons_text, str):
                        try:
                            r_list = json.loads(reasons_text)
                        except Exception:
                            r_list = [reasons_text]
                    else:
                        r_list = reasons_text
                    r_text = ' ||| '.join(str(r) for r in r_list) if isinstance(r_list, list) else str(reasons_text)

                    parts = _MODULE_TAG_RE.split(r_text)
                    seen = set()
                    vote_rows = []
                    for i in range(1, len(parts), 2):
                        mod = parts[i]
                        content = parts[i+1] if i+1 < len(parts) else ''
                        dir_match = _re.search(r'(?:→|->)\s*(CALL|PUT)\b', content)
                        if not dir_match:
                            continue
                        direction = dir_match.group(1)
                        if (mod, direction) in seen:
                            continue
                        seen.add((mod, direction))

                        eff_m = _re.search(r'\(eff=(\d+)\)', content)
                        eff_score = int(eff_m.group(1)) if eff_m else None

                        vote_rows.append((
                            None, asset, period, ctime, mod, direction,
                            _vote_correct(direction, actual),
                            eff_score, None, None,
                            category, kw.get('regime'), kw.get('strength'), ts_val
                        ))

                    if vote_rows:
                        # FIX (VOTE-DEDUP-2026-08-31): signal_log is UPSERT-keyed
                        # on (asset, period, ctime), but module_votes/theory_votes
                        # had no dedup — any re-grade of the same candle (replay
                        # backtests, manual re-grading) double-counted every vote
                        # and skewed per-module win rates. Delete the old vote
                        # rows for this candle before inserting the fresh set.
                        cur.execute("DELETE FROM module_votes "
                                    "WHERE asset=? AND period=? AND ctime=?",
                                    (asset, period, ctime))
                        cur.executemany("""INSERT INTO module_votes
                            (signal_id, asset, period, ctime, module_name, direction,
                             vote_correct, score, confidence, signal_group,
                             engine, regime, strength, ts)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            vote_rows)
                        conn.commit()

                    theory_rows = _extract_theory_votes(
                        r_list if isinstance(r_list, list) else [reasons_text],
                        asset, period, ctime, actual, category,
                        kw.get('regime'), kw.get('strength'), ts_val)
                    if theory_rows:
                        cur.execute("DELETE FROM theory_votes "
                                    "WHERE asset=? AND period=? AND ctime=?",
                                    (asset, period, ctime))
                        cur.executemany("""INSERT INTO theory_votes
                            (signal_id, asset, period, ctime, module_name,
                             theory_name, theory_group, direction, signal_type,
                             score, confidence, effective_score, vote_correct,
                             engine, regime, strength, ts)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            theory_rows)
                        conn.commit()
            except Exception as _mv_err:
                print(f"[db] module_votes write skipped: {_mv_err}")

        except (sqlite3.Error, TypeError, ValueError) as e:
            print(f"[db] log_signal {type(e).__name__}: {e}")
            try:
                conn.rollback()
            except Exception as _e:
                print(f"[silent-except] db.py:565 {type(_e).__name__}: {_e}")
    finally:
        conn.close()

    try:
        _update_hourly_pattern(asset, period, ctime, signal, accuracy, confidence)
    except Exception as _hp_err:
        print(f"[db] hourly pattern update skipped: {_hp_err}")


def _get_session_name(hour_utc: int) -> str:
    """Map UTC hour to trading session name."""
    if 0 <= hour_utc < 7:
        return "asian"
    elif 7 <= hour_utc < 12:
        return "london"
    elif 12 <= hour_utc < 17:
        return "ny"
    else:
        return "off"


def _update_hourly_pattern(asset: str, period: int, ctime: int, signal: str,
                           accuracy: str, confidence):
    """Update pair_hourly_patterns table after each graded signal.

    FIX (CONFLUENCE-V1-2026-09-02): added a last_ctime dedup ledger.
    log_signal is an UPSERT keyed on (asset, period, ctime) — any re-grade of
    the same candle used to increment these counters a second time and
    silently corrupt every time-pattern win rate. A re-grade of the SAME
    candle is now a no-op for this table.

    FIX (HOURLY-DEDUP-2026-09-07): the ledger is now PER (period, ctime) via
    the counted_keys JSON column instead of one last_ctime per (asset, hour).
    Previously a 300s candle graded at the same ctime as a 60s candle was
    silently dropped (only one could be remembered), and a re-grade that was
    not the latest write double-counted.
    """
    if not ctime or accuracy not in ('correct', 'wrong'):
        return
    try:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(int(ctime), tz=timezone.utc)
        hour_utc = dt.hour
    except Exception:
        return

    session = _get_session_name(hour_utc)
    is_correct = 1 if accuracy == 'correct' else 0
    is_call = signal == 'CALL'
    ts_val = time.time()

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT total_signals, correct, wrong, call_win_pct, put_win_pct,
                   last_ctime, avg_confidence
            FROM pair_hourly_patterns
            WHERE asset = ? AND hour_utc = ?
        """, (asset, hour_utc))
        existing = cur.fetchone()

        # FIX (CONFLUENCE-V1): dedup ledger — same candle re-graded → skip.
        # FIX (HOURLY-DEDUP-2026-09-07): the ledger is per (period, ctime) —
        # counted_keys JSON {"60": 1725…, "300": …} replaces the single
        # last_ctime check so multi-period grades at one hour never collide.
        _counted = {}
        if existing:
            try:
                _counted = json.loads(existing['counted_keys'] or '{}')
                if not isinstance(_counted, dict):
                    _counted = {}
            except Exception:
                _counted = {}
            # Legacy ledger fallback: rows written before counted_keys existed.
            if (not _counted and existing['last_ctime']
                    and int(existing['last_ctime']) == int(ctime)):
                _counted = {'60': int(existing['last_ctime'])}
        _ckey = str(period)
        if _counted.get(_ckey) and int(_counted[_ckey]) == int(ctime):
            conn.close()
            return
        _counted[_ckey] = int(ctime)
        # Bound the ledger (a candle can only be re-graded, never travel back
        # in time — keep the newest 8 period keys, always including current).
        if len(_counted) > 8:
            _counted = dict(sorted(_counted.items(), key=lambda kv: kv[1])[-8:])

        # FIX (TRUE-WR-2026-08-31): call/put win rates are now TRUE ratios
        # tracked via explicit counter columns (call_total/call_correct/
        # put_total/put_correct), replacing the old EMA approximation that
        # never converged to the actual win rate.
        if existing:
            old_total = existing['total_signals'] or 0
            old_correct = existing['correct'] or 0
            old_wrong = existing['wrong'] or 0
            new_total = old_total + 1
            new_correct = old_correct + is_correct
            new_wrong = old_wrong + (1 - is_correct)
            new_win_pct = round(100.0 * new_correct / new_total, 1) if new_total > 0 else 0

            _row = dict(existing)
            if is_call:
                ct = (_row.get('call_total') or 0) + 1
                cc = (_row.get('call_correct') or 0) + is_correct
                pt = (_row.get('put_total') or 0)
                pc = (_row.get('put_correct') or 0)
            else:
                pt = (_row.get('put_total') or 0) + 1
                pc = (_row.get('put_correct') or 0) + is_correct
                ct = (_row.get('call_total') or 0)
                cc = (_row.get('call_correct') or 0)
            new_call_wr = round(100.0 * cc / ct, 1) if ct > 0 else None
            new_put_wr = round(100.0 * pc / pt, 1) if pt > 0 else None

            # FIX (CONFLUENCE-V1): avg_confidence is now a real running mean,
            # not the last signal's confidence (the old overwrite made the
            # column meaningless for /api/pair-deep-stats).
            _n = new_total
            _old_avg = existing['avg_confidence'] or 0
            _conf_num = float(confidence) if confidence is not None else 0.0
            new_avg_conf = round(((_old_avg * old_total) + _conf_num) / _n, 2) if _n > 0 else _conf_num

            # best_direction: only commit a direction once BOTH sides have
            # enough evidence. FIX: the old code set best_direction to the
            # OPPOSITE direction on a wrong first signal (zero evidence),
            # and compared EMA percentages thereafter.
            if ct >= 5 and pt >= 5 and (new_call_wr is not None) and (new_put_wr is not None):
                best_dir = 'CALL' if new_call_wr >= new_put_wr else 'PUT'
            else:
                best_dir = existing['best_direction']  # keep whatever we had (may be NULL)

            cur.execute("""
                UPDATE pair_hourly_patterns SET
                    session = ?, total_signals = ?, correct = ?, wrong = ?,
                    win_pct = ?, avg_confidence = ?,
                    best_direction = ?, call_win_pct = ?, put_win_pct = ?,
                    call_total = ?, call_correct = ?,
                    put_total = ?, put_correct = ?,
                    counted_keys = ?,
                    last_ctime = ?, last_updated = ?, ts = ?
                WHERE asset = ? AND hour_utc = ?
            """, (session, new_total, new_correct, new_wrong, new_win_pct,
                  new_avg_conf, best_dir, new_call_wr, new_put_wr,
                  ct, cc, pt, pc,
                  json.dumps(_counted),
                  int(ctime), ts_val, ts_val, asset, hour_utc))
        else:
            win_pct = 100.0 if is_correct else 0.0
            call_wr = 100.0 if (is_call and is_correct) else (0.0 if is_call else None)
            put_wr = 100.0 if (not is_call and is_correct) else (0.0 if not is_call else None)
            # FIX: no best_direction on a single sample — the old code guessed
            # the OPPOSITE direction when the first signal lost.
            best_dir = None
            ct = 1 if is_call else 0
            cc = is_correct if is_call else 0
            pt = 0 if is_call else 1
            pc = 0 if is_call else is_correct
            _conf_num = float(confidence) if confidence is not None else 0.0

            cur.execute("""
                INSERT INTO pair_hourly_patterns
                    (asset, hour_utc, session, total_signals, correct, wrong,
                     win_pct, avg_confidence, best_direction, call_win_pct,
                     put_win_pct, call_total, call_correct, put_total,
                     put_correct, last_ctime, counted_keys, last_updated, ts)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (asset, hour_utc, session, is_correct, 1 - is_correct,
                  win_pct, _conf_num, best_dir, call_wr, put_wr,
                  ct, cc, pt, pc, int(ctime), json.dumps(_counted), ts_val, ts_val))

        conn.commit()
    except Exception as e:
        print(f"[db] _update_hourly_pattern error: {e}")
    finally:
        conn.close()


def get_hourly_pattern(asset: str, hour_utc: int = None) -> dict:
    """Get hourly pattern data for a pair."""
    try:
        with _read_cursor() as cur:
            if hour_utc is not None:
                cur.execute("""
                    SELECT * FROM pair_hourly_patterns
                    WHERE asset = ? AND hour_utc = ?
                """, (asset, hour_utc))
                row = cur.fetchone()
                return dict(row) if row else None
            else:
                cur.execute("""
                    SELECT * FROM pair_hourly_patterns
                    WHERE asset = ?
                    ORDER BY hour_utc
                """, (asset,))
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[db] get_hourly_pattern error: {e}")
        return None


def get_time_confidence_adjustment(asset: str, hour_utc: int) -> dict:
    """Get confidence adjustment for a pair at a specific hour."""
    pattern = get_hourly_pattern(asset, hour_utc)
    if not pattern or pattern.get('total_signals', 0) < 5:
        return {
            'win_pct': None,
            'total': 0,
            'adjustment': 1.0,
            'reason': 'insufficient data',
            'best_direction': None,
        }

    win_pct = pattern.get('win_pct', 50)
    total = pattern.get('total_signals', 0)
    best_dir = pattern.get('best_direction')

    if win_pct >= 65:
        adjustment = 1.2
        reason = f' excellent at {hour_utc:02d}:00 UTC ({win_pct:.0f}%, n={total})'
    elif win_pct >= 55:
        adjustment = 1.1
        reason = f' good at {hour_utc:02d}:00 UTC ({win_pct:.0f}%, n={total})'
    elif win_pct >= 45:
        adjustment = 1.0
        reason = f' average at {hour_utc:02d}:00 UTC ({win_pct:.0f}%, n={total})'
    elif win_pct >= 35:
        adjustment = 0.8
        reason = f' poor at {hour_utc:02d}:00 UTC ({win_pct:.0f}%, n={total})'
    else:
        adjustment = 0.6
        reason = f' very poor at {hour_utc:02d}:00 UTC ({win_pct:.0f}%, n={total})'

    return {
        'win_pct': win_pct,
        'total': total,
        'adjustment': adjustment,
        'reason': reason,
        'best_direction': best_dir,
    }


def get_micro_history(asset, period, n=5, before_ctime=None):
    where_parts = ["asset=?", "period=?"]
    params = [asset, period]
    if before_ctime is not None:
        where_parts.append("ctime < ?")
        params.append(before_ctime)
    params.append(n)
    with _read_cursor() as c:
        q = (f"SELECT * FROM candle_micro WHERE {' AND '.join(where_parts)} "
             f"ORDER BY ctime DESC LIMIT ?")
        rows = c.execute(q, params).fetchall()
        return [dict(r) for r in reversed(rows)]


def get_recent_signals(asset, period, limit=50, before_ctime=None):
    """Return recent signals with full details for frontend history display.

    CONFLUENCE-V1 (2026-09-02): also returns the `strategy` column so the
    history can prove which strategy version produced each signal.
    """
    with _read_cursor() as c:
        base = """SELECT asset, period, ctime, signal, accuracy, score, confidence,
                   strength, agree, theories, actual, regime, zone,
                   tags, postmortem, right_codes, wrong_codes,
                   a_open, a_close, reasons, strategy
                   FROM signal_log
                   WHERE asset=? AND period=? AND signal IN ('CALL','PUT')"""
        params = [asset, period]
        if before_ctime is not None:
            base += " AND ctime < ?"
            params.append(before_ctime)
        base += " ORDER BY ctime DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(base, params).fetchall()
        return [dict(r) for r in reversed(rows)]


def get_recent_signals_all(period, limit=100, before_ctime=None, category=None):
    """Cross-pair signal history — USER REQ (2026-09-07):
    "প্রত্যেকটি সিগন্যাল হিস্টোরি দেখাতে হবে, কোনো সময়ে কোন সিগন্যাল টি দিলো".

    Returns the newest `limit` CALL/PUT signals across ALL allowlisted pairs
    (core.constants.ALLOWED_PAIRS) for one candle period, newest-first
    ordering preserved via the same oldest-first list contract as
    get_recent_signals (rows are reversed so callers can merge identically).

    Args:
        period: candle period seconds (e.g. 60).
        limit: max rows (server clamps to 500).
        before_ctime: pagination cursor (older-than).
        category: optional 'otc' | 'real' filter.
    """
    from core.constants import allowlist_sql_filter
    frag, pair_params = allowlist_sql_filter("asset", category)
    with _read_cursor() as c:
        base = f"""SELECT asset, period, ctime, signal, accuracy, score, confidence,
                   strength, agree, theories, actual, regime, zone,
                   tags, postmortem, right_codes, wrong_codes,
                   a_open, a_close, reasons, strategy
                   FROM signal_log
                   WHERE period=? AND signal IN ('CALL','PUT'){frag}"""
        params = [period] + list(pair_params)
        if before_ctime is not None:
            # FIX (ALL-CTIME-PAGINATION-2026-09-07, HIGH): cross-pair rows
            # SHARE one ctime (up to 16 pairs signal in the same minute in
            # every-candle mode). The old strict `ctime < before` cursor
            # dropped every same-ctime row that didn't fit on the previous
            # page — the "Load older" button silently lost signals forever.
            # Using `ctime <= before` re-delivers the boundary group; the
            # frontend dedupes by (asset|ctime) before merging, so the
            # overlap is harmless and nothing is lost.
            base += " AND ctime <= ?"
            params.append(before_ctime)
        base += " ORDER BY ctime DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(base, params).fetchall()
        return [dict(r) for r in reversed(rows)]


# ── Directional win rates (NEW 2026-08-31) ─────────────────────────────────
# FIX (WINRATE-API-2026-08-31): there was NO endpoint that ran
# "SELECT signal, accuracy GROUP BY asset, signal" — i.e. no exact per-pair,
# per-direction (CALL vs PUT) FINAL-signal win rate anywhere in the app.
# /api/stats only exposed per-MODULE-VOTE call/put rates, and
# pair_hourly_patterns used an EMA approximation. This function powers the
# new /api/winrate endpoint and the Win Rate dashboard tab in the frontend.

def get_directional_winrate(period=60, days=None, category=None,
                            min_ctime=None):
    """Per-pair, per-direction final-signal win rates from signal_log.

    Args:
        period: candle period in seconds (default 60).
        days:   optional lookback window in days (from now). None = all time.
        category: optional engine filter ('otc' | 'real'). None = all.
        min_ctime: optional explicit lower ctime bound (overrides days).

    Returns dict:
        {
          "pairs": [ { asset, category, total, correct, wrong, draws,
                       graded, win_pct,
                       call: {total, correct, win_pct},
                       put:  {total, correct, win_pct},
                       last_ctime, last_signal, last_accuracy,
                       streak_type, streak_count }, ... sorted by graded desc ],
          "overall": same shape with asset="ALL",
          "window_days": days or None,
        }

    FIX (CONFLUENCE-V1 2026-09-02): results are now restricted to the
    CURRENT 16-pair allowlist. Previously legacy rows for removed pairs
    (EURUSD_otc, GBPUSD_otc, ...) silently polluted the Win Rate dashboard
    until someone manually called /api/admin/prune-pairs — making the
    dashboard disagree with /api/stats (which IS allowlist-filtered).
    """
    cutoff = min_ctime
    if cutoff is None and days is not None:
        cutoff = time.time() - days * _SECONDS_PER_DAY

    # CONFLUENCE-V1: canonical allowlist (11 OTC + 5 Real).
    try:
        from core.constants import ALLOWED_PAIRS as _ALLOWED
        _allow = tuple(sorted(_ALLOWED))
    except Exception:
        _allow = None

    where = ["period = ?", "signal IN ('CALL','PUT')"]
    params = [period]
    if cutoff is not None:
        where.append("ctime > ?")
        params.append(cutoff)
    if category in ('otc', 'real'):
        where.append("category = ?")
        params.append(category)
    if _allow:
        where.append(f"asset IN ({','.join('?' * len(_allow))})")
        params.extend(_allow)
    where_sql = " AND ".join(where)

    # PERF-FIX (WINRATE-SQL-2026-09-07, HIGH): the old implementation loaded
    # EVERY matching signal_log row into Python (every-candle mode × 16 pairs
    # × 90-day retention ⇒ up to ~2M rows) and aggregated in a Python loop —
    # and winrate.js polls this every 20 seconds. Now:
    #   1. counts come from one SQL GROUP BY (≤ ~96 result rows), and
    #   2. streaks / last-signal come from a bounded window (last 150 rows
    #      per asset) via ROW_NUMBER() — with a graceful per-asset fallback
    #      for SQLite builds without window-function support.
    with _read_cursor() as c:
        grouped = c.execute(
            f"""SELECT asset, category, signal, accuracy, COUNT(*) AS n,
                       MAX(ctime) AS max_ctime
                FROM signal_log
                WHERE {where_sql}
                GROUP BY asset, category, signal, accuracy""",
            params,
        ).fetchall()

        recent = []
        try:
            recent = c.execute(
                f"""SELECT asset, ctime, signal, accuracy FROM (
                        SELECT asset, ctime, signal, accuracy,
                               ROW_NUMBER() OVER (
                                   PARTITION BY asset
                                   ORDER BY ctime DESC, id DESC) AS rn
                        FROM signal_log
                        WHERE {where_sql})
                    WHERE rn <= 150
                    ORDER BY asset, ctime, id""",
                params,
            ).fetchall()
        except sqlite3.Error:
            recent = []

    per = {}
    for g in grouped:
        a = g["asset"]
        d = per.setdefault(a, {
            "category": g["category"] or ('otc' if str(a).endswith('_otc') else 'real'),
            "total": 0, "correct": 0, "wrong": 0, "draws": 0, "graded": 0,
            "call_total": 0, "call_correct": 0,
            "put_total": 0, "put_correct": 0,
            "last_ctime": 0, "last_signal": None, "last_accuracy": None,
            "streak_type": None, "streak_count": 0,
        })
        n = g["n"] or 0
        acc = g["accuracy"]
        sig = g["signal"]
        d["total"] += n
        if g["max_ctime"] and g["max_ctime"] > d["last_ctime"]:
            d["last_ctime"] = g["max_ctime"]
        if acc == "draw":
            d["draws"] += n
        elif acc == "correct":
            d["correct"] += n
            d["graded"] += n
            if sig == "CALL":
                d["call_total"] += n
                d["call_correct"] += n
            else:
                d["put_total"] += n
                d["put_correct"] += n
        elif acc == "wrong":
            d["wrong"] += n
            d["graded"] += n
            if sig == "CALL":
                d["call_total"] += n
            else:
                d["put_total"] += n

    # Fallback for SQLite builds without window functions: fetch the last
    # 150 rows per asset with the classic LIMIT query (16 assets ⇒ 16 cheap
    # indexed lookups — still vastly cheaper than the old full scan).
    if not recent and per:
        recent = []
        with _read_cursor() as c:
            for a in per:
                recent_rows = c.execute(
                    f"""SELECT asset, ctime, signal, accuracy
                        FROM signal_log
                        WHERE {where_sql} AND asset = ?
                        ORDER BY ctime DESC, id DESC LIMIT 150""",
                    params + [a],
                ).fetchall()
                recent.extend(reversed(recent_rows))
        recent.sort(key=lambda r: (r["asset"], r["ctime"]))

    # Streaks + last_signal/last_accuracy from the bounded recent rows
    # (chronological order per asset; draws break nothing — they are skipped
    # exactly like the old full-history loop did).
    by_asset_recent = {}
    for r in recent or []:
        by_asset_recent.setdefault(r["asset"], []).append(r)
    for a, d in per.items():
        rows_a = by_asset_recent.get(a) or []
        if rows_a:
            last = rows_a[-1]
            d["last_ctime"] = max(d["last_ctime"], last["ctime"] or 0)
            d["last_signal"] = last["signal"]
            d["last_accuracy"] = last["accuracy"]
        for r in rows_a:
            acc = r["accuracy"]
            if acc not in ("correct", "wrong"):
                continue
            want = "win" if acc == "correct" else "loss"
            if d["streak_type"] == want:
                d["streak_count"] += 1
            else:
                d["streak_type"], d["streak_count"] = want, 1

    def _finalize(d, asset=""):
        graded = d["graded"]
        out = {
            "asset": asset or "ALL",
            "category": d["category"],
            "total": d["total"],
            "correct": d["correct"],
            "wrong": d["wrong"],
            "draws": d["draws"],
            "graded": graded,
            "win_pct": round(100.0 * d["correct"] / graded, 1) if graded else None,
            "call": {
                "total": d["call_total"],
                "correct": d["call_correct"],
                "win_pct": round(100.0 * d["call_correct"] / d["call_total"], 1)
                           if d["call_total"] else None,
            },
            "put": {
                "total": d["put_total"],
                "correct": d["put_correct"],
                "win_pct": round(100.0 * d["put_correct"] / d["put_total"], 1)
                           if d["put_total"] else None,
            },
            "last_ctime": d["last_ctime"],
            "last_signal": d["last_signal"],
            "last_accuracy": d["last_accuracy"],
            "streak_type": d["streak_type"],
            "streak_count": d["streak_count"],
        }
        return out

    pairs = sorted(
        (_finalize(d, asset=a) for a, d in per.items()),
        key=lambda p: (-p["graded"], -(p["win_pct"] or 0)),
    )

    # Overall rollup across every asset in the window.
    overall_d = {
        "category": "all",
        "total": 0, "correct": 0, "wrong": 0, "draws": 0, "graded": 0,
        "call_total": 0, "call_correct": 0,
        "put_total": 0, "put_correct": 0,
        "last_ctime": 0, "last_signal": None, "last_accuracy": None,
        "streak_type": None, "streak_count": 0,
    }
    for p in pairs:
        overall_d["total"] += p["total"]
        overall_d["correct"] += p["correct"]
        overall_d["wrong"] += p["wrong"]
        overall_d["draws"] += p["draws"]
        overall_d["graded"] += p["graded"]
        overall_d["call_total"] += p["call"]["total"]
        overall_d["call_correct"] += p["call"]["correct"]
        overall_d["put_total"] += p["put"]["total"]
        overall_d["put_correct"] += p["put"]["correct"]
        if p["last_ctime"] > overall_d["last_ctime"]:
            overall_d["last_ctime"] = p["last_ctime"]
            overall_d["last_signal"] = p["last_signal"]
            overall_d["last_accuracy"] = p["last_accuracy"]
    overall = _finalize(overall_d, asset="ALL")

    return {
        "pairs": pairs,
        "overall": overall,
        "window_days": days,
        "period": period,
        "category": category,
    }


def get_signal_detail(asset, period, ctime):
    """Return a single signal's full detail (for the reason modal)."""
    cols = ("ctime, signal, accuracy, score, confidence, strength, agree, "
            "total, theories, actual, regime, zone, tags, postmortem, "
            "right_codes, wrong_codes, a_open, a_close, reasons, category")
    with _read_cursor() as c:
        row = c.execute(
            f"SELECT {cols} FROM signal_log "
            "WHERE asset=? AND period=? AND ctime=? LIMIT 1",
            (asset, period, ctime),
        ).fetchone()
        return dict(row) if row else None


def recent_accuracy(asset, period, n=20):
    """Return (accuracy_float, sample_count) over the last N graded signals."""
    seven_days_ago = time.time() - 7 * _SECONDS_PER_DAY
    with _read_cursor() as c:
        rows = c.execute("""SELECT accuracy
                   FROM signal_log
                   WHERE asset=? AND period=? AND signal IN ('CALL','PUT')
                     AND accuracy IN ('correct','wrong')
                     AND ctime > ?
                   ORDER BY ctime DESC, id DESC LIMIT ?""",
                   (asset, period, seven_days_ago, n)).fetchall()
    if not rows:
        return None, 0
    correct = sum(1 for r in rows if r["accuracy"] == "correct")
    total = len(rows)
    return correct / total, total


def per_module_accuracy(asset, period=60, n=1000):
    """Return per-module accuracy for a given (asset, period).

    STRAT-FIX 2026-09-09:
    * TIME WINDOW: the old query had NO time filter despite the adapter's
      "7-day rolling window" doc — LIMIT alone made the window ~3 hours on
      production (every-candle mode logs ~1 signal/minute). Now bounded to
      the last QX_WIN_RATE_WINDOW_DAYS days (default 7) AND n rows.
    * DIRECTION SPLITS: returns call/put sub-stats so the engine can apply
      per-DIRECTION module weights (production case: USDCOP_otc sr_bounce
      CALL 53.6% vs PUT 30.3% — a single aggregate weight destroys this
      information).
    """
    import time as _time
    window_days = int(os.environ.get("QX_WIN_RATE_WINDOW_DAYS", "7"))
    cutoff = _time.time() - window_days * 86400
    out = {m: {"correct": 0, "wrong": 0, "total": 0, "win_rate": None,
               "call_correct": 0, "call_total": 0, "call_win_rate": None,
               "put_correct": 0, "put_total": 0, "put_win_rate": None}
           for m in _MODULE_NAMES}

    with _read_cursor() as c:
        rows = c.execute("""SELECT signal, accuracy, reasons
                   FROM signal_log
                   WHERE asset=? AND period=? AND signal IN ('CALL','PUT')
                     AND accuracy IN ('correct','wrong') AND ctime >= ?
                   ORDER BY ctime DESC, id DESC LIMIT ?""",
                   (asset, period, cutoff, n)).fetchall()

    if not rows:
        return out

    _MODULE_RE = re.compile(r"^\[([^\]]+)\]")

    for row in rows:
        final_signal = row["signal"]
        accuracy = row["accuracy"]
        reasons_raw = row["reasons"] if row["reasons"] is not None else "[]"
        try:
            reasons = json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw
        except (ValueError, TypeError):
            reasons = []
        if not isinstance(reasons, list):
            reasons = []

        for reason in reasons:
            if not isinstance(reason, str):
                continue
            m_match = _MODULE_RE.match(reason)
            if not m_match:
                continue
            module = m_match.group(1).strip()
            if module not in _MODULE_NAMES:
                continue
            upper = reason.upper()
            call_hits = sum(1 for k in ("CALL", "BULL", "BUYER") if k in upper)
            put_hits = sum(1 for k in ("PUT", "BEAR", "SELLER") if k in upper)
            if call_hits > put_hits:
                module_dir = "CALL"
            elif put_hits > call_hits:
                module_dir = "PUT"
            else:
                continue

            if accuracy not in ("correct", "wrong"):
                continue
            slot = out[module]
            slot["total"] += 1
            hit = ((module_dir == final_signal and accuracy == "correct")
                   or (module_dir != final_signal and accuracy == "wrong"))
            if hit:
                slot["correct"] += 1
            else:
                slot["wrong"] += 1
            if module_dir == "CALL":
                slot["call_total"] += 1
                if hit:
                    slot["call_correct"] += 1
            else:
                slot["put_total"] += 1
                if hit:
                    slot["put_correct"] += 1

    for m in _MODULE_NAMES:
        s = out[m]
        if s["total"] > 0:
            s["win_rate"] = min(1.0, max(0.0, s["correct"] / s["total"]))
        if s["call_total"] > 0:
            s["call_win_rate"] = min(1.0, max(0.0, s["call_correct"] / s["call_total"]))
        if s["put_total"] > 0:
            s["put_win_rate"] = min(1.0, max(0.0, s["put_correct"] / s["put_total"]))

    return out


def delete_signal(asset: str, period: int, ctime: int) -> bool:
    """Delete a single signal by (asset, period, ctime)."""
    with _write_cursor() as c:
        c.execute(
            "DELETE FROM signal_log WHERE asset=? AND period=? AND ctime=?",
            (asset, period, ctime),
        )
        return c.rowcount > 0


def clear_signals(asset=None, period=None, before_ctime=None):
    """Clear signals, optionally filtered by asset/period/before_ctime."""
    q = "DELETE FROM signal_log WHERE 1=1"
    params = []
    if asset:
        q += " AND asset=?"
        params.append(asset)
    if period is not None:
        q += " AND period=?"
        params.append(period)
    if before_ctime is not None:
        q += " AND ctime < ?"
        params.append(before_ctime)
    with _write_cursor() as c:
        c.execute(q, params)
        return c.rowcount


def clear_all_signals():
    """Delete ALL signals from signal_log. Returns count deleted."""
    with _write_cursor() as c:
        c.execute("DELETE FROM signal_log")
        return c.rowcount


def cleanup(days=None):
    """Delete rows older than `days`. Returns (deleted_candle_micro, deleted_signal_log).

    FIX (CONFLUENCE-V1 2026-09-02): default retention raised 7 → 90 days
    (env QX_RETENTION_DAYS). The old 7-day cleanup ran at startup + every 6h
    and silently made the UI's "30 days" / "All time" chips cap at 7 days —
    the win-rate dashboard could never show what it claimed. 90 days keeps
    those windows meaningful while still bounding DB growth.
    """
    if days is None:
        try:
            days = int(os.environ.get("QX_RETENTION_DAYS", "90"))
        except ValueError:
            days = 90
    if not isinstance(days, int) or days < 1:
        raise ValueError(f"cleanup: days must be a positive int, got {days!r}")

    cutoff = time.time() - timedelta(days=days).total_seconds()
    cutoff_int = int(cutoff)
    BATCH = 1000

    deleted_cm = 0
    deleted_sl = 0
    conn = _conn()
    try:
        cur = conn.cursor()
        while True:
            cur.execute(
                "DELETE FROM candle_micro WHERE rowid IN ("
                "    SELECT rowid FROM candle_micro WHERE ctime < ? LIMIT ?"
                ")",
                (cutoff_int, BATCH),
            )
            n = cur.rowcount
            conn.commit()
            deleted_cm += n
            if n < BATCH:
                break
        while True:
            cur.execute(
                "DELETE FROM signal_log WHERE id IN ("
                "    SELECT id FROM signal_log WHERE ctime < ? LIMIT ?"
                ")",
                (cutoff_int, BATCH),
            )
            n = cur.rowcount
            conn.commit()
            deleted_sl += n
            if n < BATCH:
                break
    finally:
        conn.close()

    if deleted_cm or deleted_sl:
        print(f"[db] cleanup: removed {deleted_cm} candle_micro + "
              f"{deleted_sl} signal_log rows older than {days}d")
    return deleted_cm, deleted_sl


def prune_non_allowlist_assets(dry_run: bool = False) -> dict:
    """Delete rows for assets NOT in the 15-pair allowlist.

    FIX (PAIR-ALLOWLIST-2026-08-07 / A-14 #7): signal_log and 13 other tables
    were never pruned by allowlist — only by age. Rows for ~14 removed pairs
    (EURUSD_otc, USDCHF_otc, USDJPY_otc, USDARS_otc, USDBRL_otc, USDSGD_otc,
    USDCNH_otc, USDTHB_otc, USDRUB_otc, EURGBP_otc, GBPUSD_otc, USDCAD_otc,
    EURJPY_otc, GBPJPY_otc, EURAUD_otc) persisted forever and resurfaced in
    every stats query. This function deletes them.

    Returns a dict mapping table_name -> rows_deleted.
    Set dry_run=True to preview counts without deleting.
    """
    from core.constants import ALLOWED_PAIRS, ALLOWED_PAIRS_OTC, ALLOWED_PAIRS_REAL
    # Tables with an `asset` column that should be filtered.
    _ASSET_TABLES = [
        "signal_log",
        "candle_micro",
        "module_votes",
        "theory_votes",
        "pair_hourly_patterns",
        "time_session_patterns",
        "brain_predictions",
        "brain_module_votes",
        "brain_learning",
        "agent_models",
        "algorithm_changes",
        "pair_performance_daily",
        "quotex_algo_patterns",
    ]
    allowed = tuple(ALLOWED_PAIRS)
    if not allowed:
        return {"error": "ALLOWED_PAIRS is empty"}

    conn = _conn()
    cur = conn.cursor()
    results: dict = {}
    try:
        for table in _ASSET_TABLES:
            # Verify the table and column exist before deleting.
            try:
                cur.execute(f"PRAGMA table_info({table})")
                cols = [row[1] for row in cur.fetchall()]
            except sqlite3.OperationalError:
                results[table] = "table not found"
                continue
            if "asset" not in cols:
                results[table] = "no asset column"
                continue

            # Count rows to be deleted
            placeholders = ",".join("?" * len(allowed))
            cur.execute(
                f"SELECT COUNT(*) FROM {table} WHERE asset NOT IN ({placeholders})",
                allowed,
            )
            count = cur.fetchone()[0]
            if count == 0:
                results[table] = 0
                continue

            if dry_run:
                results[table] = f"would delete {count}"
            else:
                # Delete in batches to avoid locking the DB for too long.
                BATCH = 1000
                deleted = 0
                while True:
                    cur.execute(
                        f"DELETE FROM {table} WHERE rowid IN ("
                        f"    SELECT rowid FROM {table} "
                        f"    WHERE asset NOT IN ({placeholders}) LIMIT ?"
                        f")",
                        allowed + (BATCH,),
                    )
                    n = cur.rowcount
                    conn.commit()
                    deleted += n
                    if n < BATCH:
                        break
                results[table] = deleted

        # Special case: brain_insights has `applies_to` instead of `asset`
        try:
            cur.execute("PRAGMA table_info(brain_insights)")
            cols = [row[1] for row in cur.fetchall()]
            if "applies_to" in cols:
                placeholders = ",".join("?" * len(allowed))
                cur.execute(
                    f"SELECT COUNT(*) FROM brain_insights WHERE applies_to NOT IN ({placeholders})",
                    allowed,
                )
                count = cur.fetchone()[0]
                if count > 0:
                    if dry_run:
                        results["brain_insights"] = f"would delete {count}"
                    else:
                        cur.execute(
                            f"DELETE FROM brain_insights WHERE applies_to NOT IN ({placeholders})",
                            allowed,
                        )
                        conn.commit()
                        results["brain_insights"] = cur.rowcount
                else:
                    results["brain_insights"] = 0
        except sqlite3.OperationalError:
            pass

        if not dry_run:
            # Vacuum to reclaim space
            try:
                conn.commit()
                cur.execute("VACUUM")
                results["_vacuum"] = "ok"
            except sqlite3.OperationalError as e:
                results["_vacuum"] = f"failed: {e}"

        return results
    finally:
        conn.close()
