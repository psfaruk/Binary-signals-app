# PHASE H — PAPER / SHADOW TESTING (REPLAY-BASED)

> Executed 2026-10 on `rebuild/phase-c-i` (after CP-PCI-5). New ledger:
> `shadow_predictions` (additive, idempotent migration in db.py with
> `shadow_record` / `shadow_settle` / `shadow_append_correction` /
> `shadow_counts`; count-based retention, never time-purged). Runner:
> `scripts/shadow_replay.py`. Tests:
> `scripts/test_phase_h_shadow.py` (28/28). **No broker orders. No
> broker contact. No credentials.**

## Why replay (honest mode statement)

Genuine live shadow recording requires a **clearly authorized data
source**; none exists (the vendored pyquotex / raw-WS channels are
unauthorized reverse-engineered access — the standing blocker since
Phase 0). Per the directive, this phase therefore runs
**replay-based evaluation and labels every record `mode='replay'`** —
never `live_shadow`. The `live_shadow` mode is structurally supported
by the ledger (test A4) for the day an authorized provider is
integrated; it cannot be emitted by today's runner.

## The shadow ledger (production-ready recording path)

Every prediction — including NO_SIGNAL abstentions — is recorded with
the directive's full field set:

| Field | Source |
|---|---|
| prediction_id | deterministic `mode/asset/period/target` (UNIQUE → idempotent) |
| mode | `replay` (today) / `live_shadow` (authorized source only) |
| asset / period / candle identity | `asset:period:period_start` |
| prediction cutoff | target candle open epoch (evidence rule = Phase C) |
| data provenance | dataset id + sha256 + classification, stamped per row |
| input-data quality status | the Phase-E gate verdict evaluated AT PREDICTION TIME |
| signal | the gated signal — **NO_SIGNAL on every row under current policy** |
| research direction | the engine's diagnostic preference (clearly labeled) |
| strategy / model / feature versions | next_candle_v1 / cse_v1_35feat |
| confidence | internal score, `confidence_kind='internal_score'` (calibration NOT established — Phase F) |
| settlement status | pending → settled (one-way conditional UPDATE) |
| outcome | GREEN / RED / FLAT once the candle is observable |
| correction history | append-only JSON array |
| error / exclusion reason | evaluator reasons |

Ledger integrity is test-verified: idempotent record (A1–A3), one-way
settlement with outcome columns never rewritten (A6–A8), append-only
correction history (A10), invalid modes/outcomes rejected (A3/A9),
aggregates (A11), count-based retention (never the 60-minute purge).

## Replay run (recorded)

2-asset run (BTCUSDT + ETHUSDT, authorized Binance 1m, engine replay
at live parity): **17,996 rows recorded, 17,996 settled** —
100% NO_SIGNAL (the honest gated state), research-direction diagnostic
accuracy 51.6% (labeled RESEARCH DIAGNOSTIC ONLY — not an
actionable-signal accuracy), FLAT outcomes recorded (never silently
graded), `outcome_correct` verified against the documented convention
(CALL↔GREEN / PUT↔RED, tests B9–B13).

## No-order guarantee

The shadow path contains no order-submission code and no broker
contact (tests C1–C2); the app's route inventory has zero
trade/order/position/execution endpoints (Phase G test G3-12). Shadow
testing in this repository can never place a trade.

## Verdict

**PHASE H: PASS WITH LIMITATIONS** — the limitation is definitional:
without an authorized live source this is replay evidence, not live
shadow evidence, and it is labeled as such everywhere (report, ledger
rows, mode field).

## Commands + results (recorded)

```
python3 scripts/shadow_replay.py --assets BTCUSDT,ETHUSDT
    -> 17,996 recorded / 17,996 settled / exit 0
python3 scripts/test_phase_h_shadow.py   -> 28 passed, 0 failed, exit 0
```
