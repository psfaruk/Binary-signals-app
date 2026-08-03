"""
Lightweight SQLite persistence layer.
Tables: candle_micro, signal_log
"""
import json
import re
import sqlite3
import os
import time
from datetime import timedelta
import threading
from contextlib import contextmanager

# FIX (DEEP-AUDIT-2026-07-26 / F-14-01): guard against `__file__` being
# empty (REPL, exec()) — previously joined to "signals.db" in cwd which
# could overwrite an unrelated file.
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__) or ".", "signals.db")),
)

# FIX (LIVE-TICK-RELIABILITY-2026-07-31): DB_PATH can now point at
# /app/data/signals.db (Railway volume mount, see railway.json). If the
# operator hasn't added the Volume yet, /app/data doesn't exist and
# sqlite3.connect() would crash the whole app on startup. Create the
# directory defensively — this does NOT make data persistent by itself
# (a plain directory on the ephemeral filesystem also "exists"), it only
# prevents a hard crash. See _log_persistence_status() below for the
# actual persistence check.
try:
    _db_dir = os.path.dirname(DB_PATH)
    if _db_dir:
        os.makedirs(_db_dir, exist_ok=True)
except Exception as _mkdir_exc:
    print(f"[db] WARNING: could not create DB_PATH directory {DB_PATH!r}: {_mkdir_exc}")


def _log_persistence_status() -> None:
    """Log a boot counter next to signals.db so Railway logs make it
    obvious whether the data directory is really persisting across
    redeploys, instead of silently trusting the dashboard Volume
    checkbox. Call once at startup, after init() has created signals.db.

    Interpretation (see this printed line in Railway's deploy logs):
      boot_count == 1 on the FIRST ever deploy: normal.
      boot_count == 1 on EVERY redeploy after that: the volume is NOT
        actually mounted at DB_PATH's directory — data is being wiped
        every time, even if a Volume exists elsewhere in the dashboard.
      boot_count > 1 and increasing: persistence is working correctly.
    """
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

# FIX (DEEP-AUDIT-2026-07-26 / F-14-02): previously a global `_lock`
# serialized ALL writes across 38+ streams through one threading.Lock
# (db.py L241, L296). Throughput ceiling was ~1 write per commit+fsync
# (~5-20ms) = ~50-200 writes/sec. WAL mode + busy_timeout=10s already
# serializes writes safely at the SQLite layer. The global lock has
# been removed from save()/log_signal(); a dedicated lock is kept only
# for the rare multi-statement migration transaction in init()
# (single-threaded, low contention).
_migration_lock = threading.Lock()

# FIX (DEEP-AUDIT-2026-07-26 / F-14-17): allow-lists for input
# validation in log_signal(). Previously any text could be inserted
# into `signal` / `accuracy`, silently corrupting analytics. SQLite
# can't ALTER ADD CHECK on an existing table without a full rebuild,
# so we validate in Python instead.
_VALID_SIGNALS = ("CALL", "PUT", "DRAW", "PENDING")
_VALID_ACCURACY = ("correct", "wrong", "draw", "pending", None)
_SECONDS_PER_DAY = 86400  # FIX (F-14-30): named constant for magic 86400


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-03): row_factory set on every
    # connection (previously set in _cursor() only — non-cursor callers
    # of _conn() in core/stats.py, core/auto_tune.py, core/time_patterns.py
    # had to set it themselves). Now every connection gets Row factory.
    conn.row_factory = sqlite3.Row
    # WAL mode: allows concurrent reads during writes — critical for
    # avoiding lock contention when 38 streams close simultaneously.
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-04): previously bare
    # `except Exception: pass` swallowed PRAGMA errors silently,
    # causing fallback to slow DELETE journal mode with no log. Now
    # logged so operators can spot read-only FS / permission issues.
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-04b): added busy_timeout=10000
    # so concurrent writes wait up to 10s instead of raising
    # `database is locked` immediately.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except sqlite3.Error as e:
        print(f"[db] PRAGMA setup failed (falling back to defaults): {e}")
    return conn


@contextmanager
def _read_cursor():
    """Read-only cursor — no commit, no fsync overhead.

    FIX (DEEP-AUDIT-2026-07-26 / F-14-05): split out from _cursor()
    so SELECT-only callers (get_micro_history, get_recent_signals,
    get_signal_detail, recent_accuracy, per_module_accuracy) skip the
    spurious commit() at context exit. Previously every SELECT triggered
    an fsync via commit() — wasteful on a hot DB.
    """
    conn = _conn()
    cur = conn.cursor()
    try:
        yield cur
    finally:
        conn.close()


