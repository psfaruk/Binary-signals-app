#!/usr/bin/env python3
"""
verify_retention_policy.py — USER-2026-09-18 retention backtest.

USER POLICY UNDER TEST (verbatim requirement, 2026-09-18):
  "প্রয়জন হলে প্রত্যেক পেয়ার এ 12 ঘণ্টার ক্যান্ডেল ডেটা, 200 টি সিগন্যাল
   হিস্টোরি ও অন্যান্য 60 মিনিটের ডেটা, সেভ রাখো। অন্যন্য সকল ডেটা
   60 মিনিট মাত্র।"
  = candle_micro: 12 HOURS · signal_log: newest 200 ROWS PER PAIR ·
    everything else: 60 MINUTES.

What this proves, end to end, on a throwaway DB:
  1. candle_micro rows older than 12 h are deleted; newer rows survive.
  2. signal_log keeps the NEWEST 200 rows PER (asset, period) — count-
     based, not time-based: a pair with 250 rows keeps exactly the 200
     newest (by ctime, id); a pair with 50 rows keeps all 50; an OLD
     row inside the newest-200 SURVIVES (time no longer prunes it).
  3. EVERY other time-series table prunes at 60 min (old gone, new kept).
  4. Bounded state tables (api_keys etc.) are NOT time-pruned.
  5. db.cleanup() (the production call path) delegates to the policy.
  6. The DB FILE actually shrinks after a retention pass (VACUUM works).
  7. The storage watchdog soft/hard caps fire and prune.
  8. Retention is idempotent (a second pass deletes nothing).
  9. Future rows (clock skew tolerance) are never deleted.

Run: python3 scripts/verify_retention_policy.py
Exit 0 = all checks PASS. Any FAIL → exit 1.
"""
import os
import sys
import sqlite3
import tempfile
import time
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMPDIR = tempfile.mkdtemp(prefix="qx_retention_tmp_")
DB_PATH = os.path.join(TMPDIR, "tmp_retention_backtest.db")
os.environ["DB_PATH"] = DB_PATH
# Keep the daemon OUT of this test — we drive apply_retention() manually.
os.environ["QX_RETENTION_ENABLED"] = "0"

import db as _db                     # noqa: E402
from core import retention as _ret   # noqa: E402

PASS, FAIL = 0, 0
DATA_SECS_FOR_TEST = 3600    # mirror of retention.DATA_SECS in this run
SIGNAL_ROWS_FOR_TEST = 200   # mirror of retention.SIGNAL_ROWS


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def _count(table: str, where: str = "", params=()) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _ins(table: str, cols: str, rows: list):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executemany(
            f"INSERT INTO {table} ({cols}) VALUES "
            f"({','.join('?' * len(cols.split(',')))})", rows)
        conn.commit()
    finally:
        conn.close()


def phase(title: str):
    print(f"\n══ {title} ══")


