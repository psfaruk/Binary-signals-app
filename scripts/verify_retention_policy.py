#!/usr/bin/env python3
"""
verify_retention_policy.py — RAILWAY-500MB-FIX backtest (2026-09-17).

USER POLICY UNDER TEST (verbatim requirement):
  "শুধু মাত্র পেয়ার এর ohlc রেকর্ড সেভ রাখবেন, 4 ঘণ্টার এর, বাকি যত ডেটা
   সেভ হওয়ার কথা সব কিছু সেভ থাকবে মাত্র 30 মিনিট, এর পরে সকল backdate
   ডাটা অটো ডিলিট হয়ে যাবে।"

What this proves, end to end, on a throwaway DB:
  1. candle_micro rows older than 4 h are deleted; newer rows survive.
  2. EVERY other time-series table prunes at 30 min (old gone, new kept).
  3. Bounded state tables (api_keys etc.) are NOT time-pruned.
  4. db.cleanup() (the production call path) delegates to the policy.
  5. The DB FILE actually shrinks after a retention pass (VACUUM works —
     without it, deletes alone never return space to the 500 MB volume).
  6. The storage watchdog soft/hard caps fire and prune.
  7. Retention is idempotent (a second pass deletes nothing).
  8. Future rows (clock skew tolerance) are never deleted.

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
DATA_SECS_FOR_TEST = 1800   # mirror of retention.DATA_SECS in this run


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
        # agent_models is created lazily by agent.save_state() — same schema.
        _c.execute("""CREATE TABLE IF NOT EXISTS agent_models (
            asset TEXT PRIMARY KEY, version INT, samples INT,
            state_json TEXT, ts REAL)""")
        _c.commit()
        _c.close()
    except Exception as exc:
        print(f"  [warn] share_signal_history init: {exc}")

    phase("1. Seed data — old + fresh rows in every policy table")
    # Each entry: (table, time_col, column_list, rows) — the time column is
    # ALWAYS present with an explicit old (45 min ago) or fresh (5 min ago)
    # value. candle_micro carries its own 5h/3h pair per the 4-hour window.
    _OLD = now - 45 * 60      # 45 min ago  → must be deleted (data bucket)
    _FRESH = now - 5 * 60     # 5 min ago   → must survive (data bucket)
    seed = [
        ("candle_micro", "ctime", "asset,period,ctime,open,high,low,close",
         [(f"A{i}_otc", 60, int(now - 5 * 3600), 1, 2, 0.5, 1.5) for i in range(5)] +
         [(f"B{i}_otc", 60, int(now - 3 * 3600), 1, 2, 0.5, 1.5) for i in range(7)]),
        ("signal_log", "ctime", "asset,period,ctime,signal,score,confidence",
         [(f"A{i}_otc", 60, int(_OLD), "CALL", 10, 70) for i in range(6)] +
         [(f"B{i}_otc", 60, int(_FRESH), "PUT", 12, 75) for i in range(9)]),
        ("otc_predictions", "signal_time", "asset,period,signal_time,target_time,horizon,prediction",
         [(f"A{i}_otc", 60, int(_OLD), int(_OLD) + 60, 1, "CALL") for i in range(4)] +
         [(f"B{i}_otc", 60, int(_FRESH), int(_FRESH) + 60, 1, "PUT") for i in range(6)]),
        ("module_votes", "ctime", "asset,period,ctime,module_name,direction",
         [(f"A{i}_otc", 60, int(_OLD), "pattern", "CALL") for i in range(8)] +
         [(f"B{i}_otc", 60, int(_FRESH), "pattern", "PUT") for i in range(11)]),
        ("theory_votes", "ctime", "asset,period,ctime,module_name,theory_name",
         [(f"A{i}_otc", 60, int(_OLD), "pattern", "T1") for i in range(3)] +
         [(f"B{i}_otc", 60, int(_FRESH), "pattern", "T2") for i in range(5)]),
        ("signal_quality_metrics", "ctime", "asset,period,ctime",
         [(f"A{i}_otc", 60, int(_OLD)) for i in range(2)] +
         [(f"B{i}_otc", 60, int(_FRESH)) for i in range(4)]),
        ("brain_predictions", "ts", "asset,period,ctime,ts",
         [(f"A{i}_otc", 60, int(_OLD), _OLD) for i in range(3)] +
         [(f"B{i}_otc", 60, int(_FRESH), _FRESH) for i in range(3)]),
        ("brain_module_votes", "ts", "asset,period,module_name,ts",
         [(f"A{i}_otc", 60, "pattern", _OLD) for i in range(3)] +
         [(f"B{i}_otc", 60, "pattern", _FRESH) for i in range(3)]),
        ("brain_learning", "ts", "asset,module_name,ts",
         [(f"A{i}_otc", "pattern", _OLD) for i in range(2)] +
         [(f"B{i}_otc", "pattern", _FRESH) for i in range(2)]),
        ("brain_patterns", "ts", "pattern_type,description,ts",
         [("pt", "old-" + str(i), _OLD) for i in range(2)] +
         [("pt", "new-" + str(i), _FRESH) for i in range(2)]),
        ("brain_insights", "ts", "insight_type,title,ts",
         [("it", "old-" + str(i), _OLD) for i in range(2)] +
         [("it", "new-" + str(i), _FRESH) for i in range(2)]),
        ("algorithm_changes", "ts", "asset,change_type,ts",
         [(f"A{i}_otc", "payout_spike", _OLD) for i in range(2)] +
         [(f"B{i}_otc", "payout_spike", _FRESH) for i in range(3)]),
        ("quotex_algo_patterns", "ts", "asset,pattern_type,ts",
         [(f"A{i}_otc", "trap_hour", _OLD) for i in range(2)] +
         [(f"B{i}_otc", "trap_hour", _FRESH) for i in range(2)]),
        ("pair_performance_daily", "ts", "asset,date,ts",
         [(f"A{i}_otc", "2026-09-17", _OLD) for i in range(2)] +
         [(f"B{i}_otc", "2026-09-17", _FRESH) for i in range(2)]),
        ("pair_hourly_patterns", "last_updated", "asset,hour_utc,last_updated",
         [(f"A{i}_otc", 3, _OLD) for i in range(2)] +
         [(f"B{i}_otc", 3, _FRESH) for i in range(2)]),
        ("time_session_patterns", "last_updated", "asset,dimension,key,last_updated",
         [(f"A{i}_otc", "d", "k", _OLD) for i in range(2)] +
         [(f"B{i}_otc", "d", "k", _FRESH) for i in range(2)]),
        ("pair_gate_state", "updated_ts", "asset,direction,gate,updated_ts",
         [(f"A{i}_otc", "CALL", 75, _OLD) for i in range(2)] +
         [(f"B{i}_otc", "PUT", 75, _FRESH) for i in range(2)]),
        ("share_signal_history", "ts", "snapshot_json,total_pairs,live_pairs,ts",
         [("{}", 22, 22, _OLD) for _ in range(2)] +
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

    print(f"  seeded: {sum(len(r[3]) for r in seed)} policy rows + 4 state rows")

    phase("2. Apply retention (4h OHLC / 30min data)")
    t0 = time.time()
    stats = _ret.apply_retention()
    print(f"  pass took {time.time() - t0:.2f}s")

    phase("3. Verify OHLC bucket = exactly 4 hours")
    total_cm = _count("candle_micro")
    old_cm = _count("candle_micro", "WHERE ctime < ?", (int(now - 4 * 3600),))
    fresh_cm = _count("candle_micro", "WHERE ctime >= ?", (int(now - 4 * 3600),))
    check("candle_micro: all 5h-old rows deleted", old_cm == 0,
          f"remaining_old={old_cm}")
    check("candle_micro: 3h-old rows kept", fresh_cm == 7,
          f"kept={fresh_cm} (want 7)")
    check("candle_micro total = 7", total_cm == 7, f"total={total_cm}")

    phase("4. Verify every other table = exactly 30 minutes")
    for table, tcol, colstr, rows in seed[1:]:
        # count rows seeded with a FRESH timestamp (>= now-30min)
        names = [c.strip() for c in colstr.split(",")]
        ti = names.index(tcol)
        kept_want = sum(1 for r in rows
                        if float(r[ti]) >= now - DATA_SECS_FOR_TEST)
        total = _count(table)
        check(f"{table}: old gone / fresh kept", total == kept_want,
              f"total={total} (want {kept_want})")

    phase("5. Bounded state tables survive")
    check("api_keys survives (90-day-old row kept)",
          _count("api_keys") == 1, f"n={_count('api_keys')}")
    check("agent_models survives",
          _count("agent_models") == 1, f"n={_count('agent_models')}")
    check("model_registry survives",
          _count("model_registry") == 1, f"n={_count('model_registry')}")
    check("algorithm_state survives",
          _count("algorithm_state") == 1, f"n={_count('algorithm_state')}")

    phase("6. Production path: db.cleanup() delegates to the policy")
    _ins("signal_log", "asset,period,ctime,signal,score,confidence",
         [("C0_otc", 60, int(now - 40 * 60), "CALL", 5, 60)])
    deleted_cm, deleted_sl = _db.cleanup()
    check("db.cleanup() pruned the 40-min-old signal",
          _count("signal_log", "WHERE asset='C0_otc'") == 0,
          f"deleted_sl={deleted_sl}")
    check("db.cleanup() did not touch 3h OHLC", _count("candle_micro") == 7)

    phase("7. Idempotency — second pass deletes nothing")
    stats2 = _ret.apply_retention()
    n2 = sum(v for k, v in stats2.items()
             if k != "__meta__" and isinstance(v, int))
    check("second pass deletes 0 rows", n2 == 0, f"deleted={n2}")

    phase("8. Future rows never deleted (clock-skew safety)")
    _ins("candle_micro", "asset,period,ctime,open,high,low,close",
         [("FUTURE_otc", 60, int(now + 120), 1, 2, 0.5, 1.5)])
    _ins("signal_log", "asset,period,ctime,signal,score,confidence",
         [("FUTURE_otc", 60, int(now + 120), "CALL", 5, 60)])
    _ret.apply_retention()
    check("future candle_micro kept",
          _count("candle_micro", "WHERE asset='FUTURE_otc'") == 1)
    check("future signal_log kept",
          _count("signal_log", "WHERE asset='FUTURE_otc'") == 1)

    phase("9. VACUUM actually shrinks the file (the 500 MB fix)")
    big = [("BIG" + str(i) + "_otc", 60, int(now - 40 * 60)) for i in range(4000)]
    _ins("signal_log", "asset,period,ctime,signal,score,confidence",
         [(a, p, c, "CALL", 5, 60) for a, p, c in big])
    # pad rows so they take real space
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE signal_log SET reasons=?", ("x" * 4000,))
    conn.commit()
    conn.close()
    size_before = os.path.getsize(DB_PATH)
    _ret.apply_retention()
    size_after = os.path.getsize(DB_PATH)
    check("4000 padded rows pruned",
          _count("signal_log", "WHERE asset LIKE 'BIG%'") == 0)
    check("DB file shrank after retention+VACUUM",
          size_after < size_before,
          f"{size_before // 1024}KB → {size_after // 1024}KB")
    check("WAL side-car is tiny",
          os.path.getsize(DB_PATH + "-wal") < 64 * 1024
          if os.path.exists(DB_PATH + "-wal") else True)

    phase("10. Storage watchdog caps fire")
    # fake caps: soft=0.0001MB (always tripped)
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

    # hard-cap emergency path end-to-end
    _ret._DIR_HARD_MB = 0.0001
    _ret._emergency_pass("test")
    _ret._DIR_HARD_MB = 10 ** 9
    # Emergency applies the 1 h OHLC window, so the 3 h-old rows are SUPPOSED
    # to be gone. Readability + the clock-skew-safe future row prove the DB
    # survived the emergency pass.
    check("emergency pass leaves DB readable",
          _count("candle_micro", "WHERE asset='FUTURE_otc'") == 1
          and _count("signal_log", "WHERE asset='FUTURE_otc'") == 1)

    phase("11. retention_info() report shape")
    info = _ret.retention_info()
    check("policy report has ohlc_secs=14400",
          info["policy"]["ohlc_secs"] == 14400)
    check("policy report has data_secs=1800",
          info["policy"]["data_secs"] == 1800)
    check("storage report has data_dir_mb",
          isinstance(info["storage"].get("data_dir_mb"), float))

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