@contextmanager
def _write_cursor():
    """Write cursor — commits on success, rolls back on exception.

    FIX (DEEP-AUDIT-2026-07-26 / F-14-06): previously _cursor() committed
    unconditionally at context exit and relied on conn.close() to
    auto-rollback on exception. Now we rollback explicitly and re-raise
    so the caller sees the error.
    """
    conn = _conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception as _e:
            print(f"[silent-except] db.py:102 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
            pass
        raise
    finally:
        conn.close()


# Backward-compatible alias: _cursor() → _write_cursor(). External
# callers (none in this repo, but kept for safety) get commit-on-exit.
_cursor = _write_cursor


def init():
    _log_persistence_status()
    with _cursor() as c:
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-1-28): metadata table for
        # one-time migration tracking. Previously the signal_log dedup
        # query (a correlated O(n²) subquery) ran on EVERY startup. For a
        # DB with 100k+ rows this took minutes, blocking lifespan startup
        # and causing Railway health checks to time out and restart the
        # container. Now we record 'signal_log_dedup_done' in _meta so the
        # expensive dedup runs only once per DB.
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
            category TEXT,        -- FIX (2026-07-17): track which engine produced this signal
            ts REAL)""")
        # FIX (DEEP-AUDIT-2026-07-26 / F-14-09): removed the schema-level
        # `DEFAULT (strftime('%s','now'))` for `ts` — strftime('%s','now')
        # is locale-fragile and returns NULL/empty on some older SQLite
        # Windows builds. log_signal() now sets ts explicitly via Python
        # time.time() so a NULL default never leaks into graded rows.
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-3-18): add `total`
        # column so the agreement ratio (agree/total) can be computed from
        # the DB. Previously only `agree` was stored, making it impossible
        # to distinguish 4/6 agreement from 4/10.
        try:
            cols = [row["name"] for row in c.execute("PRAGMA table_info(signal_log)").fetchall()]
            if "total" not in cols:
                c.execute("ALTER TABLE signal_log ADD COLUMN total INT")
                print("[db] migrated signal_log: added `total` column")
        except Exception as _e:
            print(f"[db] signal_log `total` column migration skipped: {_e}")
        # Indexes — composite ctime index covers (asset, period) prefix
        # lookups too, so no separate ix_sl_asset_period is needed.
        # FIX (DEEP-AUDIT-2026-07-26 / F-14-07): dropped redundant
        # `ix_sl_asset_period` — it's a strict prefix of the composite
        # `ix_sl_ctime` so the query planner never picks it; pure write
        # overhead (every INSERT updated 2 indexes instead of 1).
        c.execute("DROP INDEX IF EXISTS ix_sl_asset_period")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sl_ctime ON signal_log(asset, period, ctime DESC)")
        # FIX (DEEP-AUDIT-2026-07-26 / F-14-08): dropped dead `ix_sl_ts`
        # index. Per cleanup() at the bottom of this file, deletes use
        # `ctime` (not `ts`), and no query in db.py filters/orders by ts.
        # The index wasted write throughput and disk space.
        c.execute("DROP INDEX IF EXISTS ix_sl_ts")
        # FIX (CRASH-FIX-2026-07-26 / DB-001): the `category` column
        # migration (lines 301-318 below) must run BEFORE this index is
        # created, otherwise any legacy DB created before 2026-07-17 is
        # missing the `category` column and `CREATE INDEX ix_sl_category
        # ON signal_log(category, ...)` raises `no such column: category`.
        # The except clause in init() rolled back the ENTIRE migration
        # transaction, causing feed.py lifespan startup to crash, which
        # manifested as the Railway container restart loop. Now we move
        # the category-column migration to BEFORE the index creation.
        # The migration is idempotent (PRAGMA table_info + ADD COLUMN).
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
        # FIX (2026-07-17): index for per-category accuracy queries.
        c.execute("CREATE INDEX IF NOT EXISTS ix_sl_category ON signal_log(category, asset, period)")

        # FIX (AUDIT-CRITICAL #003, 2026-07-21): add UNIQUE constraint on
        # (asset, period, ctime) so the same candle can NEVER produce two
        # signal_log rows. Previously a watchdog restart or a race between
        # the timer-close path and the tick-close path could call
        # _grade_and_log twice for the same candle, inserting duplicate rows.
        # These duplicates inflated the user-visible signal count (one of the
        # causes of the '48-signal limit' symptom: the DB had MORE rows than
        # the user saw, because the frontend's HISTORY_MAX clipped them).
        # We use a UNIQUE INDEX (not a table-level UNIQUE constraint) so we
        # can drop + recreate it during migration without rebuilding the table.
        #
        # Migration steps:
        #   1. Detect existing duplicate rows for the same (asset, period, ctime).
        #   2. If duplicates exist, keep only the LATEST one (MAX(id)) and
        #      delete the rest — the latest has the most-complete postmortem.
        #   3. Drop any legacy UNIQUE indexes (from prior schemas) so the
        #      new one can be created cleanly.
        #   4. Create the UNIQUE index.
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-1-28): gate the O(n²)
        # dedup query on a _meta flag so it only runs ONCE per DB, not on
        # every startup. On a 100k-row DB this query took 2-5 minutes,
        # causing Railway health checks to time out and restart the
        # container in a loop.
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
            # FIX (CRASH-FIX-2026-07-26 / DB-003): the _meta dedup_done flag
            # was INSERTed here, INSIDE the transaction that also holds the
            # dedup DELETE — and that transaction commits BEFORE the unique
            # index CREATE at Step 4 runs. If the index CREATE then failed
            # (duplicates somehow survived, or another error), the dedup
            # flag was already persisted — subsequent boots would skip dedup
            # and the unique-index CREATE would fail FOREVER, forcing the
            # broken non-unique fallback. Now we move the _meta INSERT to
            # AFTER the unique-index CREATE succeeds. If init() is rerun,
            # the dedup runs again — idempotent (DELETE of zero rows).
        else:
            # Dedup already ran on a prior startup — skip.
            pass

        # Step 3: drop legacy UNIQUE indexes (best-effort).
        # FIX (AUDIT-CRITICAL #008, 2026-07-21): older deployments may have
        # created UNIQUE indexes under different names. Drop them so the
        # new INSERT OR REPLACE works correctly.
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
            print(f"[silent-except] db.py:295 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
            pass

        # Step 4: create the canonical UNIQUE index.
        # FIX (DEEP-AUDIT-2026-07-26 / F-14-12): previously the failure
        # was silently logged and init() proceeded with NO unique
        # constraint — re-introducing the duplicate-row bug the index
        # was supposed to prevent. Now we log loudly AND fall back to a
        # non-unique index so writes don't fail, but operators see the
        # warning in logs (the dedup at Step 1+2 must be re-run manually
        # to fix the underlying duplicates).
        try:
            c.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_sl_asset_period_ctime
                ON signal_log(asset, period, ctime)
            """)
            # FIX (CRASH-FIX-2026-07-26 / DB-003): only persist the dedup_done
            # _meta flag AFTER the unique index CREATE succeeded. If the
            # CREATE fails, we'll fall through to the except below and the
            # dedup will rerun on the next boot (idempotent — DELETE of zero
            # rows is a no-op).
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
                print(f"[silent-except] db.py:331 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
                pass

        # FIX (2026-07-17): the `category` column migration was moved ABOVE
        # to run BEFORE `ix_sl_category` index creation (see DB-001 fix).
        # The block that used to live here is now redundant.

        # Refactor (2026-07-14): the `theory_votes` table is no longer
        # populated. The old theory engine was replaced by the
        # candle_reaction / advanced_analysis prediction path which doesn't
        # emit per-theory votes. Drop the table + its indexes if they still
        # exist from an older schema, and clean up orphaned rows.
        # FIX (DEEP-AUDIT-2026-07-26 / F-14-10): previously this DROP
        # block ran on EVERY startup — three no-op SQL statements per
        # boot after the first successful drop. Now gated on a _meta flag
        # so it runs exactly once per DB.
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

        # ── NEW TABLE: module_votes (DEEP_v2 2026-07-30) ──────────────────
        # Tracks each individual module's vote per signal, enabling:
        # - Per-module per-pair per-direction accuracy analysis
        # - Auto-calibration (weight tuning based on live performance)
        # - Module-level debugging (why did this signal fail?)
        # Previously module votes were embedded in signal_log.reasons as JSON
        # text — required regex parsing to analyze. Now queryable directly.
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

        # ── NEW TABLE: signal_quality_metrics (DEEP_v2 2026-07-30) ────────
        # Detailed quality metrics per signal for advanced analysis:
        # - Move magnitude (ATR %)
        # - Tick statistics
        # - Time-of-day / session info
        # - Module agreement patterns
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

        # ── NEW TABLE: pair_performance_daily (DEEP_v2 2026-07-30) ────────
        # Daily rollup of pair performance for trend analysis
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

        # ── NEW TABLE: pair_hourly_patterns (TIME-OF-DAY 2026-07-30) ────────
        # Tracks per-pair per-hour win rate to detect time-based patterns.
        # User insight: "কিছু পেয়ার 70% উইন রেট পায়, কিন্তু দিনের ভিন্ন
        # সময়ে 30% এর নিচে পায়। আবার খারাপ পেয়ার ভালো সময়ে 60% পায়।"
        # This table enables:
        # - Time-aware confidence adjustment (brain uses this to boost/dampen)
        # - Quotex algorithm detection (repeating time-based manipulation)
        # - "Best time to trade X pair" recommendations
        # Hour is UTC 0-23. Session names: asian(00-07), london(07-12),
        # ny(12-17), overlap(12-16), off(17-24).
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

        # ── NEW TABLE: quotex_algo_patterns (ALGO DETECTION 2026-07-30) ────
        # Detects Quotex's time-based manipulation patterns:
        # - Pairs that consistently reverse at specific hours
        # - "Trap" hours where signals systematically fail
        # - Direction bias per hour (does Quotex favor CALL or PUT at hour X?)
        # This is the user's insight: "কোয়েটেক্স এর এলগরিদম ডিটেক্ট করা সহজ হবে"
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
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-13): removed the global `_lock`
    # around execute+commit. WAL + busy_timeout=10s (set in _conn())
    # already serializes writes safely at the SQLite layer. The global
    # lock was a contention bottleneck for 38+ concurrent streams.
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-14): broadened the exception
    # catch from `sqlite3.Error` only to also include KeyError, TypeError,
    # ValueError — previously a missing `closed["open"]` key or a
    # non-JSON-serializable value in micro would propagate up and crash
    # the stream. Now logged and the row is skipped (acceptable for a
    # single candle_micro row).
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
                 # FIX (CRASH-FIX-2026-07-26 / DB-004 + DB-005): the
                 # `.get('round', {})` default only fires when the 'round'
                 # key is MISSING. If callers pass `round=None` explicitly
                 # (which JSON `null` round-trips to), `.get('round')`
                 # returns None and `.get('near_level')` raises
                 # AttributeError, which was NOT in the except tuple
                 # (KeyError, TypeError, ValueError, sqlite3.Error) —
                 # propagating up and crashing the asset stream. Now we
                 # coerce None → {} via `or {}`.
                 (micro.get("round") or {}).get("near_level"),
                 (micro.get("round") or {}).get("near_strength"),
                 micro.get("gap_pct"), micro.get("gap_type"),
                 _as_text(micro.get("key_levels")), _as_text(micro.get("ticks_json"))))
            conn.commit()
        except (sqlite3.Error, KeyError, TypeError, ValueError, AttributeError) as e:
            # FIX (DB-004): added AttributeError to the except tuple so
            # NoneType.get() no longer crashes the stream. Operational
            # errors (locked, disk full, corruption) and data-shape errors
            # (missing key, non-serializable value) are logged but NOT
            # re-raised — losing a single candle_micro row is preferable to
            # crashing the stream. Logged with the specific error class so
            # operators can spot patterns.
            print(f"[db] save {type(e).__name__}: {e}")
            try:
                conn.rollback()
            except Exception as _e:
                print(f"[silent-except] db.py:425 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
                pass
    finally:
        conn.close()


def _category_for_asset(asset):
    """Detect engine category from asset name. Single source of truth.

    FIX (DEEP-AUDIT-2026-07-26 / F-14-15): previously the rule
    `category = "otc" if asset.endswith("_otc") else "real"` was duplicated
    between the migration backfill (db.py L203-204) and log_signal()
    (db.py L302-304). Now both call this helper so the rule can change
    in one place.
    """
    return "otc" if asset.endswith("_otc") else "real"


def log_signal(asset, period, ctime, signal, score, confidence,
               theories, actual, accuracy, **kw):
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-16): use UPSERT (INSERT ... ON
    # CONFLICT DO UPDATE) instead of INSERT OR REPLACE. The old REPLACE
    # semantics (DELETE+INSERT) destroyed id stability — re-graded signals
    # got NEW autoincrement ids, silently breaking external references
    # (logs, frontend URLs, /api/signals/{asset}/{period}/{ctime} ordering).
    # UPSERT preserves the existing id when updating a row for the same
    # (asset, period, ctime).
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-13): removed the global `_lock`
    # around execute+commit (WAL + busy_timeout handles concurrency).
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-14): broadened exception catch to
    # include TypeError, ValueError (e.g. _as_text() on a non-JSON-
    # serializable object).
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-09): set ts explicitly via
    # Python time.time() — the schema default strftime('%s','now') is
    # locale-fragile on some SQLite builds (and the default has been
    # removed from the schema).
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-17): validate `signal` and
    # `accuracy` against an allow-list. Previously any text could be
    # inserted, silently corrupting analytics.
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-18): fallback `total_val` to
    # `agree` count when caller omits it — prevents NULL divisions in
    # downstream agree/total queries.
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
        # FIX (F-14-18): fallback to `agree` count so agree/total doesn't
        # produce NULL when callers omit `total`.
        total_val = kw.get("agree") or 0
    ts_val = time.time()

    conn = _conn()
    try:
        try:
            cur = conn.cursor()
            # FIX (CRASH-FIX-2026-07-26 / DB-002): when the UNIQUE index
            # creation failed earlier (init() falls back to non-unique),
            # `ON CONFLICT(asset, period, ctime)` raises
            # "ON CONFLICT clause does not match a PRIMARY or UNIQUE
            # constraint" and signals silently stop being logged. Detect
            # this and retry with INSERT OR REPLACE which works with any
            # index (or none). The downside — id stability — only matters
            # when the unique index exists, which is the happy path.
            try:
                cur.execute("""
                    INSERT INTO signal_log
                        (asset,period,ctime,signal,score,confidence,theories,
                         actual,accuracy,strength,agree,right_codes,wrong_codes,
                         reasons,a_open,a_close,regime,zone,tags,postmortem,
                         category,total,ts)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                        ts=excluded.ts
                    """,
                    (asset, period, ctime, signal, score, confidence, _as_text(theories),
                     actual, accuracy,
                     kw.get("strength"), kw.get("agree"),
                     _as_text(kw.get("right_codes")), _as_text(kw.get("wrong_codes")),
                     _as_text(kw.get("reasons")),
                     kw.get("a_open"), kw.get("a_close"),
                     kw.get("regime"), kw.get("zone"),
                     _as_text(kw.get("tags")), kw.get("postmortem"),
                     category, total_val, ts_val))
            except sqlite3.Error as _conflict_err:
                if "ON CONFLICT" in str(_conflict_err) and "UNIQUE" in str(_conflict_err).upper():
                    # Fallback path: no unique constraint exists, use
                    # INSERT OR REPLACE (works with any index, preserves
                    # the at-most-one-row-per-(asset,period,ctime) intent
                    # at the cost of id stability on update).
                    cur.execute("""
                        INSERT OR REPLACE INTO signal_log
                            (asset,period,ctime,signal,score,confidence,theories,
                             actual,accuracy,strength,agree,right_codes,wrong_codes,
                             reasons,a_open,a_close,regime,zone,tags,postmortem,
                             category,total,ts)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (asset, period, ctime, signal, score, confidence, _as_text(theories),
                         actual, accuracy,
                         kw.get("strength"), kw.get("agree"),
                         _as_text(kw.get("right_codes")), _as_text(kw.get("wrong_codes")),
                         _as_text(kw.get("reasons")),
                         kw.get("a_open"), kw.get("a_close"),
                         kw.get("regime"), kw.get("zone"),
                         _as_text(kw.get("tags")), kw.get("postmortem"),
                         category, total_val, ts_val))
                else:
                    raise
            conn.commit()

            # ── NEW (DEEP_v2 2026-07-30): write module_votes rows ──────────
            # Parse reasons JSON to extract per-module votes and store them
            # in the module_votes table for queryable analysis.
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
                    
                    # FIX (MODULE-PRUNE-2026-08-03): removed indicator, otc_pattern, trend_follow
                    # from the regex — these modules have been deleted.
                    mod_pat = _re.compile(r'\[(candle_reaction|running_tick|pattern|key_level)\]')
                    parts = _re.split(mod_pat, r_text)
                    seen = set()
                    vote_rows = []
                    for i in range(1, len(parts), 2):
                        mod = parts[i]
                        content = parts[i+1] if i+1 < len(parts) else ''
                        dir_match = _re.search(r'→\s*(CALL|PUT)\b', content)
                        if not dir_match:
                            continue
                        direction = dir_match.group(1)
                        if (mod, direction) in seen:
                            continue
                        seen.add((mod, direction))
                        
                        vote_correct = None
                        if actual and actual in ('UP', 'DOWN'):
                            vote_correct = 1 if (
                                (direction == 'CALL' and actual == 'UP') or
                                (direction == 'PUT' and actual == 'DOWN')
                            ) else 0
                        
                        vote_rows.append((
                            None, asset, period, ctime, mod, direction,
                            vote_correct, None, None, None,
                            category, kw.get('regime'), kw.get('strength'), ts_val
                        ))
                    
                    if vote_rows:
                        cur.executemany("""INSERT INTO module_votes
                            (signal_id, asset, period, ctime, module_name, direction,
                             vote_correct, score, confidence, signal_group,
                             engine, regime, strength, ts)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            vote_rows)
                        conn.commit()
            except Exception as _mv_err:
                print(f"[db] module_votes write skipped: {_mv_err}")

        except (sqlite3.Error, TypeError, ValueError) as e:
            print(f"[db] log_signal {type(e).__name__}: {e}")
            try:
                conn.rollback()
            except Exception as _e:
                print(f"[silent-except] db.py:565 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
                pass
    finally:
        conn.close()

    # ── NEW (TIME-OF-DAY 2026-07-30): update pair_hourly_patterns ────────
    # After logging the signal, update the hourly pattern stats so the brain
    # can use time-aware confidence adjustment. Runs in a separate connection
    # to avoid holding the main write transaction open.
    try:
        _update_hourly_pattern(asset, ctime, signal, accuracy, confidence)
    except Exception as _hp_err:
        print(f"[db] hourly pattern update skipped: {_hp_err}")


def _get_session_name(hour_utc: int) -> str:
    """Map UTC hour to trading session name."""
    if 0 <= hour_utc < 7:
        return "asian"
    elif 7 <= hour_utc < 12:
        return "london"
    elif 12 <= hour_utc < 17:
        return "ny"  # NY + London overlap
    else:
        return "off"


def _update_hourly_pattern(asset: str, ctime: int, signal: str,
                           accuracy: str, confidence):
    """Update pair_hourly_patterns table after each graded signal.

    Tracks per-pair per-hour win rate for time-aware predictions.
    User insight: pairs perform differently at different hours.
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
        # Upsert: if row exists, increment counters; else insert new
        cur.execute("""
            SELECT total_signals, correct, wrong, call_win_pct, put_win_pct
            FROM pair_hourly_patterns
            WHERE asset = ? AND hour_utc = ?
        """, (asset, hour_utc))
        existing = cur.fetchone()

        if existing:
            old_total = existing['total_signals'] or 0
            old_correct = existing['correct'] or 0
            old_wrong = existing['wrong'] or 0
            new_total = old_total + 1
            new_correct = old_correct + is_correct
            new_wrong = old_wrong + (1 - is_correct)
            new_win_pct = round(100.0 * new_correct / new_total, 1) if new_total > 0 else 0

            # Update direction-specific win rates
            # We don't track call/put counts separately in this table,
            # so we approximate: store the signal direction's running win rate
            if is_call:
                call_wins = existing['call_win_pct'] or 0
                # Simple exponential moving average for direction win rate
                new_call_wr = call_wins * 0.8 + is_correct * 100 * 0.2
                new_put_wr = existing['put_win_pct']
            else:
                put_wins = existing['put_win_pct'] or 0
                new_put_wr = put_wins * 0.8 + is_correct * 100 * 0.2
                new_call_wr = existing['call_win_pct']

            best_dir = 'CALL' if (new_call_wr or 0) >= (new_put_wr or 0) else 'PUT'

            cur.execute("""
                UPDATE pair_hourly_patterns SET
                    session = ?, total_signals = ?, correct = ?, wrong = ?,
                    win_pct = ?, avg_confidence = ?,
                    best_direction = ?, call_win_pct = ?, put_win_pct = ?,
                    last_updated = ?, ts = ?
                WHERE asset = ? AND hour_utc = ?
            """, (session, new_total, new_correct, new_wrong, new_win_pct,
                  confidence, best_dir, new_call_wr, new_put_wr,
                  ts_val, ts_val, asset, hour_utc))
        else:
            win_pct = 100.0 if is_correct else 0.0
            call_wr = 100.0 if (is_call and is_correct) else (0.0 if is_call else None)
            put_wr = 100.0 if (not is_call and is_correct) else (0.0 if not is_call else None)
            best_dir = signal if is_correct else ('PUT' if signal == 'CALL' else 'CALL')

            cur.execute("""
                INSERT INTO pair_hourly_patterns
                    (asset, hour_utc, session, total_signals, correct, wrong,
                     win_pct, avg_confidence, best_direction, call_win_pct,
                     put_win_pct, last_updated, ts)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (asset, hour_utc, session, is_correct, 1 - is_correct,
                  win_pct, confidence, best_dir, call_wr, put_wr, ts_val, ts_val))

        conn.commit()
    except Exception as e:
        print(f"[db] _update_hourly_pattern error: {e}")
    finally:
        conn.close()


