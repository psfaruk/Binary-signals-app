#!/usr/bin/env python3
"""Phase C — clean backtest evaluation runner (research only).

Deterministic, leakage-guarded, out-of-sample-by-construction evaluation of
the EXISTING production predictor (CSE v1, via exact live-parity replay of
``NextCandleEngine.ingest``) plus clearly-defined baselines, over authorized
historical data (public Binance 1m candles committed in the repo), synthetic
fixtures, or the app's own recorded emissions (signal_provenance join).

Data-source classifications (never mixed):

  binance    — REAL public exchange data (authorized historical). Results
               are real-market evidence for THIS dataset only; they are NOT
               OTC evidence and NOT a claim of live predictive skill.
  synthetic  — generated fixtures. Results validate PIPELINE MECHANICS only
               and are never presented as market evidence.
  provenance — the app's own recorded emissions joined to frozen settlement
               outcomes. On the repo (no runtime DB) this yields
               INSUFFICIENT_DATA — reported honestly, never padded.

FROZEN PROTOCOL (pre-committed before holdout evaluation; changing any of
these constants requires a new experiment id, not an edit):
  warmup = 600 candles per asset (engine hard gate is 61 closes; the
           margin covers rolling-state stabilization; matches no other
           harness's number by design and is reported in every output)
  split  = chronological 50% train / 25% selection / 25% holdout test
           over the post-warmup region, per asset
  embargo = 1 candle between train and evaluation regions (T+1 labels)
  walk-forward = 4 expanding-window folds over the post-warmup region
  holdout policy = reported ONCE by this pre-committed method

Usage:
  python3 scripts/backtest_clean_eval.py --source binance --payout 0.85
  python3 scripts/backtest_clean_eval.py --source binance \
      --arms cse_v1,cse_v1_research_localprior --payout 0.85
  python3 scripts/backtest_clean_eval.py --source synthetic --seed 7
  python3 scripts/backtest_clean_eval.py --source provenance --db PATH

Output: scripts/out/clean_eval_<utcstamp>.json (gitignored; committed JSONs
are never overwritten). No broker contact. No credentials. No trades.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.eval_clean.integrity import (  # noqa: E402
    validate_candle_series, validate_predictions, outcome_of,
    GREEN, RED, FLAT,
)
from core.eval_clean.splits import (  # noqa: E402
    SplitSpec, chronological_split, walk_forward_folds,
)
from core.eval_clean.baselines import (  # noqa: E402
    majority_direction, previous_candle_direction, PrespecifiedLogreg,
    research_features, research_label, RESEARCH_BASELINE_FEATURES,
)
from core.eval_clean.metrics import (  # noqa: E402
    binary_metrics, calibration_table, wilson_ci,
)
from core.eval_clean.breakeven import breakeven_report  # noqa: E402
from core.eval_clean.leakage import (  # noqa: E402
    EvidenceLedger, verify_split_independence,
)

# ── FROZEN PROTOCOL CONSTANTS ────────────────────────────────────────────
FROZEN = {
    "protocol_version": "clean_eval_v1",
    "warmup_candles": 600,
    "train_frac": 0.50,
    "selection_frac": 0.25,
    "test_frac": 0.25,
    "embargo_periods": 1,
    "walk_forward_folds": 4,
    "tie_policy": ("FLAT (close == open) outcomes are excluded from "
                   "directional metrics and counted; never labeled "
                   "GREEN/RED"),
    "outcome_convention": "GREEN: close>open (CALL correct), RED: close<open "
                          "(PUT correct), FLAT: close==open (tie)",
    "evidence_rule": ("prediction cutoff = target candle open time; "
                      "evidence at exactly the cutoff (source candle close) "
                      "is legitimate boundary evidence; anything later is "
                      "look-ahead and rejected"),
}
SPLIT_SPEC = SplitSpec(warmup=600, train_frac=0.50, selection_frac=0.25,
                       test_frac=0.25, embargo_periods=1)

BINANCE_ASSETS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT")
BINANCE_DIR = os.path.join(_REPO, "scripts", "data", "binance")
# PHASE-K (2026-10): overridable data dir (default unchanged) so the
# provenance-clean retraining campaign can run the SAME frozen protocol
# over the larger authorized 30-day dataset (scripts/data/binance_30d)
# without touching the original pre-committed-holdout files.
_gbd = os.environ.get("QX_BINANCE_DIR", "").strip()
if _gbd:
    BINANCE_DIR = _gbd


# ── DATA LOADERS ─────────────────────────────────────────────────────────

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_binance_1m(path: str, asset: str) -> list:
    """Binance 1m JSON [{t(ms), o, h, l, c, v}] -> validation rows with
    epoch-SECOND open timestamps (explicit ms->s conversion, documented)."""
    with open(path) as f:
        raw = json.load(f)
    return [{"asset": asset, "period": 60, "t": int(r["t"]) // 1000,
             "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
             "c": float(r["c"]), "v": float(r.get("v", 0.0) or 0.0)}
            for r in raw]


def load_synthetic(asset: str, n: int, seed: int) -> list:
    """Deterministic geometric-ish random walk. SYNTHETIC — never market
    evidence. Includes deliberate tie candles (o==c) so the tie policy is
    exercised on synthetic data too."""
    import random
    rng = random.Random(f"clean_eval_synth/{asset}/{seed}")
    t0 = 1_790_000_000 - (1_790_000_000 % 60)  # period-aligned start
    price = 1000.0
    rows = []
    for i in range(n):
        drift = rng.uniform(-0.0006, 0.0006)
        shock = rng.gauss(0.0, 0.0012)
        o = price
        c = o * (1.0 + drift + shock)
        if i % 297 == 0:
            c = o  # deliberate tie candle
        hi = max(o, c) * (1 + abs(rng.gauss(0.0, 0.0004)))
        lo = min(o, c) * (1 - abs(rng.gauss(0.0, 0.0004)))
        if hi <= lo:
            hi, lo = max(o, c), min(o, c)
        if min(o, c, hi, lo) <= 0:
            o, c, hi, lo = 1.0, 1.0001, 1.0002, 0.9999
        rows.append({"asset": asset, "period": 60, "t": t0 + i * 60,
                     "o": o, "h": hi, "l": lo, "c": c, "v": 0.0})
        price = c
    return rows


# ── ENGINE WORKER (subprocess isolation guarantees tunable separation) ──

def run_worker(arm: str, asset: str, source: str, seed: int) -> list:
    """Spawn a fresh interpreter running the engine replay for ONE asset.

    The cse_v1_research_localprior arm sets QX_NC_BLEND_K=1 in the child
    environment (local model dominates the unverifiable global prior) — a
    RESEARCH variant, clearly labeled, never a production-numerics change:
    production code is untouched and the default arm runs with defaults.

    PHASE-K (2026-10): the cse_v1_cleanprior arm points the child's
    global prior at a CANDIDATE weights file (QX_NC_WEIGHTS_FILE) produced
    by scripts/retrain_clean_prior.py with recorded provenance — same
    engine, same features, different (provenance-complete) prior.
    """
    env = dict(os.environ)
    if arm == "cse_v1_research_localprior":
        env["QX_NC_BLEND_K"] = "1"
    if arm == "cse_v1_cleanprior":
        _wf = getattr(run_worker, "weights_file", None)
        if not _wf:
            raise RuntimeError(
                "cse_v1_cleanprior arm requires --weights-file")
        env["QX_NC_WEIGHTS_FILE"] = _wf
    # PHASE-K: propagate the data-dir override to the child interpreter
    # (module-level default is the original binance dir otherwise).
    if BINANCE_DIR != os.path.join(_REPO, "scripts", "data", "binance"):
        env["QX_BINANCE_DIR"] = BINANCE_DIR
    out_file = tempfile.mktemp(prefix=f"clean_eval_worker_{arm}_{asset}_",
                               suffix=".json")
    cmd = [sys.executable, os.path.abspath(__file__),
           "--internal-worker", "--arm", arm, "--asset", asset,
           "--source", source, "--seed", str(seed),
           "--out-file", out_file]
    proc = subprocess.run(cmd, cwd=_REPO, env=env, capture_output=True,
                          text=True, timeout=1200)
    if proc.returncode != 0:
        raise RuntimeError(
            f"worker {arm}/{asset} failed rc={proc.returncode}\n"
            f"stdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}")
    with open(out_file) as f:
        preds = json.load(f)
    os.unlink(out_file)
    return preds


def _worker_main(args: argparse.Namespace) -> None:
    sys.path.insert(0, _REPO)
    from core import next_candle as nc

    if args.source == "binance":
        rows = load_binance_1m(
            os.path.join(BINANCE_DIR, f"{args.asset}_1m.json"), args.asset)
    else:
        rows = load_synthetic(args.asset, 8999, args.seed)
    candles = [{"time": r["t"], "open": r["o"], "high": r["h"],
                "low": r["l"], "close": r["c"]} for r in rows]

    eng = nc.NextCandleEngine(f"clean_eval/{args.arm}/{args.asset}", 60)
    preds = []
    for i in range(len(candles)):
        payload = eng.ingest(candles[i])
        if i + 1 >= len(candles):
            break  # prediction would target a candle outside the dataset
        target_t = candles[i + 1]["time"]
        if payload is None or int(payload.get("target_time", -1)) != target_t:
            preds.append({"target_t": target_t, "direction": None,
                          "prob": None, "state": "not_eligible_or_stale"})
            continue
        preds.append({
            "target_t": target_t,
            "direction": payload["direction"],
            "prob": float(payload["p_green"]),
            "state": payload.get("state"),
            "n_train": payload.get("n_train"),
        })
    with open(args.out_file, "w") as f:
        json.dump(preds, f)


# ── PREDICTION ASSEMBLY (arms -> uniform prediction records) ─────────────

def _region_index(split_by_asset: dict) -> dict:
    """(asset, target_t) -> region tag."""
    idx = {}
    for asset, s in split_by_asset.items():
        for c in s.warmup:
            idx[(asset, c.t)] = "warmup"
        for c in s.train:
            idx[(asset, c.t)] = "train"
        for c in s.selection:
            idx[(asset, c.t)] = "selection"
        for c in s.test:
            idx[(asset, c.t)] = "test"
    return idx


def _metrics_by_region(pred_report, region_index: dict, regions) -> dict:
    """accepted + no_signal rows -> binary_metrics per region (+pooled)."""
    out = {}
    rows_all = list(pred_report.accepted)
    # re-add no-signal rows as direction=None observations for coverage
    # (validate_predictions counts them but does not keep detail rows;
    # the caller passes them through accepted_rows_with_ns)
    for region in regions:
        rows = [r for r in rows_all
                if region_index.get((r["asset"], r["target_t"])) == region]
        out[region] = binary_metrics(rows, label=region)
    return out


def _assets_list(args: argparse.Namespace) -> list:
    if args.assets:
        return [a.strip() for a in args.assets.split(",") if a.strip()]
    return list(BINANCE_ASSETS)


def evaluate(args: argparse.Namespace) -> dict:
    started = dt.datetime.now(dt.timezone.utc)

    # 1. LOAD + VALIDATE ----------------------------------------------------
    datasets = []
    raw_rows = {}
    if args.source == "binance":
        for a in _assets_list(args):
            path = os.path.join(BINANCE_DIR, f"{a}_1m.json")
            if not os.path.exists(path):
                raise SystemExit(f"missing dataset: {path}")
            raw_rows[a] = load_binance_1m(path, a)
            datasets.append({
                "path": os.path.relpath(path, _REPO), "asset": a,
                "sha256": _sha256(path), "rows": len(raw_rows[a]),
                "source_classification":
                    "authorized_historical_public_exchange_binance_1m",
                "span_utc": [
                    dt.datetime.fromtimestamp(
                        raw_rows[a][0]["t"], dt.timezone.utc).isoformat(),
                    dt.datetime.fromtimestamp(
                        raw_rows[a][-1]["t"], dt.timezone.utc).isoformat()],
            })
    elif args.source == "synthetic":
        assets = ([a.strip() for a in args.assets.split(",") if a.strip()]
                  if args.assets else ["SYNTH1"])
        for a in assets:
            raw_rows[a] = load_synthetic(a, 8999, args.seed)
            datasets.append({
                "path": None, "asset": a, "sha256": None,
                "rows": len(raw_rows[a]),
                "source_classification": "SYNTHETIC_FIXTURE",
                "generator": f"seeded random walk seed={args.seed}",
            })
    else:
        return _evaluate_provenance(args, started)

    all_rows = [r for a in raw_rows for r in raw_rows[a]]
    candle_report = validate_candle_series(all_rows)

    accepted_by_asset = {}
    for c in candle_report.accepted:
        accepted_by_asset.setdefault(c.asset, []).append(c)
    for a in accepted_by_asset:
        accepted_by_asset[a].sort(key=lambda c: c.t)

    # 2. SPLIT (frozen) ------------------------------------------------------
    split = chronological_split(accepted_by_asset, SPLIT_SPEC)
    split_checks = split.verify(period=60)
    region_index = _region_index(split.by_asset)

    # 3. ENGINE ARMS (subprocess isolation) ----------------------------------
    engine_predictions = {}
    for arm in args.arms.split(","):
        arm = arm.strip()
        if not arm.startswith("cse_v1"):
            continue
        engine_predictions[arm] = {}
        for a in accepted_by_asset:
            preds = run_worker(arm, a, args.source, args.seed)
            engine_predictions[arm][a] = preds

    # 4. BASELINES -----------------------------------------------------------
    baselines = {}
    maj = {}
    for a, candles in accepted_by_asset.items():
        maj[a] = majority_direction(split.by_asset[a].train)
    logreg = {}
    logreg_fit_info = {}
    for a, candles in accepted_by_asset.items():
        X, y = [], []
        for i in range(max(2, FROZEN["warmup_candles"]), len(candles)):
            if region_index.get((a, candles[i].t)) != "train":
                continue
            lab = research_label(candles, i)
            if lab is None:
                continue
            X.append(research_features(candles, i))
            y.append(lab)
        if len(X) >= 50:
            m = PrespecifiedLogreg().fit(X, y)
            logreg[a] = m
            logreg_fit_info[a] = {"n_train_rows": len(X),
                                  "features": list(RESEARCH_BASELINE_FEATURES)}
        else:
            logreg_fit_info[a] = {"n_train_rows": len(X), "fitted": False}

    # 5. ASSEMBLE PREDICTION RECORDS PER ARM ---------------------------------
    arms_rows = {}

    def _add_arm(name: str, per_asset_directions) -> None:
        recs = []
        for a, candles in accepted_by_asset.items():
            for i in range(1, len(candles)):
                target = candles[i]
                region = region_index.get((a, target.t))
                if region == "warmup":
                    continue
                d, prob = per_asset_directions(a, candles, i)
                if d is None and prob is None:
                    recs.append({"asset": a, "period": 60,
                                 "target_t": target.t, "direction": None,
                                 "evidence_ts": float(target.t),
                                 "predictor": name})
                else:
                    recs.append({"asset": a, "period": 60,
                                 "target_t": target.t, "direction": d,
                                 "evidence_ts": float(target.t),
                                 "prob": prob, "predictor": name})
        arms_rows[name] = recs

    for arm, per_asset in engine_predictions.items():
        # PERF-FIX (Phase-K, 2026-10): by_t maps were rebuilt per candle —
        # O(n²) per arm/asset (tolerable at Phase-C's 9k candles, fatal at
        # 43k). Hoisted to one build per (arm, asset). Semantics identical:
        # same dict construction (last duplicate target_t wins), same
        # lookups — outputs byte-identical, verified by the Phase-C
        # battery re-run after this change.
        _maps = {a: {p["target_t"]: p for p in (per_asset.get(a) or [])}
                 for a in accepted_by_asset}

        def _engine(a, candles, i, _maps=_maps) -> tuple:
            p = _maps.get(a, {}).get(candles[i].t)
            if p is None or p["direction"] is None:
                return None, None
            return p["direction"], p["prob"]
        _add_arm(arm, _engine)

    _add_arm("baseline_majority_train",
             lambda a, c, i: (maj.get(a), None))
    _add_arm("baseline_prev_candle",
             lambda a, c, i: (previous_candle_direction(c[i - 1]), None))
    _add_arm("baseline_always_green", lambda a, c, i: ("CALL", None))
    _add_arm("baseline_always_red", lambda a, c, i: ("PUT", None))

    def _logreg_dir(a, candles, i):
        m = logreg.get(a)
        if m is None:
            return None, None
        p = m.predict_proba_green(research_features(candles, i))
        return ("CALL" if p >= 0.5 else "PUT"), p
    _add_arm("baseline_research_logreg_pre_specified", _logreg_dir)

    # 6. VALIDATE PREDICTIONS + METRICS --------------------------------------
    results = {}
    for name, recs in arms_rows.items():
        pred_report = validate_predictions(recs, candle_report)
        # tie-targeted emissions count in COVERAGE (a CALL/PUT was received)
        # while staying excluded from direction metrics (documented tie
        # policy) — reconstructed from the exclusion ledger.
        tie_rows = [{"asset": e["asset"], "target_t": e["target_t"],
                     "direction": e.get("direction"), "outcome": FLAT,
                     "prob": None, "correct": False}
                    for e in pred_report.excluded
                    if e["reason"] == "tie_outcome"]
        per_region = {}
        for region in ("train", "selection", "test"):
            rows = [r for r in pred_report.accepted
                    if region_index.get((r["asset"], r["target_t"])) == region]
            # coverage needs the NO_SIGNAL observations too: rebuild from recs
            ns = [{"asset": r["asset"], "target_t": r["target_t"],
                   "direction": None, "outcome": "FLAT", "prob": None,
                   "correct": False}
                  for r in recs
                  if r["direction"] is None
                  and region_index.get((r["asset"], r["target_t"])) == region]
            tr = [r for r in tie_rows
                  if region_index.get((r["asset"], r["target_t"])) == region]
            per_region[region] = binary_metrics(
                rows + ns + tr, label=f"{name}/{region}")
            if region == "test":
                per_region[region]["calibration"] = calibration_table(rows)
        per_asset_test = {}
        for a in accepted_by_asset:
            rows = [r for r in pred_report.accepted
                    if r["asset"] == a
                    and region_index.get((r["asset"], r["target_t"])) == "test"]
            ns = [{"asset": r["asset"], "target_t": r["target_t"],
                   "direction": None, "outcome": "FLAT", "prob": None,
                   "correct": False}
                  for r in recs
                  if r["direction"] is None and r["asset"] == a
                  and region_index.get((r["asset"], r["target_t"])) == "test"]
            tr = [r for r in tie_rows if r["asset"] == a
                  and region_index.get((r["asset"], r["target_t"])) == "test"]
            per_asset_test[a] = binary_metrics(rows + ns + tr,
                                               label=f"{a}/test")
        results[name] = {
            "prediction_integrity": pred_report.summary(),
            "regions": per_region,
            "per_asset_test": per_asset_test,
        }

    # 7. WALK-FORWARD (stability evidence) ------------------------------------
    wf_out = {}
    for name, recs in arms_rows.items():
        folds_metrics = []
        pooled = []
        for a, candles in accepted_by_asset.items():
            # PERF-FIX (Phase-K, 2026-10): position index + per-asset record
            # map built once per (arm, asset) instead of per fold/call —
            # _index_of's reverse linear scan was O(n) per lookup. Same
            # semantics (exact t match, None when absent).
            pos_by_t = {c.t: i for i, c in enumerate(candles)}
            rec_by_target = {r["target_t"]: r for r in recs
                             if r["asset"] == a}
            post = candles[FROZEN["warmup_candles"]:]
            folds = walk_forward_folds(post, n_folds=4, embargo_periods=1,
                                       period=60)
            for f in folds:
                if name == "baseline_research_logreg_pre_specified":
                    X, y = [], []
                    for c in f.train:
                        i = pos_by_t.get(c.t)
                        if i is None or i < 2:
                            continue
                        lab = research_label(candles, i)
                        if lab is None:
                            continue
                        X.append(research_features(candles, i))
                        y.append(lab)
                    m = PrespecifiedLogreg().fit(X, y) if len(X) >= 50 else None
                    fold_rows = []
                    for c in f.test:
                        i = pos_by_t.get(c.t)
                        if i is None or i < 2:
                            continue
                        if m is None:
                            fold_rows.append(
                                {"direction": None, "outcome": c.outcome,
                                 "prob": None, "correct": False})
                        else:
                            p = m.predict_proba_green(
                                research_features(candles, i))
                            d = "CALL" if p >= 0.5 else "PUT"
                            fold_rows.append({
                                "direction": d, "outcome": c.outcome,
                                "prob": p,
                                "correct": (d == "CALL") == (c.outcome == GREEN)})
                    folds_metrics.append({"asset": a, "fold": f.index,
                                          "n": len(fold_rows),
                                          "metrics": binary_metrics(
                                              fold_rows, label=f"wf{f.index}")})
                    pooled.extend(fold_rows)
                    continue
                # engine arms + fixed baselines: predictions already online
                # (rec_by_target hoisted per arm/asset — PERF-FIX above)
                by_region_rows = []
                for c in f.test:
                    r = rec_by_target.get(c.t)
                    if r is None or r["direction"] is None:
                        by_region_rows.append(
                            {"direction": None, "outcome": c.outcome,
                             "prob": None, "correct": False})
                    else:
                        by_region_rows.append({
                            "direction": r["direction"], "outcome": c.outcome,
                            "prob": r.get("prob"),
                            "correct": (r["direction"] == "CALL")
                                       == (c.outcome == GREEN)})
                folds_metrics.append({"asset": a, "fold": f.index,
                                      "n": len(by_region_rows),
                                      "metrics": binary_metrics(
                                          by_region_rows,
                                          label=f"wf{f.index}")})
                pooled.extend(by_region_rows)
        wf_out[name] = {"folds": folds_metrics,
                        "pooled": binary_metrics(pooled, label="wf_pooled")}

    # 8. LEAKAGE AUDIT BLOCK ---------------------------------------------------
    audit = {
        "split_verification": {k: v for k, v in split_checks.items()},
        "engine_causal_proof": _engine_perturbation_proof(args),
        "evidence_ledger_demo": _evidence_ledger_demo(),
        "label_echo_detector": "see scripts/test_phase_c_eval.py for the "
                               "adversarial battery",
    }

    # 9. BREAK-EVEN BLOCK -------------------------------------------------------
    be = None
    if args.payout is not None:
        tie_rates = {}
        for a in accepted_by_asset:
            cs = accepted_by_asset[a]
            ties = sum(1 for c in cs if c.outcome == FLAT)
            tie_rates[a] = ties / len(cs) if cs else 0.0
        mean_tie = (sum(tie_rates.values()) / len(tie_rates)
                    if tie_rates else 0.0)
        be = breakeven_report(args.payout, measured_tie_rate=mean_tie)
        be["measured_tie_rate_per_asset"] = {
            k: round(v, 4) for k, v in tie_rates.items()}
        be["payout_provenance"] = (
            "supplied by operator via --payout for illustration; NOT a "
            "measured or guaranteed platform rate")

    # 10. REPORT -----------------------------------------------------------------
    report = {
        "report_version": "clean_eval_v1",
        "generated_utc": started.isoformat(),
        "protocol": FROZEN,
        "classification": ("HISTORICAL_EXCHANGE_DATA (public Binance 1m — "
                           "authorized; NOT OTC; NOT live-feed evidence)"
                           if args.source == "binance"
                           else "SYNTHETIC (pipeline validation only — "
                                "never market evidence)"),
        "datasets": datasets,
        "integrity": candle_report.summary(),
        "splits": {
            "method": "chronological per-asset, frozen spec, embargo honored",
            "spec": {"warmup": SPLIT_SPEC.warmup,
                     "train_frac": SPLIT_SPEC.train_frac,
                     "selection_frac": SPLIT_SPEC.selection_frac,
                     "test_frac": SPLIT_SPEC.test_frac,
                     "embargo_periods": SPLIT_SPEC.embargo_periods},
            "cutoffs": split.cutoffs_report(),
        },
        "results": results,
        "walk_forward": {"n_folds": 4, "per_arm": wf_out},
        "leakage_audit": audit,
        "breakeven": be,
        "verdict": _verdict(args, results),
        "limitations": _limitations(args),
    }
    return report


def _index_of(candles, t):
    for i in range(len(candles) - 1, -1, -1):
        if candles[i].t == t:
            return i
        if candles[i].t < t:
            return None
    return None


def _engine_perturbation_proof(args) -> dict:
    """Causality proof: mutate candle j; predictions for targets <= j must
    be byte-identical. Run on the first asset, horizon 2500, j=2000."""
    first = _assets_list(args)[0] if args.source == "binance" else "SYNTH1"
    if args.source == "binance":
        rows = load_binance_1m(
            os.path.join(BINANCE_DIR, f"{first}_1m.json"), first)
    else:
        rows = load_synthetic("SYNTH1", 8999, args.seed)
    candles = [{"time": r["t"], "open": r["o"], "high": r["h"],
                "low": r["l"], "close": r["c"]} for r in rows]

    def replay(cs, horizon):
        sys.path.insert(0, _REPO)
        from core import next_candle as nc
        eng = nc.NextCandleEngine("clean_eval/perturb", 60)
        out = []
        for i in range(horizon):
            payload = eng.ingest(cs[i])
            out.append(copy.deepcopy(payload))
        return out

    horizon, j = 2500, 2000
    base = replay(candles, horizon)
    mutated = [dict(c) for c in candles]
    # adversarial future mutation: invert candle j's direction
    mutated[j]["close"] = (mutated[j]["open"]
                           + (mutated[j]["open"] - mutated[j]["close"]))
    mut_hi = max(mutated[j]["open"], mutated[j]["close"],
                 candles[j]["high"])
    mut_lo = min(mutated[j]["open"], mutated[j]["close"],
                 candles[j]["low"])
    mutated[j]["high"] = mut_hi
    mutated[j]["low"] = mut_lo
    pert = replay(mutated, horizon)
    mismatches = []
    for i in range(j):
        a, b = base[i], pert[i]
        if (a is None) != (b is None):
            mismatches.append(i)
            continue
        if a is None:
            continue
        for k in ("direction", "p_green", "target_time"):
            if a.get(k) != b.get(k):
                mismatches.append(i)
                break
    changed_after = sum(
        1 for i in range(j, horizon)
        if (base[i] is None) != (pert[i] is None)
        or (base[i] is not None and pert[i] is not None
            and base[i].get("direction") != pert[i].get("direction")))
    return {
        "method": ("engine replay on the first asset, horizon 2500, "
                   "candle 2000 adversarially inverted; predictions for "
                   "targets <= 2000 must be identical, later ones may move"),
        "predictions_before_mutation_compared": j,
        "mismatches_before_mutation": mismatches[:10],
        "n_mismatches_before_mutation": len(mismatches),
        "n_direction_changes_after_mutation": changed_after,
        "causal_by_construction": len(mismatches) == 0,
    }


def _evidence_ledger_demo() -> dict:
    """Documented boundary-evidence ledger: the 6 research features and the
    engine's single source candle all carry evidence_ts == target open time
    (source close == target open). violations == 0 for the cutoff rule."""
    led = EvidenceLedger()
    cutoff = 1_790_000_060.0
    for f in RESEARCH_BASELINE_FEATURES:
        led.add(f, cutoff)  # source candle closes exactly at target open
    led.add("engine_source_candle", cutoff)
    return {
        "rule": "evidence_ts <= target_t (boundary equality legitimate)",
        "entries": led.n_entries(),
        "violations": led.violations(cutoff),
        "boundary_note": ("strictly-greater evidence is rejected — see the "
                          "adversarial battery in test_phase_c_eval.py"),
    }


def _evaluate_provenance(args, started) -> dict:
    """Evaluate recorded emissions (signal_provenance) against frozen
    settlement outcomes (candle_settlement). READ-ONLY sqlite."""
    import sqlite3
    db_path = args.db
    if not db_path or not os.path.exists(db_path):
        return {
            "report_version": "clean_eval_v1",
            "generated_utc": started.isoformat(),
            "classification": "PROVENANCE_CAPTURE (app's own emissions)",
            "verdict": "INSUFFICIENT_DATA",
            "detail": (f"database not found: {db_path!r} — the repository "
                       "carries no runtime DB; nothing is fabricated to "
                       "pad the sample"),
        }
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri_s=True)
    conn.row_factory = sqlite3.Row
    try:
        prov = [dict(r) for r in conn.execute(
            "SELECT * FROM signal_provenance ORDER BY id")]
        sett = [dict(r) for r in conn.execute(
            "SELECT * FROM candle_settlement ORDER BY id")]
    finally:
        conn.close()
    if not prov and not sett:
        return {
            "report_version": "clean_eval_v1",
            "generated_utc": started.isoformat(),
            "classification": "PROVENANCE_CAPTURE (app's own emissions)",
            "verdict": "INSUFFICIENT_DATA",
            "detail": "signal_provenance and candle_settlement are empty",
        }
    sett_idx = {(r["asset"], int(r["period"]),
                 int(float(r["period_start"]))): r for r in sett}
    recs = []
    for r in prov:
        target_t = int(float(r["target_candle_open_epoch"]))
        direction = r.get("signal") or "NO_SIGNAL"
        rec = {"asset": r["asset"], "period": int(r["period"]),
               "target_t": target_t, "direction": direction,
               "evidence_ts": float(r.get("created_at_epoch") or 0),
               "predictor": "recorded_app_emission"}
        if r.get("research_direction") in ("CALL", "PUT") \
                and direction == "NO_SIGNAL":
            rec["research_direction"] = r["research_direction"]
        recs.append(rec)
    # candle universe from settled rows (frozen OHLC)
    candle_rows = []
    for r in sett:
        if r.get("open") is None or r.get("close") is None:
            continue
        candle_rows.append({
            "asset": r["asset"], "period": int(r["period"]),
            "t": int(float(r["period_start"])),
            "o": float(r["open"]), "h": float(r["high"] or r["open"]),
            "l": float(r["low"] or r["open"]), "c": float(r["close"]),
        })
    cr = validate_candle_series(candle_rows)
    pr = validate_predictions(recs, cr)
    # research-direction overlay: evaluate the engine's diagnostic preference
    res_rows = []
    for r in prov:
        target_t = int(float(r["target_candle_open_epoch"]))
        s = sett_idx.get((r["asset"], int(r["period"]), target_t))
        if s is None or s.get("state") != "settled":
            continue
        rd = r.get("research_direction")
        if rd not in ("CALL", "PUT"):
            continue
        oc = outcome_of(float(s["open"]), float(s["close"]))
        if oc == FLAT:
            continue
        res_rows.append({"direction": rd, "outcome": oc, "prob": None,
                         "correct": (rd == "CALL") == (oc == GREEN)})
    ns_rows = [{"direction": None, "outcome": FLAT, "prob": None,
                "correct": False} for _ in range(pr.no_signal_count)]
    return {
        "report_version": "clean_eval_v1",
        "generated_utc": started.isoformat(),
        "classification": "PROVENANCE_CAPTURE (app's own emissions; "
                          "ground-truth labels derive from the recorded "
                          "feed — see limitations)",
        "n_provenance_rows": len(prov),
        "n_settlement_rows": len(sett),
        "prediction_integrity": pr.summary(),
        "gated_signal_metrics": binary_metrics(
            [r for r in pr.accepted] + ns_rows, label="gated_signals"),
        "research_direction_metrics": binary_metrics(
            res_rows, label="research_direction_diagnostic"),
        "verdict": ("INSUFFICIENT_DATA" if len(pr.accepted) + len(res_rows) < 30
                    else "PROVENANCE_EVALUATED_WITH_LIMITATIONS"),
        "limitations": [
            "ground-truth outcomes come from the app's recorded feed "
            "(the unauthorized Quotex channel) — observational record, not "
            "independently verified market data",
            "gated signals are expected to be 100% NO_SIGNAL under the "
            "Truth Layer (unauthorized source): coverage 0 is the CORRECT "
            "current state, not a defect",
        ],
    }


def _verdict(args, results) -> dict:
    test_accs = {}
    for name, blk in results.items():
        acc = blk["regions"]["test"].get("accuracy")
        cov = blk["regions"]["test"].get("coverage")
        if acc is not None:
            test_accs[name] = {"accuracy": round(acc, 4),
                               "coverage": (round(cov, 4)
                                            if cov is not None else None),
                               "ci95": blk["regions"]["test"]["accuracy_ci95"]}
    if args.source == "synthetic":
        verdict = "SYNTHETIC_VALIDATION_ONLY"
    else:
        verdict = "OOS_EVALUATED_WITH_LIMITATIONS"
    return {
        "verdict": verdict,
        "headline_test_metrics": test_accs,
        "headline_note": ("test region = final 25% holdout, evaluated once "
                          "by the pre-committed frozen protocol; selection "
                          "region exists so tuning never needs the holdout"),
    }


def _limitations(args) -> list:
    lims = [
        "CSE v1 ships a global prior (core/next_candle_weights.json) with "
        "no training-window metadata; if it was fitted on this same Binance "
        "window, part of every prediction is in-sample. The "
        "cse_v1_research_localprior arm (QX_NC_BLEND_K=1, research variant) "
        "bounds this concern; production numerics were NOT changed",
        "public exchange 1m data is NOT OTC candle data; nothing here is "
        "evidence about Quotex OTC predictive skill",
        "single contiguous ~6.25-day window (2026-09-22..29) with no regime "
        "diversity; confidence intervals already reflect the small sample",
        "grading is open->close of the target candle; broker settlement is "
        "strike->expiry (documented grading gap, unmodelled here)",
        "warmup=600 is a protocol constant of THIS evaluator (the engine's "
        "own hard gate is 61 closes); metrics are not comparable to "
        "harnesses with different warmups without re-running",
    ]
    if args.source == "synthetic":
        lims = ["SYNTHETIC data — mechanics validation only, never market "
                "evidence"]
    return lims


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=("binance", "synthetic", "provenance"),
                    default="binance")
    ap.add_argument("--assets", default=None,
                    help="comma-separated asset filter (default: all)")
    ap.add_argument("--arms", default="cse_v1",
                    help="engine arms, comma-separated: cse_v1, "
                         "cse_v1_research_localprior, cse_v1_cleanprior")
    ap.add_argument("--binance-dir", default=None,
                    help="override the binance data dir (PHASE-K: the 30-day "
                         "dataset; default: the original pre-committed "
                         "holdout files)")
    ap.add_argument("--weights-file", default=None,
                    help="candidate global-prior weights for the "
                         "cse_v1_cleanprior arm (PHASE-K)")
    ap.add_argument("--payout", type=float, default=None,
                    help="net payout per unit for the break-even block "
                         "(explicitly supplied, never hardcoded)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--db", default=None,
                    help="sqlite DB path for --source provenance")
    ap.add_argument("--out-dir", default=os.path.join(_REPO, "scripts",
                                                      "out"))
    ap.add_argument("--internal-worker", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--arm", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--asset", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--out-file", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.internal_worker:
        _worker_main(args)
        return

    # PHASE-K wiring: data dir + candidate weights for the cleanprior arm.
    if args.binance_dir:
        global BINANCE_DIR
        BINANCE_DIR = os.path.abspath(args.binance_dir)
    if args.weights_file:
        if not os.path.isfile(args.weights_file):
            raise SystemExit(f"--weights-file not found: {args.weights_file}")
        run_worker.weights_file = os.path.abspath(args.weights_file)

    os.makedirs(args.out_dir, exist_ok=True)
    report = evaluate(args)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") \
        + f"_{os.getpid()}%06d" % (dt.datetime.now(dt.timezone.utc).microsecond)
    out_path = os.path.join(args.out_dir, f"clean_eval_{stamp}.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1, sort_keys=False, default=str)
    print(json.dumps({
        "out_path": os.path.relpath(out_path, _REPO),
        "verdict": report.get("verdict"),
        "integrity": report.get("integrity"),
    }, indent=1, default=str))
    print(f"REPORT WRITTEN: {out_path}")


if __name__ == "__main__":
    main()
