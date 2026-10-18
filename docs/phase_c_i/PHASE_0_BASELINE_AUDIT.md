# PHASE 0 — BASELINE AUDIT (Phase C–I entry point)

> Executed 2026-10-18 on the rebuild tree before any Phase C–I modification.
> Mode: read-only inspection + test re-run + temp-DB schema audit. No broker
> connection, no credentials, no destructive git operations, no code changes.
> This document records what was **actually verified**, not what prior reports
> claimed. Where a prior report value was reproduced, it is marked VERIFIED.

## 1. Repository state verification

| Check | Command | Result |
|---|---|---|
| Branch set | `git branch -a` | `main`, `rebuild/phase1`, `rebuild/phase2a`, `recovery/phase-r`, `rebuild/phase-c-i` (new, cut for this phase set) + owner's remote branches (untouched) |
| HEAD before this phase | `git rev-parse HEAD` | `f950b66549317df652de8ae17e7e5cf2c15f3440` — **VERIFIED identical to the reported Phase B checkpoint CP-P2A-1** |
| Phase A checkpoint in history | `git log --oneline` | `d5e562d CP-P1-1` present as ancestor — **VERIFIED** |
| Working tree | `git status --porcelain` | clean — **VERIFIED** (only ignored `__pycache__/` dirs; no user changes or untracked files to preserve) |
| History integrity | `git log --oneline -5` | linear chain `f950b66 → d5e562d → a0921f6 (CP-R1) → a254ebe (CP-R0) → 4deadee (baseline)` — no rewriting detected |

**Discrepancy verdict: NONE.** The reported Phase A checkpoint (d5e562d) and
Phase B checkpoint (f950b66) both exist exactly as reported, the tree is clean,
and the branch topology matches `docs/recovery/REBUILD_PLAN.md` sequencing.

## 2. Documentation review (read in full)

