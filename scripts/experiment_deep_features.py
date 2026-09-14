"""scripts/experiment_deep_features.py — DEEP-FEATURE EDGE EXPERIMENT
(2026-09-14, user ask: "Walk-forward validation ঠিক রেখে (leakage test
সহ) নতুন feature/model try করে দেখা সত্যিই edge আসে কিনা").

QUESTION
    Do the 48 new DEEP features (time / serial / regime / interaction /
    micro — features_deep.py) give the T+1/T+2 direction models a REAL
    out-of-sample edge over the 73-feature UNIFIED baseline, or is any
    apparent gain just noise + overfit?

PROTOCOL — identical to the production trainer (fast_train.py):
    * per-pair datasets built by build_dataset + enrich_rows (leak-proof
      target locking, gap-free runs, deferred hist stats);
    * EXPANDING walk-forward folds from _fast_folds() with EMBARGO=2;
    * Platt calibration on the time-ordered validation tail;
    * candidates fitted via _fit_predict_fold — the same function the
      production trainer calls;
    * SHUFFLE PROBE (mean of 5 label permutations) on every reported
      config — a leakage signature fails the run;
    * PLACEBO CONTROL: BASE + 48 pure-noise features evaluated under the
      identical protocol. Any "gain" the noise config shows is the
      empirical noise floor; the deep features must clear it;
    * PAIRED analysis: every config predicts the SAME test rows in the
      SAME folds as BASE, so per-row correctness deltas are paired;
      bootstrap 95% CIs come from resampling those rows.

DATA
    Two INDEPENDENT synthetic replicates (18 pairs × 2 days each) from
    the app's own regime-switching generator (synth_seed._gen_pair_candles)
    with different RNG streams. Honest framing: on synthetic data this
    experiment proves MECHANISM (features capture generator structure
    without leakage); the same harness re-runs on real broker data the
    moment a token lands (retrain cycle) and answers the edge question
    there. Synthetic-trained models stay status=provisional by the
    production honesty cap regardless of this experiment's outcome.

MODELS
    logreg + rf (the production FAST_CANDIDATES) for the headline
    comparison; histgb + extratrees as the "নতুন model try" tier.

EXECUTION — CHECKPOINTED
    The experiment is unit-based (one unit = one (replicate, pair) block
    of fits) and dumps its whole result pool to a pickle after every
    unit, so it can be stopped/resumed at any moment:
        python scripts/experiment_deep_features.py            # resume
        python scripts/experiment_deep_features.py --reset    # fresh
    (The sandbox kills background processes, so the intended usage is
    repeated foreground runs of a few minutes each until DONE.)

OUTPUT
    console tables + scripts/out/experiment_deep_report.json
"""

import json
import math
import os
import pickle
import random
import sys
import time
import zlib

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from core.constants import ALLOWED_PAIRS_OTC
from core.otc_dataset import build_dataset
from core.otc_predict.features_ext import (
    build_unified_row, UNIFIED_FEATURE_NAMES)
from core.otc_predict.features_deep import (
    DEEP_FEATURE_NAMES, DEEP_BLOCK_NAMES, build_deep_row)
from core.otc_predict.hist_stats import enrich_rows
from core.otc_predict.models import CANDIDATES, SKLEARN_OK
from core.otc_predict.walk_forward import _fit_predict_fold, EMBARGO
from core.otc_predict.fast_train import _fast_folds

OUT_DIR = os.path.join(REPO, "scripts", "out")
os.makedirs(OUT_DIR, exist_ok=True)
REPORT_PATH = os.path.join(OUT_DIR, "experiment_deep_report.json")
STATE_PATH = os.path.join(OUT_DIR, "experiment_state.pkl")

DAYS = 2                      # 2880 candles/pair per replicate
N_REPS = 2                    # independent synthetic replicates
WINDOW = 50                   # production trainer window
BOOTSTRAP_N = 500             # paired bootstrap resamples
SHUFFLE_DRAWS = 5             # mean-of-5 probe (production rule)
SEED = 20260914
END_TS = 1789200000           # FIXED data anchor — resumes see identical data

# ablation pairs (one per family mix, 6 of 18 — attribution tier)
ABLATION_PAIRS = ["AUDJPY_otc", "NZDUSD_otc", "USDARS_otc",
                  "USDBDT_otc", "GBPNZD_otc", "USDMXN_otc"]

