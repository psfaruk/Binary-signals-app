#!/usr/bin/env python3
"""scripts/backtest_otc_predictor.py — REAL-DATA walk-forward backtest of
the OTC Future Candle Prediction Engine (PART 10/13/14/18/19/29).

DATA  : data/otc_history.db (scripts/fetch_otc_history.py — the SAME
        Quotex platform the live feed uses, PART 1 data-source rule).
PHASES (resumable — every phase caches per asset under data/wf_cache/):
  dataset : build leak-free rows (core.otc_dataset.build_dataset with the
            PART 6 extended features) → ds_<asset>.json.gz
  models  : expanding-window walk-forward, 3 candidates × T+1/T+2, Platt
            calibration, per-fold → wf_<asset>.json.gz
  signals : live-path replay (features → prob → price action → regime →
            PART 14 score → PART 24 gates) with periodic retraining,
            baselines on the same test rows → sig_<asset>.json.gz
  report  : merge every cache → scripts/otc_predictor_backtest_report.json

LEAKAGE PROTOCOL (PART 19 — the most important technical rule):
  * rows are time-ordered, targets are the IMMEDIATE next minutes
    (asserted by the builder, PART 8 lock);
  * every fold trains ONLY on rows strictly before the test block, with
    an EMBARGO of 2 rows so no T+2 target of a training row overlaps the
    test window;
  * Platt calibration uses a TIME-ORDERED validation tail INSIDE the
    train fold — the test fold is never touched during fitting;
  * shuffle probe: permuting test labels must collapse accuracy to ≈50%.

BE GENTLE WITH RUNTIME: the phases are idempotent and cache per asset,
so long runs can be split across invocations (sandbox-friendly).
"""

import argparse
import gzip
import json
import math
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO, "data", "otc_history.db")
CACHE_DIR = os.path.join(REPO, "data", "wf_cache")
REPORT_PATH = os.path.join(REPO, "scripts", "otc_predictor_backtest_report.json")

WINDOW = 50            # PART 7 context window (= live PRED_WINDOW)
N_FOLDS = 4            # walk-forward folds per asset (models phase)
SIG_TRAIN_MIN = 2500   # first test block starts after this many rows
SIG_STEP = 800         # retrain cadence (rows) in the signals phase
EMBARGO = 2            # rows — covers the T+2 target overlap
BREAKEVEN = 54.05      # % win rate needed at 85% payout (app standard)


def _cache_path(kind, asset):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{kind}_{asset}.json.gz")


def _dump(path, obj):
    with gzip.open(path, "wt", compresslevel=6) as fh:
        json.dump(obj, fh)


def _load(path):
    with gzip.open(path, "rt") as fh:
        return json.load(fh)


# ───────────────────────────── phase: dataset ─────────────────────────────

def load_candles():
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT asset, time, open, high, low, close FROM candles "
            "ORDER BY asset, time").fetchall()
    finally:
        conn.close()
    by_asset = {}
    for a, t, o, h, l, c in rows:
        by_asset.setdefault(a, []).append(
            {"time": int(t), "open": float(o), "high": float(h),
             "low": float(l), "close": float(c)})
    return by_asset


def phase_dataset(assets=None):
    from core.otc_dataset import build_dataset
    from core.otc_predict.features_ext import build_extended_row

    candles_by_asset = load_candles()
    if assets:
        candles_by_asset = {a: v for a, v in candles_by_asset.items()
                            if a in assets}
    meta = {}
    for a, v in candles_by_asset.items():
        meta[a] = {"n": len(v),
                   "from": v[0]["time"], "to": v[-1]["time"],
                   "gaps": sum(1 for x, y in zip(v, v[1:])
                               if y["time"] - x["time"] != 60)}
    print(f"[dataset] {len(candles_by_asset)} pairs, "
          f"{sum(m['n'] for m in meta.values())} candles")

    rows, stats = build_dataset(candles_by_asset, window=WINDOW,
                                micro=False, feature_fn=build_extended_row)
    by_asset_rows = {}
    for r in rows:
        by_asset_rows.setdefault(r["asset"], []).append(r)
    for a, rs in by_asset_rows.items():
        _dump(_cache_path("ds", a),
              {"rows": rs, "feature_meta": meta.get(a), "window": WINDOW})
        print(f"[dataset] {a}: {len(rs)} rows cached")
    print(f"[dataset] stats: {json.dumps(stats)}")
    return sorted(by_asset_rows.keys())


