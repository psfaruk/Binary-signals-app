#!/usr/bin/env python3
"""
scripts/build_otc_dataset.py — OTC prediction-dataset CLI (PIPELINE PHASES 1/2/3/8).

USER SPEC (2026-09-11): "প্রথম বাস্তব কাজ তাই হবে: OTC live data →
historical data → candle builder → prediction dataset। এই data pipeline
ঠিক না হওয়া পর্যন্ত AI model তৈরি করা উচিত না।"

Modes
-----
  --db PATH            build the dataset from candle_micro (real OTC feed)
  --synthetic N        build from a deterministic synthetic walk (self-test)
  --audit              Phase-1 audit: per-pair depth / completeness / gaps
  --verify             Phase-8 proof: perturbation lock test on the data
  --out PATH           CSV output for the feature dataset
  --seq-out PATH       .npz raw window sequences (N x W x 5) for the future
                       LSTM/GRU phase (Phase 4) — same locked rows.

Examples
--------
  python3 scripts/build_otc_dataset.py --db signals.db --audit
  python3 scripts/build_otc_dataset.py --db signals.db --verify
  python3 scripts/build_otc_dataset.py --db signals.db \
      --out data/otc_dataset.csv --seq-out data/otc_seq.npz
  python3 scripts/build_otc_dataset.py --synthetic 800 --out /tmp/syn.csv
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from core.otc_dataset import (            # noqa: E402
    build_dataset, load_candles_from_db, audit_db_coverage, verify_lock,
    DEFAULT_WINDOW,
)
from core.otc_features import FEATURE_NAMES  # noqa: E402


def gen_synthetic(n, seed=7, start=1.10000, regime_len=45):
    """Regime-switching random walk (deterministic) — pipeline self-test
    data. NOT market data; it exists so every code path is runnable and
    testable before real history accumulates."""
    import random
    rng = random.Random(seed)
    candles = []
    price = start
    t0 = int(time.time()) - n * 60
    drift = 0.0
    for i in range(n):
        if i % regime_len == 0:
            drift = rng.choice([-1, 0, 1]) * rng.uniform(0.2, 1.4) * 1e-4
        o = price
        path = [o]
        for _ in range(12):  # 12 synthetic intra-minute ticks
            path.append(path[-1] + rng.gauss(drift, 8e-5))
        c = path[-1]
        hi = max(path) + abs(rng.gauss(0, 2e-5))
        lo = min(path) - abs(rng.gauss(0, 2e-5))
        candles.append({"time": t0 + i * 60, "open": o, "high": hi,
                        "low": lo, "close": c})
        price = c
    return candles


def _ts(ctime):
    return datetime.fromtimestamp(int(ctime), tz=timezone.utc) \
        .strftime("%Y-%m-%d %H:%M")


def run_audit(db_path, period):
    report = audit_db_coverage(db_path, period)
    total = sum(r["candles"] for r in report)
    print(f"══ Phase-1 DATA AUDIT (period={period}s, "
          f"{len(report)} pairs, {total} candles) ══")
    if not report:
        print("  (no candle_micro rows yet — the live feed has not "
              "persisted any candles for this period)")
        return
    hdr = (f"{'pair':14s} {'candles':>8s} {'span_d':>7s} {'compl%':>7s} "
           f"{'maxgap':>7s} {'gaps>5m':>8s}  first_utc → last_utc")
    print(hdr)
    for r in report:
        print(f"{r['asset']:14s} {r['candles']:8d} {r['span_days']:7.2f} "
              f"{r['completeness_pct']:7.1f} {r['max_gap_min']:7d} "
              f"{r['gaps_over_5min']:8d}  {_ts(r['first_utc'])} → {_ts(r['last_utc'])}")
    best_span = max((r["span_days"] for r in report), default=0)
    print(f"\n  সপ্তাহ-ভিত্তিক লক্ষ্য: best pair span = {best_span:.2f} days "
          f"(target: ≥ 14 দিন/পেয়ার; retention বর্তমানে 90 দিন)")
    print("  note: completeness < 100% = feed interruptions (weekends, "
          "reconnects); gap-free windows only — the dataset builder splits "
          "each pair at every hole, so no feature window or target ever "
          "crosses missing minutes.")


def run_verify(candles_by_asset, window):
    print(f"══ Phase-8 PREDICTION-LOCK VERIFICATION (window={window}) ══")
    total = ok = 0
    for asset, candles in sorted(candles_by_asset.items()):
        n_checks, future_hits, self_hits = verify_lock(
            candles, n_checks=15, window=window)
        total += 1
        verdict = "CLEAN" if future_hits == 0 and self_hits > 0 else "LEAK!"
        if verdict == "CLEAN":
            ok += 1
        print(f"  {asset:14s} checks={n_checks} "
              f"future-mutation-leaks={future_hits} "
              f"self-mutation-detected={self_hits} → {verdict}")
    print(f"  RESULT: {ok}/{total} pairs clean — "
          f"{'PIPELINE LOCK VERIFIED' if ok == total and total > 0 else 'FAILURE'}")
    return ok == total and total > 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", help="signals.db path (candle_micro source)")
    src.add_argument("--synthetic", type=int, metavar="N",
                     help="deterministic synthetic walk of N candles")
    ap.add_argument("--period", type=int, default=60)
    ap.add_argument("--days", type=int, default=None,
                    help="limit history to the last N days")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--out", help="dataset CSV output path")
    ap.add_argument("--seq-out", help="raw window sequences .npz output path")
    ap.add_argument("--no-micro", action="store_true",
                    help="exclude microstructure features")
    ap.add_argument("--audit", action="store_true",
                    help="Phase-1 per-pair data depth audit (db source only)")
    ap.add_argument("--verify", action="store_true",
                    help="Phase-8 prediction-lock perturbation proof")
    ap.add_argument("--pairs", default=None,
                    help="comma list to restrict assets")
    args = ap.parse_args()

    if args.synthetic:
        candles_by_asset = {
            f"SYN{s}": gen_synthetic(args.synthetic, seed=s)
            for s in (7, 42)
        }
    else:
        candles_by_asset = load_candles_from_db(
            args.db, period=args.period, days=args.days)
        if args.pairs:
            keep = {p.strip() for p in args.pairs.split(",") if p.strip()}
            candles_by_asset = {a: v for a, v in candles_by_asset.items()
                                if a in keep}

    if args.out is None and not args.audit and not args.verify:
        # default action when only a source is given: audit + verify
        args.audit = True
        args.verify = True

    if args.audit:
        if args.synthetic:
            print("audit: skipped for synthetic source")
        else:
            run_audit(args.db, args.period)

    rows = stats = None
    if args.out or args.seq_out:
        rows, stats = build_dataset(
            candles_by_asset, window=args.window, micro=not args.no_micro)
        print(f"══ Phase-3 DATASET (window={args.window}) ══")
        print(f"  stats: {json.dumps(stats)}")
        y1 = sum(r["y1_up"] for r in rows)
        y2 = sum(r["y2_up"] for r in rows)
        if rows:
            print(f"  y1_up balance: {y1}/{len(rows)} = {100.0*y1/len(rows):.1f}%")
            print(f"  y2_up balance: {y2}/{len(rows)} = {100.0*y2/len(rows):.1f}%")

    if args.out and rows is not None:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        cols = ["asset", "window_end_ctime", "t1_ctime", "t2_ctime",
                "close_i", "y1_up", "y2_up"] + list(FEATURE_NAMES)
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in cols})
        print(f"  CSV → {args.out} ({len(rows)} rows × {len(cols)} cols)")

    if args.seq_out and rows is not None:
        import numpy as np
        os.makedirs(os.path.dirname(os.path.abspath(args.seq_out)),
                    exist_ok=True)
        W = args.window
        seqs, metas, ys1, ys2 = [], [], [], []
        for asset, candles in sorted(candles_by_asset.items()):
            candles = sorted(candles, key=lambda x: x["time"])
            for i in range(W - 1, len(candles) - 2):
                t1, t2 = candles[i + 1], candles[i + 2]
                if t1["close"] == t1["open"] or t2["close"] == t2["open"]:
                    continue
                win = candles[i - W + 1: i + 1]
                seqs.append([[c["open"], c["high"], c["low"], c["close"],
                              abs(c["close"] - c["open"])] for c in win])
                metas.append([asset, candles[i]["time"]])
                ys1.append(1 if t1["close"] > t1["open"] else 0)
                ys2.append(1 if t2["close"] > t2["open"] else 0)
        np.savez_compressed(
            args.seq_out,
            X=np.asarray(seqs, dtype=np.float32),
            meta=np.asarray(metas, dtype=object),
            y1=np.asarray(ys1, dtype=np.int8),
            y2=np.asarray(ys2, dtype=np.int8))
        print(f"  SEQ → {args.seq_out} (X={np.asarray(seqs).shape if seqs else '(0,W,5)'})")

    if args.verify:
        usable = {a: c for a, c in candles_by_asset.items()
                  if len(c) >= args.window + 3}
        if not usable:
            print("verify: no pair has enough candles yet "
                  f"(need ≥ {args.window + 3})")
            sys.exit(2)
        ok = run_verify(usable, args.window)
        sys.exit(0 if ok else 3)


if __name__ == "__main__":
    main()
