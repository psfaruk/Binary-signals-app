#!/usr/bin/env python3
"""Package the live production signals (2026-09-18) into a compact,
reproducible dataset for scripts/backtest_history_gate.py.

Source: https://binary-signals-app-production.up.railway.app
  /api/signals/all?period=60&category=otc&limit=500   (108 signals)
  /api/signals/all?period=60&category=real&limit=500  (39 signals)
Captured 2026-09-18 — the app's Quotex feed token was expired at capture
time, so this is the last ~30 min of graded signals the old retention
policy had left. Fields kept: asset, period, ctime, signal, accuracy,
confidence, strategy, regime.
"""
import json
import os

LIVE_DIR = "/home/z/my-project/data/live"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "data", "live_signals_2026-09-18.json")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

rows = []
for cat, fn in (("otc", "api_signals_all_period_60_category_otc_limit_500.json"),
                ("real", "api_signals_all_period_60_category_real_limit_500.json")):
    with open(os.path.join(LIVE_DIR, fn)) as f:
        d = json.load(f)
    for s in d.get("signals", []):
        if s.get("accuracy") not in ("correct", "wrong"):
            continue  # draws/skips never enter the gate math either
        rows.append({
            "asset": s["asset"],
            "period": s["period"],
            "ctime": s["ctime"],
            "signal": s.get("signal"),
            "accuracy": s["accuracy"],
            "confidence": s.get("confidence"),
            "strategy": s.get("strategy"),
            "regime": s.get("regime"),
            "category": cat,
        })

rows.sort(key=lambda r: (r["ctime"], r["asset"]))
with open(OUT, "w") as f:
    json.dump({"captured": "2026-09-18", "source": "binary-signals-app-production.up.railway.app",
               "count": len(rows), "signals": rows}, f, indent=1)
print(f"Saved {len(rows)} graded live signals → {OUT}")
