# PHASE-1 TRUTH LAYER — Signal Contract Documentation

> Rebuilt 2026-10 (Phase A) from the single surviving Phase-1 source file
> (`core/signal_contract.py`, restored byte-exact from the verified
> snapshot — raw sha256 `5cdacf9479d9d0b89be7af18cfdfd846b4172f23e983e20f80bc1b44fce6840e`,
> stripped sha256 `aa20ed7705dbe2b7cc96bd5e74b9db5464fc25d5c16ae2d42aac2035116666b3`,
> 715 lines) plus fresh integration into the baseline. See
> `docs/recovery/RECOVERY_STATUS.md` for the loss narrative.

## Governing rule (fail-closed, not env-overridable)

```
NO AUTHORIZED DATA SOURCE + NO VALIDATED MODEL + NO VERIFIED TIMING
= NO ACTIONABLE DIRECTIONAL SIGNAL
```

Every prediction that leaves the emission boundary
(`QuotexFeed._run_eoc` wrapper in `feed.py`) passes through
`core.signal_contract.apply_truth_layer`. A prediction that fails ANY
requirement becomes `NO_SIGNAL` with a stable machine-readable
`reason_code`; the engine's diagnostic preference is preserved as
`research_*` fields (research-only: never rendered as an actionable
signal, never graded into the win-rate ledger).

There is **no environment variable** that flips authorization, the
registry, or the timing gate. The only path to actionable signals is an
authorized data source **plus** a signed validation registry entry
(`core/model_validation.json`) — neither exists today.

## Labels

| Label | Meaning |
|---|---|
| `CALL` | eligible upward/green prediction |
| `PUT` | eligible downward/red prediction |
| `NO_SIGNAL` | no actionable directional prediction |
| `STALE` | internal/UI data-freshness state (NOT a prediction) |
| `ERROR` | internal processing failure (NOT a prediction) |

## The 13 reason codes (stable, machine-readable)

| Code | Trigger |
|---|---|
| `DATA_UNAVAILABLE` | no prediction/data available at all |
| `DATA_INVALID` | required candle data missing/malformed |
| `DATA_STALE` | feed freshness beyond documented threshold (90 s) |
| `INSUFFICIENT_HISTORY` | fewer closed candles than the engine minimum (64) |
| `FEATURE_ERROR` | feature generation failed |
| `MODEL_UNAVAILABLE` | model artifact cannot be loaded |
| `MODEL_OUTPUT_INVALID` | non-finite / out-of-range / schema-mismatch |
| `MODEL_NOT_VALIDATED` | no valid OOS evidence in the registry |
| `BELOW_ELIGIBILITY_THRESHOLD` | policy threshold not met / engine abstained |
| `TARGET_CANDLE_INVALID` | target candle identity/time invalid |
| `TIMING_UNVERIFIED` | broker server-time sync not established |
| `INTERNAL_ERROR` | unexpected internal failure (fail-closed) |
| `DATA_SOURCE_UNAUTHORIZED` | the live channel is an unauthorized client (Phase 0 RC-03) |

Checks run in a FIXED order (data → model output → target → timing →
source → registry) and the FIRST failure's reason code is reported.

## Current fail-closed defaults (Phase 0 evidence, not new measurements)

* `data_source_authorization = "unauthorized"` — the repo's own docs
  describe TLS-spoofing / cookie-replay / DevTools-token access; a prior
  account block is self-documented.
