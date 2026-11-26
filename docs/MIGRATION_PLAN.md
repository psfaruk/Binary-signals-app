# MIGRATION PLAN — Aurora (legacy) → Terminal v2

**Date:** 2026-11-27 · **State:** Stage 1 + 2 complete on branch `redesign/v2-terminal`; production rollout awaits operator approval (Phase 12 gate).

---

## Stage map (each stage independently shippable & reversible)

| Stage | Content | Status |
|---|---|---|
| 0 | Baseline verified; audit (MASTER_AUDIT.md); feature branch | ✅ done |
| 1 | Security: cron gate fail-closed (C-1) + tests; deployment docs updated | ✅ done |
| 2 | New SPA built & served at `/`; legacy preserved at `/legacy`+`/app`; battery green (1,095) | ✅ done |
| 3 | Operator review on a preview deploy (Railway), both UIs side-by-side | ⏳ awaiting operator |
| 4 | Feature parity hardening on live data: tick/eoc marker rendering with a live token, ICT overlay port, win-rate model panel depth | planned |
| 5 | Legacy decommission (redirect `/legacy` → notice), remove old CSS/JS after a burn period | planned |
| 6 | Backend gradual refactor (Phase-5 tree in NEW_ARCHITECTURE.md §3), one route-group per commit, battery green each time | planned |

## Rollout procedure (Railway primary)

1. `git push origin redesign/v2-terminal` → Railway deploy on a **preview/staging** service (do NOT switch the production domain yet).
2. Set env (no new required vars for the frontend): existing `QX_TOKEN`/cookies stack unchanged. `ADMIN_KEY` set (admin endpoints fail closed otherwise). If Vercel is used: **`CRON_SECRET` is now mandatory for the cron burst** (Bearer header; see DEPLOYMENT_VERCEL.md §Hobby/verify).
3. Verify: `/healthz` ok · `/` shows the v2 terminal · `/legacy` shows the old one · RESEARCH banner visible · pair dropdown populated · no browser-console errors.
4. Compare a day of live operation: same pair, same period, both UIs — candles and signal states must agree (same WS frames).
5. Promote to production domain (Railway: re-point the domain). `main` branch is untouched; merge only after the burn period.
6. Rollback = redeploy previous commit or re-point the domain — `/legacy` also remains a one-click fallback UI.

## Data & compatibility guarantees

- **No DB migration is part of this change** — schema untouched; history immutable.
- **WS protocol frozen** — the SPA speaks the exact existing protocol (API_CONTRACT.md). Backend upgrades must keep the documented frames additive-only.
- **Old API consumers unaffected** — every legacy route/endpoint still responds; only `/` changed payload (SPA shell), and `/app` keeps the exact legacy page.
- **Deploy artifacts**: `static/app-v2/**` is committed (prebuilt) so deploys need no Node toolchain; rebuilding requires `cd frontend && npm install && npm run build` (Node ≥ 20).

## Known follow-ups (next stages, tracked)

1. CSP headers for the SPA (SECURITY_AUDIT §3).
2. Chart marker path needs a live-token verification pass (tick/eoc frames with real data).
3. Bengali `en` locale table (i18n dict is typed and ready).
4. Move `frontend/tests` into the CI workflow (Node is already installed there).
5. Backend Phase-5 extraction (only after operator sign-off on the UI).
