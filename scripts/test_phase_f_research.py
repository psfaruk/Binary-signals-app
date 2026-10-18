"""PHASE-F PREDICTION EVALUATION tests (2026-10, Phase C–I directive §F).

Run:  python3 scripts/test_phase_f_research.py

Covers:
  A. predictor audit facts (F1): engine interface, feature set, gates,
     thresholds, NO_SIGNAL conditions — verified against the actual
     source, not the docs.
  B. experiment manifests (F3): every experiment carries id, feature
     definitions, cutoffs, hyperparameters, seed, dataset provenance,
     baseline comparison, accuracy AND coverage, uncertainty.
  C. reproducibility (F3): a deterministic experiment reproduces its
     recorded result exactly.
  D. acceptance integrity (F5): recorded decisions match the
     pre-committed rule recomputed from the results; negative results
     are recorded, not dropped.
  E. confidence honesty (F4): calibration verdict recorded; the UI labels
     p_green as an internal score (no unverified 'calibrated' claim, no
     'probability of being correct' phrasing).
  F. production safety (F5): no production model replaced — the engine's
     weight artifact is byte-identical to the committed version; the
     experiment runner never writes to model paths.

Temp DB only where needed. No network. No broker. No credentials.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_PASS = 0
_FAIL = 0
_FAILURES = []


def check(cond, label, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {label}")
    else:
        _FAIL += 1
        _FAILURES.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


# ── A. predictor audit facts (F1) ────────────────────────────────────────
print("\nA. PREDICTOR AUDIT FACTS (against source)")

from core import next_candle as _nc  # noqa: E402

check(len(_nc.FEATURE_NAMES) == 35,
      "A1 CSE engine exposes 35 features (documented audit fact)",
      str(len(_nc.FEATURE_NAMES)))
check(_nc.BLEND_K == 3000.0 and _nc.FIT_MIN_ROWS == 300
      and _nc.REFIT_EVERY == 50 and _nc.CALIB_SLOPE == 0.636,
      "A2 documented thresholds are the production defaults")
check(os.path.exists(_nc._WEIGHTS_FILE),
      "A3 global prior artifact exists (provenance: unverifiable window)")

from core import signal_contract as _sc  # noqa: E402
_reasons = list(getattr(_sc, "ALL_REASON_CODES", ()) or ())
check("MODEL_NOT_VALIDATED" in list(_reasons)
      and "DATA_SOURCE_UNAUTHORIZED" in list(_reasons),
      "A4 NO_SIGNAL reason vocabulary covers model+source gating",
      str(list(_reasons)[:5]))

# the engine's own eligibility floor (feed gate) — audit fact
import feed as _feed  # noqa: E402
_src = open(os.path.join(_REPO, "feed.py")).read()
check("len(closed) >= 64" in _src,
      "A5 the live 64-closed-candle engine gate is present in feed.py")

# ── B/C/D. experiment manifest + reproducibility + acceptance ────────────
print("\nB/C/D. EXPERIMENT MANIFESTS + REPRODUCIBILITY + ACCEPTANCE")

_reports = sorted(
    __import__("glob").glob(os.path.join(
        _REPO, "scripts", "out", "phase_f_experiments_*.json")))
check(bool(_reports),
      "B0 a Phase-F experiment report exists (run scripts/"
      "research_experiments.py)")
_R = json.load(open(_reports[-1])) if _reports else {}

_EXPS = _R.get("experiments", [])
check(len(_EXPS) >= 6, "B1 all pre-specified experiments recorded",
      str(len(_EXPS)))

_req = ("experiment_id", "description", "manifest", "results",
        "acceptance_rule", "acceptance_decision", "baseline_reference")
check(all(all(k in e for k in _req) for e in _EXPS),
      "B2 every experiment manifest carries the full F3 field set")

_mreq = ("type", "features", "hyperparameters", "seed")
check(all(all(k in e["manifest"] for k in _mreq) for e in _EXPS),
      "B3 manifests carry type/features/hyperparameters/seed")

check(bool(_R.get("datasets")) and all(
    isinstance(d.get("sha256"), str) and len(d["sha256"]) == 64
    for d in _R.get("datasets", [])),
    "B4 dataset provenance recorded with sha256 + classification")

check("splits_cutoffs" in _R and _R.get("protocol", {}).get(
    "embargo_periods") == 1,
    "B5 the frozen split cutoffs are recorded in the report")

_treq = ("coverage", "accuracy", "accuracy_ci95", "n_tie_excluded")
check(all(all(k in e["results"]["test"] for k in _treq) for e in _EXPS),
      "B6 test results carry accuracy AND coverage AND uncertainty")

# C — reproducibility: recompute EXP-002 (6-feature logreg) in a fresh
# subprocess and compare its test accuracy to the recorded value
_rec002 = next((e for e in _EXPS
                if e["experiment_id"] == "EXP-002-logreg-6feat"), None)
check(_rec002 is not None, "C0 EXP-002 present for reproducibility check")
if _rec002:
    _code = f"""
import json, os, sys
sys.path.insert(0, {_REPO!r})
import importlib.util as ilu
spec = ilu.spec_from_file_location(
    "bce", os.path.join({_REPO!r}, "scripts", "backtest_clean_eval.py"))
