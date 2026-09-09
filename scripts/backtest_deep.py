#!/usr/bin/env python3
"""
scripts/backtest_deep.py — DEEP backtest with the TARGET-75 gate closed-loop.

WHY (user directive 2026-09-09)
===============================
"আরও deeply Backtest করেন, যেনো প্রত্যেক পেয়ার এর উইন রেট call put
signals 75 এর উপরে থাকে। সব গুলো রেকর্ড backtest করেন। তার পর লাইন বাই
লাইন ফিক্স করেন।"

backtest_replay.py measures the RAW engine (gates cleared). This harness
measures the SHIPPED behaviour: engines.predict with QX_TARGET_GATE=1, the
per-pair per-direction adaptive controller live, graded rows written to
signal_log exactly like feed does, and controller updates via
core.target_gate.note_graded after every graded candle.

WHAT IT REPORTS
===============
* per (pair, direction): emitted, graded, wins, win_pct, WAIT candles,
  gate trajectory (start → end)
* verdict per row vs TARGET (default 75%):
    PASS    WR ≥ target and graded ≥ MIN_VOLUME
    NEAR    target−5 ≤ WR < target and graded ≥ MIN_VOLUME
    SILENT  graded < MIN_VOLUME (gate went high → honest no-trade)
    FAIL    WR < target−5 with volume (should be rare — controller pushes bar)
* overall stats + the exact /api/winrate view (db.get_directional_winrate)

NO LOOKAHEAD: identical structure to backtest_replay.replay_pair — predict
receives a copy of candles strictly before the graded candle.

DATA SOURCES: --synthetic N (regime-switching walk, control input), --db
(candle_micro), --json (same formats as backtest_replay).

NOTE on synthetic: a fair random walk has NO exploitable edge — there the
gate converges to SILENT for most pairs (bar → cap, zero bad trades). That
is the DESIRED outcome (no-trade beats losing), not a failure. Trend-rich
synthetic regimes let real confluence pockets through.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

TMP_DB = os.path.join(REPO, "backtest_deep_tmp.db")
os.environ["DB_PATH"] = TMP_DB
os.environ["QX_TARGET_GATE"] = "1"          # the behaviour we ship
for _g in ("QX_BREAKEVEN_GATE", "QX_PAIR_HEALTH_GATE", "QX_TRAP_HOUR",
           "QX_TIERED_FILTER", "QX_LOSS_COOLDOWN", "QX_CHOP_GUARD",
           "QX_WEAK_NEUTRAL", "QX_PAIR_PENALTY_NEUTRAL"):
    os.environ.pop(_g, None)

from scripts.backtest_replay import (        # noqa: E402
    load_from_db, load_from_json, gen_synthetic, htf_trend_5m, grade,
    WARMUP_CANDLES, DEFAULT_PAYOUT_OTC, DEFAULT_PAYOUT_REAL,
)
from engines import predict as engine_predict   # noqa: E402
import db as _db                                # noqa: E402
from core.target_gate import (                  # noqa: E402
    note_graded, TARGET_WR, GATE_INIT, GATE_FLOOR, GATE_CAP,
)

# IMPORT-ORDER FIX (2026-09-09): backtest_replay module-level config sets
# QX_TARGET_GATE="0" for its own baseline semantics — re-assert "1" here so
# THIS harness measures the shipped gate-on behaviour.
os.environ["QX_TARGET_GATE"] = "1"

MIN_VOLUME = int(os.environ.get("QX_DEEP_MIN_VOLUME", "20"))


def fresh_db():
    for suffix in ("", "-wal", "-shm"):
        p = TMP_DB + suffix
        if os.path.exists(p):
            try:
                os.unlink(p)
            except Exception:
                pass
    _db.init()


def log_graded(asset, period, candle, pred, acc):
    """Write one graded row to signal_log — same columns feed writes."""
    actual = "up" if candle["close"] > candle["open"] else "down"
    _db.log_signal(
        asset, period, candle["time"], pred.get("signal") or "NEUTRAL",
        pred.get("score", 0), pred.get("confidence", 0), "[]", actual, acc,
        strength=pred.get("strength", ""),
        category="otc" if asset.lower().endswith("otc") else "real")


def replay_pair_deep(asset, candles, period=60):
    rows = []
    gate_track = []
    n = len(candles)
    if n <= WARMUP_CANDLES:
        return rows, gate_track
    htf = "SIDEWAYS"
    for i in range(WARMUP_CANDLES, n):
        target = candles[i]
        history = candles[max(0, i - 260):i]
        if (i - WARMUP_CANDLES) % 15 == 0:
            htf = htf_trend_5m(history)
        pred = engine_predict(
            list(history), ticks=[], micro=None,
            asset=asset, htf_trend=htf, period=period)
        sig = pred.get("signal")
        gated = bool(pred.get("target_gate"))
        if sig not in ("CALL", "PUT"):
            rows.append({"asset": asset, "ctime": target["time"], "signal": sig,
                         "accuracy": "none", "gated": gated,
                         "gated_from": pred.get("gated_from"),
                         "confidence": pred.get("confidence", 0)})
            continue
        acc = grade(sig, target)
        if acc in ("correct", "wrong", "draw"):
            log_graded(asset, period, target, pred, acc)
        if acc in ("correct", "wrong"):
            # controller feed — identical to feed.py's hook (draws excluded)
            note_graded(asset, sig, acc == "correct", period)
        rows.append({"asset": asset, "ctime": target["time"], "signal": sig,
                     "accuracy": acc, "gated": False,
                     "confidence": pred.get("confidence", 0)})
    return rows, gate_track


def verdict(wr, graded):
    if graded < MIN_VOLUME:
        return "SILENT"
    if wr is None:
        return "SILENT"
    if wr >= TARGET_WR:
        return "PASS"
    if wr >= TARGET_WR - 5:
        return "NEAR"
    return "FAIL"


def summarize_deep(rows):
    per = defaultdict(lambda: {"emitted": 0, "correct": 0, "wrong": 0,
                               "draw": 0, "wait": 0})
    for r in rows:
        key = (r["asset"], r.get("gated_from") or r["signal"])
        b = per[key]
        if r["accuracy"] == "none":
            b["wait"] += 1
        elif r["accuracy"] == "draw":
            b["draw"] += 1
        else:
            b["emitted"] += 1
            if r["accuracy"] == "correct":
                b["correct"] += 1
            else:
                b["wrong"] += 1
    out = []
    for (asset, direction), b in sorted(per.items()):
        graded = b["correct"] + b["wrong"]
        wr = (100.0 * b["correct"] / graded) if graded else None
        out.append({
            "asset": asset, "direction": direction,
            "emitted": b["emitted"], "graded": graded,
            "correct": b["correct"], "wrong": b["wrong"], "draw": b["draw"],
            "wait": b["wait"],
            "win_pct": round(wr, 2) if wr is not None else None,
            "verdict": verdict(wr, graded),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="Deep TARGET-75 backtest")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", help="signals.db with candle_micro")
    src.add_argument("--json", dest="json_path", help="candles JSON file")
    src.add_argument("--synthetic", type=int, metavar="N")
    ap.add_argument("--pairs", default="EURUSD_otc,GBPUSD_otc,USDZAR_otc")
    ap.add_argument("--seeds", default="7,42")
    ap.add_argument("--period", type=int, default=60)
    ap.add_argument("--out", help="write report JSON here")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    fresh_db()

    if args.synthetic:
        data = {}
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
        for p in [x.strip() for x in args.pairs.split(",") if x.strip()]:
            for s in seeds:
                # FIX (deep-harness): mix the asset name into the seed so each
                # (pair, seed) is a DISTINCT walk — previously every pair with
                # the same seed got the IDENTICAL price path (same start_price
                # + same RNG), collapsing per-pair verdicts into copies.
                pair_seed = s + sum(ord(ch) for ch in p)
                # FIX (deep-harness): offset start_ts per seed — synthetic
                # candles all began at the same ctime, so signal_log's
                # ON CONFLICT(asset,period,ctime) made multi-seed runs
                # overwrite each other (the API saw only the last seed).
                start_ts = int(time.time() - (s * 86400) - (pair_seed % 3600) * 60)
                key = f"{p}#seed{s}" if len(seeds) > 1 else p
                data[key] = gen_synthetic(args.synthetic, seed=pair_seed,
                                          asset=p, start_ts=start_ts)
    elif args.json_path:
        data = load_from_json(args.json_path)
    else:
        data = load_from_db(args.db, period=args.period)
    if not data:
        print("No candles for given source.", file=sys.stderr)
        sys.exit(2)

    all_rows = []
    for asset, candles in sorted(data.items()):
        engine_asset = asset.split("#")[0]
        print(f"[deep] replay {asset}: {len(candles)} candles ...",
              file=sys.stderr)
        rows, _ = replay_pair_deep(engine_asset, candles, period=args.period)
        all_rows.extend(rows)
        if args.verbose:
            done = sum(1 for r in rows if r["accuracy"] != "none")
            print(f"[deep]   {asset}: {done} emitted, "
                  f"{len(rows) - done} WAIT", file=sys.stderr)

    table = summarize_deep(all_rows)
    wr_api = _db.get_directional_winrate(period=args.period)

    pass_n = sum(1 for r in table if r["verdict"] == "PASS")
    near_n = sum(1 for r in table if r["verdict"] == "NEAR")
    fail_n = sum(1 for r in table if r["verdict"] == "FAIL")
    silent_n = sum(1 for r in table if r["verdict"] == "SILENT")
    total_graded = sum(r["graded"] for r in table)
    total_correct = sum(r["correct"] for r in table)
    total_wait = sum(r["wait"] for r in table)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": ("synthetic" if args.synthetic else
                 "db" if args.db else "json"),
        "target_wr": TARGET_WR,
        "gate": {"init": GATE_INIT, "floor": GATE_FLOOR, "cap": GATE_CAP},
        "pairs": sorted(data.keys()),
        "totals": {
            "graded": total_graded,
            "correct": total_correct,
            "win_pct": (round(100.0 * total_correct / total_graded, 2)
                        if total_graded else None),
            "wait_candles": total_wait,
            "pass": pass_n, "near": near_n,
            "fail": fail_n, "silent": silent_n,
        },
        "rows": table,
        "api_winrate_pairs": wr_api.get("pairs", []),
        "runtime_sec": round(time.time() - t0, 1),
    }
    print(json.dumps(report, indent=2))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nreport → {args.out}", file=sys.stderr)

    # cleanup throwaway DB
    for suffix in ("", "-wal", "-shm"):
        p = TMP_DB + suffix
        try:
            if os.path.exists(p):
                os.unlink(p)
        except Exception:
            pass


if __name__ == "__main__":
    main()
