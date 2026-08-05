#!/usr/bin/env python3
"""Does ANY simple edge exist in this data at all?

Before blaming the engine, measure the raw predictability of the target:
next-candle direction. Tests a handful of textbook one-line rules plus the
autocorrelation structure, per pair and per category. If nothing here beats
the payout break-even, no amount of engine tuning will either.

Usage: python edge_scan.py <histdir>
"""
import json
import math
import os
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


def body(c):
    return c["close"] - c["open"]


def rules(past, i, cs):
    """Yield (rule_name, predicted_up) for candle cs[i] using only cs[:i]."""
    p1 = cs[i - 1]
    p2 = cs[i - 2]
    b1, b2 = body(p1), body(p2)
    rng1 = p1["high"] - p1["low"]

    yield "always CALL", True
    yield "momentum: follow prev candle", b1 > 0
    yield "reversal: fade prev candle", b1 < 0
    yield "follow 2-candle run", (b1 > 0 and b2 > 0) if (b1 > 0) == (b2 > 0) else None
    # EMA slope
    n = 20
    win = cs[i - n:i]
    if len(win) == n:
        ema = sum(c["close"] for c in win[:5]) / 5
        k = 2 / (n + 1)
        for c in win:
            ema = c["close"] * k + ema * (1 - k)
        yield "close above EMA20", p1["close"] > ema
    # big-body continuation
    if rng1 > 0 and abs(b1) / rng1 > 0.7:
        yield "strong-body continuation", b1 > 0
    # wick rejection
    if rng1 > 0:
        upper = p1["high"] - max(p1["open"], p1["close"])
        lower = min(p1["open"], p1["close"]) - p1["low"]
        if lower > 0.6 * rng1:
            yield "long lower wick -> CALL", True
        if upper > 0.6 * rng1:
            yield "long upper wick -> PUT", False


def main():
    histdir = sys.argv[1]
    files = sorted(f for f in os.listdir(histdir)
                   if f.endswith(".json") and not f.startswith("_"))
    tally = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
    runs = collections.defaultdict(collections.Counter)
    pair_up = {}

    for fn in files:
        d = json.load(open(os.path.join(histdir, fn)))
        cs, cat, asset = d["candles"], d["category"], d["asset"]
        up_n = tot = 0
        run_len, run_dir = 0, None
        for i in range(25, len(cs)):
            if cs[i]["time"] - cs[i - 1]["time"] != 60:
                run_len, run_dir = 0, None
                continue
            b = body(cs[i])
            if abs(b) < 1e-12:
                continue
            up = b > 0
            tot += 1
            up_n += up
            if up == run_dir:
                run_len += 1
            else:
                if run_dir is not None:
                    runs[cat][min(run_len, 8)] += 1
                run_dir, run_len = up, 1
            for name, pred in rules(cs[:i], i, cs):
                if pred is None:
                    continue
                t = tally[cat][name]
                t[0] += (pred == up)
                t[1] += 1
                t2 = tally["ALL"][name]
                t2[0] += (pred == up)
                t2[1] += 1
        pair_up[asset] = (up_n, tot, cat)

    print("=" * 92)
    print("RAW PREDICTABILITY OF NEXT-CANDLE DIRECTION (7 days, 1-min candles)")
    print("=" * 92)
    for cat in ("otc", "real", "ALL"):
        if cat not in tally:
            continue
        print(f"\n--- {cat.upper()} ---")
        print(f"{'rule':34s} {'n':>8s} {'win%':>7s} {'95% CI':>16s}")
        for name, (k, n) in sorted(tally[cat].items(), key=lambda kv: -kv[1][1]):
            lo, hi = wilson(k, n)
            print(f"{name:34s} {n:8d} {100*k/n:7.2f} "
                  f"  [{100*lo:5.1f}, {100*hi:5.1f}]")

    print("\n--- per-pair UP-candle frequency (a fair coin would sit at 50%) ---")
    for a, (u, t, cat) in sorted(pair_up.items()):
        lo, hi = wilson(u, t)
        flag = "  <-- BIASED" if (lo > 0.5 or hi < 0.5) else ""
        print(f"{a:14s} {cat:4s} n={t:6d} up={100*u/t:6.2f}%  "
              f"[{100*lo:5.2f}, {100*hi:5.2f}]{flag}")

    print("\n--- run-length distribution (consecutive same-direction candles) ---")
    for cat, cnt in runs.items():
        tot = sum(cnt.values())
        exp = {L: tot * (0.5 ** L) for L in cnt}
        print(f"  {cat}: " + "  ".join(
            f"{L}:{100*cnt[L]/tot:.1f}%(exp {100*exp[L]/tot:.1f}%)"
            for L in sorted(cnt)[:6]))


if __name__ == "__main__":
    main()