* No validation registry file exists → nothing is validated (measured
  real-OTC accuracy 49.92–50.11%, `production_ready=false` in the
  repo's own committed reports).
* Timing verification only exists on the raw-WS backend
  (`quotex_ws._server_time_synced`, latched on the first valid broker
  timesync frame — added in Phase A); the production default (pyquotex,
  `QX_USE_RAW_WS=0`) has no timesync → `TIMING_UNVERIFIED`.

## Integration points (Phase A)

| Boundary | Mechanism |
|---|---|
| `feed._run_eoc` wrapper | ALL dict-producing return paths of the engine chain are gated — including the CSE early-exit (`return _nc_result`), the NEUTRAL return, and the legacy/`fade_default` CALL/PUT return. `None` returns (dead market, zero-tick candle, cooldown) are non-emissions and stay `None`. |
| Engine exception | `NO_SIGNAL` / `INTERNAL_ERROR` — a crash can never emit CALL/PUT. |
| Gate-module exception/unavailability | `NO_SIGNAL` / `INTERNAL_ERROR` fallback dict — the boundary can never fail OPEN. |
| `quotex_ws.py` | `_server_time_synced` latch (honest timing evidence source). |
| `db.py` | `signal_provenance` table (additive, idempotent; frozen-first-write on `(asset, period, target_candle_open_epoch)`). One row per emitted prediction. `signal_log` schema and INSERTs unchanged. |
| `core/retention.py` | `signal_provenance` gets the SAME count-based 200-rows-per-pair policy as `signal_log` — never the 60-minute time purge (audit trail must survive for settlement/correction audits). |
| `server.py` | `GET /api/truth-layer` (public, secret-free policy snapshot); REST rows carry the same truth fields as WS payloads; `/api/live-prediction` labels the running-candle diagnostic research-only. |
| `static/js/signal_states.js` | PURE mapping module (contract state → UI state) — NO_SIGNAL renders "⛔ NO SIGNAL" + reason; unmeasured confidence renders "—" (never 0/50). |
| `static/js/common.js` | Honest rendering: reason badge, honest confidence, research-only tag on the live-candle panel, honest countdown (broker-anchored, "≈") and device-UTC clock (no longer labeled "Broker UTC"). |

## Validation registry format (`core/model_validation.json`)

Absent file / malformed file → empty registry → nothing is validated
(fail-closed). The ONLY path to actionable signals:

```json
{
  "registry_version": "1.0",
  "approved": [
    {
      "model_id": "cse_v1",
      "artifact_sha256": "<sha256 of the artifact>",
      "asset_domain": "otc",
      "feature_schema_version": "cse_v1_35feat",
      "oos_evidence": {
        "method": "<e.g. walk_forward_chronological>",
        "balanced_accuracy": 0.0,
        "n_predictions": 0,
        "report_path": "<path to the committed OOS report>"
      }
    }
  ]
}
```

An entry matches only when the artifact hash, feature schema AND asset
domain (must cover the live domain) all agree, and the OOS evidence
documents a validation method. Evidence thresholds live in the registry
entry, NOT hardcoded in the contract. **An empty/absent registry is the
correct current state — no fake entries, ever.**

## Defense in depth

* `_strip_secret_like`: credential-shaped keys are stripped from every
  truth-layer output — a misbehaving engine can never leak a
  token/cookie through a serialized signal response.
* Contract fields are whitelisted; unknown engine fields pass through
  the base dict but provenance columns are whitelist-only.

## Known labeling caveat (documented, byte-exact restore)

The restored contract names the source candle boundary fields
`source_candle_close_time_utc` / `source_candle_close_epoch`, but the
value carried is the source candle's **"time" key (its open boundary)**
as used across this codebase (`candle["time"]` = candle open time; see
`feed.py: expected_target = closed[-1]["time"] + period`). The
target-candle relationship is CORRECT (`target_open == source.time +
period` — the check that matters for gating); only the field NAME is
imprecise. The file was restored byte-exact from the verified snapshot
(hash chain intact), so the naming is preserved and documented here
rather than edited.

## Tests

* `scripts/test_phase1_truth_layer.py` — contract-level + feed E2E +
  provenance persistence + REST/WS parity + gate-bypass attempts
  (plain `py`, no network, temp DB only).
* `scripts/test_phase1_http.py` — real FastAPI app through
  `fastapi.testclient` (NO lifespan → the feed task never starts →
  zero broker contact): `/api/truth-layer` honest snapshot,
  `/api/signals/latest` truth-field parity incl. the no-stream
  fallback row (`DATA_UNAVAILABLE`, `confidence: null`),
  `/api/live-prediction` research-only truth block, mapping module
  served + loaded before `common.js`.
* `scripts/test_phase1_signal_states.cjs` — pure JS mapping tests
  (`node`).
