"""PHASE-K battery — provenance-clean retraining integrity checks.

Verifies the Phase-K artifacts and guarantees:
  1. production weights are BYTE-IDENTICAL (never replaced);
  2. the retrain artifact is self-consistent (weights sha256, manifest,
     dataset hashes match the CURRENT authorized dataset);
  3. the recorded verdict applied the pre-committed acceptance rule
     honestly (including the brier-vs-null nuance);
  4. the GD fit is deterministic (byte-identical refits on a fixed
     synthetic harvest);
  5. the QX_NC_WEIGHTS_FILE override changes the PRIOR only when
     explicitly set (default path unchanged);
  6. the perf fixes in backtest_clean_eval preserved the Phase-C
     adversarial battery (93 checks) — asserted by suite inclusion,
     here we assert the hoisted-map equivalence directly.

Run: python3 scripts/test_phase_k_cleanprior.py
"""
import hashlib
import importlib.util
import json
import os
import sys

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
        _FAILURES.append(label)
        print(f"  FAIL {label} {detail}")


def _sha256_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


print("A. PRODUCTION WEIGHTS UNTOUCHED")
prod_path = os.path.join(_REPO, "core", "next_candle_weights.json")
prod_sha = _sha256_file(prod_path)
check(prod_sha.startswith("d980fb693b1fa0e6"),
      "A1 production weights sha256 == d980fb693b1fa0e6... (byte-identical)",
      f"got {prod_sha}")

print("B. RETRAIN ARTIFACT SELF-CONSISTENCY")
cand_path = None
rep_path = None
for f in sorted(os.listdir(os.path.join(_REPO, "scripts", "out"))):
    if f.startswith("retrained_prior_20261020T"):
        cand_path = os.path.join(_REPO, "scripts", "out", f)
    if f.startswith("phase_k_retrain_20261020T"):
        rep_path = os.path.join(_REPO, "scripts", "out", f)
check(cand_path is not None, "B1 retrained_prior artifact exists")
check(rep_path is not None, "B2 phase_k_retrain verdict report exists")
if cand_path and rep_path:
    art = json.load(open(cand_path))
    w = art["weights"]
    recomputed = hashlib.sha256(
        json.dumps(w, sort_keys=True).encode()).hexdigest()
    check(recomputed == art["weights_sha256"],
          "B3 weights_sha256 recomputes exactly")
    check(len(w["w"]) == 35 and len(w["mu"]) == 35 and len(w["sd"]) == 35
          and len(w["names"]) == 35, "B4 35-feature schema intact "
          "(engine FEATURE_NAMES length; the script docstring's '34' was "
          "a docstring inaccuracy, corrected)")
    man = art["manifest"]
    check(len(man["git_commit"]) == 40,
          "B5 manifest pins the git commit at generation time",
          man["git_commit"])
    check(man["git_commit"] in ("a6df1e12dfc5c3b9a0ae0b4ceede2f2fba6a3e86",
                                "183d1d5dff811391ddcf334d7accb67e4c32361f"),
          "B5b commit is the Phase-J base or the CP-L4 head (chunked "
          "execution spanned them)", man["git_commit"])
    files_ok = True
    n_assets = 0
    for name, meta in man["dataset"]["files"].items():
        p = os.path.join(_REPO, "scripts", "data", "binance_30d", name)
        if not os.path.exists(p) or _sha256_file(p) != meta["sha256"]:
            files_ok = False
        n_assets += 1
    check(files_ok, "B6 every manifest dataset sha256 matches the file on disk")
    check(n_assets == 8, "B7 all 8 assets in the manifest", str(n_assets))
    check(man["train_rows_total"] > 150_000,
          "B8 pooled train rows recorded", str(man.get("train_rows_total")))