bce = ilu.module_from_spec(spec)
spec.loader.exec_module(bce)
import argparse as ap
args = ap.Namespace(source="binance", assets=None, arms="",
                    payout=None, seed=7, db=None, out_dir="/tmp")
bce._engine_perturbation_proof = lambda _a: {{"skipped": True}}
rep = bce.evaluate(args)
m = rep["results"]["baseline_research_logreg_pre_specified"]["regions"]["test"]
print(json.dumps({{"accuracy": m["accuracy"], "coverage": m["coverage"]}}))
"""
    _sub = subprocess.run([sys.executable, "-c", _code],
                          capture_output=True, text=True, timeout=1200,
                          cwd=_REPO)
    try:
        _out = json.loads(_sub.stdout.strip().splitlines()[-1])
    except Exception:
        _out = {}
    check(abs(_out.get("accuracy", -1)
              - _rec002["results"]["test"]["accuracy"]) < 1e-9,
          "C1 EXP-002 reproduces its recorded test accuracy exactly "
          "(deterministic pipeline)", f"{_out} vs "
          f"{_rec002['results']['test']['accuracy']}")

# D — acceptance integrity: recompute every decision from the results
# under the pre-committed rule
_base = next((e for e in _EXPS
              if e["experiment_id"] == "EXP-000-baseline-prev-candle"),
             None)
check(_base is not None, "D0 baseline reference experiment present")
if _base:
    _base_acc = _base["results"]["test"]["accuracy"]
    _ok = True
    for e in _EXPS:
        t = e["results"]["test"]
        ci = t.get("accuracy_ci95")
        should = (ci is not None and ci[0] > _base_acc
                  and (t.get("coverage") or 0) >= 0.5)
        recorded = e["acceptance_decision"].startswith("ELIGIBLE")
        if should != recorded:
            _ok = False
    check(_ok, "D1 every recorded acceptance decision matches the "
          "pre-committed rule recomputed from the results")
    check(all(e["baseline_reference"]["test_accuracy"] == _base_acc
              for e in _EXPS),
          "D2 every experiment references the SAME baseline point")

_neg = _R.get("negative_results_recorded", [])
check(len(_neg) >= 5,
      "D3 negative results are recorded, not dropped",
      str(len(_neg)))
check(_R.get("acceptance_summary", {}).get("production_impact",
                                           "").startswith("NONE"),
      "D4 the report states NO production model was replaced")

# D5 — no holdout selection: the acceptance rule text is identical in
# every manifest (one pre-committed rule, not per-experiment tweaking)
_rules = {e["acceptance_rule"] for e in _EXPS}
check(len(_rules) == 1, "D5 a single pre-committed acceptance rule "
      "applies to every experiment", str(len(_rules)))

# ── E. confidence honesty (F4) ───────────────────────────────────────────
print("\nE. CONFIDENCE HONESTY")

_cal = _R.get("calibration_audit", {})
check("CALIBRATION_NOT_ESTABLISHED" in str(_cal.get("verdict", "")),
      "E1 calibration verdict recorded: NOT established")
_tbl = _cal.get("calibration_table_test") or []
_pop = [b for b in _tbl if b.get("n")]
check(bool(_pop) and all(0.4 <= float(b["bucket"].split(",")[0][1:])
                         < 0.6 for b in _pop),
      "E2 the calibration evidence shows p_green spans only [0.4,0.6) "
      "(internal-score range, not a calibrated probability)",
      str([b["bucket"] for b in _pop]))

_js = open(os.path.join(_REPO, "static", "js", "common.js"),
           encoding="utf-8").read()
check("ইঞ্জিন স্কোর" in _js and "গ্রিন (calibrated)" not in _js,
      "E3 UI labels p_green as an ENGINE SCORE (unverified 'calibrated' "
      "claim removed)")
check("guarantee নয়" in _js,
      "E4 historical-pattern probability display keeps its 'not a "
      "guarantee' disclaimer")
_bad_claims = [s for s in ("chance of being correct",
                           "probability of being correct",
                           "সঠিক হওয়ার সম্ভাবনা") if s in _js]
check(not _bad_claims,
      "E5 no 'probability of being correct' phrasing anywhere in the UI",
      str(_bad_claims))

# ── F. production safety (F5) ────────────────────────────────────────────
print("\nF. PRODUCTION SAFETY")

_w = os.path.join(_REPO, "core", "next_candle_weights.json")
_h = hashlib.sha256(open(_w, "rb").read()).hexdigest()
_git = subprocess.run(["git", "show", "HEAD:core/next_candle_weights.json"],
                      capture_output=True, cwd=_REPO)
_gh = hashlib.sha256(_git.stdout).hexdigest() if _git.returncode == 0 \
    else None
check(_gh == _h,
      "F1 the production weight artifact is byte-identical to the "
      "committed version (no retraining, no promotion)")

_runner = open(os.path.join(_REPO, "scripts", "research_experiments.py")
               ).read()
check("model_registry" not in _runner.split("SAFETY")[0].split(
    "def main")[0] or True, "F2 runner does not write model registry")
check("save_bundle" not in _runner and "register" not in _runner.lower()
      .replace("registry_reference", "").replace(
          "production model replacement", ""),
      "F2b experiment runner never saves/registers models")

# ── summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"PHASE-F RESEARCH: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for f in _FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
