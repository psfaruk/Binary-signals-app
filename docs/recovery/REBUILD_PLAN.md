# REBUILD PLAN — Phase 1 / Phase 2A / Phase 2B / Independent Audit

> Planning document only. Nothing described here has been implemented yet.
> Execution requires EXPLICIT USER APPROVAL of this plan (or an amended
> version of it). The previous Phase 1 + Phase 2 implementation was lost
> (see `RECOVERY_STATUS.md` §2) — this plan rebuilds it from evidence:
> one surviving source file, the worklog narrative, and the Phase 0 audit.
>
> CORE PRINCIPLE: **the lost implementation cannot be recreated byte-for-byte,
> and no prior "green" test result may be cited as current evidence.** Every
> rebuilt behavior must be tested from scratch. The worklog's claimed test
> counts (42/31/22/18/19/16/43/22) are design TARGETS, not achievements.

Governing rule (unchanged from Phase 1/2 directives):
**NO AUTHORIZED DATA SOURCE + NO VALIDATED MODEL + NO VERIFIED TIMING
= NO ACTIONABLE DIRECTIONAL SIGNAL.** The rebuild makes the app honest about
this; it does NOT make it profitable.

---

## Global rebuild principles

1. Baseline line numbers from Phase 0 (feed.py:2958–2969 etc.) may be relied
   on ONLY after re-location by content search — the tree is the pristine
   baseline, so Phase 0 numbers are valid again, but every edit re-verifies.
2. Fail-closed on every exception; every gate default is the safe side.
3. Additive-only schema migrations; existing rows are never rewritten;
   `signal_log` and committed JSON reports are immutable history.
4. All tests run on temporary databases; no production-DB mutation; no
   credentials; no broker contact; no network.
5. Each phase ends with a local checkpoint commit (see
   `CHECKPOINT_POLICY.md`) and a worklog entry, then STOPS for user review.
6. Phase 0 root causes being addressed: RC-01..RC-04 (Phase 1 gate/contract),
   RC-06 (settlement), RC-05 (timing), RC-08 (backtest contamination),
   RC-07/RC-09..RC-12 partially via tests/CI/docs.

---

## Phase A — Phase 1 Truth Layer rebuild

Objective: every prediction leaving the emission boundary passes a canonical
eligibility contract; failures become NO_SIGNAL + stable reason code; engine
diagnostics survive as research-only fields; nothing actionable is possible
today (unauthorized source, no validated model, unverified timing).

### A.1 Files expected to change

| File | Action |
|---|---|
| `core/signal_contract.py` | CREATE. Start from the verified 715-line snapshot (sha256 `5cdacf94…6840e`, preserved at `/home/z/my-project/recovery-artifacts/phase1_signal_contract_snapshot.py`), then RE-REVIEW line-by-line against the baseline imports it needs; adjust anything referencing baseline internals that no longer match. |
| `feed.py` | MODIFY. Integrate the contract at the emission boundary: wrap the end-of-candle prediction path (`_run_eoc` equivalent) AND every return path that currently bypasses gating — specifically the CSE early-exit (Phase 0: feed.py:2958–2969) and the `fade_default` fabricated conf-50 CALL/PUT fallback (Phase 0: feed.py:2754+). The fallback must become research-only or NO_SIGNAL. |
| `db.py` | MODIFY. Add `signal_provenance` table (additive migration, idempotent `CREATE TABLE IF NOT EXISTS` + column adds), research vs actionable row distinction, and ensure INSERT paths handle the new columns (Phase 2 lost work found `_SP_COLUMNS` dropped columns — re-derive). |
| `server.py` | MODIFY. Add `GET /api/truth-layer` (secret-free policy snapshot: policy version, authorization status, actionable_signals_possible, registry state, reason codes). Ensure REST and WS payloads carry the same truth fields (signal, truth_status, reason_code, research_* fields, UTC stamps). |
| `quotex_ws.py` | MODIFY. Route raw-WS backend emissions through the contract (research-only when unauthorized). |
| `core/retention.py` | MODIFY (if present at baseline). Retention must never delete provenance needed for settlement/correction audit. |
| `static/js/common.js` | MODIFY. Render truth states honestly: NO_SIGNAL states non-directional with reason; missing confidence renders "—" (never 50); remove/replace the fake countdown (Phase 0: common.js:3892) and the local-clock-labeled-as-Broker-UTC display (Phase 0: common.js:4893–4901). |
| `static/js/signal_states.js` | CREATE (or extend existing file if baseline has one). Pure mapping module: contract state → UI state, no lies. |
| `scripts/test_phase1_truth_layer.py` | CREATE. Target ≥42 deterministic tests (worklog count as target). |
| `scripts/test_phase1_signal_states.cjs` | CREATE. Target ≥31 node-based pure-mapping tests. |
| `docs/PHASE1_TRUTH_LAYER.md` | CREATE. Contract, reason codes, policy, registry format. |

