# RECOVERY STATUS — Phase R (Repository Recovery & Durable Workspace)

> Recovery documentation only. This file contains NO application code and makes
> NO claims about implemented behavior. It records verifiable repository and
> evidence state so that a later, explicitly approved rebuild phase can proceed
> from a known baseline.

Date of record: 2026-10-18 (UTC)
Recorded by: Phase R recovery session (senior Git recovery engineer role)
Recovery branch: `recovery/phase-r` (this branch)

---

## 1. Verified baseline

| Item | Value | Verification |
|---|---|---|
| Repository | https://github.com/psfaruk/Binary-signals-app | `git remote -v` |
| Local clone | `/home/z/my-project/binary-signals-app` | fresh clone this phase |
| Branch (clone default) | `main` | `git rev-parse --abbrev-ref HEAD` |
| HEAD / baseline commit | `4deadee5aec587061752cc7e6579885467afa185` | matches expected baseline exactly; matches remote `HEAD` via `git ls-remote` |
| Working tree | CLEAN (nothing staged/modified/untracked) | `git status --short` → empty |
| Object database | readable, consistent | `git cat-file -t` → `commit`; `git fsck --full` → exit 0, no output |
| Tracked files | 234 | `git ls-files \| wc -l` |
| Tags | 0 | `git tag` |

The clone contains the **pristine upstream baseline**. Nothing from any of our
prior sessions exists in it: the prior sessions never committed or pushed
(per standing directive), so the remote and this clone contain ZERO Phase 1 /
Phase 2 work.

## 2. What was lost (context, kept brief)

All uncommitted Phase 1 (Truth Layer) and Phase 2 (settlement lifecycle,
time provider, backtest integrity, tests, CI, docs) work lived in a previous
local clone at `/home/z/Binary-signals-app` — OUTSIDE the sandbox's persisted
workspace. A full environment reset on 2026-10-18T06:11:20Z (evidence:
`/tmp/boot-timeline.log`) destroyed that directory. No backup, no commit, no
push existed, so the work is **irrecoverable except by re-implementation**.
Full details: the Phase 2 Final Verification & Audit report (FAIL verdict),
preserved at `/home/z/my-project/recovery-artifacts/PHASE2_FINAL_AUDIT_REPORT.txt`.

## 3. Persisted workspace (durability evidence)

The durable location in this sandbox is **`/home/z/my-project/`** — which is
why this clone lives INSIDE it. Two independent, observed mechanisms protect
content under that path:

1. **Platform auto-commit**: the outer `/home/z/my-project/.git` receives
   automated checkpoints (UUID commit messages). Six checkpoints span the
   2026-10-18 reset (17:31:11, 18:19:17, 02:56:17, 04:07:23, 05:54:40, and
   06:25:18 UTC — the last one POST-reset). The post-reset checkpoint captured
   changes made minutes earlier, proving the mechanism is active in the
   current environment instance. Untracked new files ARE picked up.
2. **Filesystem sync tar**: `/home/sync/repo.tar` (filesystem-level tar; it
   includes git-ignored files such as `.env`, proving it is not git-based)
   was written at 06:10:57, 23 seconds before the reset, and restored the
   tree at boot with original mtimes preserved (e.g. `worklog.md` still shows
   its pre-reset mtime). Restore manifest: `/tmp/my-project/.initial_snapshot.json`.

Honest limitations:
- Neither mechanism is documented in userland; the sync trigger is not
  userland-observable (no cron/process visible). Cadence is INFERRED from
  observation (~hourly during activity + at reset boundaries).
- Only `/home/z/my-project` is protected. Anything outside it (e.g.
  `/home/z/...`, `/tmp`) must be assumed volatile.
- Total destruction of the environment (including `/home/sync`) would still
  be total loss. No off-box backup is possible: the remote is a third-party
  repository we have no authorization or credentials to push to.

## 4. Surviving evidence inventory (as of this record)

All artifacts live under `/home/z/my-project/` (persisted). Source-code
snapshots are **read-captures taken by the Read tool during prior sessions**;
they are genuine file contents at capture time, but they are NOT a working
tree and must not be treated as executable/test evidence without re-verification.

### 4.1 Source-code snapshots (Phase 1 relevant)

| Snapshot | Lines | Bytes | SHA-256 | Classification |
|---|---|---|---|---|
| `tool-results/read_1792299280497_5cdacf9479d9.txt` = **Phase 1 `core/signal_contract.py`** | 715 | 38,545 | `5cdacf9479d9d0b89be7af18cfdfd846b4172f23e983e20f80bc1b44fce6840e` (raw, with Read-tool prefixes) / `aa20ed7705dbe2b7cc96bd5e74b9db5464fc25d5c16ae2d42aac2035116666b3` (stripped source) | SOURCE — **AST-parse verified complete** this phase: every line prefix-strippable, `ast.parse` OK, 11 top-level defs ending cleanly with `truth_layer_state` (L691); captured 2026-10-18 04:54 UTC |

This is the ONLY Phase 1/2 source file that survives anywhere. Verified copies
preserved at `/home/z/my-project/recovery-artifacts/` (raw + stripped
variants, both hashed). It must NOT be restored into application code until
an approved implementation phase verifies it.

### 4.2 Source-code snapshots (Phase 0 read-only audit captures)

