# API CONTRACT — Binary Signals App

**Date:** 2026-11-27 · **Verified against:** running server (`server.py` @ redesign/v2-terminal) — every field below was read from code or a live response, not assumed.

Conventions: all timestamps UTC; wall-clock epochs are seconds (float ok); candle `time` = candle-open epoch seconds (the chart and DB key); pair ids use the broker suffix convention (`..._otc` for OTC); signal ids are hex UUIDs; direction ∈ {CALL, PUT, NO_SIGNAL}; reason codes are the 13 stable strings below.

---

## 1. REST endpoints (unchanged backend; consumed by the new SPA)

| Endpoint | Purpose | Notes |
|---|---|---|
| `GET /healthz` | liveness + feed health | `{"ok":true,"feed_connected":bool,"feed_task_alive":bool,...}` |
| `GET /api/status` | feed/client/pairs status | public |
| `GET /api/truth-layer` | Truth Layer state | `actionable_signals_possible:false` while source unauthorized + registry absent; policy/contract versions; CSE provenance |
| `GET /api/data-quality` | freshness/integrity snapshot | drives Settings diagnostics |
| `GET /api/time-sync` | broker time-sync state | TIMING_UNVERIFIED unless raw-WS backend synced |
| `GET /api/settlement-integrity` | settlement ledger health | |
| `GET /api/pairs[/{category}]` | pair lists | **entries are objects** `{asset, display, status, payout, locked}` (verified live); real list filtered to `status=="live"`, OTC never filtered |
| `GET /api/signals/latest` | latest signal rows | rows carry the full truth-contract fields below |
| `GET /api/signals/count` | counts (today) | used by Overview |
| `GET /api/history/{asset}/{period}` | closed candles | chart bootstrap |
| `GET /api/winrate?period=&days=` | verified win-rate analytics | includes breakeven context (server-side honesty fields) |
| `GET /api/prediction/models` | model registry | honest statuses; most fields null/unvalidated today |
| `GET /api/prediction/overview` | prediction summary | |
| `POST /api/auth/claim` / `POST /api/auth/verify` / `POST /api/auth/change-pin` | access PIN lifecycle | first claim gated by ADMIN_KEY or one-time setup code |
| Admin (`/api/debug`, `/api/db-download`, `/api/signals/clear`, `/api/admin/prune-pairs`, …) | **X-Admin-Key header, fail-closed 503 when unset** | |
| `GET /api/cron/refresh` (Vercel) | burst worker | **POST-C-1: `Authorization: Bearer $CRON_SECRET` only; 503 if unset; 401 otherwise** |

## 2. WebSocket protocol (`/ws`)

**Client → server** (JSON):

```jsonc
{"type":"subscribe","asset":"USDBDT_otc","period":60,"category":"otc"}   // periods: server-allowed set (5,15,30,60,120,300)
{"type":"pairs"}                                                        // request pair lists
{"type":"status"}                                                       // keepalive (MUST be sent ≤ every 15 s — server closes idle sockets)
{"type":"signals","asset":"ALL","period":60,"limit":100,"before_ctime":null}  // history page
```

**Server → client frames:**

| Frame | Key fields | Meaning |
|---|---|---|
| `pairs` | `real_pairs[], otc_pairs[], payout_floor_real/otc` (object entries) | pair lists (real filtered live-only) |
| `snapshot` | `asset, period, candles[], prediction` | full state after subscribe |
| `tick` | `asset, period, candle (running), prediction` | live tick / running-candle update; gated prediction arrives here |
| `eoc` | `asset, period, candles[], prediction:null, accuracy, signal, ctime` | candle closed (prediction gated → arrives via next tick) |
| `stale` | `asset, period` | stream stale (re-arm in progress) |
| `market_closed` | `asset, period, reason` | market latched closed |
| `status` | `connected, streams` | keepalive reply |
| `signals` | `signals[], asset, period, before_ctime` | history reply |
| `error` | `error` | validation/setup error string |
| `otc_pred` | ML T+1 diagnostic | **research-only** |

Only frames matching the client's active `(asset, period)` may mutate chart state (reducer discipline in the new SPA).

## 3. The canonical signal contract (every emitted frame / history row)

From `core/signal_contract.py::build_contract_fields` — all fields present on every emission-boundary output:

```
signal_id, trace_id, symbol,
truth_status            ∈ eligible | rejected | stale | error
reason_code             ∈ DATA_UNAVAILABLE, DATA_INVALID, DATA_STALE,
                          INSUFFICIENT_HISTORY, FEATURE_ERROR,
                          MODEL_UNAVAILABLE, MODEL_OUTPUT_INVALID,
                          MODEL_NOT_VALIDATED, BELOW_ELIGIBILITY_THRESHOLD,
                          TARGET_CANDLE_INVALID, TIMING_UNVERIFIED,
                          INTERNAL_ERROR, DATA_SOURCE_UNAUTHORIZED
eligibility_policy_version ("1.0.0"), contract_schema_version ("phase1_truth_v1")
created_at_utc / created_at_epoch
source_candle_close_time_utc / source_candle_close_epoch
target_candle_open_time_utc / target_candle_open_epoch        ← chart marker time
target_candle_close_time_utc
model_id, model_version, model_artifact_hash, feature_schema_version
data_source_id ("quotex_ws_unofficial"), data_source_authorization ("unauthorized")
data_freshness_status   ∈ fresh | stale | unknown
validation_status       ∈ validated | unvalidated
research_only           bool     (true for every frame today)
research_direction      CALL|PUT|null   (engine preference, diagnostic only)
research_confidence, research_strategy, research_reason
signal                  CALL|PUT|NO_SIGNAL (NO_SIGNAL for every frame today)
confidence              number|null     (null for every frame today — never fabricated)
strength, strategy, reasons[], actual, correct, ctime, category, regime
```

**Rules binding every consumer (frontend honors these in `toSignalView()`):**
1. `research_only:true` ⇒ render as RESEARCH_ONLY; never as an actionable CALL/PUT.
2. `confidence:null` ⇒ render "unavailable" — never a number.
3. A missing frame ⇒ DATA_UNAVAILABLE with reason text — not a neutral guess.
4. Markers use `target_candle_open_epoch` at the contract price — never the current candle.
5. History rows are immutable; grading comes only from the settlement layer (`correct` 1/0 or ungraded).

## 4. Error handling contract

- REST failures → typed `ApiError(status)`; UI renders honest error/empty states (no fake data, no endless spinners).
- WS reconnect → exponential backoff + jitter (0.8s→30s cap); resubscribe ledger replay; dedup window prevents double-render; 45s dead-connection force-close; 90s stale clock (contract freshness default).
- Rate limiting: 429 with `Retry-After` (buckets: auth 10/min, token 30/min, global 600/min per IP).
- Admin endpoints: 503 when `ADMIN_KEY` unset (fail-closed); cron: 503 when `CRON_SECRET` unset, 401 on bad Bearer.
