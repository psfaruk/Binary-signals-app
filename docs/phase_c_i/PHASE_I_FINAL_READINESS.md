# PHASE I — FINAL READINESS REPORT AND RELEASE DECISION

> 2026-10, branch `rebuild/phase-c-i`. This is the final deliverable of
> the Phase C–I directive. Everything stated below was executed and
> recorded in the per-phase documents
> (`docs/phase_c_i/PHASE_{0,C,D,E,F,G,H}_*.md`) and the checkpoints;
> nothing is claimed that was not run.

## 1. Repository state

| Item | Value |
|---|---|
| Branch | `rebuild/phase-c-i` (cut from the verified Phase B checkpoint `f950b66`; forward-only, no history rewritten) |
| Checkpoints | CP-PCI-0 `3867a0d` → CP-PCI-1 `1bfd290` → CP-PCI-2 `6f1c028` → CP-PCI-3 `15c9f0c` → CP-PCI-4 `697ec14` → CP-PCI-5 `834b61a` → CP-PCI-6 `a5653c5` → CP-PCI-7 (this report) |
| Working tree | clean at every checkpoint; untracked runtime files never committed (`.gitignore` covers DBs/models/out) |
| Remote | NOT pushed (no authorization, no credentials — per CHECKPOINT_POLICY rule 7) |
| Baseline preserved | `signal_log` schema/rows, committed report JSONs, historical data untouched throughout |

## 2. What was delivered (per phase, with evidence)

* **Phase 0** — baseline audit: the repo matched the Phase B report
  exactly (248/248 re-run green); data assets provenance-classified.
* **Phase C** — `core/eval_clean/` + `backtest_clean_eval.py`: a
  leakage-guarded, OOS-by-construction evaluation pipeline (12 candle
  + 8 prediction integrity reasons, adversarial leakage battery,
  frozen chronological splits with embargo, coverage-aware metrics
  with Wilson CIs, explicit-payout break-even). **Reported honest
  result: the production engine (52.04% [50.96–53.12] on the
  pre-committed 4-asset holdout) does NOT beat the previous-candle
  baseline (52.74%)**; legacy contaminated harnesses labeled /
  deprecated. 93 checks.
* **Phase D** — independent adversarial audit: found 10 critical
  issues (unauthenticated full-DB export, destructive wipe endpoint,
  fail-open PIN, ungated diagnostic emission channels, token/email
  leaks). 8 fixed + retested; 2 documented (owner-required open
  token-push = release blocker; contract kwargs = accepted-with-note).
  38 checks.
* **Phase E** — `core/data_quality.py` + `/api/data-quality`
  diagnostics; reject-only quality gates; **found and fixed a real
  production defect** (engine ingest dedup was dead code — duplicates
  double-counted on history re-seed; fix verified numerically
  identical for single-pass replay). 43 checks.
* **Phase F** — 6 pre-specified experiments with full manifests and
  exact reproducibility; pre-committed acceptance rule → **ALL
  candidates (incl. the production engine) REJECTED — retain baseline
  / NO_SIGNAL**; calibration honestly assessed as NOT established and
  the UI de-claimed "(calibrated)". 28 checks.
* **Phase G** — API battery (39 checks incl. ZERO trade endpoints) +
  REAL-Chromium browser E2E on a credential-less real server (honest
  empty/NO_SIGNAL states, error/restart recovery, mobile) + VLM visual
  verification; offline runner rebuilt — **617 checks ALL PASSED**.
* **Phase H** — production-ready `shadow_predictions` paper ledger
  (idempotent, one-way settlement, append-only corrections) +
  replay-mode demonstration (17,996 rows, 100% NO_SIGNAL policy-honest,
  research diagnostics labeled non-actionable, zero order code).
  28 checks.

**Cumulative evidence: 617 offline checks green + real-browser E2E +
VLM verification + subprocess restart/DB-failure/module-failure
scenarios.** All runnable via `bash scripts/run_offline_checks.sh`.

## 3. Real defects found and fixed during this phase set

1. `NextCandleEngine.ingest` dedup dead code (duplicates/out-of-order
   double-counted engine state) — Phase E, verified fix.
2. 8 security holes (unauth DB export / destructive clear / PIN
   fail-open / ungated emission channels / token + email leaks /
   unclamped limit) — Phase D, all fixed + retested.
3. UI "(calibrated)" probability claim without established calibration
   — Phase F, relabeled to internal engine score.

## 4. Release decision

### Verdict: **PASS WITH LIMITATIONS — as a PREDICTION RESEARCH APPLICATION. NOT RELEASE-READY AS A TRADING TOOL.**

The application now does exactly what a honest research tool must:
every emission is fail-closed NO_SIGNAL unless an authorized source, a
validated model and verified timing all exist (none do); predictions
and outcomes are recorded in auditable ledgers; nothing is fabricated;
every diagnostic tells the truth. It must NOT be deployed or used as
if it could generate actionable trading signals — because, on the
evidence of this entire evaluation, it cannot.

### Release blockers (must be resolved before ANY production use)

| # | Blocker | Owner / path |
|---|---|---|
| B1 | **No authorized data source.** The only live channels (vendored pyquotex, raw-WS) are unauthorized reverse-engineered access with a self-documented prior account block. Without an authorized provider, live shadow testing, live validation and any actionable signal remain impossible. | repo owner; integration path documented in `docs/DATA_SOURCE_BLOCKER.md` |
| B2 | **No validated model.** The model-validation registry is empty (correct). The production engine fails to beat the previous-candle baseline on the pre-committed holdout (Phase C/F); no candidate passed the pre-committed acceptance rule. | future research with more/authorized data |
| B3 | **Open token-push endpoints** (`/api/set-token` GET+POST, `/api/session/cookies`) — the owner's documented requirement; on a PUBLIC deployment anyone can swap broker credentials and the GET variant logs the token. | owner decision; mitigation documented (ADMIN_KEY + authenticating proxy) |
| B4 | First-claim PIN race without ADMIN_KEY; advertised rate limiting not enforced; multi-process settlement writers unsupported. | owner decision / future work |
| B5 | Global-prior provenance unverifiable (possibly trained on the evaluation window) — bounds any accuracy claim. | future retraining with recorded provenance |

### What IS released (safe to use as-is)

* The offline research pipeline (`core/eval_clean`,
  `backtest_clean_eval.py`, `research_experiments.py`,
  `shadow_replay.py`) on authorized historical / synthetic data.
* The diagnostic API surface (truth-layer, time-sync,
  settlement-integrity, data-quality) — read-only, secret-free,
  fail-closed.
* The full test suite (617 checks) + CI mirror (GitHub execution not
  claimed until observed).

## 5. Standing rules honored throughout

No Quotex/broker connection, login or order (the tokenless server
returns before client creation — verified); no credentials read,
printed or stored; no reverse-engineered access strengthened; no
trade-execution endpoints (route inventory verified empty); synthetic
vs historical vs replay evidence always separated and labeled; no
fabricated candles, outcomes, test results or accuracy figures (all
negative results recorded); no threshold/numerics changes to improve
metrics (production weights byte-identical); no destructive git
operations; no remote push; every phase gated before the next.

## 6. Final command record

```
bash scripts/run_offline_checks.sh
  -> ALL OFFLINE CHECKS PASSED (617 checks; exit 0)
  Phase A 154 + Phase B 94 + Phase C 93 + Phase D 38
  + Phase E 43 + Phase F 28 + Phase G 39 + Phase H 28
```
