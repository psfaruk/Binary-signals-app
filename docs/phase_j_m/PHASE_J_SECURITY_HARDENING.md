# PHASE J — SECURITY HARDENING (B3/B4 DEEP RESOLUTION)

> 2026-10, branch `rebuild/resolve`. Owner authorization: "সব গুলো deeply
> সমাধান করো" — closing the Phase-D/I documented release blockers B3 and
> B4. Two checkpoints: **CP-RJ-1** (`370dfbf`) — the four main fixes;
> **CP-RJ-2** — the round-2 audit fixes from the independent adversarial
> auditor. Every claim below is pinned by
> `scripts/test_phase_j_security.py` (54 checks) inside
> `bash scripts/run_offline_checks.sh` (671 checks total, ALL PASSED).

## 1. What was closed and how

### J1 — Operator gate on token-push (Blocker B3)

* Routes: `/api/set-token` (GET+POST), `/api/session/cookies`,
  `/api/session/refresh`, `/api/reconnect` (GET+POST) — new
  `operator_gate` classification (`core/api_keys.py`),
  enforced by the server middleware (`server.py`).
* Credential: ADMIN_KEY (`X-Admin-Key`) OR claimed+verified operator PIN
  (`X-App-Pin`). Fail-closed: fresh deployment without either → 401 with
  setup-code guidance (`setup_code_required: true`).
* **The token-in-URL leak is dead**: `GET /api/set-token?token=…` → 410
  GONE (rejected in the middleware before any processing); the supported
  GET form carries the token in the `X-Qx-Token` HEADER; POST keeps the
  JSON body. Boot guidance + Telegram alert text updated to the new forms.
* `token_store.authorize()` no longer returns a blanket `"open"` — the
  USER REQ 2026-08-17 open-by-design behavior is closed by the owner's
  2026-10 authorization (history documented in the docstrings).
* Frontend (`static/js/token-panel.js`): PIN field + first-run claim
  section; all three push calls carry `X-App-Pin`; 401s render honest
  guidance (claim flow / wrong-PIN retry).

### J2 — First-claim setup code (Blocker B4a)

* `core/token_store.py`: `ensure_setup_code()` generates a one-time code
  (`XXXX-XXXX-XXXX`, 48 bits) at boot while unclaimed AND no ADMIN_KEY;
  plaintext printed ONCE on the server console (lifespan banner), only
  its SHA-256 stored; 24h TTL; regenerated every boot while unclaimed;
  erased forever on successful claim.
* `claim_pin(pin, setup_code)` — AUDIT FIX #2: the whole
  check-then-write runs under the write lock; 8 concurrent claims with
  the same code → exactly ONE `ok` (test F5).
* Brute-force ceiling: 48-bit code + auth bucket 10/min/IP (see J3).

### J3 — REAL rate limiting (Blocker B4b)

* New `core/rate_limit.py` (dependency-free, in-process, thread-safe,
  fixed-window): buckets `auth` 10/min, `token_push` 30/min, `global`
  600/min per client; per-API-key caps (`rate_limit_per_min`, advertised
  since 2026-08-13 but never enforced) now actually enforced on
  `api_key_write` routes.
* 429 + `Retry-After` + truthful `X-RateLimit-Limit/-Remaining` headers
  on both block and success; kill switch `QX_RATE_LIMIT=0`; per-bucket
  env overrides (`QX_RL_AUTH`, `QX_RL_TOKEN_PUSH`, `QX_RL_GLOBAL`).
* AUDIT FIX #5: the periodic prune now removes only STALE windows (the
  first version's `_COUNTERS.clear()` also wiped ACTIVE windows).
* AUDIT FIX #8 / documented tradeoff: client identity = TCP peer by
  default (X-Forwarded-For deliberately NOT trusted — spoofing would
  give attackers a fresh bucket per request). Behind a trusted reverse
  proxy set `QX_TRUST_PROXY=1` to key on the first XFF hop. Residual:
  without it, all proxy-shared visitors share one bucket (~30–60
  concurrent viewers can self-429; single-user deployments are unaffected
  — measured frontend load is 12–25 req/min/tab vs the 600/min budget).
  In-process counters assume `WEB_CONCURRENCY=1` (the default).

### J4 — Cross-process settlement safety (Blocker B4c)

* `db._immediate_cursor()` (BEGIN IMMEDIATE + busy_timeout) applied to:
  `settlement_begin`, `settlement_finalize`, `settlement_settle`,
  `shadow_record`, `shadow_settle`, `shadow_append_correction`.
* Two-process race tests (real subprocesses, barrier-synchronized):
  exactly one winner per state transition; conflicting finalizations →
  one freeze + one audited `conflict_rejected`; settle race →
  `settled` + `already_settled`; SHARED correction history → all 10
  entries survive (the old deferred-tx version had a genuine lost-update
  window).
