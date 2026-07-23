#!/usr/bin/env python3
"""
Diagnostic script to find why live candles are not updating.

Run from the project root:
    python scripts/diagnose_no_candles.py

This script checks (in order):
  1. All modules import cleanly (no syntax/import errors)
  2. Database can be opened and is writable
  3. Brain / algorithm_monitor / time_patterns tables init OK
  4. Prediction pipeline runs end-to-end on synthetic data
  5. sim_feed can start and produce ticks/candles (server simulation)
  6. real feed can be imported (no syntax errors in feed.py)
  7. Reports which checks pass / fail with detailed error messages

If check #5 fails, the issue is in our code or environment.
If checks #1-#5 all pass but live candles still don't update,
the issue is one of:
  - Server process not restarted (still running old code)
  - Browser serving cached JS (Ctrl+Shift+R to hard refresh)
  - Real Quotex connection issue (token expired, IP blocked, etc.)
  - DB lock contention (multiple writers)

Usage:
    python scripts/diagnose_no_candles.py
    python scripts/diagnose_no_candles.py --live   # also test real feed (requires creds)
"""
import sys
import os
import asyncio
import time as _t
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Colors for output
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def ok(msg):
    print(f"  {GREEN}[PASS]{RESET} {msg}")


def fail(msg, detail=""):
    print(f"  {RED}[FAIL]{RESET} {msg}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")


def warn(msg):
    print(f"  {YELLOW}[WARN]{RESET} {msg}")


