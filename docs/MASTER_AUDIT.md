# MASTER AUDIT — Binary Signals App

**Audit date:** 2026-11-27
**Auditor:** Principal Architect (redesign engagement, Phase 0)
**Repository state audited:** branch `main` @ `9c02309` ("UI-FIX: token panel claim form supports ADMIN_KEY mode"), shallow-clone depth 100.
**Baseline verification executed before any change:** `bash scripts/run_offline_checks.sh` → **exit 0**, all 1,087 offline checks passing (Phase A 154 + B 94 + C 93 + D 38 + E 43 + F 28 + G 39 + H 28 + J 54 + K 26 + L1-L4 316 + L5 31 + L7 27 + ENSEMBLE 13 + verification R1-R3). This is the regression baseline every later phase must preserve.

---

## 0. Repository architecture map

```
Binary-signals-app/                     329 tracked files, ~47 MB
├── server.py            (4,980)  FastAPI app: ~75 routes, WS endpoint /ws, auth, admin, static serving
├── feed.py              (7,329)  QuotexFeed market engine: streams, ticks, candles, EOC signal boundary
├── db.py                (3,034)  SQLite layer: ~15 tables, additive-only migrations, backups, settlement
├── quotex_ws.py         (1,205)  Raw-WS backend alternative (has server-time sync)
├── alerts.py / supabase_sync.py / sitecustomize.py
├── core/                (74 files) Truth Layer (signal_contract.py), settlement, otc_predict/ ML,
│                                 ensemble (all-theory blender), ICT/SMC research, data quality,
│                                 rate_limit, token_store, api_keys, time_provider, qx_session
├── engines/             (30)     base/ (15 voting modules + blender + confluence + tiered filter),
│                                 otc/ + real/ (per-domain configs & weight adapters)
├── pyquotex/            (48)     VENDORED unofficial Quotex HTTP/WS client (TLS spoofing, cookie replay)
├── static/              (14)     THE FRONTEND: app.html (1,158) + 5 CSS (~2,700 lines) + 7 JS (~8,100 lines)
│                                 + vendored TradingView Lightweight Charts v4.1.3 (157 KB, single line)
├── api/                 (3)      Vercel serverless: api/index.py (ASGI wrapper), api/cron/refresh.py (burst)
├── scripts/             (115)    ~40 test batteries + ~60 research/backtest tools + evidence JSON
├── docs/                (22)     Phase 0–L audit trail, deployment docs
├── vercel.json / railway.toml / Dockerfile / Procfile / run*.sh
└── .github/workflows/ci.yml      offline-checks mirror of run_offline_checks.sh
```

**Runtime data flow (verified by reading code, not assumed):**

1. `pyquotex`/`quotex_ws` pulls ticks from Quotex (`wss://ws2.qx.market` etc.) with the operator's SSID token (env `QX_TOKEN`, or auto-minted by cookie replay in `core/qx_session.py`).
2. `feed.QuotexFeed` aggregates ticks into per-asset/period streams (`_AssetStream`), closes candles (tick-close + timer-fallback), and at every end-of-candle runs the engine chain (15 module voters → blender/confluence → ML bridge → all-theory ensemble).
3. Every engine output passes the **emission boundary** `feed._run_eoc` → `core.signal_contract.apply_truth_layer` — the *Truth Layer* that converts any ineligible prediction into `NO_SIGNAL` + machine-readable `reason_code`, preserving the engine preference only as `research_*` fields.
4. Gated predictions broadcast over `/ws` to subscribed clients; `db.log_signal` + `signal_provenance` + `candle_settlement` freeze history; `core/signal_verifier` + settlement workers grade outcomes (CALL/PUT win/lose) without rewriting history.
5. The vanilla-JS frontend renders charts (LCharts), ticks, signal states, win-rate analytics, model panels.

**The product's central honesty fact (from the repo's own Truth Layer and reports):**
- Data source: reverse-engineered, **unauthorized** client (`DATA_SOURCE_UNAUTHORIZED`) — TLS spoofing, cookie replay, DevTools token extraction; a prior account block is self-documented.
- Measured real-OTC accuracy of the best available model: **49.92–50.11%** (coin flip). `production_ready=false` in the repo's own committed reports.
- No signed validation registry (`core/model_validation.json` absent) and no server-time sync on the default backend → the contract **fails closed**: every emitted frame today is `NO_SIGNAL` / `RESEARCH_ONLY`.

This is not a defect: it is the system's design working as directed. The redesign must make this state *legible* in the UI rather than hide it.

---

## 1. Findings

Severity counts: **1 Critical, 3 High, 9 Medium, 8 Low** (22 findings). Each lists file, problem, severity, root cause, recommended solution, regression risk.