* AUDIT FIX (D-list): `_update_hourly_pattern` (SELECT→compute→UPDATE)
  also converted — was a cross-thread lost-update window on
  `pair_hourly_patterns`.
* `log_signal` deliberately NOT converted: single atomic UPSERT per
  commit, no read-then-write window (documented in `_immediate_cursor`).

## 2. Round-2: independent adversarial audit findings → all addressed

An independent auditor (fresh eyes, read-only, real-uvicorn probes) found
13 issues; dispositions:

| # | Sev | Finding | Disposition |
|---|-----|---------|-------------|
| 1 | **HIGH** (pre-existing) | Anonymous destructive `DELETE /api/signals/{a}/{p}/{c}` (public_read via prefix) — scriptable full-history wipe | **FIXED** — `_require_admin` gate; honest in-JSON error on DB failure (tests F1/F1b) |
| 2 | MEDIUM | `claim_pin` TOCTOU race (both concurrent claims could win) | **FIXED** — whole check+write under `_LOCK` (test F5: 8 claims → 1 ok) |
| 3 | MEDIUM (pre-existing) | `_require_admin` referenced but never defined → `POST /api/prediction/reload-models` always 500 (NameError) | **FIXED** — defined for real; gated (test F2) |
| 4 | MEDIUM (pre-existing) | Anonymous `POST /api/prediction/bootstrap` = remote CPU/DB-write DoS trigger | **FIXED** — admin-gated (test F3) |
| 5 | MEDIUM | Rate-limit prune wiped ACTIVE windows | **FIXED** — stale-only prune (test F4) |
| 6 | MEDIUM | Working-tree data files replaced with a new format → suite red | **FIXED** — originals restored; 30-day dataset moved to `scripts/data/binance_30d/` (new experiments only — the pre-committed Phase C/F/H holdout evidence stays bound to the original files) |
| 7 | LOW-MED | Legacy `?token=` URL still lands verbatim in uvicorn access log | **FIXED** — logger-level `uvicorn.access` filter redacts `token/api_key/pin/ssid/password/setup_code` query values (real-uvicorn test F6b: secret absent, `<REDACTED>` present) |
| 8 | LOW-MED | Proxy-shared buckets / XFF untrusted | **MITIGATED + DOCUMENTED** — `QX_TRUST_PROXY=1` opt-in (tests F7a/b); tradeoff above |
| 9 | LOW | `/api/auth/verify` docstring/status mismatch (unclaimed → 403, docs said 200) | **FIXED** — 200 + `via='unclaimed'` + guidance (test F8) |
| 10 | LOW | Stale db.py header "cross-process NOT supported" above converted code | **FIXED** — updated |
| 11 | INFO | This document did not exist yet | **FIXED** — this file |
| 12 | INFO | `/api/auth/state` + `/api/token-status` expose `state_dir` path | **ACCEPTED** — operator diagnostics by design (no secret material); documented here |
| 13 | INFO | PIN brute-force ceiling (30/min token_push ≈ 43k guesses/day) | **ACCEPTED** — 6-char-min PBKDF2-200k PINs with rotation paths; residual documented |

Auditor verdict on the four main claims: **PASS / PASS-WITH-NOTES /
PASS-WITH-NOTES / PASS**, with the notes resolved in this round.
Remaining accepted residuals: #8 (proxy bucketing tradeoff), #12, #13,
and the LOW `log_signal` votes DELETE+INSERT window (single-winner
conditional patterns make it benign; documented in `_immediate_cursor`).

## 3. Bonus robustness fix (found during verification)

`/api/stats` raised an **uncaught** `sqlite3.OperationalError` (raw 500)
on a table-less fresh DB after an unrelated endpoint touched the DB file
— reproduced on the pre-Phase-J tree (pre-existing). Now returns the
error inside the JSON like every other sub-computation.

## 4. Evidence

```
bash scripts/run_offline_checks.sh
  -> ALL OFFLINE CHECKS PASSED (671 checks; exit 0)
     Phase A 154 + Phase B 94 + Phase C 93 + Phase D 38 +
     Phase E 43 + Phase F 28 + Phase G 39 + Phase H 28 +
     Phase J 54
```
* Phase-J battery sections: A (operator gate, 12) · B (setup code, 11) ·
  C (rate limiting, 7) · D (cross-process races, 10) · E (leak
  regressions, 4) · F (audit fixes, 10... incl. real-uvicorn access-log
  redaction probe).
* The independent auditor separately re-ran the suite on the committed
  HEAD content: green; and verified no middleware path-shape bypass
  (`//`, `/./`, percent-encoded, case) exists for the operator gate.

## 5. Not done / out of scope (honest)

* No Quotex connection, no broker, no orders, no credentials touched.
* Rate limiting is in-process only (no Redis/multi-worker coordination)
  — documented; `WEB_CONCURRENCY>1` would fork counters (default 1).
* The vendored pyquotex/raw-WS channels remain unauthorized (blocker B1
  unchanged — that is Phase M's authorized-test-bed work).