# ── feature configs (module level — importable by smoke tests) ─────────
# BASE = the PRE-DEEP production baseline (73 names). Computed by
# SUBTRACTING the deep block from UNIFIED_FEATURE_NAMES so this harness
# stays correct whether or not the deep block is integrated upstream —
# the baseline must always be exactly what production used BEFORE the
# deep-feature ask.
_DEEP_SET = set(DEEP_FEATURE_NAMES)
BASE_NAMES = [n for n in UNIFIED_FEATURE_NAMES if n not in _DEEP_SET]
DEEP_NAMES = list(DEEP_FEATURE_NAMES)
NOISE_NAMES = [f"noise_{k}" for k in range(len(DEEP_NAMES))]
configs = {
    "BASE": BASE_NAMES,
    "FULL": BASE_NAMES + DEEP_NAMES,
    "NOISE": BASE_NAMES + NOISE_NAMES,
}
for _blk in ("time", "serial", "regime", "inter", "micro"):
    configs[f"BASE+{_blk.upper()}"] = BASE_NAMES + list(
        DEEP_BLOCK_NAMES[_blk])
configs["DEEP_ONLY"] = DEEP_NAMES

MODELS_T1 = ["logreg", "rf"]
MODELS_T3 = ["histgb", "extratrees"]
T1_CFGS = ("BASE", "FULL", "NOISE")
T2_CFGS = ("BASE", "FULL", "DEEP_ONLY", "BASE+TIME", "BASE+SERIAL",
           "BASE+REGIME", "BASE+INTER", "BASE+MICRO")
T2_RF_CFGS = ("BASE", "BASE+SERIAL", "BASE+REGIME")
T3_CFGS = ("BASE", "FULL")


# ─────────────────────────── data generation ───────────────────────────────

def gen_candles(asset, rep):
    """One pair's synthetic candles with a REPLICATE-SPECIFIC rng stream."""
    from core.otc_predict.synth_seed import _gen_pair_candles
    rng = random.Random(f"{asset}:deep-exp:rep{rep}")
    return _gen_pair_candles(asset, DAYS, END_TS, rng)


def _merged_fn(window, micro=None):
    feats = dict(build_unified_row(window, micro=micro))
    feats.update(build_deep_row(window))
    return feats


def build_pair_rows(asset, rep):
    candles = gen_candles(asset, rep)
    rows, stats = build_dataset({asset: candles}, window=WINDOW,
                                micro=True, feature_fn=_merged_fn)
    rows = enrich_rows(rows)
    # placebo noise features — deterministic per (rep, row order)
    base_rng = np.random.RandomState(777 + rep)
    noise = base_rng.randn(len(rows), len(DEEP_FEATURE_NAMES))
    for i, r in enumerate(rows):
        for k, val in enumerate(noise[i]):
            r[f"noise_{k}"] = float(val)
    return rows, stats


# ─────────────────────────── evaluation core ───────────────────────────────

def evaluate_config(rows, feature_names, models):
    """Walk-forward OOS predictions, PAIRED BY ROW with every other config.

    Returns {horizon: {model: {"p": [...], "y": [...]}}} — the pooled
    positions are identical across configs because folds come from the
    same _fast_folds(n) on the same rows.
    """
    n = len(rows)
    cuts = _fast_folds(n)
    out = {h: {m: {"p": [], "y": []} for m in models}
           for h in ("y1_up", "y2_up")}
    for (tr_end, te_end) in cuts:
        for h in ("y1_up", "y2_up"):
            res = _fit_predict_fold(rows, feature_names, h, list(models),
                                    tr_end, te_end)
            for name, p, y, _coefs in res:
                out[h][name]["p"].extend(float(x) for x in p)
                out[h][name]["y"].extend(int(v) for v in y)
    return out


def _acc(p, y):
    if not p:
        return None
    return sum(1 for pi, yi in zip(p, y) if (pi >= 0.5) == (yi == 1)) / len(p)


def _logloss(p, y):
    if not p:
        return None
    ll = 0.0
    for pi, yi in zip(p, y):
        pi = min(max(pi, 1e-6), 1 - 1e-6)
        ll += math.log(pi) if yi else math.log(1 - pi)
    return -ll / len(p)


def _deterministic_seed(key_str):
    """Process-stable seed (hash() is NOT — PYTHONHASHSEED randomisation)."""
    return SEED + (zlib.crc32(str(key_str).encode()) % 100000)


def _shuffle_probe(p, y, seed):
    """Mean accuracy over SHUFFLE_DRAWS label permutations (~0.5 expected)."""
    rng = random.Random(seed)
    accs = []
    ys = list(y)
    for _ in range(SHUFFLE_DRAWS):
        rng.shuffle(ys)
        accs.append(_acc(p, ys))
    return sum(accs) / len(accs)