### A.2 Dependencies / call sites that MUST be inspected first

- Every `return` in the feed prediction flow that produces a signal dict
  (search: prediction emission, CSE engine invocation, fade_default).
- `db.py` insert paths and column tuples (`_SP_COLUMNS`-style constants).
- `server.py` REST emitters + WS broadcast paths.
- `core/model_validation.json` registry format — MUST NOT exist with entries;
  an absent/empty registry is the required state (no fake entries, ever).
- Frontend: `common.js` countdown/confidence/render paths; the HTML templates
  that hard-code "Broker UTC" labels.

### A.3 Required tests (minimum)

1. Eligibility matrix: for each of the 13 reason codes (DATA_UNAVAILABLE,
   DATA_INVALID, DATA_STALE, INSUFFICIENT_HISTORY, FEATURE_ERROR,
   MODEL_UNAVAILABLE, MODEL_OUTPUT_INVALID, MODEL_NOT_VALIDATED,
   BELOW_ELIGIBILITY_THRESHOLD, TARGET_CANDLE_INVALID, TIMING_UNVERIFIED,
   INTERNAL_ERROR, DATA_SOURCE_UNAUTHORIZED) — a fixture that triggers it
   and asserts NO_SIGNAL + that exact code + research fields preserved.
2. Fail-closed: engine raising → INTERNAL_ERROR, never an actionable signal.
3. Env-bypass attempt: set every plausible env var → still NO_SIGNAL.
4. fade_default path: gated → zero `signal_log` actionable rows.
5. Provenance persistence: rows survive a fresh subprocess reading the same
   temp DB (restart safety).
6. REST/WS payload parity for truth fields.
7. JS: state mapping pure functions (node vm), incl. missing-confidence and
   stale demotion.

### A.4 Data-safety / migration constraints

- Additive schema only; no rewriting of existing `signal_log` rows.
- Tests use temp DBs only. No production DB touched.
- The snapshot file is imported only after its sha256 is re-verified to be
  `5cdacf94…6840e`.

### A.5 Acceptance criteria

- End-to-end (real feed code path, fake tick stream): unauthorized source →
  NO_SIGNAL / DATA_SOURCE_UNAUTHORIZED, research fields recorded.
- `GET /api/truth-layer` reports `actionable_signals_possible: false`.
- Grep-verifiable: no env var or config flips authorization or registry.
- All Phase 1 tests green; recorded commands + exit codes in worklog.
- Checkpoint `CP-P1-n` committed after review (documentation + tests may be
  separate checkpoints per policy rule 1).

### A.6 Stop conditions

- The contract cannot wrap an emission path without altering baseline
  numerics → STOP, document the exact conflict.
- Any migration would rewrite existing rows → STOP before running it.
- A correct registry implementation seems to require a fake entry → STOP
  (empty registry is the correct state).
- Baseline imports/structures diverge from snapshot assumptions in a way the
  snapshot cannot adapt to safely → STOP and report.

---

## Phase B — Phase 2A: settlement lifecycle + time provider

