#!/usr/bin/env python3
"""scripts/train_otc_model.py — PART 25 continuous-learning entry point.

Trains the T+1/T+2 bundle on ACCUMULATED real OTC candles and registers it
ONLY when it passes the PART 29 production gate on unseen walk-forward
data. A model that fails the gate is never registered — the live
predictor keeps its honest "মডেল প্রস্তুত নয় / NO SIGNAL" state.

DATA SOURCES
  --source micro    candle_micro rows inside the app DB (what the live
                    feed has accumulated; default, PART 1 same-feed rule)
  --source history  data/otc_history.db written by fetch_otc_history.py

PART 29 GATE (all must hold, else NOT registered):
  1. walk-forward directional accuracy (best candidate, pooled unseen
     test folds) > max(always-UP, prev-candle-direction, reversal)
     baseline + 1.5pp;
  2. logloss < ln(2)  (better than coin-flip confidence);
  3. shuffle probe < 0.53  (no leakage signature);
  4. ≥ 400 pooled test predictions per horizon.

Usage:
  python3 scripts/train_otc_model.py --db data/signals.db --source micro
  python3 scripts/train_otc_model.py --source history --assets EURUSD_otc
"""

import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from core.otc_dataset import build_dataset, load_candles_from_db
# UNIFIED-SIGNAL (2026-09-13): manual full trainer uses the SAME unified
# feature set as the fast-train daemon (classic strategy votes included).
from core.otc_predict.features_ext import (build_unified_row,
                                           UNIFIED_FEATURE_NAMES)
from core.otc_predict.models import (CANDIDATES, fit_candidate,
                                     platt_calibrate, apply_platt,
                                     ModelBundle, save_bundle, SKLEARN_OK)
from core.otc_predict.walk_forward import _folds, _fit_predict_fold

WINDOW = 50
EMBARGO = 2
MIN_TRAIN = 2500
VAL_FRAC = 0.2
BASELINE_MARGIN_PP = 1.5
SHUFFLE_MAX = 0.53
MIN_TEST_PRED = 400


