"""scripts/retrain_clean_prior.py — PHASE-K (2026-10): provenance-clean
retraining of the CSE global prior.

WHY (blocker B5): the production weights (core/next_candle_weights.json)
have UNVERIFIABLE provenance — nobody can prove the global prior was not
trained on data overlapping the evaluation window. This script bounds
that question permanently:

  1. It retrains the SAME model family (regularized logistic regression
     over the SAME 35 features, harvested with the engine's own feature
     code — zero reimplementation drift) on the AUTHORIZED 30-day Binance
     dataset (scripts/data/binance_30d, sha256-fingerprinted).
  2. It records a COMPLETE provenance manifest (dataset hashes, split
     cutoffs, hyperparameters, git commit, timestamps).
  3. It evaluates BOTH priors (production vs clean-retrained) under the
     SAME frozen protocol (eval_clean splits/metrics/baselines via
     backtest_clean_eval) on identical data.
  4. It applies a PRE-COMMITTED acceptance rule (below) and registers the
     candidate in core/model_validation.json ONLY if it passes.

PRE-COMMITTED ACCEPTANCE RULE (fixed before any evaluation ran;
deliberately mirrors the Phase-F rule applied to a fresh dataset):

    The clean-prior candidate (as the cse_v1_cleanprior engine arm) passes
    IFF, on the pooled TEST region of the 30-day dataset:
      (a) Wilson-95% lower bound of accuracy  >  accuracy of the
          previous-candle baseline (point estimate), AND
      (b) coverage >= 0.5, AND
      (c) Brier score <= the previous-candle baseline's Brier.

    Any other outcome -> REJECTED; production weights are NOT replaced
    (byte-identical guarantee) and the registry stays unchanged. The
    clean-retrained artifact is still recorded as a RESEARCH artifact with
    full provenance — that is what bounds B5: a provenance-complete
    equivalent exists and its honest measured performance is documented.

DETERMINISM: the fit is full-batch gradient descent from zero
initialization on a fixed chronological train set — no RNG anywhere, so
two runs produce byte-identical weights (pinned by the battery).

Usage:
    python3 scripts/retrain_clean_prior.py            # quick: 2 assets
    python3 scripts/retrain_clean_prior.py --full     # all 8 assets
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

DATA_DIR = os.path.join(_REPO, "scripts", "data", "binance_30d")
OUT_DIR = os.path.join(_REPO, "scripts", "out")
QUICK_ASSETS = ("BTCUSDT", "ETHUSDT")
FULL_ASSETS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT",
               "SOLUSDT", "DOGEUSDT", "LTCUSDT")
WARMUP = 600           # mirrors the frozen SplitSpec
TRAIN_FRAC = 0.50
SELECTION_FRAC = 0.25
TEST_FRAC = 0.25
EMBARGO = 1

ACCEPTANCE_RULE = (
    "pooled TEST region: wilson_lower(cleanprior accuracy) > "
    "accuracy(prev_candle baseline) AND coverage >= 0.5 AND "
    "brier(cleanprior) <= brier(prev_candle)"
)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_REPO,
            capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception:
        return "unknown"


def harvest(asset: str) -> tuple:
    """Feed the 30-day candles through a REAL NextCandleEngine and collect
    (feature_row, next_candle_color) pairs using the engine's own
    ``_pending_x`` — the exact features its live predictions use."""
    from core import next_candle as nc

    path = os.path.join(DATA_DIR, f"{asset}_1m.json")
    with open(path) as f:
        raw = json.load(f)
    candles = [{"time": int(r["t"]) // 1000, "open": float(r["o"]),
                "high": float(r["h"]), "low": float(r["l"]),
                "close": float(r["c"])} for r in raw]

    eng = nc.NextCandleEngine(f"retrain/{asset}", 60)
    X, Y, t_of_row = [], [], []
    for i in range(len(candles) - 1):
        payload = eng.ingest(candles[i])
        x = eng._pending_x
        if payload is None or x is None:
            continue
        nxt = candles[i + 1]
        color = 1 if nxt["close"] > nxt["open"] else (
            -1 if nxt["close"] < nxt["open"] else 0)
        if color == 0:      # doji target — engine skips; parity kept
            continue
        X.append([float(v) for v in x])
        Y.append(color)
        t_of_row.append(candles[i + 1]["time"])
    return X, Y, t_of_row, path, len(candles)


def chronological_bounds(n_rows: int) -> dict:
    """Frozen split arithmetic over the eligible (post-warmup) rows."""
    n_elig = max(0, n_rows - WARMUP)
    n_train = int(n_elig * TRAIN_FRAC)
    n_sel = int(n_elig * SELECTION_FRAC)
    # test = the rest (minus embargo between selection and test)
    return {
        "train_lo": WARMUP, "train_hi": WARMUP + n_train,
        "selection_hi": WARMUP + n_train + n_sel,
        "test_lo": WARMUP + n_train + n_sel + EMBARGO,
        "test_hi": n_rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true",
                    help="run all 8 assets (default: quick 2-asset run)")
    ap.add_argument("--out-dir", default=OUT_DIR)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    started = datetime.now(timezone.utc)
    assets = list(FULL_ASSETS if args.full else QUICK_ASSETS)
    print(f"[phase-k] provenance-clean retraining — assets: {assets}")

    # ── 1. harvest + split + pooled fit ────────────────────────────────
    from core import next_candle as nc

    pooled_X, pooled_Y = [], []
    manifest = {
        "phase": "K",
        "purpose": "provenance-clean retraining of the CSE global prior "
                   "(blocker B5 resolution)",
        "generated_utc": started.isoformat(),
        "git_commit": _git_commit(),
        "dataset": {
            "classification": "HISTORICAL_EXCHANGE_DATA (public Binance 1m "
                              "klines — authorized; NOT OTC; NOT "
                              "live-feed evidence)",
            "dir": os.path.relpath(DATA_DIR, _REPO),
            "files": {},
        },
        "model_family": "regularized logistic regression, 35 standardized "
                        "features, full-batch GD (L2=1.0, iters=500, "
                        "lr=0.3, momentum 0.9) — identical math to "
                        "core.next_candle._Logistic",
        "determinism": "no RNG: zero-init GD on a fixed chronological "
                       "train set; re-runs are byte-identical",
        "split_spec": {"warmup": WARMUP, "train_frac": TRAIN_FRAC,
                       "selection_frac": SELECTION_FRAC,
                       "test_frac": TEST_FRAC, "embargo": EMBARGO,
                       "note": "identical to the frozen Phase-C SplitSpec"},
        "acceptance_rule_pre_committed": ACCEPTANCE_RULE,
        "per_asset": {},
        "train_rows_total": 0,
    }

    for asset in assets:
        t0 = time.time()
        X, Y, t_of_row, path, n_candles = harvest(asset)
        n_rows = len(X)
        b = chronological_bounds(n_rows)
        X_tr = [X[i] for i in range(b["train_lo"], b["train_hi"])]
        Y_tr = [Y[i] for i in range(b["train_lo"], b["train_hi"])]
        pooled_X.extend(X_tr)
        pooled_Y.extend(Y_tr)
        manifest["dataset"]["files"][f"{asset}_1m.json"] = {
            "sha256": _sha256(path), "candles": n_candles,
        }
        manifest["per_asset"][asset] = {
            "eligible_rows": n_rows,
            "train_rows": len(X_tr),
            "train_time_range_utc": [
                datetime.fromtimestamp(t_of_row[b["train_lo"]],
                                       tz=timezone.utc).isoformat(),
                datetime.fromtimestamp(t_of_row[b["train_hi"] - 1],
                                       tz=timezone.utc).isoformat()],
            "test_time_range_utc": [
                datetime.fromtimestamp(t_of_row[min(b["test_lo"],
                                                   n_rows - 1)],
                                       tz=timezone.utc).isoformat(),
                datetime.fromtimestamp(t_of_row[n_rows - 1],
                                       tz=timezone.utc).isoformat()],
        }
        print(f"[phase-k] {asset}: {n_rows} eligible rows, "
              f"{len(X_tr)} train rows ({time.time()-t0:.0f}s)")

    manifest["train_rows_total"] = len(pooled_X)
    print(f"[phase-k] pooled train rows: {len(pooled_X)}")

    # ── 2. fit the pooled model ────────────────────────────────────────
    model = nc._Logistic()
    model.fit(pooled_X, pooled_Y)
    weights = {
        "w": [float(v) for v in model.w],
        "b": float(model.b),
        "mu": [float(v) for v in model.mu],
        "sd": [float(v) for v in model.sd],
        "names": list(nc.FEATURE_NAMES),
    }
    stamp = started.strftime("%Y%m%dT%H%M%S")
    cand_path = os.path.join(args.out_dir, f"retrained_prior_{stamp}.json")
    artifact = {
        "weights": weights,
        "manifest": manifest,
        "weights_sha256": hashlib.sha256(
            json.dumps(weights, sort_keys=True).encode()).hexdigest(),
    }
    with open(cand_path, "w") as f:
        json.dump(artifact, f, indent=1)
    print(f"[phase-k] candidate weights + manifest -> {cand_path}")

    # ── 3. frozen-protocol evaluation (both priors, identical data) ────
    import backtest_clean_eval as bce
    ns = argparse.Namespace(
        source="binance", assets=",".join(assets),
        arms="cse_v1,cse_v1_cleanprior",
        payout=0.85, seed=7, db=None,
        out_dir=args.out_dir,
        internal_worker=False, arm=None, asset=None, out_file=None,
        binance_dir=DATA_DIR, weights_file=cand_path,
    )
    bce.BINANCE_DIR = DATA_DIR
    bce.run_worker.weights_file = cand_path
    report = bce.evaluate(ns)

    # ── 4. acceptance (pre-committed rule) ─────────────────────────────
    def _test_metrics(arm_name: str) -> dict:
        try:
            return (report.get("results", {})
                    .get(arm_name, {})
                    .get("regions", {})
                    .get("test", {}))
        except AttributeError:
            return {}

    cse_t = _test_metrics("cse_v1")
    clean_t = _test_metrics("cse_v1_cleanprior")
    prev_t = _test_metrics("baseline_prev_candle")

    def _wilson_lower(acc, n, z=1.96):
        if not n:
            return 0.0
        p = acc
        denom = 1 + z * z / n
        centre = p + z * z / (2 * n)
        adj = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
        return (centre - adj) / denom

    n_test = clean_t.get("graded") or 0
    acc_clean = clean_t.get("accuracy") or 0.0
    acc_prev = prev_t.get("accuracy") or 0.0
    wilson_lo = _wilson_lower(acc_clean, n_test)
    cov_clean = clean_t.get("coverage") or 0.0
    brier_clean = clean_t.get("brier_score")
    brier_prev = prev_t.get("brier_score")
    checks = {
        "wilson_lower_gt_baseline": wilson_lo > acc_prev,
        "coverage_ge_0.5": cov_clean >= 0.5,
        "brier_le_baseline": (brier_clean is not None
                              and brier_prev is not None
                              and brier_clean <= brier_prev),
    }
    passed = all(checks.values())

    verdict = {
        "phase": "K",
        "candidate": os.path.relpath(cand_path, _REPO),
        "acceptance_rule": ACCEPTANCE_RULE,
        "metrics_test_region": {
            "n_test": n_test,
            "cleanprior_accuracy": acc_clean,
            "cleanprior_wilson_lower": round(wilson_lo, 6),
            "cleanprior_coverage": cov_clean,
            "cleanprior_brier": brier_clean,
            "production_prior_accuracy": cse_t.get("accuracy"),
            "production_prior_brier": cse_t.get("brier_score"),
            "prev_candle_baseline_accuracy": acc_prev,
            "prev_candle_baseline_brier": brier_prev,
        },
        "checks": checks,
        "passed": passed,
        "decision": ("REGISTERED (entered model_validation.json)"
                     if passed else
                     "REJECTED — production weights NOT replaced "
                     "(byte-identical); candidate recorded as a "
                     "provenance-complete RESEARCH artifact"),
        "b5_bound_statement": (
            "A provenance-complete retrained equivalent of the global "
            "prior now exists with full lineage. Its honest measured "
            f"performance on the pre-committed test region is "
            f"{acc_clean:.4f} (production prior: "
            f"{cse_t.get('accuracy')}) "
            "— the unverifiable lineage of the OLD weights can no longer "
            "inflate any claim, because the clean equivalent is measured "
            "under the identical protocol."),
    }

    # ── 5. registry (ONLY on pass; honest either way) ──────────────────
    registry_touched = False
    if passed:
        reg_path = os.path.join(_REPO, "core", "model_validation.json")
        reg = {"registry_version": "1.0", "approved": [{
            "model_id": "cse_v1_cleanprior",
            "artifact_sha256": artifact["weights_sha256"],
            "asset_domain": "research_crypto_1m",
            "feature_schema_version": "next_candle_v1_34f",
            "oos_evidence": {
                "method": "frozen chronological split (warmup600/"
                          "50-25-25/embargo1), pooled test region, "
                          "wilson-lower > prev-candle baseline",
                "balanced_accuracy": round(acc_clean, 6),
                "n_predictions": int(n_test),
                "report_path": os.path.relpath(cand_path, _REPO),
            },
        }]}
        with open(reg_path, "w") as f:
            json.dump(reg, f, indent=1)
        registry_touched = True
        print("[phase-k] ACCEPTED — registry entry written "
              f"(core/model_validation.json)")
    else:
        print("[phase-k] REJECTED (pre-committed rule) — registry "
              "untouched, production weights untouched")

    report_path = os.path.join(
        args.out_dir, f"phase_k_retrain_{stamp}.json")
    with open(report_path, "w") as f:
        json.dump({"retrain": verdict,
                   "evaluation_report": report,
                   "registry_touched": registry_touched}, f, indent=1,
                  default=str)
    print(f"[phase-k] report -> {report_path}")
    print(json.dumps(verdict["metrics_test_region"], indent=1))
    print(f"[phase-k] DECISION: {verdict['decision']}")


if __name__ == "__main__":
    main()
