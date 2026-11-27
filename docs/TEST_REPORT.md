# TEST REPORT — Binary Signals Redesign (Phase 10 evidence)

**Date:** 2026-11-27 · **Branch:** `redesign/v2-terminal`
**Rule honored:** nothing below is claimed that was not actually executed in this session; commands and exit codes are reproduced verbatim.

---

## 1. Baseline (before any change)

`bash scripts/run_offline_checks.sh` → **exit 0**. ALL OFFLINE CHECKS PASSED: 1,087 checks (Phase A 154 + B 94 + C 93 + D 38 + E 43 + F 28 + G 39 + H 28 + J 54 + K 26 + L1-L4 316 + L5 31 + L7 27 + ENSEMBLE 13 + verification R1-R3). This re-verifies the repo's own "1071/1071" CI claim locally before work began.

## 2. After all changes (backend routes + cron fix + SPA build)

`bash scripts/run_offline_checks.sh` → **exit 0** again, now **1,095 checks** (1,087 baseline + 8 new CRON-GATE checks; Phase-1 HTTP battery extended from 25 → 27 checks covering the new route topology: root=v2 SPA, `/legacy`+`/app` keep the legacy script-order invariant).

One pre-existing test was UPDATED (not deleted) — `scripts/test_phase1_http.py` check "app.html includes signal_states.js": its protective intent (truth-layer states loaded before app logic) is now asserted against `/legacy` and `/app`, plus a new assertion that `/` serves the v2 SPA shell. Directive rule "Do not delete tests simply because they fail" — honored.

## 3. New security battery (executed)

`python3 scripts/test_cron_gate.py` → 8/8 pass:
R1 unset-secret→503 (fail-closed) · R2 no-auth→401 · R3 **spoofed x-vercel-cron header alone→401 (the closed bypass)** · R4 query-string secret→401 · R5 wrong Bearer→401 · R6 correct Bearer passes gate (reaches the QX_TOKEN 503 — burst cannot run) · R7 header+correct-Bearer passes.

## 4. Frontend suite (executed)

`vitest run` → **4 files, 22 tests, 22 passed**:
- `ws-manager.test.ts` (7): exponential backoff caps at 30 s *including jitter* (found & fixed a real bug: jitter used to exceed the cap), ≥100 ms floor, subscription keys, dedup semantics (keepalive frames not deduped; identical tick identity deduped; new candle times pass; window bounded).
- `signal-states.test.ts` (10): the Phase-7 honesty rules as code — null frame→DATA_UNAVAILABLE; research CALL→RESEARCH_ONLY with direction NO_SIGNAL and null confidence; reason codes carry Bengali text; INTERNAL_ERROR→ERROR; DATA_STALE→STALE; eligible CALL (future path) renders actionable; confidence null→"— অনুপলব্ধ"; normalizeDirection rejects junk; outcome badges only from graded fields.
- `routes.test.ts` (3): hash routing + junk fallback to overview.
- `protocol.test.ts` (2): wire-parity regression — pairs are OBJECTS `{asset,display,status,payout,locked}` (this exact assumption crash was found live and fixed).

`tsc --noEmit` → clean. `vite build` → 348 KB JS / 110 KB gzip + 17 KB CSS.

## 5. End-to-end smoke (executed against a real running server)

`uvicorn server:app` on :8765 (temp DB, no credentials — feed intentionally dead):
- `GET /` → v2 SPA shell (Bengali `lang="bn"`, no `user-scalable=no`)
- `GET /legacy`, `GET /app` → 200 legacy app
- `GET /healthz` → `{"ok":true,...}`
- `GET /api/truth-layer` → `actionable_signals_possible:false`, `data_source_authorization:"unauthorized"`
- `GET /api/pairs` → real live OTC pair objects (BRL/USD, NZD/USD, USD/BDT, USD/COP, …)
- bundle asset served: 200, 351,373 bytes

## 6. Browser QA (executed — headless Chromium via agent-browser)

- **All six pages render with zero console/page errors** (`#/overview`, `#/terminal`, `#/history`, `#/analytics`, `#/models`, `#/settings`).
- Terminal pair dropdown populated from the LIVE backend over WS (real pair list).
- Signal Intelligence panel renders the honest state: `NO_SIGNAL`, `কনফিডেন্স — অনুপলব্ধ`, `টার্গেট ক্যান্ডেল —`.
- RESEARCH banner present on every page (truth-layer driven).
- **Responsive:** at 360×780 — no horizontal overflow (`scrollWidth==clientWidth==360`), sidebar hidden, bottom nav visible, topbar visible; at 320×568 — no horizontal overflow (w=320). Screenshots saved (`download/qa/*.png`).
- **Navigation:** bottom-nav click "হিস্টোরি" → URL → `#/history`; "সেটিংস" → `#/settings`.
- **Settings work:** theme switch toggles `data-theme` dark↔light and persists to localStorage (verified via `localStorage.getItem('bsx.settings')`); every visible control is wired.
- Found & fixed during this QA: React #31 crash (pairs-as-objects), toggle switches missing aria-labels (both fixed, rebuilt, re-verified).

## 7. What was NOT tested (honest limits)

- Live Quotex ticks/EOC signal flow — requires real credentials (operator's token); the feed was intentionally dead in all tests. The chart therefore rendered empty (honest no-data state) — candle rendering path is exercised by unit-level merge logic and the legacy suite, not against live ticks.
- GitHub Actions CI run — requires a push (deferred to the operator's push of this branch; workflow mirrors the local runner).
- Vercel serverless deployment — requires the operator's account; the code path is covered by tests and the deployment doc was updated for the new Bearer cron.

## 8. Regression statement

The 1,087-check offline battery is green before AND after; the only backend deltas are the root/legacy route split, the cron gate hardening, and one test extension. No DB, feed, engine, or Truth Layer behavior changed.