def train_and_gate(rows, cand_names=None, seed=13):
    """Walk-forward every candidate; gate the winner against baselines.
    Returns (report_dict, best_bundle_or_None).

    Baselines use each row's own `direction` feature (the prediction-time
    candle's direction, +1/-1) — valid for pooled cross-asset rows too.
    """
    import random
    import numpy as np
    rng = random.Random(seed)
    cand_names = cand_names or list(CANDIDATES().keys())
    n = len(rows)
    cuts = _folds(n, 4, None)

    pooled = {h: {c: {"p": [], "y": []} for c in cand_names}
              for h in ("y1_up", "y2_up")}
    base = {h: {"always_call": [0, 0], "prev_dir": [0, 0],
                "rev_dir": [0, 0]} for h in ("y1_up", "y2_up")}

    for (tr_end, te_end) in cuts:
        for h in ("y1_up", "y2_up"):
            # baselines on the same test rows
            for i in range(tr_end + EMBARGO, te_end):
                r = rows[i]
                y = 1 if r[h] else 0
                cur_dir = 1 if r.get("direction", 0) > 0 else 0
                b = base[h]
                b["always_call"][1] += 1
                b["always_call"][0] += y
                b["prev_dir"][1] += 1
                b["prev_dir"][0] += (y == cur_dir)
                b["rev_dir"][1] += 1
                b["rev_dir"][0] += (y == (1 - cur_dir))
            for name in cand_names:
                res = _fit_predict_fold(rows, UNIFIED_FEATURE_NAMES, h,
                                        [name], tr_end, te_end)
                _, p, y, _ = res[0]
                pooled[h][name]["p"].extend(float(x) for x in p)
                pooled[h][name]["y"].extend(int(v) for v in y)

    def _stats(p, y):
        if not p:
            return None
        acc = sum(1 for pi, yi in zip(p, y)
                  if (pi >= 0.5) == (yi == 1)) / len(p)
        ll = 0.0
        for pi, yi in zip(p, y):
            pi = min(max(pi, 1e-6), 1 - 1e-6)
            ll += math.log(pi) if yi else math.log(1 - pi)
        return {"n": len(p), "acc": round(acc, 4),
                "logloss": round(-ll / len(p), 4)}

    report = {"rows": n, "folds": len(cuts), "candidates": {}, "gate": {}}
    best_overall = None
    for h in ("y1_up", "y2_up"):
        stats = {c: _stats(v["p"], v["y"]) for c, v in pooled[h].items()}
        stats = {c: s for c, s in stats.items() if s}
        report["candidates"][h] = stats
        if not stats:
            continue
        cname, cs = min(stats.items(), key=lambda kv: kv[1]["logloss"])
        ys = pooled[h][cname]["y"][:]
        rng.shuffle(ys)
        ps = pooled[h][cname]["p"]
        shuf_acc = sum(1 for pi, yi in zip(ps, ys)
                       if (pi >= 0.5) == (yi == 1)) / len(ps)
        bl = base[h]
        bl_rates = {
            "always_call": 100 * bl["always_call"][0] / bl["always_call"][1],
            "prev_dir": 100 * bl["prev_dir"][0] / bl["prev_dir"][1],
            "rev_dir": 100 * bl["rev_dir"][0] / bl["rev_dir"][1],
        }
        acc_pp = 100 * cs["acc"]
        best_base = max(bl_rates.values())
        checks = {
            "beats_baseline_1.5pp": acc_pp > best_base + BASELINE_MARGIN_PP,
            "logloss_below_coinflip": cs["logloss"] < math.log(2),
            "shuffle_probe_clean": shuf_acc < SHUFFLE_MAX,
            "enough_test_rows": cs["n"] >= MIN_TEST_PRED,
        }
        report["gate"][h] = {
            "selected": cname, **cs,
            "acc_pct": round(acc_pp, 2),
            "baselines": {k: round(v, 2) for k, v in bl_rates.items()},
            "shuffle_acc": round(shuf_acc, 4),
            "checks": checks,
            "pass": all(checks.values()),
        }
        if best_overall is None or cs["logloss"] < best_overall[1]:
            best_overall = (h, cs["logloss"], cname)

    all_pass = bool(report["gate"]) and all(
        g["pass"] for g in report["gate"].values())
    report["gate"]["ALL"] = {"pass": all_pass}

    if not all_pass:
        return report, None

    # retrain the winning candidate on ALL rows for the production bundle
    # (walk-forward proved the family; the shipped model uses every minute)
    version = time.strftime("v%Y%m%d-%H%M", time.gmtime())
    bundle = ModelBundle(version, UNIFIED_FEATURE_NAMES, None, None,
                         {"trained_rows": n, "walk_forward": report["gate"]})
    import numpy as np
    Xall = np.array([[r[k] for k in UNIFIED_FEATURE_NAMES] for r in rows])
    for h, key, slot in (("y1_up", 1, "t1"), ("y2_up", 2, "t2")):
        yall = np.array([1 if r[h] else 0 for r in rows])
        val_n = max(200, int(len(Xall) * VAL_FRAC))
        model = fit_candidate(best_overall[2], Xall[:-val_n], yall[:-val_n])
        coefs = platt_calibrate(model, Xall[-val_n:], yall[-val_n:])
        setattr(bundle, slot, {"model": model, "platt": coefs,
                               "name": best_overall[2]})
    return report, bundle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(REPO, "data", "signals.db"))
    ap.add_argument("--source", choices=["micro", "history"],
                    default="micro")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--assets", default=None)
    ap.add_argument("--register", action="store_true",
                    help="actually register when the gate passes "
                         "(default: evaluate only)")
    args = ap.parse_args()

    if not SKLEARN_OK:
        print("sklearn unavailable — cannot train")
        return 2

    if args.source == "micro":
        candles_by_asset = load_candles_from_db(args.db, days=args.days)
    else:
        hist = os.path.join(REPO, "data", "otc_history.db")
        import sqlite3
        conn = sqlite3.connect(hist)
        ar = [r[0] for r in conn.execute(
            "SELECT DISTINCT asset FROM candles")]
        candles_by_asset = {}
        for a in ar:
            cs = [{"time": t, "open": o, "high": h, "low": l, "close": c}
                  for t, o, h, l, c in conn.execute(
                      "SELECT time, open, high, low, close FROM candles "
                      "WHERE asset=? ORDER BY time", (a,))]
            candles_by_asset[a] = cs
        conn.close()

    if args.assets:
        keep = {x.strip() for x in args.assets.split(",")}
        candles_by_asset = {a: v for a, v in candles_by_asset.items()
                            if a in keep}
    if not candles_by_asset:
        print("no candles found — is the feed data present?")
        return 2

    rows, dstats = build_dataset(candles_by_asset, window=WINDOW,
                                 micro=(args.source == "micro"),
                                 feature_fn=build_unified_row)
    print(f"[train] dataset: {dstats['rows']} rows from "
          f"{len(candles_by_asset)} pairs "
          f"(doji dropped t1={dstats['dropped_doji_t1']} "
          f"t2={dstats['dropped_doji_t2']})")

    by_asset = {}
    for r in rows:
        by_asset.setdefault(r["asset"], []).append(r)

    registered = []
    # ── per-pair bundles (PART 22) ──────────────────────────────────────
    for asset, arows in sorted(by_asset.items()):
        if len(arows) < MIN_TEST_PRED * 3:
            print(f"[train] {asset}: {len(arows)} rows — too few, skipped")
            continue
        report, bundle = train_and_gate(arows)
        gate = report["gate"]
        passed = gate.get("ALL", {}).get("pass")
        print(f"[train] {asset}: gate "
              f"{'PASS' if passed else 'FAIL'}")
        for h, g in gate.items():
            if h == "ALL" or not isinstance(g, dict) or "selected" not in g:
                continue
            print(f"    {h}: {g['selected']} acc={g['acc_pct']}% "
                  f"ll={g['logloss']} baselines={g['baselines']} "
                  f"shuffle={g['shuffle_acc']}")
        if bundle is not None:
            path = save_bundle(bundle)
            print(f"[train] {asset}: bundle {bundle.version} → {path}")
            if args.register:
                from core.otc_predict.tracker import register_model
                register_model(asset, bundle.version, "pair", asset,
                               report["gate"], path, activate=True)
                registered.append(asset)
        else:
            print(f"[train] {asset}: NO registration — gate failed "
                  f"(PART 29: model does not beat baseline on unseen data)")

    # ── pooled global fallback: only when no per-pair model passed ──────
    if not registered and len(by_asset) > 1:
        report, bundle = train_and_gate(rows)
        passed = report["gate"].get("ALL", {}).get("pass")
        print(f"[train] pooled-global gate: "
              f"{'PASS' if passed else 'FAIL'}")
        for h, g in report["gate"].items():
            if h == "ALL" or not isinstance(g, dict) or "selected" not in g:
                continue
            print(f"    {h}: {g['selected']} acc={g['acc_pct']}% "
                  f"ll={g['logloss']} baselines={g['baselines']} "
                  f"shuffle={g['shuffle_acc']}")
        if bundle is not None:
            path = save_bundle(bundle)
            print(f"[train] global: bundle {bundle.version} → {path}")
            if args.register:
                from core.otc_predict.tracker import register_model
                register_model("global", bundle.version, "global", "",
                               report["gate"], path, activate=True)
                registered.append("global")

    if registered:
        print(f"[train] registered: {registered}")
    else:
        print("[train] nothing registered — the live predictor stays in "
              "its honest no-model state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
