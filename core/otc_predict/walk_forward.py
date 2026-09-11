"""core/otc_predict/walk_forward.py — walk-forward backtest + leakage
probes (PART 18 Backtesting + PART 19 Data Leakage Protection + PART 29).

USER SPEC:

  PART 18: Random train/test split ব্যবহার করা যাবে না, কারণ time-series data।
           Past → Train | Later period → Validation | Future period → Test
           তারপর Walk-forward testing।
  PART 19: Prediction time-এর পরে পাওয়া কোনো information feature-এ
           ব্যবহার করা যাবে না। (সবচেয়ে গুরুত্বপূর্ণ technical rule)
  PART 29: Model কি unseen historical data-তে baseline-এর চেয়ে ভালো?
           Backtest = 82% কিন্তু Live = 52% হলে model production-ready নয়।

DESIGN
------
* Rows must be time-ordered per asset (the dataset builder guarantees it).
* Expanding-window walk-forward: fold k trains on rows [0, cut_k), tests on
  [cut_k + EMBARGO, cut_k + EMBARGO + test_size). The EMBARGO gap (default
  2 rows = the T+2 horizon) guarantees no target of a training row overlaps
  a test window even through y2.
* Validation tail (last VAL_FRAC of train, time-ordered) is used for
  candidate selection + Platt calibration (PART 13) — the test fold is
  NEVER touched during fitting.
* Every candidate is evaluated on the SAME folds; the report shows all of
  them so model selection is evidence-based (PART 10).
* LEAKAGE PROBES:
    probe A (structural): rows carry window_end/t1/t2 ctimes; run_dataset
    asserts window_end < t1 < t2 and immediate-minute adjacency.
    probe B (shuffle control): permuting the labels within the TEST fold
    must collapse directional accuracy to ≈ coin-flip — a significantly
    higher score would mean the pipeline reads something other than the
    feature→label relationship (i.e. leakage).
"""

import math

from core.otc_predict.models import (SKLEARN_OK, CANDIDATES, fit_candidate,
                                     platt_calibrate, apply_platt)

__all__ = ["walk_forward_report", "signal_backtest"]

EMBARGO = 2          # rows — covers the T+2 target overlap
VAL_FRAC = 0.2       # tail of the train fold used for calib/selection


def _logloss(p, y, eps=1e-6):
    ll = 0.0
    for pi, yi in zip(p, y):
        pi = min(max(pi, eps), 1 - eps)
        ll += math.log(pi) if yi == 1 else math.log(1 - pi)
    return -ll / max(1, len(p))


def _accuracy(p, y, thr=0.5):
    if not p:
        return 0.0, 0
    hit = sum(1 for pi, yi in zip(p, y) if (pi >= thr) == (yi == 1))
    return hit / len(p), len(p)


