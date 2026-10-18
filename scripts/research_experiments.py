#!/usr/bin/env python3
"""PHASE F — controlled prediction research experiments (research only).

Runs PRE-SPECIFIED candidate experiments through the Phase-C frozen
evaluation protocol (core/eval_clean + backtest_clean_eval loaders) on
the authorized Binance 1m datasets. Every experiment carries a full
manifest (F3): unique experiment id, feature definitions, train/
selection/test cutoffs (= the frozen protocol's), hyperparameters,
seeds (all candidates are deterministic), dataset provenance (sha256 +
classification), results on the SAME eligible observations as the SAME
baselines, accuracy AND coverage, Wilson 95% uncertainty, and an
explicit acceptance decision.

PRE-COMMITTED ACCEPTANCE RULE (F5, frozen before the holdout was
touched): a candidate is eligible for further paper/shadow evaluation
ONLY IF (a) its TEST-region Wilson CI lower bound exceeds the
previous-candle baseline's TEST point estimate AND (b) its TEST coverage
is >= 0.5 (no coverage gaming). Anything else is REJECTED and recorded
as a negative result — including the production engine itself if it
fails. No selection on the holdout: every experiment is fully specified
in code before evaluation and all results (positive AND negative) are
recorded in the output manifest.

NO production model is replaced, retrained or promoted by this script.
No broker contact. No credentials. No trades.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.eval_clean.integrity import (  # noqa: E402
    validate_candle_series, GREEN, RED, FLAT)
from core.eval_clean.splits import SplitSpec, chronological_split  # noqa: E402
from core.eval_clean.baselines import (  # noqa: E402
    previous_candle_direction, PrespecifiedLogreg, RESEARCH_BASELINE_FEATURES)
from core.eval_clean.metrics import binary_metrics, wilson_ci  # noqa: E402
from core.eval_clean.leakage import SplitScaler  # noqa: E402

# FROZEN protocol (identical to backtest_clean_eval — one protocol for
# every experiment; changing anything here requires a new experiment id)
FROZEN = {
    "protocol_version": "clean_eval_v1",
    "warmup_candles": 600,
    "train_frac": 0.50, "selection_frac": 0.25, "test_frac": 0.25,
    "embargo_periods": 1,
    "tie_policy": "FLAT excluded from direction metrics, counted",
}
SPLIT_SPEC = SplitSpec(warmup=600, train_frac=0.50, selection_frac=0.25,
                       test_frac=0.25, embargo_periods=1)

BINANCE_ASSETS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT")
BINANCE_DIR = os.path.join(_REPO, "scripts", "data", "binance")

# Pre-specified extended feature set (EXP-003): candle anatomy, returns,
# volatility, trend/momentum, time-of-observation. Frozen definitions.
EXTENDED_FEATURES = {
    "body_ratio":     "(close-open)/(high-low) of source candle",
    "upper_wick":     "(high-max(open,close))/(high-low)",
    "lower_wick":     "(min(open,close)-low)/(high-low)",
    "ret1":           "close/open - 1 (source)",
    "ret2":           "close[t-2]/open[t-2] - 1",
    "ret5":           "close[t-5]/open[t-5] - 1",
    "range_atr":      "source range / mean range of last 14",
    "ema_gap":        "(close - ema8) / close",
    "streak":         "signed consecutive same-color count ending at source",
    "body_atr":       "(close-open) / mean range of last 14",
    "hour_sin":       "sin(2*pi*utc_hour/24)",
    "hour_cos":       "cos(2*pi*utc_hour/24)",
}

LOGREG_HP = {"lr": 0.1, "iters": 400, "l2": 1.0,
             "standardization": "train-split only (SplitScaler)"}

# EXP-004 filter thresholds (pre-specified; no sweeps)
FILTER_AGREE = {"name": "engine+prev-candle agreement",
                "rule": "emit only when engine direction == previous "
                        "candle direction"}
FILTER_CONVICTION = {"name": "engine conviction",
                     "rule": "emit only when |p_green - 0.5| >= 0.02"}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_binance_1m(path: str, asset: str) -> list:
    with open(path) as f:
        raw = json.load(f)
    return [{"asset": asset, "period": 60, "t": int(r["t"]) // 1000,
             "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
             "c": float(r["c"]), "v": float(r.get("v", 0.0) or 0.0)}
            for r in raw]


def _extended_features(candles, i):
    """EXP-003 features for target candle i (evidence: candles < i)."""
    src = candles[i - 1]
    rng = src.high - src.low
    if rng <= 0:
        rng = 1e-9
    body = (src.close - src.open) / rng
    upper = (src.high - max(src.open, src.close)) / rng
    lower = (min(src.open, src.close) - src.low) / rng
    ret1 = src.close / src.open - 1.0
    prev = candles[i - 2]
    ret2 = prev.close / prev.open - 1.0
    p5 = candles[i - 5]
    ret5 = p5.close / p5.open - 1.0
    w14 = candles[max(0, i - 14):i]
    rngs = [c.high - c.low for c in w14 if c.high - c.low > 0]
    atr = (sum(rngs) / len(rngs)) if rngs else rng
    range_atr = (src.high - src.low) / atr if atr > 0 else 0.0
    closes = [c.close for c in candles[max(0, i - 8):i]]
    ema = closes[0] if closes else src.close
    k = 2.0 / (len(closes) + 1.0) if closes else 0.25
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    ema_gap = (src.close - ema) / src.close if src.close else 0.0
    streak = 0
    for j in range(i - 1, max(0, i - 30) - 1, -1):
        col = 1 if candles[j].close > candles[j].open else (
            -1 if candles[j].close < candles[j].open else 0)
        if col == 0:
            break
        if streak == 0 or (col > 0) == (streak > 0):
            streak += col
        else:
            break
    body_atr = (src.close - src.open) / atr if atr > 0 else 0.0
    hour = (candles[i].t // 3600) % 24
    import math
    return [body, upper, lower, ret1, ret2, ret5, range_atr, ema_gap,
            streak, body_atr,
            math.sin(2 * math.pi * hour / 24.0),
            math.cos(2 * math.pi * hour / 24.0)]


def _engine_predictions(asset: str, rows):
    """Subprocess-isolated CSE replay (identical to backtest_clean_eval
    workers) — the production engine AS IS."""
    import subprocess
    import tempfile
    out_file = tempfile.mktemp(prefix=f"phase_f_eng_{asset}_",
                               suffix=".json")
    cmd = [sys.executable,
           os.path.join(_REPO, "scripts", "backtest_clean_eval.py"),
           "--internal-worker", "--arm", "cse_v1", "--asset", asset,
           "--source", "binance", "--seed", "7",
           "--out-file", out_file]
    proc = subprocess.run(cmd, cwd=_REPO, capture_output=True, text=True,
                          timeout=1200)
    if proc.returncode != 0:
        raise RuntimeError(f"engine worker failed: {proc.stderr[-500:]}")
    with open(out_file) as f:
        preds = json.load(f)
    os.unlink(out_file)
    return {p["target_t"]: p for p in preds}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=os.path.join(_REPO, "scripts",
                                                      "out"))
    args = ap.parse_args()

    started = dt.datetime.now(dt.timezone.utc)
    datasets = []
    raw = {}
    for a in BINANCE_ASSETS:
        path = os.path.join(BINANCE_DIR, f"{a}_1m.json")
        raw[a] = load_binance_1m(path, a)
        datasets.append({
            "asset": a, "sha256": _sha256(path), "rows": len(raw[a]),
            "classification":
                "authorized_historical_public_exchange (NOT OTC)"})
    report = validate_candle_series(
        [r for a in raw for r in raw[a]])
    accepted_by_asset = {}
    for c in report.accepted:
        accepted_by_asset.setdefault(c.asset, []).append(c)
    for a in accepted_by_asset:
        accepted_by_asset[a].sort(key=lambda c: c.t)
    split = chronological_split(accepted_by_asset, SPLIT_SPEC)
    split.verify(period=60)
    region_index = {}
    for a, s in split.by_asset.items():
        for c in s.warmup:
            region_index[(a, c.t)] = "warmup"
        for c in s.train:
            region_index[(a, c.t)] = "train"
        for c in s.selection:
            region_index[(a, c.t)] = "selection"
        for c in s.test:
            region_index[(a, c.t)] = "test"

    # engine predictions (one subprocess per asset)
    engine_preds = {a: _engine_predictions(a, raw[a]) for a in
                    BINANCE_ASSETS}

    def _metrics_for(direction_fn):
        """direction_fn(asset, candles, i) -> ("CALL"|"PUT"|None, prob).
        Returns per-region metrics dicts pooled across assets."""
        out = {}
        for region in ("selection", "test"):
            rows = []
            for a, candles in accepted_by_asset.items():
                for i in range(2, len(candles)):
                    if region_index.get((a, candles[i].t)) != region:
                        continue
                    d, prob = direction_fn(a, candles, i)
                    oc = candles[i].outcome
                    rows.append({
                        "direction": d, "outcome": oc, "prob": prob,
                        "correct": (d == "CALL") == (oc == GREEN)
                        if d in ("CALL", "PUT") else False})
            out[region] = binary_metrics(rows, label=region)
        return out

    experiments = []

    def _add(exp_id, desc, manifest, mets, accept_rule):
        test = mets["test"]
        sel = mets["selection"]
        # F5 acceptance (pre-committed): CI lower bound > baseline point
        base_acc = BASELINE_PREV["test"]["accuracy"]
        ci = test.get("accuracy_ci95")
        eligible = (ci is not None and ci[0] > base_acc
                    and (test.get("coverage") or 0) >= 0.5)
        decision = ("ELIGIBLE_FOR_PAPER_SHADOW" if eligible else
                    "REJECTED_RETAIN_BASELINE")
        experiments.append({
            "experiment_id": exp_id,
            "description": desc,
            "manifest": manifest,
            "results": {
                "selection": {k: sel[k] for k in
                              ("n_total", "coverage", "accuracy",
                               "accuracy_ci95", "n_tie_excluded")},
                "test": {k: test[k] for k in
                         ("n_total", "coverage", "accuracy",
                          "accuracy_ci95", "n_tie_excluded",
                          "call_precision", "put_precision",
                          "balanced_accuracy", "brier_score")},
            },
            "acceptance_rule": accept_rule,
            "acceptance_decision": decision,
            "baseline_reference": {"name": "previous_candle",
                                  "test_accuracy": base_acc},
        })

    ACCEPT_RULE = ("eligible iff test CI95 lower bound > prev-candle "
                   "test accuracy AND test coverage >= 0.5 (pre-committed "
                   "before holdout evaluation; no selection on holdout)")

    # ── reference baselines on the same observations ─────────────────
    BASELINE_PREV = _metrics_for(
        lambda a, c, i: (previous_candle_direction(c[i - 1]), None))
    _add("EXP-000-baseline-prev-candle",
         "Reference baseline: continuation (previous candle direction).",
         {"type": "baseline", "features": "previous candle outcome",
          "hyperparameters": "none", "seed": "deterministic (no RNG)"},
         BASELINE_PREV, ACCEPT_RULE)

    # ── EXP-001: production engine AS-IS (CSE v1) ────────────────────
    mets = _metrics_for(
        lambda a, c, i: (
            (lambda p: (p["direction"], p["prob"]) if p and
             p.get("direction") else (None, None))(
                engine_preds[a].get(c[i].t))))
    _add("EXP-001-cse-v1-production",
         "The PRODUCTION engine exactly as deployed (subprocess replay, "
         "live-parity ingest).",
         {"type": "production_engine_replay", "engine": "next_candle_v1",
          "features": "35-feature CSE anatomy set (core/next_candle.py "
                      "FEATURE_NAMES)",
          "hyperparameters": "production defaults (BLEND_K=3000, "
                             "FIT_MIN=300, REFIT=50, CALIB_SLOPE=0.636)",
          "seed": "deterministic (GD, no RNG)",
          "known_limitation": "global prior provenance unverifiable — "
                              "may be in-sample on this window"},
         mets, ACCEPT_RULE)

    # ── EXP-002: pre-specified 6-feature logreg (Phase C baseline) ───
    models_6f = {}
    for a, candles in accepted_by_asset.items():
        X, y = [], []
        for i in range(max(2, 600), len(candles)):
            if region_index.get((a, candles[i].t)) != "train":
                continue
            from core.eval_clean.baselines import (
                research_features, research_label)
            lab = research_label(candles, i)
            if lab is None:
                continue
            X.append(research_features(candles, i))
            y.append(lab)
        models_6f[a] = PrespecifiedLogreg().fit(X, y)

    def _dir_6f(a, candles, i):
        m = models_6f.get(a)
        if m is None:
            return None, None
        from core.eval_clean.baselines import research_features
        p = m.predict_proba_green(research_features(candles, i))
        return ("CALL" if p >= 0.5 else "PUT"), p
    mets = _metrics_for(_dir_6f)
    _add("EXP-002-logreg-6feat",
         "Pre-specified 6-feature logistic regression (Phase C research "
         "baseline).",
         {"type": "statistical_model", "features":
             dict(zip(RESEARCH_BASELINE_FEATURES,
                      ["body_ratio", "upper_wick", "lower_wick", "ret1",
                       "ret2", "range_atr_ratio"])),
          "hyperparameters": LOGREG_HP, "seed": "deterministic (GD)"},
         mets, ACCEPT_RULE)

    # ── EXP-003: extended 12-feature logreg ──────────────────────────
    models_12f = {}
    for a, candles in accepted_by_asset.items():
        X, y = [], []
        for i in range(max(14, 600), len(candles)):
            if region_index.get((a, candles[i].t)) != "train":
                continue
            oc = candles[i].outcome
            if oc == FLAT:
                continue
            X.append(_extended_features(candles, i))
            y.append(1 if oc == GREEN else 0)
        if len(X) >= 50:
            models_12f[a] = PrespecifiedLogreg().fit(X, y)

    def _dir_12f(a, candles, i):
        m = models_12f.get(a)
        if m is None:
            return None, None
        p = m.predict_proba_green(_extended_features(candles, i))
        return ("CALL" if p >= 0.5 else "PUT"), p
    mets = _metrics_for(_dir_12f)
    _add("EXP-003-logreg-12feat-extended",
         "Extended pre-specified 12-feature logistic regression "
         "(anatomy + returns + volatility + trend + time-of-day).",
         {"type": "statistical_model", "features": EXTENDED_FEATURES,
          "hyperparameters": LOGREG_HP, "seed": "deterministic (GD)"},
         mets, ACCEPT_RULE)

    # ── EXP-004a: engine + prev-candle agreement filter ──────────────
    def _dir_agree(a, candles, i):
        p = engine_preds[a].get(candles[i].t)
        if not p or not p.get("direction"):
            return None, None
        prev = previous_candle_direction(candles[i - 1])
        if prev is None or prev != p["direction"]:
            return None, None          # abstain (filter)
        return p["direction"], p["prob"]
    mets = _metrics_for(_dir_agree)
    _add("EXP-004a-engine-agreement-filter",
         "Conservative rule-based filter: engine emits only when it "
         "agrees with the previous candle's direction.",
         {"type": "rule_based_filter_on_EXP-001", **FILTER_AGREE,
          "features": "engine direction (EXP-001) + previous candle "
                      "direction",
          "hyperparameters": "none (pre-specified rule)",
          "seed": "deterministic (no RNG)"},
         mets, ACCEPT_RULE)

    # ── EXP-004b: engine conviction filter ───────────────────────────
    def _dir_conviction(a, candles, i):
        p = engine_preds[a].get(candles[i].t)
        if not p or not p.get("direction") or p.get("prob") is None:
            return None, None
        if abs(float(p["prob"]) - 0.5) < 0.02:
            return None, None          # abstain (filter)
        return p["direction"], p["prob"]
    mets = _metrics_for(_dir_conviction)
    _add("EXP-004b-engine-conviction-filter",
         "Conservative rule-based filter: engine emits only with "
         "|p_green - 0.5| >= 0.02.",
         {"type": "rule_based_filter_on_EXP-001",
          **FILTER_CONVICTION,
          "features": "engine direction + p_green (EXP-001)",
          "hyperparameters": "threshold 0.02 (pre-specified, no sweep)",
          "seed": "deterministic (no RNG)"},
         mets, ACCEPT_RULE)

    # ── EXP-005: engine calibration report (F4 evidence) ─────────────
    from core.eval_clean.metrics import calibration_table
    cal_rows = []
    for a, candles in accepted_by_asset.items():
        for i in range(2, len(candles)):
            if region_index.get((a, candles[i].t)) != "test":
                continue
            p = engine_preds[a].get(candles[i].t)
            if p and p.get("direction") and p.get("prob") is not None:
                cal_rows.append({"direction": p["direction"],
                                 "outcome": candles[i].outcome,
                                 "prob": float(p["prob"]),
                                 "correct": False})
    calibration = calibration_table(cal_rows)
    exp5 = {
        "experiment_id": "EXP-005-engine-calibration-audit",
        "description": ("F4 confidence honesty: evaluate whether the "
                        "engine's p_green can be presented as a "
                        "probability. Verdict feeds the UI labeling "
                        "policy (internal score unless established)."),
        "calibration_table_test": calibration,
        "verdict": ("CALIBRATION_NOT_ESTABLISHED — p_green spans only "
                    "~[0.4,0.6); treat as an INTERNAL score, never 'the "
                    "chance of being correct'"),
    }

    # ── final acceptance summary (F5) ────────────────────────────────
    eligible = [e["experiment_id"] for e in experiments
                if e["acceptance_decision"].startswith("ELIGIBLE")]
    summary = {
        "report_version": "phase_f_research_v1",
        "generated_utc": started.isoformat(),
        "protocol": FROZEN,
        "datasets": datasets,
        "integrity": report.summary(),
        "splits_cutoffs": split.cutoffs_report(),
        "experiments": experiments,
        "calibration_audit": exp5,
        "acceptance_summary": {
            "rule": ACCEPT_RULE,
            "eligible_for_paper_shadow": eligible,
            "decision": ("RETAIN BASELINE / NO_SIGNAL — no candidate "
                         "convincingly beats the previous-candle "
                         "baseline on the pre-committed holdout"
                         if not eligible else
                         "candidates eligible: " + ", ".join(eligible)),
            "production_impact": "NONE — no production model replaced, "
                                 "retrained or promoted (directive: no "
                                 "automatic production model replacement)",
        },
        "negative_results_recorded": [
            e["experiment_id"] for e in experiments
            if e["acceptance_decision"].startswith("REJECTED")],
    }

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime(
        "%Y%m%dT%H%M%S") + f"_{os.getpid()}"
    out_path = os.path.join(args.out_dir, f"phase_f_experiments_"
                                          f"{stamp}.json")
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print(json.dumps({
        "out_path": os.path.relpath(out_path, _REPO),
        "experiments": [e["experiment_id"] for e in experiments],
        "decisions": {e["experiment_id"]: e["acceptance_decision"]
                      for e in experiments},
        "calibration_verdict": exp5["verdict"],
        "final": summary["acceptance_summary"]["decision"],
    }, indent=1))
    print(f"REPORT WRITTEN: {out_path}")


if __name__ == "__main__":
    main()
