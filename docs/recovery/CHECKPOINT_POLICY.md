# CHECKPOINT POLICY — Rebuild Safety Rules

> Binding policy for every commit made on the recovery/rebuild branches of
> this repository. Written in Phase R (2026-10-18) after the total loss of the
> uncommitted Phase 1 + Phase 2 work. The loss happened precisely because no
> checkpoints existed; this policy exists so it cannot happen again.

## 1. The seven rules (from the Phase R directive)

1. **Small, logically complete changes.** One checkpoint = one coherent unit
   (a module + its tests, or an integration seam + its tests). Never mix
   unrelated concerns in one commit.
2. **Tests run before each checkpoint.** The relevant offline test suite must
   be executed and pass BEFORE `git commit`. A checkpoint whose tests were
   not run is an invalid checkpoint. Record the exact command + exit code in
   the worklog.
3. **Review the staged diff before committing.** Always inspect
   `git status --short` and `git diff --cached` (at minimum `--stat`, plus
   full diff for anything non-trivial) and confirm ONLY intended files are
   staged.
4. **Never include secrets or runtime databases.** The baseline `.gitignore`
   already covers `.env`, `session.json`, `qx_token.json`, `qx_session.json`,
   `qx_env_seen.json`, `app_auth.json`, `*.db`, `*.db-shm`, `*.db-wal`,
   `models/`, `scripts/out/`, logs, caches. If a staged diff ever shows any
   credential, token, cookie, or runtime DB/WAL file, ABORT the commit and
   investigate. Never print secrets in reports.
5. **Never use `git reset --hard`, `git clean`, force push, or any history
   rewriting.** Mistakes are fixed by new commits, not by rewriting old ones.
   Branches move forward only. `git checkout -- <file>` / `git restore` on
   UNCOMMITTED work is likewise forbidden (it destroys work); prefer
   re-editing forward.
6. **Never commit untested claims as verified behavior.** Documentation may
   record aspirations and plans, but must label them as such. A statement
   like "tests pass" may only be committed alongside the run evidence
   recorded in the same checkpoint's worklog entry.
7. **No remote push without explicit authorization.** This remote is a
   third-party repository (psfaruk/Binary-signals-app). We have no
   authorization and no credentials to push. All work stays local.

## 2. Branch and commit conventions

- `recovery/phase-r` — current branch. Contains ONLY recovery documentation
  (this directory). Baseline: `4deadee` (upstream `main`).
- Implementation branches (to be created only after explicit user approval):
  `rebuild/phase1`, `rebuild/phase2a`, `rebuild/phase2b`, `rebuild/audit`,
  each cut from `recovery/phase-r` (or its successor state) at the moment
  approval is granted.
- Commit message prefix convention:
  - `CP-R<n>:` recovery-phase checkpoints (documentation only)
  - `CP-P1-<n>:` Phase 1 rebuild checkpoints
  - `CP-P2A-<n>:`, `CP-P2B-<n>:`, `CP-AUD-<n>:` later phases
  - Body must state: what changed, which tests were run (exact command +
    result), and any known limitation.

## 3. Durability guardrails (lesson of the 2026-10-18 loss)

- ALL work must live under `/home/z/my-project/` — the only path the sandbox
  demonstrably preserves across environment resets (platform auto-commit of
  the outer workspace + filesystem sync tar at reset boundaries). Never place
  repos, backups, or evidence outside it.
- Commit locally at every green checkpoint. The outer workspace auto-commit
  and the reset-boundary sync tar then preserve BOTH the working tree and the
  nested `.git` history.
- Local commits are a mitigation, not a guarantee: a total environment loss
  would still lose them. Honest statement: there is no off-box backup and
  none is possible without push authorization (which does not exist).

## 4. Content prohibitions (unchanged from prior phases)

- No actionable CALL/PUT enablement; fail-closed everywhere.
- No fake model-validation registry entries; an empty registry is the
  correct current state.
- No retraining or model-weight changes.
- No live broker connections, credentials, or order sending.
- No modification/deletion of historical data or committed report JSONs.
- No browser-E2E claims without actually running a browser.

## 5. Worklog protocol (unchanged)

Every task appends an entry to `/home/z/my-project/worklog.md` (append-only,
`---` separator, Task ID / Agent / Task / Work Log / Stage Summary). The
worklog is narrative coordination, not test evidence; test evidence lives in
committed tests + recorded commands.