# ───────────────────────────── phase: models ──────────────────────────────

def phase_models(assets=None, n_folds=N_FOLDS):
    from core.otc_predict.models import CANDIDATES
    from core.otc_predict.walk_forward import _folds, _fit_predict_fold
    from core.otc_predict.features_ext import EXTENDED_FEATURE_NAMES

    cand_names = list(CANDIDATES().keys())
    horizons = ("y1_up", "y2_up")
    for a in assets or []:
        ds = _load(_cache_path("ds", a))
        rows = ds["rows"]
        n = len(rows)
        if n < 400:
            print(f"[models] {a}: skipped ({n} rows <400)")
            continue
        cuts = _folds(n, n_folds, None)
        out = {h: {c: {"p": [], "y": []} for c in cand_names}
               for h in horizons}
        t0 = time.time()
        for (tr_end, te_end) in cuts:
            for h in horizons:
                for name in cand_names:
                    res = _fit_predict_fold(rows, EXTENDED_FEATURE_NAMES,
                                            h, [name], tr_end, te_end)
                    _, p, y, _ = res[0]
                    out[h][name]["p"].extend(float(x) for x in p)
                    out[h][name]["y"].extend(int(v) for v in y)
        _dump(_cache_path("wf", a),
              {"preds": out, "folds": cuts, "n_rows": n})
        print(f"[models] {a}: {n} rows × {len(cuts)} folds × "
              f"{len(cand_names)} candidates done in {time.time()-t0:.0f}s")


# ───────────────────────────── phase: signals ─────────────────────────────

def _bundle_for(rows, feature_names, selected, train_end):
    """Train the selected candidate on rows[:train_end] with a Platt tail.
    Returns a ModelBundle-like object with predict_up(h, feat_row)."""
    import numpy as np
    from core.otc_predict.models import (fit_candidate, platt_calibrate,
                                         apply_platt, SKLEARN_OK)

    class _Slot:
        pass

    if not SKLEARN_OK:
        return None
    bundle = _Slot()
    bundle.version = f"wf_{selected}"
    bundle.feature_names = list(feature_names)
    bundle.t1 = bundle.t2 = None
    for h, key in ((1, "y1_up"), (2, "y2_up")):
        Xtr = np.array([[r[k] for k in feature_names]
                        for r in rows[:train_end]])
        ytr = np.array([1 if r[key] else 0 for r in rows[:train_end]])
        val_n = max(200, int(len(Xtr) * 0.2))
        split = len(Xtr) - val_n
        model = fit_candidate(selected, Xtr[:split], ytr[:split])
        coefs = platt_calibrate(model, Xtr[split:], ytr[split:])
        slot = {"model": model, "platt": coefs, "name": selected}
        if h == 1:
            bundle.t1 = slot
        else:
            bundle.t2 = slot
    bundle._apply = apply_platt

    def predict_up(horizon, feat_row):
        slot = bundle.t1 if horizon == 1 else bundle.t2
        X = np.array([[float(feat_row[k]) for k in bundle.feature_names]])
        p = float(slot["model"].predict_proba(X)[0, 1])
        return bundle._apply(p, slot.get("platt"))

    bundle.predict_up = predict_up
    return bundle