### CRITICAL

#### C-1. Cron endpoint authentication is bypassable AND fails open
* **File:** `api/cron/refresh.py` (`_authorized()`, lines 59–72)
* **Problem:** Three stacked defects:
  1. **Bypass when configured:** `return request.headers.get("x-vercel-cron", "").strip() == "1"` — any client can send a spoofed `x-vercel-cron: 1` header and pass the gate **even when CRON_SECRET is set**. The header is client-controlled, not a Vercel-verified credential.
  2. **Fail-open when unset:** no `CRON_SECRET` in env → `_authorized()` returns `True` for everyone. The directive explicitly requires: *cron endpoints must reject requests when CRON_SECRET is missing*.
  3. **Secret in query string:** `?secret=…` is accepted; secrets in URLs leak into Vercel access logs, browser history, and any intermediary proxy.
* **Severity:** Critical — a public Vercel URL lets any third party trigger `QX_CRON_BUDGET` (up to 540 s) feed bursts against the operator's Quotex account, burning the daily-lived token and risking the account block the repo itself documents.
* **Root cause:** the gate was written to make local testing convenient; the spoofable header was added to let the Vercel scheduler through without pinning how Vercel actually authenticates.
* **Recommended solution:** (a) reject with 503 when `CRON_SECRET` unset (fail closed, same policy as `_check_admin_key`); (b) accept the secret only via `Authorization: Bearer` header (constant-time compare); (c) accept `x-vercel-cron` **only** together with the matching secret, never as an alternative to it; (d) add tests: spoofed header without secret → 401; unset secret → 503; correct Bearer → 200.
* **Regression risks:** Vercel Cron's own scheduler must send the header **and** the env must carry `CRON_SECRET`; `vercel.json` cron config and `DEPLOYMENT_VERCEL.md` must document the new required variable. Local curl invocations using `?secret=` break (intended — log-leaking pattern).

### HIGH

#### H-1. Frontend is a monolith with fragile, order-dependent CSS and ~60 mutable globals
* **Files:** `static/app.html` (1,158 lines, 261 `id=` hooks), `static/js/common.js` (5,337 lines), `static/js/*.js` (7 files, ~8,100 lines), `static/css/*` (5 files, ~2,700 lines).
* **Problem:**
  * One page hosts every feature; navigation is tab-switching in one DOM (not real pages).
  * `common.js` declares ~60 top-level mutable globals (`ws`, `chart`, `candleData`, `lastPrediction`, …) with cross-file mutation.
  * CSS correctness depends on **load order**: `app.html` documents that swapping `app-v3-panels.css` / `app-v3.css` breaks responsive layouts — a 2026-09 production incident ("phones had horizontal overflow") was fixed by reordering link tags. `app-responsive-fix.css` exists solely to load *last* so equal-specificity overrides win.
  * Duplicate-listener hazards are documented in-file (comment at common.js ~4500: a past bug registered another listener per `addEventListener` call).
* **Severity:** High — every UI bug fix risks cascade breaks; mobile regressions already happened twice (documented in CSS comments dated 2026-09-08 and 2026-09-14).
* **Root cause:** organic growth of a single-page app with no module boundaries, no bundler, no CSS scoping strategy.
* **Recommended solution:** the Phase 4 migration — React + TypeScript + Vite, centralized design tokens, module-scoped CSS; feature-by-feature, never big-bang. Old UI stays at `/legacy` until feature parity is verified.
* **Regression risks:** legacy UI must keep working unchanged while both live (risk: shared route clobbering — mitigated by distinct paths `/` (new) and `/legacy` (old)).

#### H-2. `server.py` is a 4,980-line monolith with ~75 routes
* **File:** `server.py` (also `feed.py` 7,329 lines, `db.py` 3,034 lines).
* **Problem:** one module holds route definitions, auth, admin, static serving, preview-gateway patching, demo banner injection, WS lifecycle, and business helpers. Import-time side effects (`_bootstrap_persisted_token`, `_report_auto_session`, `webbrowser` import) make the module un-importable in exotic environments without care. Route-level unit testing requires importing the whole world.
* **Severity:** High (maintainability), not a runtime defect — the app works and is unusually well-commented.
* **Root cause:** additive growth; past audits fixed behavior, not structure.
* **Recommended solution:** Phase 5 target structure (routes split by domain) — but ONLY as mechanical, import-preserving extractions, one route-group per commit, verified against the offline battery each time. The directive is right: *don't rewrite a working backend for fashion*.
* **Regression risks:** any import-order mistake re-breaks the serverless boot split (`api/index.py` depends on `QX_DISABLE_FEED` semantics); the WS `clients` dict and `broadcast()` are shared globals.

