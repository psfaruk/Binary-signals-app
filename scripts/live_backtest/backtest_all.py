#!/usr/bin/env python3
"""Walk-forward backtest of the live signal engine over real Quotex history.

Replicates feed.py's live call path exactly:
  * prediction for candle i is made from candles[:i] only (no lookahead)
  * htf_trend derived from the same 5m aggregation feed.py uses
  * recent_accuracy fed back walk-forward from the backtest's own results
  * graded with feed.py's rule: close>open = UP, close==open = draw

Usage: python backtest_all.py <histdir> <outjson>
"""
import json
import os
import sys
import collections

sys.path.insert(0, os.getcwd())

SCRATCH = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("DB_PATH", os.path.join(SCRATCH, "backtest_empty.db"))

import db as _db                                    # noqa: E402
_db.init()   # empty schema — engine adaptation starts from zero knowledge

from feed import _aggregate_5m_closes, _ema_simple  # noqa: E402
from engines import predict as engine_predict       # noqa: E402

WARMUP = 120          # candles of history before the first prediction
RECENT_N = 50         # matches feed.py RECENT_ACCURACY_N default


def htf_trend(candles_1m, period=60):
    """Byte-for-byte port of feed._get_htf_trend's maths (no cache)."""
    if not candles_1m:
        return "SIDEWAYS"
    closes_5m = _aggregate_5m_closes(candles_1m[-105:], period)
    n5 = len(closes_5m)
    if n5 < 9:
        return "SIDEWAYS"
    ema9, ema21 = _ema_simple(closes_5m, 9), _ema_simple(closes_5m, 21)
    sep = abs(ema9 - ema21) / ema21 if ema21 > 0 else 0
    thresh = 0.0003 if n5 >= 21 else (0.0005 if n5 >= 14 else 0.0008)
    if ema9 > ema21 and sep > thresh:
        return "UPTREND"
    if ema9 < ema21 and sep > thresh:
        return "DOWNTREND"
    return "SIDEWAYS"


def grade(sig, candle):
    """feed.py _accuracy(): degenerate candle = skip, close==open = draw."""
    o, c = candle["open"], candle["close"]
    if candle["high"] == candle["low"] == o == c:
        return "skip"
    if abs(c - o) < 1e-9:
        return "draw"
    return "correct" if ((c > o) == (sig == "CALL")) else "wrong"


def backtest_pair(asset, category, candles):
    results = []
    hist = collections.deque(maxlen=RECENT_N)   # walk-forward accuracy feed
    for i in range(WARMUP, len(candles)):
        past = candles[:i]
        # live gates on contiguity too; skip if the previous candle isn't
        # the immediately-preceding minute (weekend / feed gap)
        if candles[i]["time"] - past[-1]["time"] != 60:
            continue
        n_ok = sum(1 for a in hist if a == "correct")
        recent = ((n_ok / len(hist), len(hist)) if len(hist) >= 8
                  else (None, 0))
        try:
            pred = engine_predict(
                [dict(c) for c in past], ticks=[], micro=None, asset=asset,
                htf_trend=htf_trend(past), period=60, category=category,
                recent_accuracy=recent)
        except Exception as e:
            results.append({"t": candles[i]["time"], "err": f"{type(e).__name__}: {e}"})
            continue
        sig = (pred or {}).get("signal")
        if sig not in ("CALL", "PUT"):
            results.append({"t": candles[i]["time"], "signal": sig or "NONE"})
            continue
        acc = grade(sig, candles[i])
        if acc == "skip":
            continue
        if acc in ("correct", "wrong"):
            hist.append(acc)
        reg = (pred.get("regime") or {})
        results.append({
            "t": candles[i]["time"], "signal": sig, "acc": acc,
            "conf": pred.get("confidence"), "score": pred.get("score"),
            "strength": pred.get("strength"), "agree": pred.get("agree"),
            "quality": pred.get("signal_quality"),
            "regime": reg.get("regime"),
            "htf": htf_trend(past),
            "prev_dir": ("UP" if past[-1]["close"] > past[-1]["open"]
                         else "DOWN" if past[-1]["close"] < past[-1]["open"]
                         else "FLAT"),
            "move": round(candles[i]["close"] - candles[i]["open"], 8),
        })
    return results


def main():
    histdir, outpath = sys.argv[1], sys.argv[2]
    allres = {}
    files = sorted(f for f in os.listdir(histdir)
                   if f.endswith(".json") and not f.startswith("_"))
    for fn in files:
        with open(os.path.join(histdir, fn)) as f:
            d = json.load(f)
        cs = d["candles"]
        if len(cs) < WARMUP + 50:
            print(f"  {d['asset']:14s} SKIP (only {len(cs)} candles)", flush=True)
            continue
        res = backtest_pair(d["asset"], d["category"], cs)
        allres[d["asset"]] = {"category": d["category"], "results": res}
        traded = [r for r in res if r.get("acc") in ("correct", "wrong")]
        w = sum(1 for r in traded if r["acc"] == "correct")
        errs = sum(1 for r in res if "err" in r)
        print(f"  {d['asset']:14s} {d['category']:4s} candles={len(cs):6d} "
              f"traded={len(traded):5d} wr={100*w/len(traded) if traded else 0:5.1f}%"
              f"{'  ERRORS=' + str(errs) if errs else ''}", flush=True)
    with open(outpath, "w") as f:
        json.dump(allres, f)
    print(f"\nwrote {outpath}")


if __name__ == "__main__":
    main()
