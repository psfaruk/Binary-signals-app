#!/usr/bin/env python3
"""scripts/analyze_otc_edges.py — deep conditional-edge scan on REAL OTC
candles (data/otc_history.db).

Answers, per pair and pooled, with 95% binomial CI on the pooled stats:
  1. persistence  : P(i+1 UP | i UP)  vs  P(i+1 UP | i DOWN)
  2. follow       : P(i+1 same direction as i)  (audit hypothesis)
  3. hour-of-day  : follow-rate by UTC hour (Quotex OTC algorithm cycles)
  4. after-streak : follow-rate after 2/3/4 same-direction runs
  5. doji rate    : share of exact ties (feed character)

Output: JSON to stdout (small) + printed Bengali summary.
This is DIAGNOSTIC — it does not claim tradeable edge; significant,
reproducible deviations are what we look for.
"""

import json
import math
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, "data", "otc_history.db")


def ci95(p, n):
    if n == 0:
        return None
    z = 1.96
    e = z * math.sqrt(p * (1 - p) / n)
    return [round(100 * (p - e), 2), round(100 * (p + e), 2)]


def main():
    conn = sqlite3.connect(DB)
    assets = [r[0] for r in conn.execute(
        "SELECT DISTINCT asset FROM candles ORDER BY asset")]
    out = {}
    pooled = defaultdict(int)
    hourly = defaultdict(lambda: [0, 0])   # hour → [n_same, n]
    streak_stats = defaultdict(lambda: [0, 0])  # run_len → [n_same, n]

    for a in assets:
        rows = conn.execute(
            "SELECT time, open, close FROM candles WHERE asset=? "
            "ORDER BY time", (a,)).fetchall()
        s = {"n": len(rows), "doji": 0, "up_after_up": 0, "n_up_after_up": 0,
             "up_after_down": 0, "n_up_after_down": 0,
             "same": 0, "n_dir": 0}
        # CLEAN PASS — direction uses ±1; None = no usable previous candle
        prev_dir = None
        run = 0
        run_dir = 0
        for t, o, c in rows:
            if c == o:
                s["doji"] += 1
                prev_dir = None
                run = 0
                continue
            d = 1 if c > o else -1
            s["n_dir"] += 1
            if prev_dir is not None:
                s["same"] += int(d == prev_dir)
                h = (t // 3600) % 24
                hourly[h][0] += int(d == prev_dir)
                hourly[h][1] += 1
                key = min(run, 4)
                streak_stats[key][0] += int(d == prev_dir)
                streak_stats[key][1] += 1
                if prev_dir == 1:
                    s["n_up_after_up"] += 1
                    s["up_after_up"] += (d == 1)
                else:
                    s["n_up_after_down"] += 1
                    s["up_after_down"] += (d == 1)
            if run and d == run_dir:
                run += 1
            else:
                run, run_dir = 1, d
            prev_dir = d
        p_up_aft_up = (s["up_after_up"] / s["n_up_after_up"]
                       if s["n_up_after_up"] else None)
        p_up_aft_dn = (s["up_after_down"] / s["n_up_after_down"]
                       if s["n_up_after_down"] else None)
        follow = s["same"] / (s["n_dir"] - 1) if s["n_dir"] > 1 else None
        out[a] = {
            "n": s["n"], "doji_pct": round(100 * s["doji"] / s["n"], 2),
            "p_up_given_up": round(p_up_aft_up, 4) if p_up_aft_up else None,
            "p_up_given_down": round(p_up_aft_dn, 4) if p_up_aft_dn else None,
            "follow_rate": round(follow, 4) if follow is not None else None,
            "n_dir_pairs": s["n_dir"] - 1,
        }
        pooled["same"] += s["same"]
        pooled["n"] += (s["n_dir"] - 1)

    fr = pooled["same"] / pooled["n"]
    summary = {
        "pooled_follow_rate": round(fr, 4),
        "pooled_follow_ci95": ci95(fr, pooled["n"]),
        "hourly_follow": {h: {"rate": round(v[0] / v[1], 4) if v[1] else None,
                              "n": v[1]} for h, v in sorted(hourly.items())},
        "streak_follow": {str(k): {"rate": round(v[0] / v[1], 4) if v[1] else None,
                                   "n": v[1]}
                          for k, v in sorted(streak_stats.items())},
        "per_pair": out,
    }
    print(json.dumps(summary, indent=1))

    print("\n── বাংলা সারসংক্ষেপ ──")
    print(f"pooled follow-rate: {fr*100:.2f}%  "
          f"(CI95 {summary['pooled_follow_ci95']})")
    for a, v in out.items():
        print(f"  {a:14s} follow={v['follow_rate']*100:6.2f}%  "
              f"up|up={100*(v['p_up_given_up'] or 0):5.2f}%  "
              f"up|down={100*(v['p_up_given_down'] or 0):5.2f}%")
    sig_hours = {h: v for h, v in summary["hourly_follow"].items()
                 if v["n"] > 2000 and v["rate"] and
                 abs(v["rate"] - 0.5) > 0.02}
    if sig_hours:
        print("hourly |dev|>2% (n>2000): "
              + ", ".join(f"{h}h={v['rate']*100:.1f}%" for h, v in
                          sorted(sig_hours.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