def _paired_bootstrap(correct_a, correct_b, seed, n_boot=BOOTSTRAP_N):
    """95% CI of mean(correct_a - correct_b) over resampled rows."""
    rng = np.random.RandomState(seed)
    a = np.asarray(correct_a, dtype=np.float64)
    b = np.asarray(correct_b, dtype=np.float64)
    d = a - b
    n = len(d)
    if n == 0:
        return None
    idx = rng.randint(0, n, size=(n_boot, n))
    means = d[idx].mean(axis=1)
    return (float(np.percentile(means, 2.5)),
            float(np.percentile(means, 97.5)),
            float(d.mean()))


# ─────────────────────────── state management ──────────────────────────────

def fresh_state():
    return {
        "t1": {},        # (h, model, cfg) → {"p": [], "y": [], "correct": []}
        "t1_pp": {},     # (h, model, cfg, asset) → acc
        "t1_done": [],
        "t2": {},        # (h, cfg) → {"p": [], "y": []}
        "t2_done": [],
        "t3": {},        # (h, model, cfg) → {"p": [], "y": []}
        "t3_done": [],
        "done": False,
    }


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "rb") as f:
            return pickle.load(f)
    return fresh_state()


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, STATE_PATH)


# ─────────────────────────── unit workers ──────────────────────────────────

def tier1_unit(state, rep, asset):
    rows, _stats = build_pair_rows(asset, rep)
    if len(rows) < 550:
        print(f"  skip {asset} rep{rep}: {len(rows)} rows")
        return
    for cfg in T1_CFGS:
        res = evaluate_config(rows, configs[cfg], MODELS_T1)
        for h in ("y1_up", "y2_up"):
            for m in MODELS_T1:
                d = res[h][m]
                key = (h, m, cfg)
                slot = state["t1"].setdefault(
                    key, {"p": [], "y": [], "correct": []})
                corr = [1.0 if (pi >= 0.5) == (yi == 1) else 0.0
                        for pi, yi in zip(d["p"], d["y"])]
                slot["p"].extend(d["p"])
                slot["y"].extend(d["y"])
                slot["correct"].extend(corr)
                state["t1_pp"][(h, m, cfg, asset)] = _acc(d["p"], d["y"])


def tier2_unit(state, rep, asset):
    rows, _stats = build_pair_rows(asset, rep)
    for cfg in T2_CFGS:
        res = evaluate_config(rows, configs[cfg], ["logreg"])
        for h in ("y1_up", "y2_up"):
            d = res[h]["logreg"]
            key = (h, cfg)
            slot = state["t2"].setdefault(key, {"p": [], "y": []})
            slot["p"].extend(d["p"])
            slot["y"].extend(d["y"])
    for cfg in T2_RF_CFGS:
        res = evaluate_config(rows, configs[cfg], ["rf"])
        for h in ("y1_up", "y2_up"):
            d = res[h]["rf"]
            key = (h, cfg + "|rf")
            slot = state["t2"].setdefault(key, {"p": [], "y": []})
            slot["p"].extend(d["p"])
            slot["y"].extend(d["y"])


def tier3_unit(state, rep, asset):
    rows, _stats = build_pair_rows(asset, rep)
    for cfg in T3_CFGS:
        res = evaluate_config(rows, configs[cfg], MODELS_T3)
        for h in ("y1_up", "y2_up"):
            for m in MODELS_T3:
                d = res[h][m]
                key = (h, m, cfg)
                slot = state["t3"].setdefault(key, {"p": [], "y": []})
                slot["p"].extend(d["p"])
                slot["y"].extend(d["y"])


# ─────────────────────────── report assembly ───────────────────────────────

def _correct_list(slot):
    return [1.0 if (pi >= 0.5) == (yi == 1) else 0.0
            for pi, yi in zip(slot["p"], slot["y"])]


