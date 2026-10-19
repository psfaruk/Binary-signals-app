"""PHASE-J race worker — one of two concurrent processes hammering the
settlement/shadow ledgers to prove cross-process safety (BEGIN IMMEDIATE).

Usage: python3 scripts/phase_j_race_worker.py <mode> <go_signal_file> <db_path>
Modes:
  finalize_same    — settlement_begin + settlement_finalize (SAME OHLC)
  finalize_conflict— settlement_finalize (DIFFERENT OHLC vs the other proc)
  settle           — settlement_settle on the same finalized row
  shadow           — shadow_record + shadow_settle + 5 correction appends
The worker waits for <go_signal_file> to exist, then executes immediately
(so both workers overlap in time), and prints a JSON result line prefixed
RESULT: for the parent to parse.
"""
import json
import os
import sys
import time


def main() -> int:
    mode, go_file, db_path = sys.argv[1], sys.argv[2], sys.argv[3]
    os.environ["DB_PATH"] = db_path
    os.environ["QX_RETENTION_ENABLED"] = "0"
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # barrier: wait for the parent's go signal (max 10s)
    deadline = time.time() + 10.0
    while not os.path.exists(go_file):
        if time.time() > deadline:
            print(json.dumps({"error": "go signal timeout"}))
            return 2
        time.sleep(0.01)

    import db  # noqa: E402  (after DB_PATH is set)

    out = {"mode": mode, "actions": []}
    asset = "RACEUSD"
    period = 60
    period_start = 1700000000.0

    try:
        if mode == "finalize_same":
            r1 = db.settlement_begin(asset, period, period_start,
                                     source="race-worker")
            r2 = db.settlement_finalize(asset, period, period_start,
                                        1.0, 2.0, 0.5, 1.5, 42,
                                        source="race-worker")
            out["actions"] = [r1.get("state"), r2.get("action")]
        elif mode == "finalize_conflict":
            # THIS worker finalizes with different OHLC than the peer —
            # the parent passes PHASE_J_VARIANT=0/1 so the two processes
            # are DETERMINISTICALLY different (a random pick could give
            # both the same value -> 'duplicate' instead of conflict).
            variant = int(os.environ.get("PHASE_J_VARIANT", "0") or "0")
            o = 1.0 + (0.001 * variant)
            r = db.settlement_finalize(asset, period, period_start,
                                       o, 2.0, 0.5, 1.5, 42,
                                       source="race-worker-conflict")
            out["actions"] = [r.get("action")]
        elif mode == "settle":
            # ensure the row exists and is finalized BEFORE the settle
            # race (both workers do the same idempotent begin+finalize;
            # one wins, the other records 'duplicate' — then both race
            # the settle transition itself).
            db.settlement_begin(asset, period, period_start,
                                source="race-worker")
            db.settlement_finalize(asset, period, period_start,
                                   1.0, 2.0, 0.5, 1.5, 42,
                                   source="race-worker")
            r = db.settlement_settle(asset, period, period_start,
                                     signal_id="race-sig",
                                     research_direction="CALL",
                                     outcome="GREEN",
                                     research_correct=True)
            out["actions"] = [r.get("action")]
        elif mode == "shadow":
            pid = os.getpid()
            pred_id = f"race-shadow-{pid}"
            rec = db.shadow_record({
                "prediction_id": pred_id, "mode": "replay",
                "asset": asset, "period": period,
                "target_candle_open_epoch": period_start,
                "signal": "NO_SIGNAL",
            })
            st = db.shadow_settle(pred_id, "GREEN", correct=True)
            # 5 correction appends each — both workers' entries must ALL
            # survive (no lost updates).
            for i in range(5):
                db.shadow_append_correction(
                    pred_id, {"i": i, "worker": pid, "ts": time.time()})
            row = db.shadow_get(pred_id)
            hist = json.loads(row["correction_history"] or "[]")
            # shared row for BOTH workers:
            shared_id = "race-shadow-shared"
            db.shadow_record({
                "prediction_id": shared_id, "mode": "replay",
                "asset": asset, "period": period,
                "target_candle_open_epoch": period_start,
                "signal": "NO_SIGNAL",
            })
            for i in range(5):
                db.shadow_append_correction(
                    shared_id, {"i": i, "worker": pid})
            shared = db.shadow_get(shared_id)
            shared_hist = json.loads(shared["correction_history"] or "[]")
            out["actions"] = [bool(rec), bool(st), len(hist), len(shared_hist),
                            pid]
        else:
            out = {"error": f"unknown mode {mode}"}
            return 2
    except Exception as exc:  # any failure is a result, not a crash
        out = {"error": f"{type(exc).__name__}: {exc}"}

    print("RESULT:" + json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