#### H-3. `user-scalable=no` + `maximum-scale` accessibility violation
* **File:** `static/app.html` line 4 (`viewport-fit=cover, user-scalable=no`).
* **Problem:** zooming is disabled — WCAG 1.4.4 failure; low-vision traders cannot magnify price text on a *trading* terminal. Also the Google Fonts `<link>` introduces a render-blocking third-party origin (BD latency, offline failure mode).
* **Severity:** High for this audience (accessibility of a money tool) though trivial to fix.
* **Root cause:** pinch-zoom was disabled to stop double-tap-zoom during chart interaction.
* **Recommended solution:** new UI: allow zoom, rely on `touch-action: manipulation` on interactive surfaces to suppress double-tap zoom; self-host fonts or use `font-display: swap` with system-font fallback stack.
* **Regression risks:** none meaningful; test double-tap on the chart canvas after the change.

#### H-4 (recounted below as deployment) → folded into M-6/M-7.

### MEDIUM

#### M-1. Bengali-only UI, hardcoded `lang="bn"`, mixed-script labels
* **Files:** `static/app.html`, all `static/js/*`.
* **Problem:** no i18n layer; Bengali labels are string literals in markup/JS; financial terms are English anyway (CALL/PUT/ATR). Non-Bengali users cannot use the app; string changes require code edits.
* **Severity:** Medium.
* **Root cause:** single-audience bootstrap.
* **Recommended solution:** new frontend: string dictionary (`bn` primary) with `en` fallback — a thin typed i18n module, not a framework; keeps bilingual expansion possible later without rewrites.

#### M-2. `scripts/` sprawl: 115 files mixing test batteries, research tools, evidence JSON, and dev utilities
* **Files:** `scripts/*` (~40 `test_*`/`verify_*`, ~60 tools, plus committed report JSONs).
* **Problem:** tests live outside any `tests/` tree and are not pytest-collectable as a suite; evidence JSON sits beside code; `run_offline_checks.sh` hard-codes the battery list (a new test file is invisible to CI unless listed).
* **Severity:** Medium (process, not runtime).
* **Root cause:** phase-based auditing appended script after script.
* **Recommended solution:** Phase 5/10: keep the runner as the source of truth (do not break the CI mirror), but add a manifest check that every `scripts/test_*.py` appears in `run_offline_checks.sh` — prevents silent test rot.
* **Regression risks:** moving files breaks the runner's relative paths and the CI workflow's install list; leave paths alone this engagement.

#### M-3. WS message handling in the frontend is stringly-typed with a single global `ws`
* **File:** `static/js/common.js` lines ~90, ~3487–3541, ~5036 (keepalive).
* **Problem:** one WebSocket for the app lifetime; keepalive = `{type:'status'}` every 15 s with a 45 s dead-connection force-close; no dedup, no backoff jitter, no server-aware subscription bookkeeping on the client (server-side `interested_cids` is the real subscription ledger). Navigating tabs re-enters `initApp` and must defensively clear intervals (comments admit re-entry stacking bugs).
* **Severity:** Medium — works, but brittle; reconnect storms after server restart are plausible (immediate reconnect, no jitter).
* **Recommended solution:** Phase 4 WS manager module: exponential backoff + jitter, dedup by message identity, explicit subscribe/unsubscribe bookkeeping, stale-data clock, heartbeat = the existing `{type:'status'}` poll, safe cleanup on route change.
* **Regression risks:** the server closes idle sockets (`_WS_IDLE_TIMEOUT`) — the new manager MUST keep sending keepalives at ≤ 15 s or every client will be dropped; keepalive interval and dead-connection threshold are protocol facts, not tunables.