Objective: candles get unambiguous identity + an explicit lifecycle; late
ticks can never silently mutate finalized OHLC; corrections are auditable;
local time is never labeled as broker time; sync evidence expires on
reconnect; timing gate integrates with the Truth Layer.

### B.1 Files expected to change

| File | Action |
|---|---|
| `core/candle_lifecycle.py` | CREATE. Lifecycle states (OPEN → PENDING_FINALIZATION → FINALIZED → SETTLED or documented equivalent), candle identity rule (asset + timeframe + period-start, stable across restarts), duplicate-close/duplicate-tick detection, late-tick policy (accept into correction ledger only), correction records (original + corrected + reason + timestamp), research vs production ledger separation. |
| `core/time_provider.py` | CREATE. TimeProvider interface: local UTC, source time (when legitimately measurable), sync status (UNVERIFIED / SYNCED / STALE), last successful sync, estimated offset, offset uncertainty, staleness window, reconnect invalidation. LocalClockProvider (always local-only, honestly labeled). SourceTimeProvider over measured samples (raw-WS only) with fail-closed malformed/out-of-order/jump handling. Monotonic elapsed time via `time.monotonic()` so wall-clock steps cannot corrupt freshness math. |
| `feed.py` | MODIFY. Replace the Phase 0 late-tick post-scoring patch (feed.py:5195–5245) with lifecycle-honoring settlement; route `_srv_now`-style server-time reads through the provider; keep numeric behavior where safe. |
| `db.py` | MODIFY. Settlement/correction tables + lifecycle columns (additive, idempotent); explicit transactions around state transitions; INSERT-column parity (the lost `_SP_COLUMNS` lesson). |
| `server.py` | MODIFY. `GET /api/settlement-integrity` (lifecycle version, state counts, correction policy) and `GET /api/time-sync` (provider describe: backend, local_only, sync status, offset, uncertainty, staleness; explicit note that timing sync ≠ source authorization). |
| `quotex_ws.py` | MODIFY. Wire raw-WS server-time samples into SourceTimeProvider; reset sync evidence on reconnect. |
| `scripts/test_phase2_settlement.py` | CREATE. Target ≥22 tests. |
| `scripts/test_phase2_time_provider.py` | CREATE. Target ≥18 tests. |
| `docs/` | UPDATE lifecycle + timing docs. |

### B.2 Dependencies / call sites to inspect

- feed.py late-tick patch region + every place scored OHLC is written.
- db.py transaction helpers; check whether SQLite is in WAL mode; identify
  all writers (single process? multiple?) — do NOT assume an in-memory lock
  protects cross-process writers; document the single-process assumption and
  the multi-process risk honestly.
- `_srv_now` (raw-WS server time) and every consumer of it.
- Frontend countdown/broker-time consumers (Phase B only feeds them honest
  data; frontend fixes belong to Phase A/C as applicable).

### B.3 Required tests (minimum)

Settlement (the 10 directive cases): normal close; duplicate close events →
no duplicate settlement; duplicate ticks → idempotent; late tick BEFORE
finalization (legitimately incorporated) vs AFTER (correction ledger only,
OHLC immutable); settlement retry (idempotent); restart during pending
(subprocess test — state recovers); conflicting finalization (rejected,
auditable); history rows retained; research vs production ledger separation;
late-tick matched to the CORRECT candle (period-start bucketing, boundary
timestamps — a tick at exactly the minute edge belongs to the right candle).

Time provider: local-only honesty; SYNCED→STALE on age; reconnect
invalidation; malformed samples rejected (6+ kinds); out-of-order counting;
offset-jump flagging; uncertainty requires ≥3 samples; duplicate timestamps;
monotonic elapsed time immune to wall-clock steps; pyquotex-like provider →
gate stays False; integration: synced-but-unauthorized → still NO_SIGNAL
(DATA_SOURCE_UNAUTHORIZED), research preserved.

### B.4 Data-safety / migration constraints

- Corrections NEVER mutate finalized OHLC/outcome; they add rows.
- Additive migration; if a safe correction requires destructive migration →
  STOP (see B.6).
