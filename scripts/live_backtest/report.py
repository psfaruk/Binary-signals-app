#!/usr/bin/env python3
"""Aggregate the walk-forward backtest into the numbers that matter.

Usage: python report.py <backtest_out.json> <pairs.json>
"""
import json
import math
import sys
import collections


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def wr(rs):
    return 100 * sum(1 for r in rs if r["acc"] == "correct") / len(rs) if rs else 0


def breakeven(payout_pct):
    return 100 / (1 + payout_pct / 100)


def main():
    d = json.load(open(sys.argv[1]))
    payouts = {}
    if len(sys.argv) > 2:
        p = json.load(open(sys.argv[2]))
        for row in p.get("real_pairs", []) + p.get("otc_pairs", []):
            payouts[row["asset"]] = row["payout"]

    allt = []
    print("=" * 100)
    print("WALK-FORWARD BACKTEST — live engine, real Quotex history, no lookahead")
    print("=" * 100)
    print(f"{'pair':14s} {'cat':4s} {'signals':>8s} {'win%':>7s} {'95% CI':>16s} "
          f"{'payout':>7s} {'breakeven':>10s} {'edge':>7s}")
    for asset, blk in sorted(d.items()):
        tr = [r for r in blk["results"] if r.get("acc") in ("correct", "wrong")]
        if not tr:
            continue
        allt += tr
        k = sum(1 for r in tr if r["acc"] == "correct")
        lo, hi = wilson(k, len(tr))
        pay = payouts.get(asset)
        be = breakeven(pay) if pay else None
        edge = (100 * k / len(tr) - be) if be else None
        print(f"{asset:14s} {blk['category']:4s} {len(tr):8d} {100*k/len(tr):7.2f} "
              f"  [{100*lo:5.1f},{100*hi:5.1f}] "
              f"{(str(pay)+'%') if pay else '-':>7s} "
              f"{(f'{be:.1f}%') if be else '-':>10s} "
              f"{(f'{edge:+.1f}') if edge is not None else '-':>7s}")

    k = sum(1 for r in allt if r["acc"] == "correct")
    lo, hi = wilson(k, len(allt))
    print("-" * 100)
    print(f"{'TOTAL':14s} {'':4s} {len(allt):8d} {100*k/len(allt):7.2f} "
          f"  [{100*lo:5.1f},{100*hi:5.1f}]")

    for cat in ("otc", "real"):
        sub = [r for a, b in d.items() if b["category"] == cat
               for r in b["results"] if r.get("acc") in ("correct", "wrong")]
        if not sub:
            continue
        k = sum(1 for r in sub if r["acc"] == "correct")
        lo, hi = wilson(k, len(sub))
        print(f"{cat.upper():14s} {'':4s} {len(sub):8d} {100*k/len(sub):7.2f} "
              f"  [{100*lo:5.1f},{100*hi:5.1f}]")

    def slice_by(key, label, bucket=lambda v: v):
        b = collections.defaultdict(list)
        for r in allt:
            b[bucket(r.get(key))].append(r)
        print(f"\n--- win rate by {label} ---")
        for kk, v in sorted(b.items(), key=lambda kv: -len(kv[1])):
            lo, hi = wilson(sum(1 for r in v if r["acc"] == "correct"), len(v))
            print(f"  {str(kk):14s} n={len(v):6d} wr={wr(v):5.2f}%  "
                  f"[{100*lo:5.1f},{100*hi:5.1f}]")

    slice_by("conf", "displayed confidence",
             lambda v: f"{int((v or 0)//10*10)}-{int((v or 0)//10*10)+9}")
    slice_by("quality", "signal_quality tier")
    slice_by("strength", "strength tier")
    slice_by("agree", "agreeing groups")
    slice_by("regime", "regime")
    slice_by("htf", "HTF trend")
    slice_by("signal", "direction")


if __name__ == "__main__":
    main()
