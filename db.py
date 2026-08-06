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

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__) or ".", "signals.db")),
)

try:
    _db_dir = os.path.dirname(DB_PATH)
    if _db_dir:
        os.makedirs(_db_dir, exist_ok=True)
except Exception as _mkdir_exc:
    print(f"[db] WARNING: could not create DB_PATH directory {DB_PATH!r}: {_mkdir_exc}")


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
        (r'Hammer', 'Hammer'),
        (r'Shooting Star', 'Shooting Star'),
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
        dir_match = _re_module.search(r'→\s*(CALL|PUT)\b', reason_str)
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
        try:
            cur = conn.cursor()
            try:
                cur.execute("""
                    INSERT INTO signal_log
                        (asset,period,ctime,signal,score,confidence,theories,
                         actual,accuracy,strength,agree,right_codes,wrong_codes,
                         reasons,a_open,a_close,regime,zone,tags,postmortem,
                         category,total,ts,signal_quality)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                        signal_quality=excluded.signal_quality
                    """,
                    (asset, period, ctime, signal, score, confidence, _as_text(theories),
                     actual, accuracy,
                     kw.get("strength"), kw.get("agree"),
                     _as_text(kw.get("right_codes")), _as_text(kw.get("wrong_codes")),
                     _as_text(kw.get("reasons")),
                     kw.get("a_open"), kw.get("a_close"),
                     kw.get("regime"), kw.get("zone"),
                     _as_text(kw.get("tags")), kw.get("postmortem"),
                     category, total_val, ts_val, kw.get("signal_quality")))
            except sqlite3.Error as _conflict_err:
                if "ON CONFLICT" in str(_conflict_err) and "UNIQUE" in str(_conflict_err).upper():
                    # Fallback: no unique constraint, use INSERT OR REPLACE.
                    cur.execute("""
                        INSERT OR REPLACE INTO signal_log
                            (asset,period,ctime,signal,score,confidence,theories,
                             actual,accuracy,strength,agree,right_codes,wrong_codes,
                             reasons,a_open,a_close,regime,zone,tags,postmortem,
                             category,total,ts,signal_quality)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (asset, period, ctime, signal, score, confidence, _as_text(theories),
                         actual, accuracy,
                         kw.get("strength"), kw.get("agree"),
                         _as_text(kw.get("right_codes")), _as_text(kw.get("wrong_codes")),
                         _as_text(kw.get("reasons")),
                         kw.get("a_open"), kw.get("a_close"),
                         kw.get("regime"), kw.get("zone"),
                         _as_text(kw.get("tags")), kw.get("postmortem"),
                         category, total_val, ts_val, kw.get("signal_quality")))
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
                        dir_match = _re.search(r'→\s*(CALL|PUT)\b', content)
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
        return "ny"
    else:
        return "off"


def _update_hourly_pattern(asset: str, ctime: int, signal: str,
                           accuracy: str, confidence):
    """Update pair_hourly_patterns table after each graded signal."""
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

            if is_call:
                call_wins = existing['call_win_pct'] or 0
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
    """Return recent signals with full details for frontend history display."""
    with _read_cursor() as c:
        base = """SELECT asset, period, ctime, signal, accuracy, score, confidence,
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


def per_module_accuracy(asset, period=60, n=200):
    """Return per-module accuracy for a given (asset, period)."""
    out = {m: {"correct": 0, "wrong": 0, "total": 0, "win_rate": None}
           for m in _MODULE_NAMES}

    with _read_cursor() as c:
        rows = c.execute("""SELECT signal, accuracy, reasons
                   FROM signal_log
                   WHERE asset=? AND period=? AND signal IN ('CALL','PUT')
                     AND accuracy IN ('correct','wrong')
                   ORDER BY ctime DESC, id DESC LIMIT ?""",
                   (asset, period, n)).fetchall()

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
            out[module]["total"] += 1
            if module_dir == final_signal and accuracy == "correct":
                out[module]["correct"] += 1
            elif module_dir != final_signal and accuracy == "wrong":
                out[module]["correct"] += 1
            else:
                out[module]["wrong"] += 1

    for m in _MODULE_NAMES:
        s = out[m]
        if s["total"] > 0:
            s["win_rate"] = min(1.0, max(0.0, s["correct"] / s["total"]))

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


def cleanup(days=7):
    """Delete rows older than `days`. Returns (deleted_candle_micro, deleted_signal_log)."""
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