def assemble_report(state):
    pairs = sorted(ALLOWED_PAIRS_OTC)

    # ── Tier 1 tables ────────────────────────────────────────────────────
    t1_summary = {}
    print("\nTIER 1 RESULTS (pooled OOS, per horizon × model × config)")
    print(f"{'horizon':8s} {'model':10s} {'config':7s} {'n':>7s} "
          f"{'acc':>7s} {'logloss':>8s} {'shuffle':>8s}")
    for h in ("y1_up", "y2_up"):
        for m in MODELS_T1:
            for cfg in T1_CFGS:
                slot = state["t1"].get((h, m, cfg))
                if not slot or not slot["p"]:
                    continue
                key = f"{h}|{m}|{cfg}"
                shuf = _shuffle_probe(
                    slot["p"], slot["y"], _deterministic_seed(key))
                acc = _acc(slot["p"], slot["y"])
                ll = _logloss(slot["p"], slot["y"])
                t1_summary[key] = {"n": len(slot["p"]), "acc": round(acc, 4),
                                   "logloss": round(ll, 4),
                                   "shuffle_probe": round(shuf, 4)}
                print(f"{h:8s} {m:10s} {cfg:7s} {len(slot['p']):7d} "
                      f"{acc:7.4f} {ll:8.4f} {shuf:8.4f}")

    # paired deltas vs BASE
    print("\nPAIRED DELTAS vs BASE (same folds, same rows — bootstrap 95% CI)")
    delta_results = {}
    for h in ("y1_up", "y2_up"):
        for m in MODELS_T1:
            b = state["t1"].get((h, m, "BASE"))
            if not b:
                continue
            for cfg in ("FULL", "NOISE"):
                c = state["t1"].get((h, m, cfg))
                if not c or len(c["correct"]) != len(b["correct"]):
                    continue
                key = f"{h}|{m}|{cfg}-BASE"
                ci = _paired_bootstrap(c["correct"], b["correct"],
                                       _deterministic_seed(key))
                delta_results[key] = {
                    "delta_acc": round(ci[2], 4), "ci_lo": round(ci[0], 4),
                    "ci_hi": round(ci[1], 4),
                    "significant": bool(ci[0] > 0 or ci[1] < 0)}
                print(f"  {h} {m:8s} {cfg:5s}-BASE: Δacc={ci[2]*100:+.2f}pp "
                      f"[{ci[0]*100:+.2f}, {ci[1]*100:+.2f}] "
                      f"{'SIGNIFICANT' if (ci[0] > 0 or ci[1] < 0) else 'ns'}")

    print("\nPER-PAIR: #pair-reps where FULL beats BASE (of 36)")
    for h in ("y1_up", "y2_up"):
        for m in MODELS_T1:
            wins = tot = 0
            for asset in pairs:
                fa = state["t1_pp"].get((h, m, "FULL", asset))
                ba = state["t1_pp"].get((h, m, "BASE", asset))
                if fa is None or ba is None:
                    continue
                tot += 1
                if fa > ba:
                    wins += 1
            print(f"  {h} {m:8s}: {wins}/{tot} pair-reps improved")

    # ── Tier 2 tables ────────────────────────────────────────────────────
    t2_summary = {}
    print("\nTIER 2 RESULTS — BLOCK ABLATION (logreg unless |rf)")
    for key in sorted(state["t2"]):
        h, cfg = key
        slot = state["t2"][key]
        key_str = f"{h}|{cfg}"
        acc = _acc(slot["p"], slot["y"])
        ll = _logloss(slot["p"], slot["y"])
        shuf = _shuffle_probe(slot["p"], slot["y"],
                              _deterministic_seed(key_str))
        t2_summary[key_str] = {"n": len(slot["p"]), "acc": round(acc, 4),
                               "logloss": round(ll, 4),
                               "shuffle_probe": round(shuf, 4)}
        print(f"  {h} {cfg:16s} n={len(slot['p']):6d} acc={acc:.4f} "
              f"logloss={ll:.4f} shuffle={shuf:.4f}")

    # ── Tier 3 tables ────────────────────────────────────────────────────
    t3_summary = {}
    print("\nTIER 3 RESULTS — NEW MODELS (histgb / extratrees)")
    for key in sorted(state["t3"]):
        h, m, cfg = key
        slot = state["t3"][key]
        key_str = f"{h}|{m}|{cfg}"
        acc = _acc(slot["p"], slot["y"])
        ll = _logloss(slot["p"], slot["y"])
        shuf = _shuffle_probe(slot["p"], slot["y"],
                              _deterministic_seed(key_str))
        t3_summary[key_str] = {"n": len(slot["p"]), "acc": round(acc, 4),
                               "logloss": round(ll, 4),
                               "shuffle_probe": round(shuf, 4)}
        print(f"  {h} {m:10s} {cfg:5s} n={len(slot['p']):6d} acc={acc:.4f} "
              f"logloss={ll:.4f} shuffle={shuf:.4f}")

    # tier3 paired deltas
    for h in ("y1_up", "y2_up"):
        for m in MODELS_T3:
            b = state["t3"].get((h, m, "BASE"))
            c = state["t3"].get((h, m, "FULL"))
            if not b or not c or len(b["p"]) != len(c["p"]):
                continue
            key = f"{h}|{m}|FULL-BASE"
            ci = _paired_bootstrap(_correct_list(c), _correct_list(b),
                                   _deterministic_seed(key))
            delta_results[key] = {
                "delta_acc": round(ci[2], 4), "ci_lo": round(ci[0], 4),
                "ci_hi": round(ci[1], 4),
                "significant": bool(ci[0] > 0 or ci[1] < 0)}
            print(f"  {h} {m:10s} FULL-BASE: Δacc={ci[2]*100:+.2f}pp "
                  f"[{ci[0]*100:+.2f}, {ci[1]*100:+.2f}] "
                  f"{'SIGNIFICANT' if (ci[0] > 0 or ci[1] < 0) else 'ns'}")

    # ── verdict ──────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("VERDICT (synthetic-replicate evidence — mechanism proof)")
    print("=" * 78)
    for key, d in sorted(delta_results.items()):
        if not key.endswith("FULL-BASE"):
            continue
        print(f"  {key:28s}: Δacc={d['delta_acc']*100:+.2f}pp "
              f"[{d['ci_lo']*100:+.2f}, {d['ci_hi']*100:+.2f}] "
              f"→ {'EDGE DETECTED' if d['significant'] else 'no proven edge'}")
    noise_keys = [k for k in delta_results if "NOISE-BASE" in k]
    for k in noise_keys:
        d = delta_results[k]
        print(f"  noise-floor {k:18s}: Δacc={d['delta_acc']*100:+.2f}pp "
              f"[{d['ci_lo']*100:+.2f}, {d['ci_hi']*100:+.2f}] "
              f"→ {'CONTAMINATED (must be ns!)' if d['significant'] else 'clean (ns)'}")

    report = {
        "date": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "protocol": {
            "folds": "expanding walk-forward (_fast_folds), EMBARGO=2",
            "platt": "time-ordered val tail",
            "shuffle_draws": SHUFFLE_DRAWS,
            "bootstrap_n": BOOTSTRAP_N,
            "data": f"synthetic regime-switching, {N_REPS} reps × "
                    f"{len(pairs)} pairs × {DAYS}d, end_ts={END_TS}",
            "features_base": len(BASE_NAMES),
            "features_deep": len(DEEP_NAMES),
            "honesty": "synthetic data ⇒ mechanism proof only; real edge "
                       "measured when live token lands (retrain cycle)"},
        "tier1": t1_summary,
        "tier1_paired_deltas": delta_results,
        "tier1_per_pair_acc": {
            f"{h}|{m}|{c}|{a}": round(v, 4)
            for (h, m, c, a), v in state["t1_pp"].items()},
        "tier2_ablation": t2_summary,
        "tier3_new_models": t3_summary,
    }
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=1)
    print(f"\nreport saved → {REPORT_PATH}")
    return report


