#!/usr/bin/env python3
"""Out-of-sample win rate of EVERY individual theory, across all pairs.

Each module is run standalone on real history; every ModuleResult it emits is
graded against the very next candle. This isolates each theory's own edge,
independent of the blender, so we can see which of the "58-63% win rate"
claims in the module docstrings survive on fresh data.

Usage: python backtest_theories.py <histdir> <outjson>
"""
import json
import os
import sys
import collections
import math

sys.path.insert(0, os.getcwd())
SCRATCH = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("DB_PATH", os.path.join(SCRATCH, "backtest_empty.db"))

import db as _db          # noqa: E402
_db.init()

from engines.base.context import compute_context           # noqa: E402
from engines.base.modules import (candle_reaction, key_level,  # noqa: E402
                                  pattern)

MODULES = {"candle_reaction": candle_reaction, "key_level": key_level,
           "pattern": pattern}
WARMUP = 120


def theory_key(res):
    """A stable label for one theory: first reason line, trimmed."""
    r = (res.reasons or [""])[0]
    for cut in (" (", " →", ":", " —"):
        i = r.find(cut)
        if i > 0:
            r = r[:i]
    return r.strip()[:60] or res.group


def wilson_lo(k, n, z=1.96):
    """Lower bound of the 95% Wilson interval — the honest 'at least' number."""
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d


def main():
    histdir, outpath = sys.argv[1], sys.argv[2]
    # key -> [correct, total]; also split by pair category
    tally = collections.defaultdict(lambda: [0, 0])
    by_cat = collections.defaultdict(lambda: [0, 0])
    files = sorted(f for f in os.listdir(histdir)
                   if f.endswith(".json") and not f.startswith("_"))
    for fn in files:
        with open(os.path.join(histdir, fn)) as f:
            d = json.load(f)
        cs, cat = d["candles"], d["category"]
        if len(cs) < WARMUP + 50:
            continue
        for i in range(WARMUP, len(cs)):
            past = cs[:i]
            nxt = cs[i]
            if nxt["time"] - past[-1]["time"] != 60:
                continue
            if nxt["high"] == nxt["low"] == nxt["open"] == nxt["close"]:
                continue
            if abs(nxt["close"] - nxt["open"]) < 1e-9:
                continue           # draw — broker refunds, exclude
            up = nxt["close"] > nxt["open"]
            ctx = compute_context(past)
            for mname, mod in MODULES.items():
                try:
                    out = mod.analyze(past, ctx) or []
                except Exception:
                    continue
                for res in out:
                    if res.direction not in ("CALL", "PUT"):
                        continue
                    ok = (res.direction == "CALL") == up
                    k = f"{mname} | {theory_key(res)}"
                    tally[k][0] += ok
                    tally[k][1] += 1
                    ck = f"{k} @{cat}"
                    by_cat[ck][0] += ok
                    by_cat[ck][1] += 1
        print(f"  scanned {d['asset']}", flush=True)

    rows = []
    for k, (c, n) in sorted(tally.items(), key=lambda kv: -kv[1][1]):
        rows.append({"theory": k, "correct": c, "total": n,
                     "wr": round(100 * c / n, 1),
                     "wilson_lo": round(100 * wilson_lo(c, n), 1)})
    cat_rows = [{"theory": k, "correct": c, "total": n,
                 "wr": round(100 * c / n, 1),
                 "wilson_lo": round(100 * wilson_lo(c, n), 1)}
                for k, (c, n) in sorted(by_cat.items(), key=lambda kv: -kv[1][1])]
    with open(outpath, "w") as f:
        json.dump({"global": rows, "by_category": cat_rows}, f, indent=1)

    print(f"\n{'theory':62s} {'n':>7s} {'win%':>7s} {'95%lo':>7s}")
    print("-" * 88)
    for r in rows:
        print(f"{r['theory']:62s} {r['total']:7d} {r['wr']:7.1f} {r['wilson_lo']:7.1f}")


if __name__ == "__main__":
    main()
