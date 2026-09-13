"""scripts/backtest_hist.py — HIST-ENGINE verification (2026-09-13).

Deep Report §13/§26/§28: the Historical Setup-Match Engine is the report's
"সবচেয়ে শক্তিশালী অংশ" — this backtest measures, with numbers:

  A) STANDALONE EDGE — the hist voice alone (hist_p_up > 0.5 → UP) on
     time-ordered, leak-deferred counts:
       • on the PERSISTENCE edge (φ=0.12, the only edge in the production
         ledger) it must beat 50% by the amount the edge actually allows
       • resolution rate / avg n / level mix — production realism stats
       • Brier score of the hist voice vs the 0.25 coin-flip bar
  B) ENSEMBLE VALUE — when the hist voice AGREES with the ML direction
     (walk-forward unified model, hist features included) vs when they
     CONFLICT: hit-rate delta (the "একমত হলে নির্ভল" question, now for
     the third voice) + that the 73-name unified set trains cleanly.
  C) LEAK CONTROL — on a FAIR random walk (edge=none) the hist voice must
     stay ~50%: if the deferral ever leaked future outcomes, a fair walk
     would still show an edge. The white-box deferral proof lives in
     scripts/test_hist_stats.py §3; this is the end-to-end probe.

DATA: scripts/synthetic_otc.py — same generator as backtest_unified.py.
"""
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.otc_dataset import build_dataset
from core.otc_predict.features_ext import (
    build_unified_row, UNIFIED_FEATURE_NAMES)
from core.otc_predict.hist_stats import enrich_rows
from core.otc_predict import fast_train
from core.otc_predict.walk_forward import _fit_predict_fold
from scripts.synthetic_otc import gen_candles

N_PAIRS = 8
N_CANDLES = 2600
SEED = 20260913
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "backtest_hist_report.json")


def gen_pairs(edge):
    names = ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDCAD_otc",
             "NZDUSD_otc", "USDCHF_otc", "EURGBP_otc", "USDBRL_otc"]
    out = {}
    for i, a in enumerate(names[:N_PAIRS]):
        out[a] = gen_candles(a, N_CANDLES, seed=SEED + i, edge=edge,
                             phi=0.12)
    return out


def build_enriched_rows(pairs):
    """build_dataset + hist enrichment (exactly the fast-train path)."""
    rows, _dstats = build_dataset(pairs, window=50, micro=True,
                                  feature_fn=build_unified_row)
    rows = enrich_rows(rows)
    return rows


def hist_voice_stats(rows, horizon="y1_up", p_key="hist_p_up_t1"):
    """Standalone hist-voice accuracy + resolution + Brier (resolved only)."""
    n = hits = resolved = 0
    brier_sum = 0.0
    for r in rows:
        n += 1
        y = r[horizon]
        if r.get("hist_conf", 0.0) > 0.0:
            resolved += 1
            p = r[p_key]
            brier_sum += (p - y) ** 2
            pred_up = p > 0.5
            if (pred_up and y == 1) or (not pred_up and y == 0):
                hits += 1
    return {"n": n, "resolved": resolved,
            "resolution_pct": round(100.0 * resolved / n, 2) if n else 0.0,
            "acc_pct": round(100.0 * hits / resolved, 2) if resolved else None,
            "brier": round(brier_sum / resolved, 4) if resolved else None}


def agreement_analysis(rows, ml_probs, horizon="y1_up",
                       p_key="hist_p_up_t1"):
    """Agree-vs-conflict hit rates: ML (walk-forward) vs the hist voice.

    `ml_probs` — list aligned with `rows`: ML P(UP) per row (None = no
    model output for that row).
    """
    buckets = {"agree": [0, 0], "conflict": [0, 0]}
    for r, mp in zip(rows, ml_probs):
        if mp is None or r.get("hist_conf", 0.0) == 0.0:
            continue
        hp = r[p_key]
        ml_up = mp >= 0.5
        h_up = hp >= 0.5
        y = r[horizon]
        key = "agree" if ml_up == h_up else "conflict"
        buckets[key][0] += 1
        if (ml_up and y == 1) or (not ml_up and y == 0):
            buckets[key][1] += 1
    out = {}
    for k, (n, hits) in buckets.items():
        out[k] = {"n": n, "acc_pct": round(100.0 * hits / n, 2) if n else None}
    out["delta_pp"] = (round(out["agree"]["acc_pct"]
                             - out["conflict"]["acc_pct"], 2)
                       if out["agree"]["acc_pct"] is not None
                       and out["conflict"]["acc_pct"] is not None else None)
    return out