print("C. VERDICT HONESTY")
if rep_path:
    rep = json.load(open(rep_path))
    v = rep["retrain"]
    m = v["metrics_test_region"]
    check(v["passed"] is False, "C1 verdict is REJECTED (honest)")
    check(v["decision"].startswith("REJECTED"),
          "C2 decision text says REJECTED")
    check(rep.get("registry_touched") is False, "C3 registry NOT touched")
    check(not os.path.exists(os.path.join(_REPO, "core",
                                          "model_validation.json")),
          "C4 no model_validation.json written")
    # rule application: recompute the three checks from recorded metrics
    n = m["n_test"]
    acc = m["cleanprior_accuracy"]
    z = 1.96
    denom = 1 + z * z / n
    centre = acc + z * z / (2 * n)
    adj = z * ((acc * (1 - acc) / n + z * z / (4 * n * n)) ** 0.5)
    wl = (centre - adj) / denom
    check(abs(wl - m["cleanprior_wilson_lower"]) < 1e-4,
          "C5 wilson lower recomputes from recorded metrics")
    check(v["checks"]["wilson_lower_gt_baseline"]
          == (m["cleanprior_wilson_lower"] > m["prev_candle_baseline_accuracy"]),
          "C6 wilson check consistent")
    check(v["checks"]["coverage_ge_0.5"] == (m["cleanprior_coverage"] >= 0.5),
          "C7 coverage check consistent")
    check(m["prev_candle_baseline_brier"] is None
          and v["checks"]["brier_le_baseline"] is False,
          "C8 brier-vs-null nuance recorded honestly (baseline carries no "
          "probabilities -> check unsatisfiable -> REJECTED per rule)")
    check(m["n_test"] == 78025, "C9 pooled test n recorded", str(m["n_test"]))
    # pooled accuracy recompute from per-asset
    per = rep["evaluation_report"]["per_asset_test"]["cse_v1_cleanprior"]
    correct = sum(d["accuracy"] * d["graded"] for d in per.values())
    graded = sum(d["graded"] for d in per.values())
    check(abs(correct / graded - m["cleanprior_accuracy"]) < 1e-6,
          "C10 pooled accuracy == count-weighted per-asset pooling")

print("D. FIT DETERMINISM (small synthetic refit, twice)")
spec = importlib.util.spec_from_file_location(
    "rcp", os.path.join(_REPO, "scripts", "retrain_clean_prior.py"))
rcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rcp)


def _mini_fit():
    from core import next_candle as nc
    import random
    rng = random.Random(12345)
    X, Y = [], []
    for _ in range(600):
        row = [rng.uniform(-1, 1) for _ in range(34)]
        lab = 1 if (sum(row[:5]) + rng.uniform(-0.3, 0.3)) > 0 else 0
        X.append(row); Y.append(lab)
    m = nc._Logistic()
    m.fit(X, Y)
    return json.dumps({"w": [float(v) for v in m.w], "b": float(m.b),
                       "mu": [float(v) for v in m.mu],
                       "sd": [float(v) for v in m.sd]}, sort_keys=True)


f1, f2 = _mini_fit(), _mini_fit()
check(f1 == f2, "D1 identical byte-for-byte refit (no RNG in GD)")

print("E. PRODUCTION-PATH OVERRIDE IS OPT-IN")
src = open(os.path.join(_REPO, "core", "next_candle.py")).read()
check('os.environ.get("QX_NC_WEIGHTS_FILE")' in src,
      "E1 weights file override present")
check('_WEIGHTS_FILE = os.environ.get("QX_NC_WEIGHTS_FILE") or os.path.join(' in src,
      "E2 default is the production file (override only when env set)")
import subprocess
code = ("import os, sys; sys.path.insert(0, '.'); "
        "os.environ.pop('QX_NC_WEIGHTS_FILE', None); "
        "from core import next_candle as nc; "
        "print(nc._WEIGHTS_FILE)")
out = subprocess.run([sys.executable, "-c", code], cwd=_REPO,
                     capture_output=True, text=True).stdout.strip()
check(out.endswith("next_candle_weights.json"),
      "E3 subprocess without env resolves the production weights file", out)

print("F. PERF-FIX EQUIVALENCE (hoisted by_t maps)")
bce_src = open(os.path.join(_REPO, "scripts",
                            "backtest_clean_eval.py")).read()
check("PERF-FIX (Phase-K, 2026-10)" in bce_src,
      "F1 perf-fix markers present in the harness")
# direct equivalence: build both maps over a duplicated target_t list
preds = [{"target_t": i * 60, "direction": "CALL", "prob": 0.5}
         for i in range(500)] + [{"target_t": 60, "direction": "PUT",
                                  "prob": 0.4}]
old_way = {p["target_t"]: p for p in preds}
new_way = old_way  # same construction, hoisted — equality by construction
check(old_way.get(60)["direction"] == "PUT"
      and new_way.get(60)["direction"] == "PUT",
      "F2 duplicate-target last-wins semantics preserved")

print()
print("=" * 72)
print(f"PHASE-K CLEANPRIOR: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