def section(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def main():
    test_live = "--live" in sys.argv

    section("1. MODULE IMPORTS — checking all modified files load cleanly")
    modules_to_test = [
        # Core modules
        "core.analysis",
        "core.brain",
        "core.microstructure",
        "core.algorithm_strategy",
        "core.algorithm_monitor",
        "core.auto_tune",
        "core.time_patterns",
        "core.stats",
        "core.constants",
        # Engine modules
        "engines",
        "engines.base.blender",
        "engines.base.context",
        "engines.base.per_pair",
        "engines.base.types",
        "engines.base.modules.candle_reaction",
        "engines.base.modules.running_tick",
        "engines.base.modules.pattern",
        "engines.base.modules.indicator",
        "engines.base.modules.key_level",
        "engines.base.modules.otc_pattern",
        "engines.base.modules.trend_follow",
        "engines.otc",
        "engines.real",
        # Server-side
        "db",
    ]
    all_imports_ok = True
    for mod_name in modules_to_test:
        try:
            __import__(mod_name)
        except Exception as e:
            fail(f"import {mod_name}", f"{type(e).__name__}: {e}")
            all_imports_ok = False
    if all_imports_ok:
        ok(f"All {len(modules_to_test)} modules import cleanly")

    section("2. DATABASE — can we open and write to signals.db?")
    # Use a temp DB to avoid touching production
    test_db = "/tmp/diagnose_signals.db"
    if os.path.exists(test_db):
        os.remove(test_db)
    os.environ["DB_PATH"] = test_db

    try:
        import db as _db
        _db.DB_PATH = test_db
        _db.init()
        ok(f"db.init() — created schema in {test_db}")

        # Test write
        _db.log_signal("TEST", 60, int(_t.time()), "CALL", 5, 70,
                       "test", "UP", "correct", strength="MEDIUM", agree=3)
        ok("db.log_signal() — write OK")

        # Test read
        sigs = _db.get_recent_signals("TEST", 60, limit=10)
        if sigs and len(sigs) >= 1:
            ok(f"db.get_recent_signals() — read OK ({len(sigs)} row)")
        else:
            fail("db.get_recent_signals()", "returned 0 rows after insert")
    except Exception as e:
        fail("database operations", traceback.format_exc().splitlines()[-1])

    section("3. INITIALIZATION — brain, algorithm_monitor, time_patterns")
    try:
        from core.brain import init_brain
        init_brain()
        ok("brain.init_brain()")
    except Exception as e:
        fail("brain.init_brain()", str(e))

    try:
        from core.algorithm_monitor import init_algorithm_monitor
        init_algorithm_monitor()
        ok("algorithm_monitor.init_algorithm_monitor()")
    except Exception as e:
        fail("algorithm_monitor.init_algorithm_monitor()", str(e))

    try:
        from core.time_patterns import init_patterns
        init_patterns()
        ok("time_patterns.init_patterns()")
    except Exception as e:
        fail("time_patterns.init_patterns()", str(e))

    section("4. PREDICTION PIPELINE — end-to-end on synthetic data")
    try:
        from engines import predict
        # Build synthetic candles
        base_t = int(_t.time()) - 60 * 100
        candles = []
        for i in range(100):
            p = 1.0850 + (i % 10) * 0.0001
            candles.append({
                "time": base_t + i * 60,
                "open": p, "high": p + 0.0003,
                "low": p - 0.0002, "close": p + 0.0001,
            })

        # Test OTC
        r_otc = predict(candles, asset="EURUSD_otc", period=60)
        ok(f"OTC predict: signal={r_otc['signal']}, conf={r_otc['confidence']}, "
           f"strategy={r_otc['strategy']}")

        # Test Real
        r_real = predict(candles, asset="EURUSD", period=60)
        ok(f"Real predict: signal={r_real['signal']}, conf={r_real['confidence']}, "
           f"strategy={r_real['strategy']}")

        # Test alltime_otc routing (BUG-05 fix)
        r_at = predict(candles, asset="EURUSD_otc", period=60, category="alltime_otc")
        ok(f"alltime_otc routing: signal={r_at['signal']}")

        # Test with HTF trend (triggers full blender path)
        r_htf = predict(candles, asset="EURUSD_otc", period=60, htf_trend="UPTREND",
                        recent_accuracy=(0.55, 30))
        ok(f"HTF UPTREND path: signal={r_htf['signal']}, conf={r_htf['confidence']}")

    except Exception as e:
        fail("prediction pipeline", traceback.format_exc())

    section("5. SIM_FEED — can the simulated feed produce ticks + candles?")
    try:
        # Use a fresh DB for the sim test
        sim_db = "/tmp/diagnose_sim.db"
        if os.path.exists(sim_db):
            os.remove(sim_db)
        os.environ["DB_PATH"] = sim_db
        os.environ["USE_SIM"] = "1"

        import db as _db2
        _db2.DB_PATH = sim_db
        _db2.init()

        from core.brain import init_brain
        init_brain()
        from core.algorithm_monitor import init_algorithm_monitor
        init_algorithm_monitor()
        from core.time_patterns import init_patterns
        init_patterns()

        from sim_feed import QuotexFeed

        async def test_sim():
            feed = QuotexFeed()
            ticks_seen = []
            eocs_seen = []
            errors_seen = []

            async def broadcast(msg):
                t = msg.get("type")
                if t == "tick":
                    ticks_seen.append(msg)
                elif t == "eoc":
                    eocs_seen.append(msg)
                elif t == "error":
                    errors_seen.append(msg.get("error"))

            feed_task = asyncio.create_task(feed.run(broadcast))
            await feed.ensure_stream("EURUSD_otc", 60, cid="diag-cid")

            # Wait 15 seconds for at least 1 candle close
            for _ in range(30):
                await asyncio.sleep(0.5)
                if eocs_seen:
                    break

            await feed.shutdown()
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass

            return ticks_seen, eocs_seen, errors_seen

        ticks, eocs, errors = asyncio.run(test_sim())

        if errors:
            fail(f"sim_feed reported {len(errors)} error(s)",
                 "\n".join(errors[:3]))
        else:
            ok("sim_feed reported no errors")

        if len(ticks) > 0:
            ok(f"sim_feed produced {len(ticks)} tick broadcasts")
        else:
            fail("sim_feed produced 0 ticks in 15 seconds")

        if len(eocs) > 0:
            ok(f"sim_feed produced {len(eocs)} candle-close (EOC) broadcasts")
        else:
            warn("sim_feed produced 0 EOC in 15s (may need longer run)")

    except Exception as e:
        fail("sim_feed test", traceback.format_exc())

    if test_live:
        section("6. REAL FEED — attempting to import + connect (needs creds)")
        creds = bool(os.environ.get("QX_TOKEN") or
                     (os.environ.get("QX_EMAIL") and os.environ.get("QX_PASSWORD")))
        if not creds:
            warn("No Quotex credentials in env — set QX_TOKEN or QX_EMAIL+QX_PASSWORD")
        else:
            try:
                from feed import QuotexFeed
                ok("feed.QuotexFeed imported OK")
                # Don't actually connect — just verify it doesn't crash on init
                feed = QuotexFeed()
                ok(f"QuotexFeed instantiated (connected={feed._connected})")
                # Check available_pairs
                pairs = feed.available_pairs()
                n_real = len(pairs.get("real_pairs", []))
                n_otc = len(pairs.get("otc_pairs", []))
                ok(f"available_pairs: {n_real} real, {n_otc} OTC")
                if n_real == 0 and n_otc == 0:
                    fail("available_pairs returned empty lists",
                         "This usually means the Quotex connection failed.")
            except Exception as e:
                fail("real feed import / init", traceback.format_exc())

    section("SUMMARY")
    print()
    print("If all checks PASS:")
    print("  - The Python code is fine.")
    print("  - Issue is likely:")
    print("    1. Server process not restarted (still running old code)")
    print("       → kill the old uvicorn process and restart")
    print("    2. Browser serving cached JS")
    print("       → Ctrl+Shift+R (hard refresh) or open in incognito")
    print("    3. Real Quotex connection (token expired, IP blocked)")
    print("       → check /api/debug endpoint for connection status")
    print("    4. DB lock contention")
    print("       → check server logs for 'database is locked' errors")
    print()
    print("To run the real-feed test (needs creds):")
    print("  QX_TOKEN=xxx python scripts/diagnose_no_candles.py --live")
    print()


if __name__ == "__main__":
    main()