| Snapshot (verified baseline file) | Lines | SHA-256 (first 16) | Notes |
|---|---|---|---|
| `pyquotex/api.py` (`read_1792259886863`) | 1,232 | `d5307aca0d889f839` | **byte-identical to baseline after prefix strip** (earlier narratives mislabeled this capture "quotex_ws.py"; root `quotex_ws.py` is 1,161 lines and is a different file) |
| same (`read_1792259889829`) | 1,232 | `b179eae4594301ac` | double-prefixed capture; identical after double strip |
| `static/app.html` (`read_1792259887113`) | 1,140 | `a2837b27b647aea0` | byte-identical to baseline |
| `static/app.html` partial (`read_1792259889415`) | 401 | `5787a2fedc434d4e` | partial capture from char offset 1940; redundant (full file in clone) |
| `engines/base/confluence.py` (`read_1792259888037`) | 659 | `6595301e5d21358e` | byte-identical to baseline |
| `core/otc_predict/fast_train.py` (`read_1792259900271`) | 1,017 | `08cc006cdbab8369` | byte-identical to baseline |
| `core/otc_predict/tracker.py` (`read_1792259928355`) | 703 | `2119a8abcc6a510a` | byte-identical to baseline |
| `core/roadmap.py` (`read_1792259935037`) | 596 | `506fc3cc518f63a7` | byte-identical to baseline |
| `core/next_candle.py` (`read_1792260698461`) | 714 | `cfe6d0e96162c8a1` | byte-identical to baseline |
| `core/next_candle.py` (`read_1792260701903`) | 714 | `d8e5ff4bae2e5eb8` | double-prefixed; identical after double strip |
| `scripts/data/binance/BTCUSDT_1m.json` (`read_1792260016071`) | 1 (748,820 B raw) | `82c06e1bb037ad71` | single-line JSON; byte-identical to baseline |

All ten full captures were re-verified this phase against the fresh clone:
**byte-identical after prefix stripping** — simultaneously proving the
snapshots authentic AND the fresh clone an untampered copy of the audited
baseline.

Full hashes are recorded in
`/home/z/my-project/recovery-artifacts/EVIDENCE_MANIFEST.txt`.

### 4.3 Narrative records (NOT code, NOT test evidence)

- `/home/z/my-project/worklog.md` (350 lines at capture; sha256
  `a032ae64eef8ae78a6fe138cc03051169cab6e4fd82511554402586ae47b78af`) —
  narrative task logs of Phase 0 / Phase 1 / Phase 2 / Phase 2 final audit.
  Useful as design guidance; every claim in it about implemented behavior is
  currently UNVERIFIED because the implementation is lost.
- Phase 2 Final Verification & Audit report (FAIL) — preserved at
  `/home/z/my-project/recovery-artifacts/PHASE2_FINAL_AUDIT_REPORT.txt`.

## 5. Irrecoverably missing (must be rebuilt, cannot be restored)

Phase 1 (all except `core/signal_contract.py`): its integrations in
`feed.py`, `db.py` (signal_provenance), `server.py` (/api/truth-layer),
`quotex_ws.py`, `core/retention.py`, `static/js/common.js`,
`static/js/signal_states.js`, `scripts/test_phase1_truth_layer.py`,
`scripts/test_phase1_signal_states.cjs`, `docs/PHASE1_TRUTH_LAYER.md`,
model-validation registry code.

Phase 2 (all): `core/candle_lifecycle.py`, `core/time_provider.py`,
`core/eval_protocol.py`, `scripts/run_offline_checks.sh`,
`scripts/ci_syntax_check.py`, `.github/workflows/ci.yml`,
`scripts/test_phase2_settlement.py`, `scripts/test_phase2_time_provider.py`,
`scripts/test_phase2_eval_protocol.py`, `scripts/test_phase2_integration.py`,
`scripts/backtest_clean_eval.py`, corrected `core/otc_predict/fast_train.py` /
`walk_forward.py` / `scripts/train_otc_model.py`, legacy-backtest labels and
guards, `README.md`, `docs/BACKTEST_INTEGRITY.md`, `docs/PHASE_STATUS.md`,
`docs/DATA_SOURCE_BLOCKER.md`, db.py settlement-lifecycle schema.

## 6. Current safety posture (IMPORTANT)

This repository is at the **pristine baseline**, which the Phase 0 audit
documented as containing live fail-open behaviors: gate-bypass early-exit for
the CSE engine, a fabricated conf-50 CALL/PUT fallback, a fake countdown, and
local device time labeled as broker UTC. On top of that, the data source is an
unauthorized reverse-engineered Quotex channel and no model is validated.

Therefore:
- Do NOT run the application against a live feed.
- Do NOT enter broker credentials.
- Actionable CALL/PUT must remain impossible until the Truth Layer is rebuilt
  AND an authorized data source AND a validated model AND verified timing all
  exist. Current blockers stand exactly as documented in Phase 0/Phase 2.

## 7. Status and next step

- Phase R (recovery): COMPLETE — clone verified, recovery branch created,
  checkpoint policy written, evidence preserved, rebuild plan written.
- Phase 1 / Phase 2 rebuild: NOT STARTED; requires explicit user approval.
  See `docs/recovery/REBUILD_PLAN.md`.
- Phase 3: NOT approved, NOT started.