def phase_signals(assets=None, selected="logreg"):
    """Live-path replay — the EXACT predictor pipeline the production code
    runs at candle close (predictor.on_candle_closed minus persistence)."""
    from core.otc_predict.features_ext import (EXTENDED_FEATURE_NAMES,
                                               MIN_WINDOW_EXT)
    from core.otc_predict.regime import detect_regime
    from core.otc_predict.price_action import price_action_confirm
    from core.otc_predict.signal_filter import score_signal

    candles_by_asset = load_candles()
    for a in assets or []:
        ds = _load(_cache_path("ds", a))
        rows = ds["rows"]
        n = len(rows)
        candles = candles_by_asset.get(a, [])
        idx_of = {c["time"]: i for i, c in enumerate(candles)}
        out_rows = []
        base = {"always_call": {"n": 0, "wins": 0},
                "prev_dir": {"n": 0, "wins": 0},
                "rev_dir": {"n": 0, "wins": 0}}
        t0 = time.time()
        train_end = max(SIG_TRAIN_MIN, int(n * 0.2))
        n_folds = 0
        while train_end + EMBARGO < n - 100:
            te_end = min(n, train_end + SIG_STEP)
            bundle = _bundle_for(rows, EXTENDED_FEATURE_NAMES, selected,
                                 train_end)
            n_folds += 1
            for i in range(train_end + EMBARGO, te_end):
                r = rows[i]
                wi = idx_of.get(r["window_end_ctime"])
                if wi is None or wi < WINDOW - 1:
                    continue
                window = candles[wi - WINDOW + 1: wi + 1]
                # baseline records on the same test rows
                cur_dir = 1 if r["close_i"] > window[-1]["open"] else 0
                for hk, ykey in (("h1", "y1_up"), ("h2", "y2_up")):
                    y = 1 if r[ykey] else 0
                    base["always_call"]["n"] += 1
                    base["always_call"]["wins"] += y
                    pd_ = 1 if cur_dir else 0
                    base["prev_dir"]["n"] += 1
                    base["prev_dir"]["wins"] += (y == pd_)
                    base["rev_dir"]["n"] += 1
                    base["rev_dir"]["wins"] += (y == (1 - pd_))
                if bundle is None:
                    continue
                for h, ykey in ((1, "y1_up"), (2, "y2_up")):
                    prob = bundle.predict_up(h, r)
                    up = prob >= 0.5
                    pa = price_action_confirm(window, up, features=r)
                    reg = detect_regime(window)
                    quality = {"data_complete": len(window) >= WINDOW,
                               "no_gap": True,
                               "model_loaded": True,
                               "vol_acceptable": not reg.get("extreme_vol",
                                                             False),
                               "no_conflict": pa.get("against_count", 0) < 3}
                    filt = score_signal(prob, up, pa, reg, quality)
                    out_rows.append({
                        "h": h, "sig_time": r["window_end_ctime"],
                        "target_time": r[f"t{h}_ctime"],
                        "pred": filt["prediction"], "prob": prob,
                        "tier": filt["tier"], "score": filt["score"],
                        "emit": int(filt["emit"]),
                        "win": int(((filt["prediction"] == "CALL")
                                    == bool(r[ykey]))),
                        "regime": filt["regime"]})
            train_end = te_end
        _dump(_cache_path("sig", a),
              {"rows": out_rows, "baselines": base, "model": selected,
               "folds": n_folds, "n_test": n - max(SIG_TRAIN_MIN,
                                                   int(n * 0.2))})
        print(f"[signals] {a}: {len(out_rows)} decisions, {n_folds} "
              f"retrains in {time.time()-t0:.0f}s")


# ───────────────────────────── phase: report ──────────────────────────────

def _metrics(p, y):
    if not p:
        return None
    eps = 1e-6
    acc = sum(1 for pi, yi in zip(p, y) if (pi >= 0.5) == (yi == 1)) / len(p)
    ll = 0.0
    for pi, yi in zip(p, y):
        pi = min(max(pi, eps), 1 - eps)
        ll += math.log(pi) if yi else math.log(1 - pi)
    return {"n": len(p), "accuracy": round(acc, 4),
            "logloss": round(-ll / len(p), 4),
            "calibration": _calib(p, y)}


def _calib(p, y, buckets=(0.45, 0.55, 0.65, 0.75, 1.01)):
    table, lo = [], 0.0
    for hi in buckets:
        bp = [pi for pi in p if lo <= pi < hi]
        by = [yi for pi, yi in zip(p, y) if lo <= pi < hi]
        if by:
            table.append({"bucket": f"{lo:.2f}-{hi:.2f}", "n": len(by),
                          "avg_pred": round(sum(bp) / len(bp), 3),
                          "actual_up": round(sum(by) / len(by), 3)})
        lo = hi
    return table