def get_hourly_pattern(asset: str, hour_utc: int = None) -> dict:
    """Get hourly pattern data for a pair.

    If hour_utc is None, returns all 24 hours for the pair.
    Used by the brain for time-aware confidence adjustment.
    """
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
    """Get confidence adjustment for a pair at a specific hour.

    Returns:
        {
            'win_pct': float,        # historical win rate at this hour
            'total': int,            # sample count
            'adjustment': float,     # multiplier (0.5 to 1.3)
            'reason': str,           # human-readable reason
            'best_direction': str,   # CALL or PUT (which wins more)
        }

    Adjustment logic:
    - win_pct >= 65% and total >= 5 → 1.2 (boost)
    - win_pct 55-65% and total >= 5 → 1.1 (slight boost)
    - win_pct 45-55% or total < 5  → 1.0 (neutral)
    - win_pct 35-45% and total >= 5 → 0.8 (dampen)
    - win_pct < 35% and total >= 5 → 0.6 (strong dampen)
    """
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
    # candle_micro PK is (asset, period, ctime) — ctime is unique per
    # (asset, period) so no secondary tiebreaker is needed (unlike
    # signal_log which uses AUTOINCREMENT id).
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-19): replaced the SQL string-replace
    # mutation `q.replace("ORDER BY ctime DESC", "AND ctime < ? ORDER BY ...")`
    # with an explicit where_parts list. The string-replace silently
    # produced malformed SQL if the base query text changed (e.g. a comment
    # containing the literal "ORDER BY ctime DESC"). Now uses parameterized
    # clause composition.
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-05): switched to _read_cursor() —
    # SELECT-only, no spurious commit/fsync on context exit.
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
    Includes postmortem (win/loss reason), tags.

    FIX (AUDIT-CORE #005, 2026-07-21): default limit raised from 20 to 50
    to match the WS handler and the HTTP endpoint. Previously a caller
    forgetting to pass `limit` would silently get only 20 rows.
    FIX (AUDIT-CRITICAL #003, 2026-07-21): supports `before_ctime` for
    pagination — the frontend's "Load more" button uses this to fetch
    older signals on demand, bypassing the HISTORY_MAX cap.
    """
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-05): switched to _read_cursor() —
    # SELECT-only, no spurious commit/fsync on context exit.
    with _read_cursor() as c:
        base = """SELECT ctime, signal, accuracy, score, confidence,
                   strength, agree, theories, actual, regime, zone,
                   tags, postmortem, right_codes, wrong_codes,
                   a_open, a_close, reasons
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


def get_signal_detail(asset, period, ctime):
    """Return a single signal's full detail (for the reason modal).

    FIX (DEEP-AUDIT-2026-07-26 / F-14-20): explicit column list instead
    of SELECT * — previously leaked internal `id` and `ts` columns to
    the frontend's reason modal. Now returns only the columns the
    frontend expects.
    FIX (DEEP-AUDIT-2026-07-26 / F-14-05): switched to _read_cursor().
    """
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
    """Return (accuracy_float, sample_count) over the last N graded signals.

    accuracy_float = correct / (correct + wrong)   — draws excluded.
    Returns (None, 0) when no graded rows exist.
    A single graded row returns (1.0 or 0.0, 1) — caller is responsible for
    gating on sample size (prediction engine requires recent_n >= 8 before flipping).

    NOTE (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-3-09): the N-sample window
    can stretch across multiple algorithm regimes if the pair was in a
    cooldown (no graded rows for 30 min). feed.py's recent_accuracy cache
    now uses a smaller N (RECENT_ACCURACY_N env, default 50) to limit the
    stretch, but the legacy LIMIT-N semantics are preserved here for
    backwards compatibility with all callers.
    """
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-21): added a 7-day time floor so
    # a pair that was idle for 30 days doesn't mix regimes in the
    # "last N graded" window. Comment at the original code acknowledged
    # the bug but didn't fix it. Now `ctime > (now - 7d)` is ANDed in.
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-05): switched to _read_cursor().
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