#### M-4. `feed.py` staleness machinery is excellent but enormous and partially duplicated across two backends
* **Files:** `feed.py` (7,329 lines; `QuotexFeed`, `_AssetStream`, timer-close, stale re-arm, market-closed latch), `quotex_ws.py` (raw-WS alternative with time sync).
* **Problem:** two feed backends must be kept behaviorally in sync (the repo documents one latching bug weekend 2026-09-19 fixed only after users saw fabricated weekend candles). `_run_eoc` wrapper + `_apply_truth_gate` correctly enforce the boundary, but the same knowledge (freshness, market-closed) exists in multiple places.
* **Severity:** Medium.
* **Recommended solution:** Phase 5: do NOT refactor the feed engine this engagement (it is the system's heart and fully test-covered); document the invariants in NEW_ARCHITECTURE.md so future work has a contract to check.
* **Regression risks:** any feed change invalidates 1,087 offline checks that encode current semantics.

#### M-5. Analytics surface is oversized relative to the product's honest state
* **Files:** `server.py` routes: `/api/brain`, `/api/patterns`, `/api/theory-analysis`, `/api/quality-analysis`, `/api/psychology`, `/api/quotex-algo-detect`, `/api/otc-fingerprint`, `/api/time-patterns`, `/api/pair-deep-stats`, `/api/winrate`, `/api/target-gate`, …
* **Problem:** ~20 analytics endpoints, several overlapping (winrate vs stats vs theory-analysis all slice the same `signal_log`); the new UI would present research-grade outputs as first-class product pages, inviting over-interpretation of ~50% accuracy research data.
* **Severity:** Medium (product honesty, API surface bloat).
* **Recommended solution:** keep endpoints (legacy consumers exist), but the new UI presents them as *research analytics* under a clearly labeled section with sample-size and break-even context everywhere; API_CONTRACT.md marks which endpoints are measured vs research-grade.
* **Regression risks:** none if endpoints stay untouched.

#### M-6. Vercel mode degrades silently (WS dead, SQLite ephemeral, data only as fresh as the last burst)
* **Files:** `api/index.py`, `api/cron/refresh.py`, `vercel.json`, `supabase_sync.py`.
* **Problem:** in serverless mode the UI's WebSocket fails silently and REST polling takes over; SQLite is a per-instance cache; durable data lives in Supabase only as mirrored by bursts. A user can open the app and see a stale chart with no obvious indicator that the feed is not live (server-side `streaming-status` exists; the UI communicates it weakly).
* **Severity:** Medium — user-facing truthfulness of "is this data live?" (primary deployment is Railway, but the repo retains Vercel support and the user has flip-flopped).
* **Recommended solution:** new Overview/Terminal must surface `data_freshness` and connection state loudly (the honesty banner + freshness chips); deployment adapters documented in MIGRATION_PLAN.md.
* **Regression risks:** none; UI-only.

#### M-7. Deployment config duplication: three deployment paths, four story files
* **Files:** `vercel.json`, `railway.toml`, `Dockerfile`, `Procfile`, `run.sh`, `run_service.sh`, `DEPLOYMENT_VERCEL.md`, `DEPLOYMENT_V2.md`, `docs/DEPLOYMENT_RAILWAY.md`.
* **Problem:** Railway→Vercel→Railway flip-flopping left near-duplicate deployment stories; `vercel.json` `includeFiles` must list every new frontend asset path (the new Vite build output) or the serverless bundle silently misses static files (a class of bug already hit once per git history "include requirements.txt + api/** in both bundles").
* **Severity:** Medium.
* **Recommended solution:** MIGRATION_PLAN.md becomes the single source; vercel.json updated for the new build output; old stories kept but marked superseded.
* **Regression risks:** vercel build cache needs busting after includeFiles change.

#### M-8. `js/common.js` REST polling + WS duplication without a single data source of truth
* **File:** `static/js/common.js` (REST fallback polling documented at ~10 s cadence).
* **Problem:** chart state comes from WS frames and REST fallback simultaneously; both write the same globals; a server that answers both leads to racing duplicates (the code mitigates but the architecture invites it).
* **Severity:** Medium.
* **Recommended solution:** Phase 4: one typed client module with a single normalized store; WS is primary, REST is fallback by the same state machine — never both writing concurrently.
* **Regression risks:** server `/api/status` & `signals/latest` shapes must be captured exactly in the typed client (API_CONTRACT.md).

#### M-9. Secret-shaped strings can reach logs via `print` in several paths
* **Files:** `server.py` (`_AccessLogSecretRedact` exists for HTTP access logs — good), `feed.py`, `core/qx_session.py` (various prints).
* **Problem:** the access-log redaction filter is solid, but ad-hoc `print(...)` paths (feed errors, cron logs) can embed tokens/cookies when exceptions interpolate env content. Phase-J tests cover `/api/*` responses; ad-hoc stdout is less controlled.
* **Severity:** Medium.
* **Root cause:** print-first debugging culture, no structured logging.
* **Recommended solution:** Phase 5 (gradual): route new code through `logging` with the existing redaction filter pattern; do not mass-replace prints in the feed hot path this engagement (regression risk to 1,087 checks that assert on printed behavior in some batteries).
* **Regression risks:** some offline batteries capture stdout; changing prints can break them — verify after any change.

### LOW

#### L-1. Root-level module sprawl
`server.py`, `feed.py`, `db.py`, `alerts.py`, `supabase_sync.py`, `sitecustomize.py`, `quotex_ws.py` at root. Cosmetic; fixed implicitly by the Phase 5 target tree (do not physically move files this engagement — import-path breakage).

#### L-2. `sitecustomize.py` boot hacks
Root `sitecustomize.py` (24 lines) manipulates env at interpreter start. Works, documented; unusual but justified for the vendored pyquotex import quirks. Keep; document.

#### L-3. Vendored `pyquotex` (48 files) with local TLS-spoofing patches
Noted in requirements.txt comments. Legally/operationally the riskiest dependency in the repo (and the reason the Truth Layer fails closed). Keep as-is this engagement; the contract already treats it as unauthorized.

#### L-4. `Dockerfile`/`Procfile` exist but Railway uses `railway.toml` nixpacks path; Docker path is stale
Deployment docs diverge (see M-7). Low; documentation fix in MIGRATION_PLAN.md.

#### L-5. `lightweight-charts.js` is a 157 KB single-line vendored blob
No integrity hash pinned anywhere. Low: add a SHA comment or npm-pin the same version in the new frontend (4.1.3) — which is what we do (npm `lightweight-charts@4.1.3`) for feature parity.

#### L-6. `docs/` mixes normative and historical docs without an index
22 docs, no README index. Low; MASTER_AUDIT/NEW_ARCHITECTURE/MIGRATION_PLAN become the entry points, linking the rest.

#### L-7. Demo/gateway patch machinery (`_PREVIEW_PATCH_JS`, `_DEMO_BANNER_JS`) is string-embedded JS inside Python
Hard to lint/test (node --check doesn't see it). Low: superseded by the new frontend; keep for legacy route only.

#### L-8. Bengali text in Python docstrings/comments mixed with English
No action; documentation language is a feature, not a bug, for this team. Noted so future phases don't "fix" it away.

### Negative findings (checked, NOT broken — the audit's devil's-advocate duty)

The directive asked specifically; each was verified in code and most were already fixed in earlier phases (self-documented):

1. **Admin auth:** `_check_admin_key` is constant-time AND fail-closed (503 when ADMIN_KEY unset) — correct (server.py 229–249).
2. **WS origin validation:** present, deny-by-default on missing Origin, Railway-domain allowance when user list unset (4538–4587) — correct.
3. **WS input validation:** asset length, period whitelist, category/asset mismatch checks, message-size cap, idle timeout, max clients — all present (4589–4730).
4. **Rate limiting:** real, enforced, per-bucket with truthful headers (core/rate_limit.py, Phase-J) — correct.
5. **SQL injection:** all SQLite access parameterized; `f"…{asset}…"` interpolations found are into `print` strings, not SQL cursors — spot-checked `db.py`, `server.py` — no exploitable pattern found.
6. **Secrets in responses:** Phase-1 test battery asserts no secret-like keys survive the emission boundary; `_strip_secret_like` at the contract layer.
7. **Truth Layer integrity:** intact and adversarially tested (70-check battery). No bypass found; fail-closed on contract-module-absence too.
8. **Settlement immutability:** `INSERT OR IGNORE` on frozen keys + corrections table instead of rewriting history (`candle_corrections`) — the design preserves immutable history.
9. **CORS:** no wildcard middleware; the app serves same-origin — correct posture.
10. **Dead/unused code:** none material found; `sim_feed.py.DISABLED` is intentionally retained-disabled (documented), legacy `/otc.html` `/real.html` routes serve the legacy pages (kept for transition).

---

## 2. What must NOT change (locked invariants)

The redesign is allowed to touch everything EXCEPT these, which the audit confirms are load-bearing:

1. The Truth Layer contract (`core/signal_contract.py`) and its fail-closed defaults — **not env-overridable, keep**.
2. The WS protocol: client messages `{type: subscribe|pairs|status|signals}` and server frames `{pairs|snapshot|tick|eoc|stale|market_closed|error|status|signals|otc_pred}` with the documented field shapes (captured in API_CONTRACT.md).
3. The offline check battery (1,087 checks) and its runner — the regression gate.
4. Database schema (additive-only migration policy) and all persisted history.
5. The keepalive cadence (≤15 s) required by `_WS_IDLE_TIMEOUT`.
6. `QX_DISABLE_FEED` serverless boot split used by `api/index.py`.

## 3. Phase-0 exit criteria

All findings above are either (a) fixed in this engagement (C-1 cron gate — mandatory before any deploy; H-3 viewport), (b) explicitly scheduled in MIGRATION_PLAN.md (H-1, H-2, M-1..M-9), or (c) accepted with rationale (L-*). The baseline (1,087 checks green) is established; every subsequent phase must re-run it and stay green.