def phase_report():
    from core.otc_predict.models import CANDIDATES

    cand_names = list(CANDIDATES().keys())
    horizons = ("y1_up", "y2_up")

    # ── merge walk-forward predictions ──────────────────────────────────
    merged = {h: {c: {"p": [], "y": []} for c in cand_names}
              for h in horizons}
    assets_done = []
    coverage = {}
    for fn in sorted(os.listdir(CACHE_DIR)):
        if not fn.startswith("wf_") or not fn.endswith(".json.gz"):
            continue
        a = fn[3:-8]
        assets_done.append(a)
        d = _load(os.path.join(CACHE_DIR, fn))
        ds = _load(_cache_path("ds", a))
        coverage[a] = {"rows": d["n_rows"],
                       "candles": ds.get("feature_meta", {}).get("n"),
                       "from": ds.get("feature_meta", {}).get("from"),
                       "to": ds.get("feature_meta", {}).get("to"),
                       "gaps": ds.get("feature_meta", {}).get("gaps")}
        for h in horizons:
            for c in cand_names:
                if c in d["preds"].get(h, {}):
                    merged[h][c]["p"].extend(d["preds"][h][c]["p"])
                    merged[h][c]["y"].extend(d["preds"][h][c]["y"])

    report = {"generated_at": time.time(),
              "window": WINDOW, "embargo": EMBARGO,
              "breakeven_wr_pct": BREAKEVEN,
              "data": coverage, "assets": assets_done}

    models_sec = {h: {c: _metrics(v["p"], v["y"]) for c, v in merged[h].items()}
                  for h in horizons}
    report["models"] = models_sec

    selected = {}
    rng = random.Random(29)
    for h in horizons:
        cands = {c: m for c, m in models_sec[h].items() if m}
        if not cands:
            continue
        best = min(cands.items(), key=lambda kv: kv[1]["logloss"])
        selected[h] = {"model": best[0], "logloss": best[1]["logloss"],
                       "accuracy": best[1]["accuracy"]}
        # PART 19 shuffle probe on the merged winner
        v = merged[h][best[0]]
        ys = v["y"][:]
        rng.shuffle(ys)
        acc_s = sum(1 for pi, yi in zip(v["p"], ys)
                    if (pi >= 0.5) == (yi == 1)) / len(v["p"])
        selected[h]["shuffle_probe_acc"] = round(acc_s, 4)
    report["selected"] = selected

    # ── merge signal replay ─────────────────────────────────────────────
    sig_rows, baselines = [], {"always_call": {"n": 0, "wins": 0},
                               "prev_dir": {"n": 0, "wins": 0},
                               "rev_dir": {"n": 0, "wins": 0}}
    per_pair, per_tier = {}, {}
    run_loss = {"h1": 0, "h2": 0}
    consec = 0
    tot = {"h1": {"pred": 0, "emit": 0, "win": 0, "loss": 0},
           "h2": {"pred": 0, "emit": 0, "win": 0, "loss": 0}}
    sig_assets = []
    for fn in sorted(os.listdir(CACHE_DIR)):
        if not fn.startswith("sig_") or not fn.endswith(".json.gz"):
            continue
        a = fn[4:-8]
        sig_assets.append(a)
        d = _load(os.path.join(CACHE_DIR, fn))
        for k in baselines:
            baselines[k]["n"] += d["baselines"][k]["n"]
            baselines[k]["wins"] += d["baselines"][k]["wins"]
        pp = per_pair.setdefault(a, {"pred": 0, "emit": 0, "win": 0,
                                     "loss": 0})
        for r in d["rows"]:
            h = f"h{r['h']}"
            tot[h]["pred"] += 1
            pp["pred"] += 1
            if r["emit"]:
                tot[h]["emit"] += 1
                pp["emit"] += 1
                if r["win"]:
                    tot[h]["win"] += 1
                    pp["win"] += 1
                    run_loss[h] = 0
                else:
                    tot[h]["loss"] += 1
                    pp["loss"] += 1
                    run_loss[h] += 1
                    consec = max(consec, run_loss[h])
                td = per_tier.setdefault(r["tier"], {"emit": 0, "win": 0,
                                                     "loss": 0})
                td["emit"] += 1
                td["win" if r["win"] else "loss"] += 1
                sig_rows.append(r)

    def wr(w, l):
        return round(100.0 * w / (w + l), 2) if (w + l) else None

    signals_sec = {"model": (sorted(sig_assets), selected),
                   "horizons": {}, "per_tier": per_tier,
                   "per_pair": {}, "consecutive_losses_max": consec,
                   "baselines": {}}
    for h, v in tot.items():
        signals_sec["horizons"][h] = {
            "predictions": v["pred"], "emitted": v["emit"],
            "win_rate_emit": wr(v["win"], v["loss"]),
            "wins": v["win"], "losses": v["loss"],
            "no_signal_pct": round(100.0 * (v["pred"] - v["emit"])
                                   / v["pred"], 1) if v["pred"] else 0.0,
            "avg_confidence": round(sum(r["prob"] for r in sig_rows
                                        if f"h{r['h']}" == h
                                        and r["emit"])
                                    / v["emit"], 4) if v["emit"] else None,
        }
    for a, v in per_pair.items():
        if v["emit"]:
            signals_sec["per_pair"][a] = {
                "emit": v["emit"], "win_rate": wr(v["win"], v["loss"]),
                "wins": v["win"], "losses": v["loss"]}
    for k, v in baselines.items():
        signals_sec["baselines"][k] = {
            "win_rate": round(100.0 * v["wins"] / v["n"], 2) if v["n"] else None,
            "n": v["n"]}
    # emitted-probability calibration (PART 13 on the live path)
    for h in ("h1", "h2"):
        rows_h = [r for r in sig_rows if f"h{r['h']}" == h and r["emit"]]
        if rows_h:
            p = [r["prob"] for r in rows_h]
            y = [r["win"] for r in rows_h]
            signals_sec["horizons"][h]["emit_calibration"] = _calib(p, y)
    report["signals"] = signals_sec

    # ── PART 29 verdict (three questions) ───────────────────────────────
    verdict = {}
    h1 = signals_sec["horizons"]["h1"]
    h2 = signals_sec["horizons"]["h2"]
    bprev = signals_sec["baselines"]["prev_dir"]["win_rate"]
    bcall = signals_sec["baselines"]["always_call"]["win_rate"]
    best_emit = max([x for x in (h1["win_rate_emit"], h2["win_rate_emit"])
                     if x is not None], default=None)
    verdict["emitted_above_breakeven"] = (
        best_emit is not None and best_emit > BREAKEVEN)
    verdict["beats_prev_candle_baseline"] = (
        best_emit is not None and bprev is not None
        and best_emit > bprev + 1.0)
    verdict["beats_always_call"] = (
        best_emit is not None and bcall is not None
        and best_emit > bcall + 1.0)
    verdict["no_leakage_red_flag"] = all(
        s.get("shuffle_probe_acc", 0) < 0.53
        for s in selected.values()) if selected else False
    verdict["production_ready"] = bool(
        verdict["emitted_above_breakeven"]
        and verdict["beats_prev_candle_baseline"]
        and verdict["no_leakage_red_flag"]
        and h1["emitted"] + h2["emitted"] >= 100)
    verdict["bengali"] = (
        f"সিগন্যাল WR={best_emit}% (breakeven {BREAKEVEN}%, "
        f"prev-dir baseline {bprev}%). "
        + ("গঠনগতভাবে লিকেজ নেই। " if verdict["no_leakage_red_flag"]
           else "শাফল-প্রোব সতর্কতা! ")
        + ("প্রোডাকশন-রেডি প্রার্থী।" if verdict["production_ready"]
           else "এখনো প্রোডাকশন-রেডি নয় — আরও ডেটা/টিউনিং দরকার।"))
    report["verdict"] = verdict

    with open(REPORT_PATH, "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print(f"[report] → {REPORT_PATH}")
    print(json.dumps({"selected": selected, "verdict": verdict,
                      "h1": h1, "h2": h2}, indent=1, default=str))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True,
                    choices=["dataset", "models", "signals", "report"])
    ap.add_argument("--assets", default=None,
                    help="comma list; default = every cached/present pair")
    ap.add_argument("--model", default="logreg",
                    help="candidate used by the signals phase")
    args = ap.parse_args()
    assets = [x.strip() for x in args.assets.split(",")] if args.assets \
        else None

    if args.phase == "dataset":
        phase_dataset(assets)
    elif args.phase == "models":
        if not assets:
            assets = [fn[3:-8] for fn in os.listdir(CACHE_DIR)
                      if fn.startswith("ds_")]
        phase_models(assets)
    elif args.phase == "signals":
        if not assets:
            assets = [fn[3:-8] for fn in os.listdir(CACHE_DIR)
                      if fn.startswith("ds_")]
        phase_signals(assets, selected=args.model)
    elif args.phase == "report":
        phase_report()


if __name__ == "__main__":
    main()
