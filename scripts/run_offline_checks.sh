#!/usr/bin/env bash
# OFFLINE CHECK RUNNER (rebuilt 2026-10, Phase C–I directive §G)
#
# One command reproduces EVERY offline check in this repository, in
# dependency order, with truthful exit propagation (any failure fails
# the whole run). Temp DBs only; no network; no broker; no credentials;
# no model writes; no push.
#
#   bash scripts/run_offline_checks.sh
#
# The GitHub workflow (.github/workflows/ci.yml) mirrors this runner;
# GitHub execution is NOT claimed as passing unless a real run is
# observed (local-only until then).
set -u
cd "$(dirname "$0")/.."

FAIL=0
step() {
  echo ""
  echo "=================================================================="
  echo "  $1"
  echo "=================================================================="
}
run() {
  # run <label> <command...>
  local label="$1"; shift
  step "$label"
  "$@"
  local rc=$?
  if [ $rc -ne 0 ]; then
    echo "!!! FAILED (exit $rc): $label"
    FAIL=1
  fi
  return $rc
}

# 0. syntax gates (read-only)
run "SYNTAX GATE — python (ast.parse over changed/new modules)" \
  python3 - <<'PYEOF'
import ast, sys
files = [
    "core/eval_clean/__init__.py", "core/eval_clean/integrity.py",
    "core/eval_clean/leakage.py", "core/eval_clean/splits.py",
    "core/eval_clean/baselines.py", "core/eval_clean/metrics.py",
    "core/eval_clean/breakeven.py", "core/data_quality.py",
    "scripts/backtest_clean_eval.py", "scripts/research_experiments.py",
    "scripts/test_phase_c_eval.py", "scripts/test_phase_d_audit.py",
    "scripts/test_phase_e_data_quality.py",
    "scripts/test_phase_f_research.py", "scripts/test_phase_g_api.py",
]
for f in files:
    ast.parse(open(f).read(), filename=f)
print(f"ast.parse OK over {len(files)} files")
PYEOF
run "SYNTAX GATE — changed JS" node --check static/js/common.js
run "SYNTAX GATE — signal_states.js" node --check static/js/signal_states.js

# 1. Phase A (Truth Layer)
run "PHASE 1 — truth layer (70)" python3 scripts/test_phase1_truth_layer.py
run "PHASE 1 — signal states JS (59)" node scripts/test_phase1_signal_states.cjs
run "PHASE 1 — HTTP (25)" python3 scripts/test_phase1_http.py

# 2. Phase B (settlement lifecycle + time provider)
run "PHASE 2A — settlement (54)" python3 scripts/test_phase2_settlement.py
run "PHASE 2A — time provider (40)" python3 scripts/test_phase2_time_provider.py

# 3. Phase C (clean evaluation pipeline)
run "PHASE C — clean eval adversarial (93)" python3 scripts/test_phase_c_eval.py

# 4. Phase D (independent audit battery)
run "PHASE D — audit battery (38)" python3 scripts/test_phase_d_audit.py

# 5. Phase E (data quality)
run "PHASE E — data quality (43)" python3 scripts/test_phase_e_data_quality.py

# 6. Phase F (research experiments): regenerate the manifest (needed by
# the test on fresh checkouts — scripts/out is gitignored), then test.
run "PHASE F — research experiments runner" python3 scripts/research_experiments.py
run "PHASE F — research checks (28)" python3 scripts/test_phase_f_research.py

# 7. Phase G (API battery)
run "PHASE G — API battery (39)" python3 scripts/test_phase_g_api.py

# 8. Phase H (shadow/paper via replay)
run "PHASE H — shadow replay runner (2 assets)" python3 scripts/shadow_replay.py --assets BTCUSDT,ETHUSDT
run "PHASE H — shadow ledger checks (28)" python3 scripts/test_phase_h_shadow.py

echo ""
echo "=================================================================="
if [ $FAIL -eq 0 ]; then
  echo "  ALL OFFLINE CHECKS PASSED"
  echo "  (Phase A 154 + Phase B 94 + Phase C 93 + Phase D 38 +"
  echo "   Phase E 43 + Phase F 28 + Phase G 39 + Phase H 28 = 617 checks)"
else
  echo "  OFFLINE CHECKS FAILED — see the FAILED step above"
fi
echo "=================================================================="
exit $FAIL