def wf_ml_probs(rows, cand="rf"):
    """Walk-forward ML P(UP) per row (unified 73-name set incl. hist)."""
    from core.otc_predict.walk_forward import EMBARGO
    n = len(rows)
    cuts = fast_train._fast_folds(n)
    probs = [None] * n
    for (tr_end, te_end) in cuts:
        res = _fit_predict_fold(rows, UNIFIED_FEATURE_NAMES, "y1_up",
                                [cand], tr_end, te_end)
        _, p, _y, _coefs = res[0]
        test_lo = tr_end + EMBARGO
        for j, pv in zip(range(test_lo, te_end), p):
            probs[int(j)] = float(pv)
    return probs


def main():
    t0 = time.time()
    report = {"generated_at": time.time(), "pairs": N_PAIRS,
              "candles_per_pair": N_CANDLES, "seed": SEED}

    print("=" * 72)
    print("HIST-ENGINE BACKTEST — Deep Report §13/§26/§28")
    print("=" * 72)

    # ── A) persistence edge ────────────────────────────────────────────
    print("\n[A] PERSISTENCE edge (φ=0.12) — hist voice standalone")
    rows = build_enriched_rows(gen_pairs("persistence"))
    print(f"    rows: {len(rows)} (unified 73-name set, hist enriched)")
    s1 = hist_voice_stats(rows, "y1_up", "hist_p_up_t1")
    s2 = hist_voice_stats(rows, "y2_up", "hist_p_up_t2")
    print(f"    T+1: resolved {s1['resolution_pct']}% "
          f"acc {s1['acc_pct']}% brier {s1['brier']}")
    print(f"    T+2: resolved {s2['resolution_pct']}% "
          f"acc {s2['acc_pct']}% brier {s2['brier']}")
    report["persistence"] = {"t1": s1, "t2": s2, "rows": len(rows)}

    # ── B) agreement with the ML direction ────────────────────────────
    print("\n[B] ENSEMBLE — hist voice vs walk-forward ML (T+1)")
    probs = wf_ml_probs(rows, cand="rf")
    got = sum(1 for p in probs if p is not None)
    print(f"    ML predictions: {got}/{len(rows)} rows (rf, walk-forward)")
    agg = agreement_analysis(rows, probs)
    print(f"    agree    n={agg['agree']['n']} "
          f"acc={agg['agree']['acc_pct']}%")
    print(f"    conflict n={agg['conflict']['n']} "
          f"acc={agg['conflict']['acc_pct']}%")
    print(f"    delta = {agg['delta_pp']}pp "
          + ("(একমত হলে নির্ভল — পুনরায় প্রমাণিত)" if (agg['delta_pp'] or 0) > 0
             else ""))
    report["agreement"] = agg

    # ── C) fair walk leakage probe ────────────────────────────────────
    print("\n[C] FAIR random walk (edge=none) — leakage probe")
    fair_rows = build_enriched_rows(gen_pairs("none"))
    f1 = hist_voice_stats(fair_rows, "y1_up", "hist_p_up_t1")
    f2 = hist_voice_stats(fair_rows, "y2_up", "hist_p_up_t2")
    print(f"    T+1: resolved {f1['resolution_pct']}% "
          f"acc {f1['acc_pct']}% brier {f1['brier']}")
    print(f"    T+2: resolved {f2['resolution_pct']}% "
          f"acc {f2['acc_pct']}% brier {f2['brier']}")
    clean = all((s.get("acc_pct") or 50) <= 52.0 for s in (f1, f2))
    print("    probe: " + ("CLEAN (~50% on fair walk — no leakage)"
                          if clean else
                          "SUSPECT — edge on a fair walk means leakage!"))
    report["fair_walk"] = {"t1": f1, "t2": f2, "clean": clean}

    report["elapsed_secs"] = round(time.time() - t0, 1)
    with open(REPORT_PATH, "w") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print(f"\nreport → {REPORT_PATH} ({report['elapsed_secs']}s)")

    verdict = (clean
               and (s1["acc_pct"] or 0) > (f1["acc_pct"] or 0))
    print("\nVERDICT: " + (
        "HIST-ENGINE captures the persistence edge on ordered data and "
        "stays ~50% on a fair walk — honest statistics, no leakage."
        if verdict else
        "CHECK NUMBERS — voice did not separate edge from fair walk."))
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
