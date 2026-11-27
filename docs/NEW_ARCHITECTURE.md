# NEW ARCHITECTURE — Binary Signals Terminal v2

**Date:** 2026-11-27 · **Branch:** `redesign/v2-terminal` · **Status:** implemented (this engagement), legacy UI preserved at `/legacy` and `/app`

---

## 1. What changed (and what deliberately did not)

| Layer | Before | After |
|---|---|---|
| Frontend | 1 HTML file (1,158 lines) + 7 vanilla JS files (~8,100 lines, ~60 globals) + 5 CSS files with load-order fragility | `frontend/` React 18 + TypeScript + Vite 5, 6 pages, typed WS manager, typed REST client, centralized store, design-token CSS, hash router |
| Serving | `/` → `static/app.html` | `/` → `static/app-v2/index.html` (built SPA); legacy at `/legacy` + `/app` |
| Cron auth | spoofable header + fail-open + query-string secret | **fail-closed**, Bearer-only, constant-time (audit fix C-1) |
| Backend logic | — | **unchanged by design**: Truth Layer, feed engine, DB, settlement, engines all byte-identical; only the root-route handler gained a v2 branch + legacy routes |

The backend monolith (`server.py` 4,980 lines, `feed.py` 7,329) was NOT rewritten. It works, it is unusually well-commented, and it is guarded by 1,087 offline checks. The directive's Phase 5 target tree is documented below as the *gradual* destination — moving files without checking dependencies is forbidden, and this engagement chose zero risky backend movement.

## 2. Frontend architecture (as built)

```
frontend/
├── vite.config.ts          outDir → ../static/app-v2, base /static/app-v2/
│                           (assets served by the EXISTING /static mount — zero new server code)
├── index.html              lang="bn", zoom ALLOWED (audit H-3), safe-area ready
├── src/
│   ├── main.tsx            entry
│   ├── routes.tsx          hand-rolled hash router (6 routes; no react-router dep —
│   │                       justified: static hosting + XTransformPort gateway compat)
│   ├── stores/AppStore.tsx central state: Context+useReducer; WS frames normalized ONCE
│   │                       (selection, candles, prediction, truth snapshot, settings)
│   ├── services/
│   │   ├── websocket/protocol.ts    wire types, VERIFIED against the running backend
│   │   │                            (pairs are OBJECTS {asset,display,status,payout,locked})
│   │   ├── websocket/manager.ts     WsManager: backoff+jitter (≤30s cap), dedup window,
│   │   │                            subscription ledger, heartbeat {type:"status"} @15s,
│   │   │                            dead-connection 45s, stale clock 90s (contract default)
│   │   ├── api/client.ts            typed REST client; ApiError(401/403/429/5xx) — honest
│   │   │                            error states instead of spinners
│   │   └── signals/states.ts        THE honesty choke point: toSignalView() maps any
│   │                                Prediction → UiSignalState (CALL/PUT/NO_SIGNAL/STALE/
│   │                                ERROR/RESEARCH_ONLY); confidence null → "—"
│   ├── components/
│   │   ├── layout/          AppShell (sidebar ≥1024px / bottom nav <1024px / sticky topbar),
│   │   │                    ConnBadge, RESEARCH banner (truth-layer driven)
│   │   ├── charts/          CandleChart — LCharts 4.1.3 (npm, same version as vendored
│   │   │                    legacy = feature parity). Markers ONLY for eligible signals,
│   │   │                    at the contract's target candle time — never a guessed candle.
│   │   ├── signals/         SignalPanel (intelligence panel), StateBadge
│   │   ├── market/          MarketSwitch, PairSelect (display names), PriceTicker, Countdown
│   │   └── common/          (chips, panels, skeletons in base.css primitives)
│   ├── pages/               Overview · Terminal · History · Analytics · Models · Settings
│   ├── i18n/dict.ts         Bengali-first string map (typed; en table addable later)
│   └── styles/              tokens.css (design system single source) + base.css
└── tests/                   vitest: ws-manager, signal-states (honesty rules), routes,
                            protocol wire-parity (pairs-are-objects regression)
```