# ────────────────────────────── main driver ────────────────────────────────

def main():
    assert SKLEARN_OK, "sklearn unavailable — experiment impossible"
    if "--reset" in sys.argv and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
        print("[experiment] state reset — starting fresh")
    state = load_state()

    units = {
        "t1": [(rep, a) for rep in range(N_REPS)
               for a in sorted(ALLOWED_PAIRS_OTC)],
        "t2": [(rep, a) for rep in range(N_REPS) for a in ABLATION_PAIRS],
        "t3": [(rep, a) for rep in range(N_REPS) for a in ABLATION_PAIRS],
    }
    workers = {"t1": tier1_unit, "t2": tier2_unit, "t3": tier3_unit}

    t_start = time.time()
    for tier in ("t1", "t2", "t3"):
        done = set(map(tuple, state[f"{tier}_done"]))
        todo = [u for u in units[tier] if u not in done]
        if todo:
            print(f"[experiment] TIER {tier.upper()}: {len(todo)} unit(s) "
                  f"remaining of {len(units[tier])}")
        for i, (rep, asset) in enumerate(todo):
            if time.time() - t_start > 480:   # leave margin inside 10-min cap
                print("[experiment] time budget reached — resume with the "
                      "same command to continue")
                return 1
            workers[tier](state, rep, asset)
            state[f"{tier}_done"].append([rep, asset])
            save_state(state)
            print(f"  [{tier}] rep{rep} {asset} done "
                  f"(+{time.time() - t_start:.0f}s, "
                  f"saved {len(state[f'{tier}_done'])}/{len(units[tier])})")

    if not state["done"]:
        assemble_report(state)
        state["done"] = True
        save_state(state)
        print("[experiment] COMPLETE — report assembled")
    else:
        print("[experiment] already complete — report at", REPORT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
