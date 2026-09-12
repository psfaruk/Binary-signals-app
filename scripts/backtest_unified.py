"""scripts/backtest_unified.py — UNIFIED-SIGNAL verification (2026-09-13).

USER REQ: "যদি পুরো সিস্টেম টি কে একটি সিস্টেম এর মধ্যে নিয়ে আসা যায়, তাহলে
আমি মনে করি আরও নির্ভল সিগন্যাল হবে। Deeply করেন।" + "backtest করে ভেরিফাই
করবেন"।

THE QUESTION this backtest answers, with numbers:

  A) Does the UNIFIED feature set (classic strategy votes as ML inputs)
     beat the EXTENDED-only feature set on unseen, time-ordered folds?
  B) When the ML direction and the classic strategies' net vote AGREE,
     is the hit rate actually higher than when they CONFLICT? (the user's
     "একমত হলে নির্ভল" hypothesis — measured, not assumed)
  C) LEAK CONTROL: on a fair random walk (edge="none") the unified
     pipeline must show ~50% and NO edge — otherwise it leaks.

DATA: synthetic_otc.py candles with the PERSISTENCE edge (φ=0.12) — the
only edge ever verified in the production ledger (AUDIT_2026-09-11).
Fair-walk control for the leakage probe. Same generator the previous
backtests used (backtest_every_candle / fast-train suites).
"""
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.otc_dataset import build_dataset
from core.otc_predict.features_ext import (
    build_extended_row, build_unified_row, EXTENDED_FEATURE_NAMES,
    UNIFIED_FEATURE_NAMES)
from core.otc_predict import fast_train
from core.otc_predict.walk_forward import _fit_predict_fold, EMBARGO
from scripts.synthetic_otc import gen_candles

N_PAIRS = 8
N_CANDLES = 2600          # per pair → ~2500 usable rows each
SEED = 20260913
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "backtest_unified_report.json")


def gen_pairs(edge):
    """The standard OTC pair set on synthetic candles with the given edge."""
    names = ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDCAD_otc",
             "NZDUSD_otc", "USDCHF_otc", "EURGBP_otc", "USDBRL_otc"]
    out = {}
    for i, a in enumerate(names[:N_PAIRS]):
        out[a] = gen_candles(a, N_CANDLES, seed=SEED + i, edge=edge,
                             phi=0.12)
    return out


def wf_evaluate(rows, feature_names, horizons=("y1_up", "y2_up")):
    """Pooled walk-forward over every pair's rows (time-ordered, EMBARGO).

    Uses the same _folds the fast-train daemon uses, candidates
    logreg+rf. Returns {(horizon, cand): {"acc", "logloss", "n"}}.
    """
    import random
    rng = random.Random(SEED)
    cands = [c for c in ("logreg", "rf") if c in
             __import__("core.otc_predict.models",
                        fromlist=["CANDIDATES"]).CANDIDATES()]
    n = len(rows)
    cuts = fast_train._fast_folds(n)
    pooled = {(h, c): {"p": [], "y": []}
              for h in horizons for c in cands}
    for (tr_end, te_end) in cuts:
        for h in horizons:
            for name in cands:
                res = _fit_predict_fold(rows, feature_names, h,
                                        [name], tr_end, te_end)
                _, p, y, _ = res[0]
                pooled[(h, name)]["p"].extend(float(x) for x in p)
                pooled[(h, name)]["y"].extend(int(v) for v in y)

    out = {}
    for (h, c), v in pooled.items():
        if not v["p"]:
            continue
        acc = sum(1 for pi, yi in zip(v["p"], v["y"])
                  if (pi >= 0.5) == (yi == 1)) / len(v["p"])
        ll = 0.0
        for pi, yi in zip(v["p"], v["y"]):
            pi = min(max(pi, 1e-6), 1 - 1e-6)
            ll += math.log(pi) if yi else math.log(1 - pi)
        rng.random()  # keep rng state advancing deterministically
        out[(h, c)] = {"acc": round(acc, 4),
                       "logloss": round(-ll / len(v["p"]), 4),
                       "n": len(v["p"])}
    return out