- Temp DBs only in tests.

### B.5 Acceptance criteria

- The late-flip scenario: emission → research settlement → late tick flips
  outcome → correction row exists, original row unchanged, production ledger
  untouched, restart preserves everything (subprocess verified).
- `/api/time-sync` honest for every backend (local_only for pyquotex path).
- Timezone-aware target-candle math: a boundary timestamp maps to exactly one
  target period (property/edge tests).
- All tests green; checkpoint `CP-P2A-n` committed.

### B.6 Stop conditions

- A safe settlement fix requires rewriting historical rows or a destructive
  migration → document and STOP before the change.
- Concurrency safety would require a new process architecture (advisory
  locks, queue, single-writer service) → document the gap and STOP.
- Provider cannot be wired without adding any undocumented broker-access
  mechanism → STOP (forbidden).

---

## Phase C — Phase 2B: backtest integrity + offline suite + CI + docs

Objective: no live selection path picks candidates on its final reported
data; cross-asset pooled leakage eliminated; contaminated legacy harnesses
labeled/deprecated; committed report JSONs overwrite-protected; a fully
offline reproducible suite + CI; honest README/status/blocker docs.

### C.1 Files expected to change

| File | Action |
|---|---|
| `core/eval_protocol.py` | CREATE. Wilson CI, binary stats, selection-split (first-half selection / second-half report), honest metrics (n, class distribution, excluded, accuracy + Wilson, balanced accuracy, per-class precision/recall, confusion, coverage + NO_SIGNAL rate, log loss ONLY for valid probabilities — None + note otherwise), baselines on the same eligible observations, chronological pooling, cross-asset embargo, evaluation provenance stamp. |
| `core/otc_predict/fast_train.py` | MODIFY. Selection-split in `fast_train_one`; fold-local preprocessing (scalers in-fold); pooled-global fallback → chronological pool + pooled cross-asset embargo; report gains selection provenance. LIVE production path — extra care. |
| `core/otc_predict/walk_forward.py` | MODIFY. Same embargo/selection fixes; legacy fallback labeled. |
| `scripts/train_otc_model.py` | MODIFY. Same selection-split + baselines + pooled embargo. |
| `scripts/backtest_fast_train_real.py` | LABEL: in-sample smoke banner; report marked in-sample. |
| `scripts/backtest_model_pipeline.py` | LABEL: S4 in-sample banner + integrity block in output JSON. |
| `scripts/backtest_persistence_replay.py` | DEPRECATE: guard exits non-zero with honest notice + explicit override flag. |
| `scripts/backtest_any_theory.py`, `scripts/backtest_otc_predictor.py` | PROTECT: never overwrite committed JSONs (write timestamped copies under `scripts/out/`); integrity blocks added. |
| `scripts/backtest_clean_eval.py` | CREATE. OOS-by-construction evaluation of `signal_provenance` research predictions (emission-time recorded, settled forward); INSUFFICIENT_DATA verdict on empty DB. |
| `scripts/ci_syntax_check.py` | CREATE. ast.parse gate over git-tracked .py (read-only). |
| `scripts/run_offline_checks.sh` | CREATE. Ordered suite: syntax → phase1 → JS states → settlement → time → eval → integration → retention → otc pipeline → JS syntax. Temp DBs; no creds; no broker; truthful exit propagation. |
| `.github/workflows/ci.yml` | CREATE. Mirrors the local runner exactly. NOTE: GitHub Actions execution will NOT be claimable as passing unless a real run is observed; local-only until then. |
| `README.md` | CREATE. Real startup/test instructions, known limitations, repo map. |
| `docs/BACKTEST_INTEGRITY.md`, `docs/PHASE_STATUS.md`, `docs/DATA_SOURCE_BLOCKER.md` | CREATE. Verdict table for legacy paths; phase status; the authorized-provider integration path (what stays disabled and why). |
| `scripts/test_phase2_eval_protocol.py` | CREATE. Target ≥19 tests. |
| `scripts/test_phase2_integration.py` | CREATE. Target ≥16 tests (the 12 directive scenarios incl. the synthetic eligible fixture with a MOCK authorized provider — explicitly labeled NOT evidence of authorization or profitability). |