def main() -> int:
    now = time.time()
    _db.init()   # creates every table the app has
    # Tables owned by feature modules (created by their own init in prod).
    try:
        from core.brain import init_brain
        init_brain()
    except Exception as exc:
        print(f"  [warn] brain init: {exc}")
    try:
        from core.time_patterns import init_patterns
        init_patterns()
    except Exception as exc:
        print(f"  [warn] time_patterns init: {exc}")
    try:
        from core.algorithm_monitor import init_algorithm_monitor
        init_algorithm_monitor()
    except Exception as exc:
        print(f"  [warn] algorithm_monitor init: {exc}")
    try:
        from core.target_gate import _ensure_table
        _c = _db._conn()
        try:
            _ensure_table(_c)
        finally:
            _c.close()
    except Exception as exc:
        print(f"  [warn] target_gate init: {exc}")
    try:
        from core.api_keys import _ensure_table as _ak_ensure
        _ak_ensure()
    except Exception as exc:
        print(f"  [warn] api_keys init: {exc}")
    try:
        _c = sqlite3.connect(DB_PATH)
        _c.execute("""CREATE TABLE IF NOT EXISTS share_signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_json TEXT, total_pairs INT, live_pairs INT, ts REAL)""")
        _c.execute("""CREATE TABLE IF NOT EXISTS agent_models (
            asset TEXT PRIMARY KEY, version INT, samples INT,
            state_json TEXT, ts REAL)""")
        _c.commit()
        _c.close()
    except Exception as exc:
        print(f"  [warn] share_signal_history init: {exc}")

    phase("1. Seed data — old + fresh rows in every policy table")
    # _OLD_DATA (90 min ago) → must be DELETED (60-min data bucket).
    # _MID_DATA (45 min ago) → must SURVIVE (inside 60-min window; this is
    #                           the row class the 2026-09-17 policy deleted!).
    # _FRESH (5 min ago)     → must survive.
    _OLD_DATA = now - 90 * 60
    _MID_DATA = now - 45 * 60
    _FRESH = now - 5 * 60
    seed = [
        # candle_micro: 13 h-old deleted (12 h window), 3 h-old kept.
        ("candle_micro", "ctime", "asset,period,ctime,open,high,low,close",
         [(f"A{i}_otc", 60, int(now - 13 * 3600), 1, 2, 0.5, 1.5) for i in range(5)] +
         [(f"B{i}_otc", 60, int(now - 3 * 3600), 1, 2, 0.5, 1.5) for i in range(7)]),
        # signal_log: count-based (see the dedicated phase below). Some old
        # + fresh rows here just to prove time alone no longer prunes them.
        ("signal_log", "ctime", "asset,period,ctime,signal,score,confidence",
         [(f"A{i}_otc", 60, int(_OLD_DATA), "CALL", 10, 70) for i in range(6)] +
         [(f"B{i}_otc", 60, int(_FRESH), "PUT", 12, 75) for i in range(9)]),
        ("otc_predictions", "signal_time", "asset,period,signal_time,target_time,horizon,prediction",
         [(f"A{i}_otc", 60, int(_OLD_DATA), int(_OLD_DATA) + 60, 1, "CALL") for i in range(4)] +
         [(f"M{i}_otc", 60, int(_MID_DATA), int(_MID_DATA) + 60, 1, "CALL") for i in range(3)] +
         [(f"B{i}_otc", 60, int(_FRESH), int(_FRESH) + 60, 1, "PUT") for i in range(6)]),
        ("module_votes", "ctime", "asset,period,ctime,module_name,direction",
         [(f"A{i}_otc", 60, int(_OLD_DATA), "pattern", "CALL") for i in range(8)] +
         [(f"B{i}_otc", 60, int(_FRESH), "pattern", "PUT") for i in range(11)]),
        ("theory_votes", "ctime", "asset,period,ctime,module_name,theory_name",
         [(f"A{i}_otc", 60, int(_OLD_DATA), "pattern", "T1") for i in range(3)] +
         [(f"B{i}_otc", 60, int(_FRESH), "pattern", "T2") for i in range(5)]),
        ("signal_quality_metrics", "ctime", "asset,period,ctime",
         [(f"A{i}_otc", 60, int(_OLD_DATA)) for i in range(2)] +
         [(f"B{i}_otc", 60, int(_FRESH)) for i in range(4)]),
        ("brain_predictions", "ts", "asset,period,ctime,ts",
         [(f"A{i}_otc", 60, int(_OLD_DATA), _OLD_DATA) for i in range(3)] +
         [(f"B{i}_otc", 60, int(_FRESH), _FRESH) for i in range(3)]),
        ("brain_module_votes", "ts", "asset,period,module_name,ts",
         [(f"A{i}_otc", 60, "pattern", _OLD_DATA) for i in range(3)] +
         [(f"B{i}_otc", 60, "pattern", _FRESH) for i in range(3)]),
        ("brain_learning", "ts", "asset,module_name,ts",
         [(f"A{i}_otc", "pattern", _OLD_DATA) for i in range(2)] +
         [(f"B{i}_otc", "pattern", _FRESH) for i in range(2)]),
        ("brain_patterns", "ts", "pattern_type,description,ts",
         [("pt", "old-" + str(i), _OLD_DATA) for i in range(2)] +
         [("pt", "new-" + str(i), _FRESH) for i in range(2)]),
        ("brain_insights", "ts", "insight_type,title,ts",
         [("it", "old-" + str(i), _OLD_DATA) for i in range(2)] +
         [("it", "new-" + str(i), _FRESH) for i in range(2)]),
        ("algorithm_changes", "ts", "asset,change_type,ts",
         [(f"A{i}_otc", "payout_spike", _OLD_DATA) for i in range(2)] +
         [(f"B{i}_otc", "payout_spike", _FRESH) for i in range(3)]),
        ("quotex_algo_patterns", "ts", "asset,pattern_type,ts",
         [(f"A{i}_otc", "trap_hour", _OLD_DATA) for i in range(2)] +
         [(f"B{i}_otc", "trap_hour", _FRESH) for i in range(2)]),
        ("pair_performance_daily", "ts", "asset,date,ts",
         [(f"A{i}_otc", "2026-09-18", _OLD_DATA) for i in range(2)] +
         [(f"B{i}_otc", "2026-09-18", _FRESH) for i in range(2)]),
        ("pair_hourly_patterns", "last_updated", "asset,hour_utc,last_updated",
         [(f"A{i}_otc", 3, _OLD_DATA) for i in range(2)] +
         [(f"B{i}_otc", 3, _FRESH) for i in range(2)]),
        ("time_session_patterns", "last_updated", "asset,dimension,key,last_updated",
         [(f"A{i}_otc", "d", "k", _OLD_DATA) for i in range(2)] +
         [(f"B{i}_otc", "d", "k", _FRESH) for i in range(2)]),
        ("pair_gate_state", "updated_ts", "asset,direction,gate,updated_ts",
         [(f"A{i}_otc", "CALL", 75, _OLD_DATA) for i in range(2)] +
         [(f"B{i}_otc", "PUT", 75, _FRESH) for i in range(2)]),
        ("share_signal_history", "ts", "snapshot_json,total_pairs,live_pairs,ts",
         [("{}", 22, 22, _OLD_DATA) for _ in range(2)] +
         [("{}", 22, 22, _FRESH) for _ in range(2)]),
    ]
    for table, tcol, colstr, rows in seed:
        _ins(table, colstr, rows)

    # Bounded state: old timestamps must SURVIVE (never time-pruned).
    _ins("api_keys", "label,key_hash,key_prefix,created",
         [("survivor-key", "hash", "qx_abcd", now - 90 * 86400)])
    _ins("agent_models", "asset,version,samples,state_json,ts",
         [("A0_otc", 2, 10, "{}", now - 90 * 86400)])
    _ins("model_registry", "name,version,scope,asset,trained_at,path,active,created_at",
         [("global", "v1", "global", None, now - 90 * 86400, "x", 1, now - 90 * 86400)])
    _ins("algorithm_state", "asset,last_payout,last_regime_summary,last_update_ts,candle_history",
         [("A0_otc", 90, "s", now - 90 * 86400, "{}")])

    # ── signal_log count-based seeding ────────────────────────────────────
    # COUNTY_otc: 250 rows, ctime spread over 250 minutes (newest first).
    # After retention: exactly the 200 NEWEST survive — including rows that
    # are older than 60 min (time no longer prunes the ledger).
    county_rows = []
    for i in range(250):
        county_rows.append(("COUNTY_otc", 60, int(now - i * 60),
                            "CALL" if i % 2 == 0 else "PUT", 10, 70))
    _ins("signal_log", "asset,period,ctime,signal,score,confidence", county_rows)
    # LIGHT_otc: only 50 rows → all 50 survive.
    light_rows = [("LIGHT_otc", 60, int(now - i * 60),
                   "CALL", 10, 70) for i in range(50)]
    _ins("signal_log", "asset,period,ctime,signal,score,confidence", light_rows)

    print(f"  seeded: {sum(len(r[3]) for r in seed)} policy rows + "
          f"count-based signal_log (250 + 50) + 4 state rows")

    phase("2. Apply retention (12h OHLC / 200 rows per pair / 60min data)")
    t0 = time.time()
    stats = _ret.apply_retention()
    print(f"  pass took {time.time() - t0:.2f}s")

    phase("3. Verify OHLC bucket = exactly 12 hours")
    total_cm = _count("candle_micro")
    old_cm = _count("candle_micro", "WHERE ctime < ?", (int(now - 12 * 3600),))
    fresh_cm = _count("candle_micro", "WHERE ctime >= ?", (int(now - 12 * 3600),))
    check("candle_micro: all 13h-old rows deleted", old_cm == 0,
          f"remaining_old={old_cm}")
    check("candle_micro: 3h-old rows kept", fresh_cm == 7,
          f"kept={fresh_cm} (want 7)")
    check("candle_micro total = 7", total_cm == 7, f"total={total_cm}")

    phase("4. signal_log = newest 200 rows PER PAIR (count-based)")
    county_total = _count("signal_log", "WHERE asset='COUNTY_otc'")
    check("COUNTY_otc pruned to exactly 200 rows", county_total == 200,
          f"n={county_total}")
    # The newest 200 rows span now-0 .. now-199*60; the 50 OLDEST
    # (now-200*60 .. now-249*60) must be the deleted ones.
    oldest_kept = sqlite3.connect(DB_PATH).execute(
        "SELECT MIN(ctime) FROM signal_log WHERE asset='COUNTY_otc'"
    ).fetchone()[0]
    check("COUNTY_otc keeps the NEWEST (min ctime = now-199min)",
          oldest_kept == int(now - 199 * 60),
          f"min_ctime={oldest_kept}, want={int(now - 199 * 60)}")
    # Rows older than 60 min but inside the newest-200 must SURVIVE —
    # the whole point of the 2026-09-18 count-based ledger. 90-min margin
    # avoids the exact 60-min boundary: rows i=90..199 (90+ min old).
    survivors_old = _count(
        "signal_log",
        "WHERE asset='COUNTY_otc' AND ctime < ?",
        (int(now - 90 * 60),))
    check("COUNTY_otc keeps rows older than 60 min (time no longer prunes)",
          survivors_old == 109,
          f"n={survivors_old} of 200 (want 109: rows 91..199 min old)")
    light_total = _count("signal_log", "WHERE asset='LIGHT_otc'")
    check("LIGHT_otc keeps all 50 rows (< 200 budget)", light_total == 50,
          f"n={light_total}")
    # The phase-1 seed rows (6 A-pairs @90 min + 9 B-pairs @5 min, each a
    # DISTINCT asset with 1 row) survive — each is its own (asset, period).
    check("phase-1 seed signal rows survive (1 row per asset, in budget)",
          _count("signal_log", "WHERE asset LIKE 'A%_otc'") == 6,
          f"n={_count('signal_log', 'WHERE asset LIKE \'A%_otc\'')}")

    phase("5. Verify every other table = exactly 60 minutes")
    for table, tcol, colstr, rows in seed[2:]:
        names = [c.strip() for c in colstr.split(",")]
        ti = names.index(tcol)
        kept_want = sum(1 for r in rows
                        if float(r[ti]) >= now - DATA_SECS_FOR_TEST)
        total = _count(table)
        check(f"{table}: old gone / fresh kept", total == kept_want,
              f"total={total} (want {kept_want})")

    phase("6. Bounded state tables survive")
    check("api_keys survives (90-day-old row kept)",
          _count("api_keys") == 1, f"n={_count('api_keys')}")
    check("agent_models survives",
          _count("agent_models") == 1, f"n={_count('agent_models')}")
    check("model_registry survives",
          _count("model_registry") == 1, f"n={_count('model_registry')}")
    check("algorithm_state survives",
          _count("algorithm_state") == 1, f"n={_count('algorithm_state')}")

    phase("7. Production path: db.cleanup() delegates to the policy")
    # 40-min-old signal on a NEW asset: inside the 200 budget → KEPT
    # (the 2026-09-17 policy deleted it; the 2026-09-18 ledger keeps it).
    _ins("signal_log", "asset,period,ctime,signal,score,confidence",
         [("C0_otc", 60, int(now - 40 * 60), "CALL", 5, 60)])
    deleted_cm, deleted_sl = _db.cleanup()
    check("db.cleanup() KEEPS the 40-min-old signal (count-based ledger)",
          _count("signal_log", "WHERE asset='C0_otc'") == 1,
          f"deleted_sl={deleted_sl}")
    # But a 201-row pair gets count-pruned through the same path.
    _ins("signal_log", "asset,period,ctime,signal,score,confidence",
         [("D0_otc", 60, int(now - i * 30), "CALL", 5, 60)
          for i in range(201)])
    _db.cleanup()
    check("db.cleanup() count-prunes a 201-row pair to 200",
          _count("signal_log", "WHERE asset='D0_otc'") == 200,
          f"n={_count('signal_log', 'WHERE asset=\'D0_otc\'')}")
    check("db.cleanup() did not touch 3h OHLC", _count("candle_micro") == 7)

    phase("8. Idempotency — second pass deletes nothing")
    stats2 = _ret.apply_retention()
    n2 = sum(v for k, v in stats2.items()
             if k != "__meta__" and isinstance(v, int))
    check("second pass deletes 0 rows", n2 == 0, f"deleted={n2}")

    phase("9. Future rows never deleted (clock-skew safety)")
    _ins("candle_micro", "asset,period,ctime,open,high,low,close",
         [("FUTURE_otc", 60, int(now + 120), 1, 2, 0.5, 1.5)])
    _ins("signal_log", "asset,period,ctime,signal,score,confidence",
         [("FUTURE_otc", 60, int(now + 120), "CALL", 5, 60)])
    _ret.apply_retention()
    check("future candle_micro kept",
          _count("candle_micro", "WHERE asset='FUTURE_otc'") == 1)
    check("future signal_log kept",
          _count("signal_log", "WHERE asset='FUTURE_otc'") == 1)

    phase("10. VACUUM actually shrinks the file (the 500 MB fix)")
    # Time-pruned bucket (otc_predictions) provides the bulk for VACUUM —
    # signal_log is count-bounded now and must NOT be the growth vector.
    big = [(f"BIG{i}_otc", 60, int(now - 90 * 60),
            int(now - 90 * 60) + 60, 1, "CALL") for i in range(4000)]
    _ins("otc_predictions", "asset,period,signal_time,target_time,horizon,prediction",
         big)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE otc_predictions SET prediction=?", ("x" * 4000,))
    conn.commit()
    conn.close()
    size_before = os.path.getsize(DB_PATH)
    _ret.apply_retention()
    size_after = os.path.getsize(DB_PATH)
    check("4000 padded otc_predictions rows pruned (60-min bucket)",
          _count("otc_predictions", "WHERE asset LIKE 'BIG%'") == 0)
    check("DB file shrank after retention+VACUUM",
          size_after < size_before,
          f"{size_before // 1024}KB → {size_after // 1024}KB")
    check("WAL side-car is tiny",
          os.path.getsize(DB_PATH + "-wal") < 64 * 1024
          if os.path.exists(DB_PATH + "-wal") else True)

    phase("11. Storage watchdog caps fire")
    _ret._DIR_SOFT_MB = 0.0001
    level = _ret.watchdog_check()
    check("soft cap detected", level == "soft", f"level={level}")
    _ret._DIR_SOFT_MB = 10 ** 9
    _ret._DIR_HARD_MB = 0.0001
    level = _ret.watchdog_check()
    check("hard cap detected", level == "hard", f"level={level}")
    _ret._DIR_HARD_MB = 10 ** 9
    level = _ret.watchdog_check()
    check("no false trip when healthy", level is None, f"level={level}")

    phase("12. retention_info() report shape")
    info = _ret.retention_info()
    check("policy report has ohlc_secs=43200 (12 h)",
          info["policy"]["ohlc_secs"] == 43200)
    check("policy report has data_secs=3600 (60 min)",
          info["policy"]["data_secs"] == 3600)
    check("policy report has signal_rows_per_pair=200",
          info["policy"].get("signal_rows_per_pair") == 200)

    print(f"\n{'=' * 60}")
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print(f"DB path was: {DB_PATH} (throwaway)")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        try:
            shutil.rmtree(TMPDIR, ignore_errors=True)
        except Exception:
            pass
    sys.exit(code)