# ── Per-module per-pair accuracy (added Bug #5 fix, 2026-07-17) ─────────────
# The `reasons` column in signal_log stores reason strings prefixed with
# [module_name]. We parse those to extract per-module win rates per asset,
# which lets per_pair.get_weights() adapt from historical accuracy instead
# of relying on the hardcoded PAIR_CONFIGS alone.

# FIX M6 (2026-07-19): import from core.constants instead of defining a
# local tuple. Prevents drift if a new module is added — previously
# /api/stats (which uses core.constants.MODULE_NAMES) would show the new
# module while per_module_accuracy (which used the local tuple) would
# silently skip it. Now both use the single source of truth.
try:
    from core.constants import MODULE_NAMES as _MODULE_NAMES
except ImportError:
    # Fallback for contexts where core.constants isn't importable (e.g.
    # standalone test scripts). Keeps the local tuple as a safety net.
    # FIX (MODULE-PRUNE-2026-08-03): removed indicator, otc_pattern,
    # trend_follow — these modules have been deleted.
    _MODULE_NAMES = (
        "candle_reaction", "running_tick", "pattern", "key_level",
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

    # FIX (DEEP-AUDIT-2026-07-26 / F-14-05): switched to _read_cursor().
    with _read_cursor() as c:
        rows = c.execute("""SELECT signal, accuracy, reasons
                   FROM signal_log
                   WHERE asset=? AND period=? AND signal IN ('CALL','PUT')
                     AND accuracy IN ('correct','wrong')
                   ORDER BY ctime DESC, id DESC LIMIT ?""",
                   (asset, period, n)).fetchall()

    if not rows:
        return out

    # FIX (DEEP-AUDIT-2026-07-26 / F-14-22): pre-compile the module-name
    # extraction regex. Previously `reason_str.find("]")` found the FIRST
    # `]`, mis-extracting module name on reasons like
    # `"[indicator] RSI[0] > 70"` (extracted `indicator] RSI[0`).
    # Regex `^\\[([^\\]]+)\\]` correctly extracts the first bracketed
    # group at the start of the string.
    _MODULE_RE = re.compile(r"^\[([^\]]+)\]")

    for row in rows:
        final_signal = row["signal"]
        accuracy = row["accuracy"]
        # FIX (DEEP-AUDIT-2026-07-26 / F-14-23): distinguish NULL from
        # empty string. Previously `row["reasons"] or "[]"` masked both.
        reasons_raw = row["reasons"] if row["reasons"] is not None else "[]"
        try:
            reasons = json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw
        except (ValueError, TypeError):
            reasons = []
        if not isinstance(reasons, list):
            reasons = []

        for reason in reasons:
            # FIX (DEEP-AUDIT-2026-07-26 / F-14-24): skip non-string
            # entries silently. Previously `str(reason)` converted dicts
            # to their repr, which never matched the [module] pattern.
            if not isinstance(reason, str):
                continue
            m_match = _MODULE_RE.match(reason)
            if not m_match:
                continue
            module = m_match.group(1).strip()
            if module not in _MODULE_NAMES:
                continue
            upper = reason.upper()
            # FIX (DEEP-AUDIT-2026-07-26 / F-14-25): direction parser is
            # no longer order-dependent. Previously `if "PUT" in upper or
            # "BEAR" in upper or "SELLER" in upper` ran BEFORE the CALL
            # check, so `"[indicator] CALL signal but bearish divergence"`
            # was classified as PUT. Now counts CALL-likes vs PUT-likes
            # and picks the majority; ties default to None (skip).
            call_hits = sum(1 for k in ("CALL", "BULL", "BUYER") if k in upper)
            put_hits = sum(1 for k in ("PUT", "BEAR", "SELLER") if k in upper)
            if call_hits > put_hits:
                module_dir = "CALL"
            elif put_hits > call_hits:
                module_dir = "PUT"
            else:
                continue

            # FIX (DEEP-AUDIT-2026-07-26 / F-14-26): check accuracy BEFORE
            # incrementing total. Previously total was incremented then
            # decremented for DRAW/PENDING — a future maintainer adding an
            # early `continue` between those two lines would silently
            # overcount totals.
            if accuracy not in ("correct", "wrong"):
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
            # FIX (DEEP-AUDIT-2026-07-26 / F-14-27): clamp win_rate to
            # [0, 1] — defensive against future bugs in the increment
            # logic that could push correct > total.
            s["win_rate"] = min(1.0, max(0.0, s["correct"] / s["total"]))

    return out


def cleanup(days=7):
    """Delete rows older than `days`. Returns (deleted_candle_micro, deleted_signal_log).

    FIX (DEEP-AUDIT-2026-07-26 / F-14-28): validate `days >= 1` —
    previously `cleanup(days=-1)` would compute cutoff = now + 86400
    (tomorrow) and DELETE ALL rows. `cleanup(days=0)` deleted everything
    older than now.
    FIX (DEEP-AUDIT-2026-07-26 / F-14-29): batched delete in chunks of
    1000 rows with commit() between batches — previously a single giant
    DELETE locked the table for seconds on a 100k+ row DB, stalling
    concurrent reads. Uses `WHERE rowid IN (SELECT ... LIMIT ?)` because
    `DELETE ... LIMIT` requires SQLITE_ENABLE_UPDATE_DELETE_LIMIT which
    is not always enabled.
    FIX (DEEP-AUDIT-2026-07-26 / F-14-30): magic number 86400 replaced
    with named constant `_SECONDS_PER_DAY` (via timedelta).
    FIX (DEEP-AUDIT-2026-07-26 / F-14-31): returns the deletion counts so
    callers can log cleanup progress. Previously no return value.
    NOTE (2026-07-13): synchronous blocking sqlite3 call. feed.py's run()
    calls it once at startup; periodic 6-hour cleanups go through
    asyncio.to_thread(_db.cleanup) to avoid blocking the loop.
    FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-3-20): clean signal_log
    by `ctime` (candle open time, immutable) instead of `ts`.
    NOTE on VACUUM: skipped — requires exclusive lock and can take
    minutes on a large DB. Operators should run `VACUUM` manually after
    large prunes (Problem 92 mitigation).
    """
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
        # Batched delete for candle_micro (PK is (asset, period, ctime),
        # so we use the implicit rowid for batching).
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
        # Batched delete for signal_log (has AUTOINCREMENT id).
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