def agreement_analysis(rows):
    """B) When ML (unified model) and the classic strategies' net vote agree,
    what is the hit rate vs when they conflict?

    Uses the walk-forward pooled predictions of the best unified candidate
    so the ML side is genuinely out-of-sample; the strategy side is sv_net
    (the 13 modules' net direction at prediction time, frozen in the row).
    """
    # train the unified model per fold and collect out-of-sample rows
    cands = ["logreg", "rf"]
    best = {}
    # figure out the best candidate per horizon from the unified run (passed
    # in via rows' feature names) — caller passes unified_res
    return best, cands


def main():
    t0 = time.time()
    print("UNIFIED-SIGNAL BACKTEST (2026-09-13)")
    print("=" * 62)
    report = {"generated_at": time.time(), "pairs": N_PAIRS,
              "candles_per_pair": N_CANDLES, "seed": SEED}

    # ── A) persistence-edge data: extended vs unified ──────────────────
    print("\n[A] persistence edge (phi=0.12): extended vs unified")
    candles = gen_pairs("persistence")
    rows_ext, se = build_dataset(candles, window=50, micro=False,
                                 feature_fn=build_extended_row)
    rows_uni, su = build_dataset(candles, window=50, micro=False,
                                 feature_fn=build_unified_row)
    print(f"    dataset rows: ext={len(rows_ext)} uni={len(rows_uni)}")
    report["dataset_rows"] = {"extended": len(rows_ext),
                              "unified": len(rows_uni)}

    res_ext = wf_evaluate(rows_ext, EXTENDED_FEATURE_NAMES)
    res_uni = wf_evaluate(rows_uni, UNIFIED_FEATURE_NAMES)

    print(f"    {'horizon':8} {'cand':7} {'EXT acc':>8} {'UNI acc':>8} "
          f"{'Δpp':>7} {'EXT ll':>7} {'UNI ll':>7} {'n':>6}")
    report["wf"] = []
    for h in ("y1_up", "y2_up"):
        for c in ("logreg", "rf"):
            e = res_ext.get((h, c))
            u = res_uni.get((h, c))
            if not e or not u:
                continue
            dpp = round((u["acc"] - e["acc"]) * 100, 2)
            print(f"    {h:8} {c:7} {e['acc']*100:7.2f}% "
                  f"{u['acc']*100:7.2f}% {dpp:+6.2f} "
                  f"{e['logloss']:7.4f} {u['logloss']:7.4f} {u['n']:6}")
            report["wf"].append({
                "horizon": h, "cand": c,
                "ext_acc": e["acc"], "uni_acc": u["acc"],
                "delta_pp": dpp,
                "ext_logloss": e["logloss"], "uni_logloss": u["logloss"],
                "n": u["n"]})

    # ── B) agreement analysis on out-of-sample unified predictions ─────
    print("\n[B] ML × classic-strategy agreement (out-of-sample, unified rf)")
    # walk-forward the unified rf again but keep the per-row predictions
    cands = ["rf"]
    n = len(rows_uni)
    cuts = fast_train._fast_folds(n)
    per_row = []   # (ml_up, y_up, sv_net)
    from core.otc_predict.models import fit_candidate
    import numpy as np
    for (tr_end, te_end) in cuts:
        Xtr = np.array([[r[k] for k in UNIFIED_FEATURE_NAMES]
                        for r in rows_uni[:tr_end]])
        for h, ykey in (("y1_up", "y1_up"), ("y2_up", "y2_up")):
            ytr = np.array([1 if r[ykey] else 0 for r in rows_uni[:tr_end]])
            try:
                m = fit_candidate("rf", Xtr, ytr)
            except Exception:
                continue
            for i in range(tr_end + EMBARGO, te_end):
                r = rows_uni[i]
                X1 = np.array([[float(r[k]) for k in UNIFIED_FEATURE_NAMES]])
                p = float(m.predict_proba(X1)[0, 1])
                per_row.append((p >= 0.5, bool(r[ykey]), r["sv_net"]))

    buckets = {"agree": [0, 0], "conflict": [0, 0], "neutral": [0, 0]}
    for ml_up, y_up, sv_net in per_row:
        if sv_net > 0.15:
            k = "agree" if ml_up else "conflict"
        elif sv_net < -0.15:
            k = "agree" if not ml_up else "conflict"
        else:
            k = "neutral"
        buckets[k][0] += (1 if (ml_up == y_up) else 0)
        buckets[k][1] += 1

    print(f"    {'state':9} {'n':>6} {'hit rate':>9}")
    report["agreement"] = {}
    for k, (hit, cnt) in buckets.items():
        rate = round(100.0 * hit / cnt, 2) if cnt else None
        print(f"    {k:9} {cnt:6} {rate if rate is not None else '—':>8}%")
        report["agreement"][k] = {"n": cnt, "hits": hit, "rate": rate}
    ag, cf = buckets["agree"], buckets["conflict"]
    if ag[1] and cf[1]:
        diff = round(100.0 * ag[0] / ag[1] - 100.0 * cf[0] / cf[1], 2)
        print(f"    → agreement beats conflict by {diff:+.2f}pp")
        report["agreement_edge_pp"] = diff

    # strategy-vote-only baseline (classic engine alone)
    sv_hit = sv_n = 0
    for ml_up, y_up, sv_net in per_row:
        if abs(sv_net) <= 0.15:
            continue
        sv_up = sv_net > 0
        sv_hit += (1 if sv_up == y_up else 0)
        sv_n += 1
    if sv_n:
        print(f"    strategy-vote-only hit rate: "
              f"{100.0*sv_hit/sv_n:.2f}% (n={sv_n})")
        report["strategy_only"] = {"n": sv_n,
                                   "rate": round(100.0 * sv_hit / sv_n, 2)}

    # ── C) fair random-walk leakage probe ──────────────────────────────
    print("\n[C] fair random walk (edge=none) — leakage probe")
    fair = gen_pairs("none")
    rows_fair, _ = build_dataset(fair, window=50, micro=False,
                                 feature_fn=build_unified_row)
    res_fair = wf_evaluate(rows_fair, UNIFIED_FEATURE_NAMES)
    report["fair_walk"] = []
    for h in ("y1_up", "y2_up"):
        for c in ("logreg", "rf"):
            f = res_fair.get((h, c))
            if not f:
                continue
            verdict = "OK (~coin-flip)" if abs(f["acc"] - 0.5) <= 0.012 \
                else "⚠ EDGE ON FAIR WALK — LEAK?"
            print(f"    {h} {c}: acc={f['acc']*100:.2f}% "
                  f"logloss={f['logloss']:.4f} n={f['n']} — {verdict}")
            report["fair_walk"].append({"horizon": h, "cand": c,
                                        "acc": f["acc"],
                                        "logloss": f["logloss"],
                                        "n": f["n"], "verdict": verdict})

    report["secs"] = round(time.time() - t0, 1)
    with open(REPORT_PATH, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nreport → {REPORT_PATH} ({report['secs']}s)")

    # honest summary
    print("\n" + "=" * 62)
    print("VERDICT (honest):")
    best_wf = max(report["wf"], key=lambda r: r["delta_pp"])
    print(f"  • unified vs extended, best cell: {best_wf['delta_pp']:+.2f}pp "
          f"({best_wf['horizon']}/{best_hw_label(best_wf['cand'])})")
    if "agreement_edge_pp" in report:
        print(f"  • ML+strategies agreement edge: "
              f"{report['agreement_edge_pp']:+.2f}pp over conflict state")
    print("  • fair-walk probe: "
          + ("CLEAN" if all(f["acc"] <= 0.512 for f in report["fair_walk"])
             else "CHECK LEAK"))
    print("  • PART 29 honesty: an edge on the injected persistence is "
          "expected;\n    the SAME pipeline must stay ~50% on a fair walk.")


def best_hw_label(c):
    return c


if __name__ == "__main__":
    main()
