#!/usr/bin/env python3
"""CODE-LEVEL BACKTEST — replay the REAL live signal history through the
NEW fallback code path (engines.base.confluence._fallback_direction).

For every candle i in every asset's sequence (time-ordered):
  • build the CLOSED candle list candles[0..i-1] (strictly past — the engine
    receives exactly this at predict time),
  • call the ACTUAL new cf._fallback_direction({}, {}, 'SIDEWAYS', candles),
  • grade the returned direction against candle i's real open→close move.

This validates the persistence-first logic + fixed tie-break chain
(htf_fade / body_fade) end-to-end on real production data, with zero
look-ahead (persistence stats are computed inside the function from the
passed history only).

Data source (either):
  --json PATH       a signals_full.json dump (list of signal dicts with
                     asset/ctime/a_open/a_close/signal) — produced by
                     paginated GET /api/signals/all?limit=500
  --url BASE        live app base URL — the script paginates /api/signals/all
                     itself (default: the production Railway URL)

Comparison baselines from the 2026-09-11 audit ledger (7,083 candles):
  • OLD engine (recorded live signals): 50.30% (n=6833)
  • OLD tie-break chain bases measured: htf_trend 32%, body_direction 45.5%,
    cluster_majority 44.4%, best_strategy_vote 50.28%
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("QX_DB_PATH", os.path.join(REPO, "signals.db"))

from engines.base import confluence as cf


def fetch_signals(base_url):
    """Paginate /api/signals/all to pull the full graded ledger."""
    all_signals = {}
    before = None
    while True:
        url = f"{base_url.rstrip('/')}/api/signals/all?period=60&limit=500"
        if before:
            url += f"&before_ctime={before}"
        req = urllib.request.Request(url, headers={"User-Agent": "backtest/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
        sigs = data.get("signals", [])
        if not sigs:
            break
        for s in sigs:
            all_signals[(s["asset"], s["ctime"])] = s
        before = min(s["ctime"] for s in sigs)
        print(f"  fetched {len(all_signals)} unique signals...")
        if len(sigs) < 500:
            break
        time.sleep(0.3)
    return list(all_signals.values())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="path to signals_full.json dump")
    ap.add_argument("--url", default="https://binary-signals-app-production-321a.up.railway.app",
                    help="live app base URL (used when --json is absent)")
    args = ap.parse_args()

    if args.json:
        with open(args.json) as f:
            signals = json.load(f)["signals"]
    else:
        print(f"Fetching full ledger from {args.url} ...")
        signals = fetch_signals(args.url)
    print(f"Loaded {len(signals)} signals")
else:
    raise SystemExit("run as a script")

by_asset = defaultdict(list)
for s in signals:
    if s.get('a_open') is not None and s.get('a_close') is not None:
        by_asset[s['asset']].append((s['ctime'], s['a_open'], s['a_close'],
                                     s['signal']))
for a in by_asset:
    by_asset[a].sort(key=lambda x: x[0])

res = {'new': [0, 0], 'persist_used': 0, 'basis': defaultdict(lambda: [0, 0])}
per_asset = {}
persist_debug = []

for asset, rows in by_asset.items():
    pa = [0, 0]
    for i in range(1, len(rows)):
        ctime, o, c = rows[i][0], rows[i][1], rows[i][2]
        if c == o:
            continue  # draw — excluded from win-rate math
        actual_up = c > o
        # strictly-past candle history as engine receives it
        hist = [{'time': t, 'open': oo, 'close': cc, 'high': max(oo, cc),
                 'low': min(oo, cc)}
                for (t, oo, cc, _) in rows[:i]]
        direction, net, basis, best_mod, persist = cf._fallback_direction(
            {}, {}, "SIDEWAYS", hist)
        res['new'][0] += (direction == "CALL") == actual_up
        res['new'][1] += 1
        pa[0] += (direction == "CALL") == actual_up
        pa[1] += 1
        res['basis'][basis][0] += 1
        res['basis'][basis][1] += ((direction == "CALL") == actual_up)
        if persist is not None:
            res['persist_used'] += 1
    per_asset[asset] = pa

n = res['new'][1]
w = res['new'][0]
print("═══ CODE-LEVEL REPLAY: NEW _fallback_direction on real history ═══\n")
print(f"TOTAL: {w}/{n} = {100*w/n:.2f}%   (OLD engine: 50.30%, n=6833)")
print(f"persistence step fired: {res['persist_used']} / {n} "
      f"({100*res['persist_used']/n:.1f}%)")
print("\nBY BASIS:")
for b, (bn, bw) in sorted(res['basis'].items(), key=lambda x: -x[1][0]):
    print(f"  {b:28s} n={bn:5d} win={100*bw/bn:5.2f}%")
print("\nPER ASSET (new code path):")
for a in sorted(per_asset):
    wins, total = per_asset[a]
    if total >= 100:
        print(f"  {a:16s} n={total:4d} win={100*wins/total:5.2f}%")

print("\n═══ VERIFICATION ═══")
ok = (100*w/n) > 50.30
print(f"  new-path win rate {100*w/n:.2f}% vs old engine 50.30% → "
      f"{'✅ IMPROVED' if ok else '❌ NOT IMPROVED'} "
      f"({100*w/n - 50.30:+.2f}pp)")