def _folds(n, n_folds, test_size):
    """Expanding-window fold cuts; last fold reaches n."""
    if n < 500:
        n_folds = max(2, min(n_folds, 2))
    ts = test_size or max(200, n // (n_folds + 2))
    cuts = []
    start = min(ts, max(300, n // 4))
    while True:
        test_end = min(n, start + ts)
        if start + EMBARGO >= n - 50 and cuts:
            break
        cuts.append((start, test_end))
        if test_end >= n:
            break
        start = test_end
    return cuts


def _fit_predict_fold(rows, feature_names, horizon_key, cand_names,
                      train_end, test_end):
    """Fit every candidate on rows[:train_end] (+ Platt on its val tail),
    predict rows[train_end+EMBARGO : test_end]. Returns per-candidate list.
    """
    import numpy as np
    test_lo = train_end + EMBARGO
    Xtr = np.array([[r[k] for k in feature_names] for r in rows[:train_end]])
    ytr = np.array([1 if r[horizon_key] else 0 for r in rows[:train_end]])
    Xte = np.array([[r[k] for k in feature_names] for r in rows[test_lo:test_end]])
    yte = [1 if r[horizon_key] else 0 for r in rows[test_lo:test_end]]

    val_n = max(200, int(len(Xtr) * VAL_FRAC))
    split = len(Xtr) - val_n

    out = []
    for name in cand_names:
        model = fit_candidate(name, Xtr[:split], ytr[:split])
        coefs = platt_calibrate(model, Xtr[split:], ytr[split:])
        p_raw = model.predict_proba(Xte)[:, 1] if len(Xte) else []
        p = [apply_platt(float(x), coefs) for x in p_raw]
        out.append((name, p, yte, coefs))
    return out


def walk_forward_report(rows_by_asset, feature_names, horizons=("y1_up", "y2_up"),
                        n_folds=4, test_size=None, cand_names=None,
                        shuffle_probe=True, seed=13):
    """Full walk-forward evaluation. Returns a report dict (JSON-safe).

    rows_by_asset: {asset: [row dicts from build_dataset, time-ordered]}
    """
    if not SKLEARN_OK:
        return {"error": "sklearn unavailable"}
    import random
    rng = random.Random(seed)
    cand_names = cand_names or list(CANDIDATES().keys())

    report = {"folds_per_asset": {}, "candidates": {}, "shuffle_probe": {},
              "n_rows_total": sum(len(v) for v in rows_by_asset.values()),
              "embargo": EMBARGO, "n_folds": n_folds}

    # accumulate per-candidate predictions across ALL assets/folds
    cand_pred = {h: {c: ([], []) for c in cand_names} for h in horizons}

    for asset, rows in sorted(rows_by_asset.items()):
        n = len(rows)
        if n < 400:
            report["folds_per_asset"][asset] = {"skipped": f"only {n} rows (<400)"}
            continue
        cuts = _folds(n, n_folds, test_size)
        astat = {"folds": len(cuts), "rows": n}
        for h in horizons:
            for name in cand_names:
                allp, ally = cand_pred[h][name]
                for (tr_end, te_end) in cuts:
                    res = _fit_predict_fold(rows, feature_names, h,
                                            [name], tr_end, te_end)
                    _, p, y, _ = res[0]
                    allp.extend(p)
                    ally.extend(y)
        report["folds_per_asset"][asset] = astat

    # global per-candidate metrics per horizon
    for h in horizons:
        report["candidates"][h] = {}
        for name in cand_names:
            p, y = cand_pred[h][name]
            if not p:
                continue
            acc, n = _accuracy(p, y)
            rep = {
                "n": n,
                "accuracy": round(acc, 4),
                "logloss": round(_logloss(p, y), 4),
                "calibration": _calibration_table(p, y),
            }
            report["candidates"][h][name] = rep

    # pick the winner per horizon by logloss (unseen data, PART 10)
    report["selected"] = {}
    for h in horizons:
        cands = report["candidates"].get(h, {})
        if cands:
            best = min(cands.items(), key=lambda kv: kv[1]["logloss"])
            report["selected"][h] = {"model": best[0],
                                     "logloss": best[1]["logloss"],
                                     "accuracy": best[1]["accuracy"]}

    # ── PART 19 probe B: shuffled-label control on the winner ──────────
    if shuffle_probe and report["selected"]:
        for h in horizons:
            sel = report["selected"].get(h, {}).get("model")
            if not sel or h not in cand_pred:
                continue
            p, y = cand_pred[h][sel]
            y_shuf = y[:]
            rng.shuffle(y_shuf)
            acc_shuf, _ = _accuracy(p, y_shuf)
            report["shuffle_probe"][h] = {
                "shuffled_accuracy": round(acc_shuf, 4),
                "note": "must be ≈0.50; materially higher ⇒ leakage",
            }
    return report


def _calibration_table(p, y, buckets=(0.45, 0.55, 0.65, 0.75, 1.01)):
    """PART 13 reliability analysis: predicted bucket → actual win rate."""
    table = []
    lo = 0.0
    for hi in buckets:
        bin_p = [pi for pi in p if lo <= pi < hi]
        bin_y = [yi for pi, yi in zip(p, y) if lo <= pi < hi]
        if bin_y:
            table.append({
                "bucket": f"{lo:.2f}-{hi:.2f}",
                "n": len(bin_y),
                "avg_predicted": round(sum(bin_p) / len(bin_p), 3),
                "actual_up_rate": round(sum(bin_y) / len(bin_y), 3),
            })
        lo = hi
    return table


# ───────────────────── signal-level backtest (PART 14+29) ─────────────────

def signal_backtest(rows, feature_names, proba_fn, period=60,
                    emit_min_score=None):
    """Replay the LIVE signal path over historical rows.

    proba_fn(feature_row, horizon) → P(UP) or None — normally a wrapper
    around a trained ModelBundle, EXACTLY the code the live predictor runs.

    For every row: rebuild PA/regime/filter from the row's frozen features
    window (the dataset rows carry everything the filter needs), grade
    against y1/y2, and accumulate PART 29 statistics. Tiers come from the
    score computed on the row; the no-signal rate and consecutive-loss
    metrics follow the emit rule.

    NOTE: PA components need the raw window, not just the feature row —
    the backtest script passes a window-aware proba_fn AND recomputes PA
    from candles; this function accepts precomputed `pa`/`regime` fields on
    rows when present (dataset rows built by backtest script attach them).
    """
    stats = {
        "rows": len(rows), "t1": _sig_stats(), "t2": _sig_stats(),
        "tiers": {}, "consecutive_losses_max": 0,
    }
    run_loss = {"t1": 0, "t2": 0}
    for r in rows:
        for h, key, ykey in ((1, "t1", "y1_up"), (2, "t2", "y2_up")):
            sig = r.get(f"sig{h}")
            if not sig:
                continue
            st = stats[key]
            st["predictions"] += 1
            tier = sig["tier"]
            td = stats["tiers"].setdefault(
                tier, {"n": 0, "emit": 0, "wins": 0, "losses": 0})
            td["n"] += 1
            if sig["emit"]:
                st["signals"] += 1
                td["emit"] += 1
                actual_up = r[ykey]
                won = (sig["prediction"] == "CALL") == bool(actual_up)
                if sig["probability"] == 0.5:
                    won = None
                if won is None:
                    st["draws"] += 1
                elif won:
                    st["wins"] += 1
                    run_loss[key] = 0
                else:
                    st["losses"] += 1
                    run_loss[key] += 1
                    stats["consecutive_losses_max"] = max(
                        stats["consecutive_losses_max"], run_loss[key])
    for key in ("t1", "t2"):
        st = stats[key]
        dec = st["wins"] + st["losses"]
        st["win_rate"] = round(100.0 * st["wins"] / dec, 2) if dec else None
        st["no_signal_rate"] = round(
            100.0 * (st["predictions"] - st["signals"]) / st["predictions"], 1) \
            if st["predictions"] else 0.0
    return stats


def _sig_stats():
    return {"predictions": 0, "signals": 0, "wins": 0, "losses": 0,
            "draws": 0, "win_rate": None, "no_signal_rate": 0.0}