### Design tokens (tokens.css)
Directive color system, dark default + `[data-theme="light"]`:
navy/charcoal ground (`#0B1220/#111A2C/#18233A`), sky-blue accent `#38BDF8`, cyan `#22D3EE`, green `#34D399` (CALL/win), red `#F87171` (PUT/loss), amber `#FBBF24` (STALE/warn), slate `#94A3B8` (NO_SIGNAL), violet `#A78BFA` (RESEARCH_ONLY). Radii 6/10/14px, restrained shadows, `env(safe-area-inset-*)` honored, touch targets ≥40px, `touch-action: manipulation` instead of `user-scalable=no` (audit H-3). Fonts: system stack with Bengali fallbacks — **no external Google Fonts dependency** (audit M-item: BD latency/offline).

### Mobile-first (Phase 3, designed independently)
- `<1024px`: sidebar hidden; sticky compact topbar (safe-top); 6-item bottom nav (safe-bottom); short Bengali labels (হোম/টার্মিনাল/হিস্টোরি/রেজাল্ট/মডেল/সেটিংস) — nothing clips at 320px.
- Terminal grid stacks: command bar → chart (≥300px height) → signal panel → history strip.
- Verified in-browser: 320/360/1440 — **zero horizontal overflow at every width tested**; all six pages reachable from the bottom nav (click-tested).

### Data flow (single source of truth)
WS frames → `WsManager` (dedup/backoff/heartbeat) → `AppStore.reducer` (normalizes once; only frames for the active subscription mutate chart state) → pages read derived state. REST is the *fallback* path (poller active only when WS ≠ open — Vercel mode) and the source for overview/truth/models — never a second writer to chart state while WS is open (audit M-8 resolved by construction).

## 3. Backend target architecture (Phase 5 — gradual destination, NOT executed here)

Current monolith → destination (one route-group per commit, offline battery green after each):

```
backend/app/
  main.py            (today's server.py bootstrap + lifespan)
  api/routes/        health, market, signals, history, analytics, models, settings, admin
  core/              (already exists as /core — unchanged)
  services/          market_feed (=feed.py), signal_service, analytics_service, model_service
  engines/           (already exists as /engines — unchanged)
  contracts/         signal_contract (exists), schemas
  repositories/      db.py split by table family
  infrastructure/    database (SQLite/Supabase mirror), websocket hub
  workers/           feed worker, training worker, settlement worker
```

**Invariants that must survive any such move** (from MASTER_AUDIT §2): the Truth Layer emission boundary; the WS wire protocol (API_CONTRACT.md); `_WS_IDLE_TIMEOUT` keepalive contract; `QX_DISABLE_FEED` boot split; additive-only DB migrations; the 1,087-check battery as the gate.

## 4. Deployment adapters (Phase 6 requirement)

| Mode | Frontend | Feed | Durability | Honesty note (surfaced in UI) |
|---|---|---|---|---|
| **Railway (primary)** | `/` SPA; WS live; keepalive 15s | continuous `feed.run` | SQLite + volume + backups (+Supabase mirror if set) | live ticks; stale banner only when actually stale |
| **Vercel (fallback)** | same SPA; WS fails fast → REST poller (settings-tunable, default 10s) | bounded bursts via `/api/cron/refresh` (Bearer-gated, fail-closed) | Supabase (SQLite per-instance cache) | REST data only as fresh as last burst — the banner/freshness chips state this |

## 5. Honesty architecture (Phase 7 — preserved, made legible)

The Truth Layer is untouched. The frontend now *surfaces* its state instead of implying actionability:
- `/api/truth-layer` (`actionable_signals_possible:false` today) drives the persistent RESEARCH banner.
- Every signal card/frame passes `toSignalView()` — research frames render `RESEARCH_ONLY` + reason code; `confidence` renders "— অনুপলব্ধ" when unmeasured; history rows show win/loss/ungraded strictly from graded fields.
- Chart markers only for eligible signals at the contract target candle (unreachable today by design — nothing fake drawn).
- Settings/Overview show measured values or "—" — never invented.

## 6. Dependency decisions (trade-offs, per directive)

| Choice | Decision | Rationale |
|---|---|---|
| react-router | **hand-rolled hash router (~60 lines)** | 6 static pages; hash routing works behind static mount + XTransformPort gateway with zero server config; one less dependency |
| state lib | **Context+useReducer** | app is small; no zustand/Redux needed |
| chart lib | **npm lightweight-charts@4.1.3** | exact same version as legacy vendored copy → feature parity guaranteed |
| CSS | **hand-authored tokens + primitives** | no Tailwind runtime; the directive's design system expressed as CSS custom properties |
| i18n | **typed flat dictionary (bn)** | no framework; `en` table addable without component changes |
| tests | **vitest** | Vite-native; node environment for pure logic |