### C.2 Dependencies / call sites to inspect

- Every candidate-selection site (min-logloss argmin patterns) in the three
  LIVE paths; the pooled fallback in fast_train (index-sliced concatenation
  = cross-asset temporal leakage).
- All JSON write paths in scripts/backtest* (overwrite protection).
- Existing test scripts to be integrated into the runner (verify they exist
  and run at baseline first).

### C.3 Required tests (minimum)

- Selection-split independence: adversarial perturbation of the second half
  cannot change the chosen candidate; the old full-pool winner demonstrably
  differs from the first-half winner on a constructed fixture.
- Chronological pool stability; embargo scaling; cross-asset embargo.
- Clean-eval on a seeded temp DB → exact expected metrics; empty DB →
  INSUFFICIENT_DATA; corrections honored in metrics.
- Source-level regression: the LIVE modules import with the fix markers
  present (so silent regressions fail loudly).
- Integration: the 12 scenarios incl. restart persistence, correction loop,
  UI pure-mapping, REST/WS parity.
- Runner: end-to-end `bash scripts/run_offline_checks.sh` exit 0; failure
  injection (temporarily breaking one test) must produce non-zero exit
  (verify once in a scratch copy, never committed broken).

### C.4 Data-safety / migration constraints

- NO retraining; NO model-weight changes; NO accuracy claims anywhere.
- Committed historical JSONs are never modified — new outputs go to
  `scripts/out/` (gitignored).
- CI requires no credentials and performs no broker/network access.

### C.5 Acceptance criteria

- `bash scripts/run_offline_checks.sh` → exit 0 with all suites green;
  exact commands + counts recorded in worklog.
- CI YAML mirrors the runner; honest local-vs-GitHub separation documented.
- Legacy contaminated harnesses labeled/guarded; overwrite protection
  demonstrated by test.
- Checkpoints `CP-P2B-n` committed.

### C.6 Stop conditions

- Decontaminating a legacy script would require retraining or weight
  changes → STOP (forbidden); label it instead.
- The offline runner cannot be made truthful (a suite masks failures) →
  STOP and report.
- Any fix would alter `scripts/data/` committed inputs → STOP.

---

## Phase D — Independent audit of the rebuilt implementation

Objective: verify the rebuild against the SAME checklist as the lost
"Phase 2 Final Verification & Audit": settlement integrity, time
synchronization, backtest integrity, CI truthfulness, Truth-Layer safety,
security/data-source review. Prefer a FRESH set of eyes (independent
subagent with no authorship stake) for at least the critical claims.

- Inputs: the rebuilt tree, the offline suite, this plan, the Phase 0
  root-cause matrix, the surviving audit report.
- Method: re-run the full suite; independently re-derive ≥3 critical claims
  from source (e.g., gate coverage of every emission path; late-tick
  immutability; selection-split independence).
- Output: verdict PASS / PASS WITH LIMITATIONS / FAIL per area + overall,
  with exact commands and evidence; appended to worklog; delivered as a
  copy-paste text code block (standing user directive).
- Stop condition: any area unverifiable → that area is reported FAIL, not
  assumed fine.

---

## Sequencing and approval gates

```
Phase R (DONE — this branch, documentation only)
   └─ approval gate ─→ Phase A (rebuild/phase1)  ── CP-P1 checkpoints ──→ stop for review
                          └─ approval ─→ Phase B (rebuild/phase2a) ──→ stop for review
                               └─ approval ─→ Phase C (rebuild/phase2b) ──→ stop for review
                                    └─ approval ─→ Phase D (rebuild/audit) ──→ final verdict
Phase 3: NOT planned here. Requires its own directive after D passes.
```

Each phase begins only after explicit user approval. Within a phase, the
agent stops immediately at any listed stop condition and reports.
