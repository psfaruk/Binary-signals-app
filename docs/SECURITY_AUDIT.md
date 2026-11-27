# SECURITY AUDIT — Binary Signals App (Red-Team Review)

**Date:** 2026-11-27 · **Scope:** pre-release gate for branch `redesign/v2-terminal` · **Method:** code review of auth/admin/token/WS/cron/deploy paths + executed exploit-style tests (no live attacks against third parties; the Quotex feed itself was out of scope beyond reading the vendored client's documented behavior).

---

## 1. Findings summary

| # | Finding | Severity | Status |
|---|---|---|---|
| S-1 | Cron endpoint auth bypass (spoofable `x-vercel-cron` header passes even with secret set; fail-open when unset; secret accepted in query string) | **Critical** | **FIXED + 8 regression tests** (`scripts/test_cron_gate.py`, in CI runner) |
| S-2 | GitHub PAT pasted in plaintext by the operator (outside the repo, OPSEC) | Critical (ops) | Reported to user; **revoke immediately**; not usable in-repo |
| S-3 | Admin auth fail-open risk | — | Not present: `_check_admin_key` is constant-time + fail-closed 503 (verified) |
| S-4 | WS origin validation | — | Present: deny-by-default on missing Origin; Railway-domain allowance only when user list unset (verified lines 4538–4587) |
| S-5 | WS input validation | — | Present: asset length ≤32, period whitelist, category/asset mismatch checks, 1009 on oversized frames, idle timeout, max clients |
| S-6 | Rate limiting | — | Present & enforced per-bucket with truthful headers (core/rate_limit.py, Phase-J) |
| S-7 | SQL injection | — | Not found: all SQLite access parameterized; `f"…{asset}…"` occurrences are print-strings, not cursors (spot-checked db.py/server.py/feed.py) |
| S-8 | Secrets in responses | — | Phase-1 battery asserts none survive the emission boundary; `_strip_secret_like` at the contract layer |
| S-9 | Secret-shaped values in ad-hoc stdout prints | Medium | Accepted residual (documented MASTER_AUDIT M-9); access-log redaction exists (`_AccessLogSecretRedact`); recommendation: structured logging in Phase-5 refactor |
| S-10 | `.env` committed? | — | No: `.gitignore` covers it; `.env.example` contains no real values (verified) |
| S-11 | Token in query strings (GET /api/set-token) | — | Already removed by a prior phase (PHASE-J note in code) |
| S-12 | First-claim PIN race | — | Closed by prior phase (setup-code / ADMIN_KEY claim gate) |
| S-13 | CORS | — | No wildcard middleware; same-origin serving posture |
| S-14 | Unauthorized data source (reverse-engineered Quotex client with TLS spoofing/cookie replay) | Structural | Out of code-fix scope; the Truth Layer fails closed on it (`DATA_SOURCE_UNAUTHORIZED`) — the product cannot emit actionable signals because of this; account-block history is self-documented |

## 2. C-1 fix detail (the one Critical closed in this engagement)

**Before** (`api/cron/refresh.py::_authorized`):
```python
if not secret: return True                        # fail-open
if request.query_params.get("secret") == secret:  # secret in URL → access logs
    return True
...
return request.headers.get("x-vercel-cron","") == "1"   # forgeable → bypasses the secret
```
**After:** unset secret → 503 (fail-closed, mirrors `_check_admin_key`); secret accepted ONLY as `Authorization: Bearer` (hmac.compare_digest); `x-vercel-cron` never substitutes for the secret.
**Tests executed:** 8/8 pass (`R1` unset→503, `R2` no-auth→401, `R3` spoofed-header→401 (the bypass regression), `R4` query-string-secret→401, `R5` wrong-bearer→401, `R6` correct-bearer passes gate (reaches QX_TOKEN check), `R7` header alongside bearer OK).
**Deployment impact:** Vercel Cron must be configured to send the Bearer token; `DEPLOYMENT_VERCEL.md` updated (curl examples now use `-H "Authorization: Bearer $CRON_SECRET"`).

## 3. Frontend security posture (new SPA)

- No credentials in frontend code or localStorage beyond UI preferences (theme/sound/refresh — no tokens).
- No `eval`/`innerHTML` with remote content; React JSX escapes all rendered strings.
- WS URL is same-origin; origin allow-list enforced server-side.
- CSP hardening is a recommended *next* step (add `Content-Security-Policy` headers when serving `static/app-v2` — documented as follow-up, not blocking).

## 4. Residual risks (accepted, documented)

1. **Unauthorized data source (S-14):** the platform's feed client is against Quotex ToS; the account has been blocked once before. This is a product-level decision owned by the operator, not a code defect; the Truth Layer correctly refuses to let it produce actionable signals.
2. In-process rate limiting is not a DDoS shield (documented in-repo).
3. `ADMIN_KEY`/`PIN` brute-force bounded by auth bucket (10/min) — adequate for a single-operator deployment.
4. Vercel mode's SQLite is a per-instance cache; secrets never written there (checked `db.py` paths).

## 5. Release-gate checklist (directive Phase 12)

- [x] Feature branch only (`redesign/v2-terminal`); `main` untouched
- [x] Offline battery green after every change (1,095 checks incl. new gates)
- [x] No new dependency on the backend; frontend deps pinned exactly
- [x] No secrets in repo or query strings; cron Bearer-only
- [x] Visual + interaction QA executed in a real browser (6 pages, 320/360/1440, zero console errors, zero horizontal overflow)
- [ ] **Operator approval before production deploy** (awaiting user)
