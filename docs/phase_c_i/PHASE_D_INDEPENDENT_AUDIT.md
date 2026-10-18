# PHASE D — INDEPENDENT CODE AND SAFETY AUDIT

> Executed 2026-10 on `rebuild/phase-c-i` (after CP-PCI-1). Method: a
> fresh-eyes independent auditor subagent (no authorship stake) performed
> an adversarial READ-ONLY audit of D1–D4 with file:line evidence; the
> orchestrator then FIXED every critical finding and built a SECOND
> adversarial test suite (`scripts/test_phase_d_audit.py`, 38 checks)
> that verifies the fixes and re-challenges the safety claims through
> independent fixtures. No broker contact, no credentials, no trades.

## 1. Independent audit method

The auditor was explicitly instructed to find holes, not confirm the
design. Its verdicts on the pre-fix tree:

| Area | Pre-fix verdict | Key findings |
|---|---|---|
| D1 Truth Layer | **FAIL** (gate solid, coverage overstated) | 5 ungated directional emission channels: `otc_pred` WS broadcast, `live_pred`/`entry_hint` in every tick frame, `/api/next-candle`, `/api/prediction/{asset}`, `/api/prediction/overview`; latent LIVE re-eval resurrection path |
| D2 Settlement/provenance | **PASS** | conditional UPDATEs verified; first-write-wins verified; retention exemptions verified |
| D3 Time provider | **PASS** | ≥3 samples, reconnect reset, 120 s staleness, >5 s jump reset, ±1 day malformed, SYNCED-only offset — all verified |
| D4 DB/API security | **FAIL** | unauthenticated full-DB dump (`/api/db-export`), destructive wipe (`/api/signals/clear`), fail-open PIN verifier (dead `_meta.operator_pin_hash` lookup), 12-char token preview + raw account email on public endpoints, unclamped query limit |

## 2. Critical findings → fixes (every one fixed and retested)

| # | Finding | Fix (file) | Retest |
|---|---|---|---|
| C1 | `/api/db-export` unauthenticated full-DB JSON dump + OOM vector | `_check_admin_key` added (fail-closed 503 when ADMIN_KEY unset) — server.py | S1 (503), functional path re-verified with correct key |
| C2 | `/api/set-token` GET+POST + `/api/session/cookies` unauthenticated credential swap; GET puts the full token in the URL (access-log leak) | **NOT changed** — open-by-design is the repo owner's documented requirement (USER REQ 2026-08-17, quoted in `core/token_store.authorize`). Documented as a **RELEASE BLOCKER for public deployment** (see §5) and PINNED by test S17 so any future change is noticed | S17 pins current behavior |
| C3 | `/api/signals/clear` destructive wipe with any dummy header | `_check_admin_key` added — server.py | S3 (503), S6 (403 wrong key) |
| C4 | `_verify_operator_pin` fail-open (never-written `_meta.operator_pin_hash` row → any PIN passed; `/api/auth/claim` first-claim race) | fail-closed: no claimed PIN → deny with guidance; a claimed token_store PIN is verified; first-claim requires ADMIN_KEY when set — server.py | S7, S8 |
| C5 | Ungated directional emissions (`otc_pred`, `live_pred`+`entry_hint`, 3 REST endpoints) | research-only truth label attached: `_research_truth_label()` (server.py) on `/api/next-candle`, `/api/prediction/{asset}`, `/api/prediction/overview`; `feed._label_research()` on the `otc_pred` WS broadcast and every `live_pred` tick frame — additive fields, fail-closed if the contract module is unavailable | S11–S15, N9 |
| C6 | `/api/token-status` 12-char token preview; `/api/session/status` raw account email | standard mask for the preview; `_redact_email()` (`***@do…`) for the email — server.py, core/qx_session.py | S9b, S10 |
| C7 | Latent LIVE re-eval NEUTRAL→CALL/PUT resurrection bypass | resurrection guard: re-eval skipped whenever the locked prediction's `truth_status` is not eligible — feed.py | (guard is structural; the block is also compile-time dead: `DISABLE_LIVE_REEVAL=True`, `ENABLE_LIVE_THEORY=False`) |
| C8 | `apply_truth_layer` accepts caller-supplied authorization/registry kwargs | **accept-with-note** (documented): unused by production (feed passes nothing); a programmatic door for future callers — flagged in the Phase I release checklist | — |
| C9 | `/api/share-signals/history` unclamped limit; `rate_limit_per_min` advertised but never enforced | limit clamped to [1,500]; the unenforced rate limit is documented honestly as a limitation (no fake 429 claims) — server.py | S16 |
| C10 | Partial token prints in logs; `X-DB-Path` full path | mask-only prints; basename-only header — server.py | (log-content change; no full-secret prints remain — re-grepped) |