| Document | Status | Key content confirmed against code |
|---|---|---|
| `docs/recovery/CHECKPOINT_POLICY.md` | read | 7 binding rules (tests-before-commit, staged-diff review, secret scan, no history rewriting, no push, honest claims only); commit prefixes CP-R/CP-P1/CP-P2A/CP-P2B/CP-AUD |
| `docs/recovery/REBUILD_PLAN.md` | read | Phase A/B plans match implementation; Phase C (2B) = backtest integrity — **NOT rebuilt after the 2026-10-18 loss** (this phase set now implements it under the new directive's broader scope); Phase D = independent audit |
| `docs/PHASE1_TRUTH_LAYER.md` | read | 13 reason codes, fixed check order (data→model→target→timing→source→registry), no env bypass, registry format, A.6 caveats |
| `docs/PHASE2A_SETTLEMENT.md` | read | lifecycle states, right-edge bucketing, correction ledger, SourceTimeProvider evidence policy (≥3 samples, 120 s staleness, reconnect reset, >5 s jump reset), single-process concurrency statement |
| `docs/recovery/RECOVERY_STATUS.md` | read (Phase R entry) | loss narrative + evidence inventory |
| `docs/PREDICTION_AUDIT.md` | skimmed | the 123-problem audit (Bengali) that motivated the rebuild; A1/A2 forced-signal + A5 grading≠settlement + A6 late-tick mutation are the root causes Phases A/B addressed |

## 3. Implementation existence verification

| Phase B expectation | File | Size | Status |
|---|---|---|---|
| Truth Layer contract | `core/signal_contract.py` | 715 lines | present (restored byte-exact, hash chain per worklog) |
| Settlement lifecycle | `core/candle_lifecycle.py` | 18,951 B | present |
| Time provider | `core/time_provider.py` | 18,394 B | present |
| Feed integration | `feed.py` | 413,350 B | `_run_eoc` gate wrapper + `_note_late_ticks` + lifecycle hooks present (spot-verified via test suite) |
| DB migrations | `db.py` | 124,763 B | `signal_provenance`, `candle_settlement`, `candle_corrections` CREATE TABLE IF NOT EXISTS at lines 422/466/496 |
| Diagnostic endpoints | `server.py` | 192,668 B | `/api/truth-layer` (L689), `/api/settlement-integrity` (L729), `/api/time-sync` (L762) |
| Frontend mapping | `static/js/signal_states.js` | present | pure mapping module |
| Retention | `core/retention.py` | present | count-based policy for new tables (test-verified in suite) |

## 4. Baseline test-suite re-run (before any modification)

All commands executed from the repo root, 2026-10-18, on temp DBs, no network,
no broker, no credentials. Exit codes captured inline.

| # | Command | Reported (Phase B) | Re-run result | Exit |
|---|---|---|---|---|
| 1 | `python3 scripts/test_phase1_truth_layer.py` | 70/70 | **70 passed, 0 failed, 70 total** | 0 |
| 2 | `node scripts/test_phase1_signal_states.cjs` | 59/59 | **59 passed, 0 failed, 59 total** | 0 |
| 3 | `python3 scripts/test_phase1_http.py` | 25/25 | **25 passed, 0 failed, 25 total** | 0 |
| 4 | `python3 scripts/test_phase2_settlement.py` | 54/54 | **54 passed, 0 failed, 54 total** | 0 |
| 5 | `python3 scripts/test_phase2_time_provider.py` | 40/40 | **40 passed, 0 failed, 40 total** | 0 |

**Total: 248/248 checks green, all exit 0 — the Phase B report's test claims are
VERIFIED reproducible.** No unexpected warnings observed in suite output.

## 5. Database schema and migration audit (temp DB only)

Method: `DB_PATH=<tempdir>/audit.db QX_RETENTION_ENABLED=0 python3 -c "import db; db.init()"`
run **twice** on a fresh temporary database. The repo's own runtime DB is
absent (`ls *.db` → none; only ignored `__pycache__` exists) so nothing was at
risk. No user database was touched.

Findings:

* Fresh init creates **15 tables**: `_meta`, `candle_micro`, `signal_log`,
  `signal_provenance`, `candle_settlement`, `candle_corrections`,
  `otc_predictions`, `model_registry`, `module_votes`, `theory_votes`,
  `signal_quality_metrics`, `pair_performance_daily`, `pair_hourly_patterns`,
  `quotex_algo_patterns`, `sqlite_sequence`.
* Column counts: `signal_log` 26, `signal_provenance` 30,
  `candle_settlement` 25, `candle_corrections` 16 (full column lists dumped to
  `/tmp/p0_schema.json` during the audit).
* **Idempotence VERIFIED**: second `init()` yields the identical 15-table set;
  ALTER-based column adds are guarded by `PRAGMA table_info` presence checks;
  migration messages appear only on first run.
* Journal mode: **WAL**, `busy_timeout=10000` (matches PHASE2A doc).
* All row counts zero on fresh DB (no fixture contamination in migrations).
* Known baseline migration behaviors preserved: `DROP INDEX IF EXISTS
  ix_sl_asset_period` / `ix_sl_ts` followed by CREATE of replacement indexes
  (idempotent by design), and legacy `signal_log` backfill UPDATEs
  (`category` fill) that only run when the column is being added — no
  destructive rewrite of existing rows at this checkpoint.

## 6. Data assets inventory (provenance-classified)

| Asset | Content | Provenance classification | Notes |
|---|---|---|---|
| `scripts/data/binance/BTCUSDT_1m.json` | 8,999 1-minute candles, 2026-09-22 23:38 → 2026-09-29 05:36 UTC | **Authorized historical (public Binance exchange data, committed by repo owner)** | real market data; NOT OTC — usable for evaluation-pipeline validation and non-OTC research only |
| `scripts/data/binance/ETHUSDT_1m.json` | 8,999 candles, same span | same | same |
| `scripts/data/binance/XRPUSDT_1m.json` | 8,999 candles, same span | same | same |
| `scripts/data/binance/BNBUSDT_1m.json` | 8,999 candles, same span | same | same |
| `scripts/data/live_signals_2026-09-18.json` | 141 emitted signals with `accuracy` fields, captured 2026-09-18 from the production deployment | **Observational record (user-committed capture); ground-truth labels derive from the unauthorized Quotex channel** | usable only as an audit artifact of past behavior; NOT evidence of OTC predictive skill (Phase C treats it accordingly) |
| 12 committed `scripts/backtest_*_report.json` + `deep_backtest_report.json` + `otc_predictor_backtest_report.json` | historical backtest outputs | **Immutable history (never modified)** | some produced by contaminated harnesses (see §7) — kept verbatim, labeled by Phase C |
| Test fixtures inside `scripts/test_phase*.py` | synthetic candles/ticks | **Synthetic (explicitly generated)** | never presented as market evidence |

## 7. Known unresolved items found at baseline (expected; scoped to later phases)

These are **pre-existing conditions carried into this phase set**, recorded here
so Phase C–I work cannot silently inherit them:

1. **Legacy backtest contamination NOT yet remediated.** The Phase-0-era
   selection-on-test / in-sample replay defects in
   `core/otc_predict/walk_forward.py` (L169–179 selection from test-fold
   results), `scripts/train_otc_model.py` (L146 `best_overall` min-logloss
   selection), `scripts/backtest_fast_train_real.py`,
   `scripts/backtest_model_pipeline.py`, and
   `scripts/backtest_persistence_replay.py` are all still present — the
   "Phase 2B" decontamination was destroyed in the sandbox reset and never
   rebuilt. **Phase C of this directive implements the clean evaluation
   pipeline; the contaminated scripts must be labeled/deprecated, not
   trusted.**
2. **`core/model_validation.json` absent** — empty registry is the correct
   fail-closed state (no validated models, no actionable signals possible).
3. **No offline runner / CI exists** (`scripts/run_offline_checks.sh`,
   `.github/workflows/ci.yml` were lost with Phase 2B) — Phase C/G rebuild this.
4. **Sensitive-by-nature endpoints exist on the public API surface**
   (`/api/db-download`, `/api/db-export`, `/api/session/cookies`,
   `/api/set-token`, `/api/keys`) — carried from baseline; audited in Phase D,
   not silently changed.
5. **No browser E2E** has ever been run (documented limitation since Phase A).
6. **Single-process write assumption** for settlement lifecycle (documented in
   PHASE2A §Concurrency; multi-writer architecture is out of scope per stop
   conditions).
7. **Phase B known limitations** (opportunistic `settle_pending`, corrections
   not fed back into engine context, `_srv_now` local-clock degradation while
   UNSYNCED) — documented, unchanged.

## 8. Preservation and compliance statement

* No broker connection, login, authentication, order placement, or live-account
  access at any point.
* No credentials read, printed, stored, or exposed.
* No destructive git commands (`reset --hard`, `clean`, force-push, history
  rewriting) — the only git operations were read-only inspection plus creating
  the `rebuild/phase-c-i` branch (forward-only, from the verified checkpoint).
* No push (no authorization, no credentials).
* All schema inspection on temporary databases under `/tmp`.
* Untracked/ignored files preserved (only `__pycache__/` — regenerated, never
  committed).
* The 248-check regression suite was re-run and is green at the exact commit
  this phase set starts from.

## 9. Baseline verdict

**BASELINE VERIFIED — MATCHES THE PHASE B REPORT.**
Repository, checkpoints, implementation, and tests all reproduce. The phase
gate for starting Phase C is satisfied: the previous phase's acceptance
(248/248) is independently re-confirmed on the actual tree.