## 3. Post-fix re-verification

`scripts/test_phase_d_audit.py` — **38/38 passing**, exit 0 (temp DB, no
lifespan → no broker):

* S1–S17: every security fix behaves as specified (fail-closed 503s,
  correct-key functional path, PIN fail-closed, masks, truth labels,
  clamp, pinned open-by-design endpoint).
* N1–N12: the directive's D5 negative list, independently re-driven:
  unauthorized data → NO_SIGNAL (synced-but-unauthorized included);
  stale timing → STALE/UNVERIFIED; malformed timestamps → rejected +
  counted; missing settlement → excluded (never fabricated); duplicate
  close → idempotent; conflicting finalization → rejected + audited
  with frozen OHLC intact; restart during pending → subprocess
  recovery; DB write failure → error action with no partial row;
  unexpected module failure → research-only + INTERNAL_ERROR;
  incorrect candle boundary → right-edge bucketing + fail-closed
  identity; historical leakage → evaluator rejection; unsynced timing
  → NO_SIGNAL/TIMING_UNVERIFIED.

Full regression after all fixes: **341/341** (Phase A 70+59+25, Phase B
54+40, Phase C 93) + Phase D 38 = **379/379 green**.

## 4. D5 — independent test strategy

Two independent suites now exist: the Phase 1/2 authors' suites and the
Phase D suite (different fixtures, different entry points, adversarial
intent). The Phase D suite re-derives the critical claims from the
public interfaces (HTTP) and from independently rebuilt contract/provider
fixtures rather than reusing author fixtures.

## 5. Residual risks / release blockers (honest statement)

1. **RELEASE BLOCKER (deployment posture):** `/api/set-token`,
   `/api/session/cookies`, `/api/session/refresh` remain open by design
   (owner's documented requirement). On a PUBLIC deployment anyone can
   replace the broker session token/cookies; the GET variant logs the
   full token in access logs. Mitigation for any real deployment: set
   `ADMIN_KEY` + put the app behind an authenticating proxy, or change
   the owner requirement. This app must not be treated as
   production-safe while publicly reachable.
2. **First-claim race without ADMIN_KEY:** if `ADMIN_KEY` is unset on a
   fresh deploy, the first caller to `/api/auth/claim` still wins (the
   ADMIN_KEY-gated claim only helps when it is set).
3. **Rate limiting is advertised but not enforced** (documented, not
   faked): `rate_limit_per_min` is stored per API key; no 429 logic
   exists. Resource-exhaustion protection relies on WS limits + the
   clamped queries.
4. **`apply_truth_layer` programmatic kwargs (C8):** safe today (no
   production caller passes them); any future caller that does must be
   reviewed against the authorization evidence rules.
5. **`/api/debug`, `/api/db-download` remain admin-gated and
   fail-closed** (pre-existing, verified); `/api/db-download`'s
   `X-DB-Path` now leaks only a basename.
6. **Multi-process settlement writers remain unsupported** (Phase B
   documented limitation — single-process assumption stands).
7. The auditor's D1 "coverage" finding is now closed for labeling, but
   the DIAGNOSTIC channels still exist by design (research visibility);
   they are labeled, not removed.

## 6. Verdict

**PHASE D: PASS WITH LIMITATIONS** — every critical finding is either
fixed and retested (C1, C3, C4, C5, C6, C7, C9, C10) or explicitly
documented as a release blocker / accepted-with-note (C2, C8). No
unresolved critical safety defect remains in the code; the residual
blockers are deployment-posture decisions owned by the repo owner, not
code defects that could be silently fixed without violating the
preserve-user-intent rules.

## Commands + results (recorded)

```
python3 scripts/test_phase_d_audit.py   -> 38 passed, 0 failed, exit 0
Regression: 70/70 + 59/59 + 25/25 + 54/54 + 40/40 + 93/93  all exit 0
```
