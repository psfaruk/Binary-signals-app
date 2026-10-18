# PHASE E — MARKET DATA QUALITY AND CANDLE RELIABILITY

> Executed 2026-10 on `rebuild/phase-c-i` (after CP-PCI-2). New module
> `core/data_quality.py` (E1 provenance + E3 gates), new endpoint
> `GET /api/data-quality` (E4), new adversarial suite
> `scripts/test_phase_e_data_quality.py` (43 checks — E2 candle
> construction through the REAL feed close path). No broker connection;
> no credentials; every claim below was executed, not assumed.

## E2 — Candle construction (tests A1–A15)

All construction behavior is driven through the REAL close path
(`QuotexFeed._close_running_and_start_new`) or the real identity math:

| Requirement | Check | Result |
|---|---|---|
| Correct period bucketing | whole-candle consistency + parity between `feed._floor_to_period` and `lifecycle.period_start_for` | PASS |
| Exact-boundary behavior | tick at T0+60 belongs to the NEW candle (right-edge), T0+59.9 to the old one | PASS |
| Tick ordering / out-of-order arrivals | integrity validator rejects + counts | PASS |
| Duplicate tick handling | same (ts, price) late tick → exactly ONE correction row (UNIQUE idempotency) | PASS |
| Late tick BEFORE vs AFTER finalization | open-state incorporation vs correction-ledger-only with frozen OHLC immutable (E2E through the real close path) | PASS |
| Missing tick intervals | `count_series_gaps` detects gaps; quality gate rejects gapped prediction windows | PASS |
| Reconnect recovery | `ingest_replace` deep-seed replay recovers a clean engine state | PASS |
| Duplicate candles after restart | **found and fixed a REAL production bug — see below** | PASS (after fix) |
| Timezone / DST edge cases | epoch bucketing is UTC arithmetic — verified invariant across the 2026 US fall-back and EU spring-forward transitions | PASS |
| Settlement / correction consistency | frozen OHLC byte-identical after late ticks/conflicts (Phase B behavior re-verified in this suite) | PASS |

### Real defect found by the adversarial suite (fixed)

**`NextCandleEngine.ingest` dedup was dead code.** The check
`t <= self.last_time` read a field that was initialized to `-1` and
NEVER written anywhere in the module — so duplicate AND out-of-order
candles were fully processed: pattern stats, streaks and training rows
double-counted, and the same prediction could be graded twice. In the
live feed this corrupts engine state whenever history is re-seeded
(watchdog restart replay, stream re-subscription). Test A13 exposed it
(the duplicate returned a payload with `streak: 71` and grown stats
instead of `None`). The fix is the one documented line: set
`self.last_time = t` inside the ingest lock (core/next_candle.py, tagged
`PHASE-E FIX`). Post-fix verification:

* Phase E 43/43 green; full regression 341/341 green.
* Single-pass replay numbers are IDENTICAL to the pre-fix run
  (BTCUSDT cse_v1 test 0.5189 [0.4973, 0.5404] before and after) — the
  fix changes only duplicate/out-of-order handling, never the
  forward-only replay the evaluation uses.
* `ingest_replace`'s documented contract ("plain ingest would skip
  overlapping timestamps") now actually holds.

## E1 — Data-source provenance (`core/data_quality.py`)

Every dataset/observation class the app can touch has an explicit
`DatasetProvenance` record: source identity, authorization status,
classification, assets/timeframe, observation window, receipt time,
clock source + sync state, quality flags, exclusion reasons.

| Dataset | Classification | Authorization |
|---|---|---|
| Binance public 1m (repo-committed) | `authorized_historical_public_exchange` | authorized (real market; NOT OTC) |
| Synthetic fixtures | `synthetic_fixture` | authorized (never market evidence) |
| Live Quotex channels (both backends) | `unauthorized_reverse_engineered_channel` | **unauthorized — never fabricated** |
| Captured past emissions | `observational_capture_from_unauthorized_channel` | unauthorized + `ground_truth_from_unauthorized_channel` flag |

Unknown authorization stays `unknown` (fail-closed label). No source is
ever marked trusted without evidence (B3/B5 tests).

## E3 — Staleness and quality gates

`QualityPolicy` (documented, configurable, `describe_policy()` embedded
in every diagnostics response). Defaults align with the app's EXISTING
documented gates rather than inventing new numbers: `stale_secs=90`
(= feed.STALE_SECS), `min_closed_candles=64` (= the engine's live gate),
`max_clock_uncertainty_secs=2.0` (this module's protocol constant — our
threshold, not a claimed broker tolerance), `max_missing_intervals=0`.

`assess()` rejects/downgrades: stale data, unknown source, insufficient
observations, malformed OHLC, clock uncertainty beyond threshold,
missing candle intervals, contradictory source data (unresolved
conflicts). Each has a dedicated adversarial test (C4–C11); healthy
input passes (C3); custom policies are honored (C13). **The reason
vocabulary contains no enabling/pass verdict — the gates can only
reject, never increase signal frequency (C12, by construction).**

## E4 — Operational diagnostics (`GET /api/data-quality`)

Public, read-only, secret-free (D7 sweep test). Reports: the documented
policy; the honest source classification (constant `unauthorized`
today); the timing-verification state (sync status, offset,
uncertainty, sample age from the provider); per active stream — last
observation UTC, last-receipt age, candle open time, tick counts,
market-closed latch, latest settled candle, missing-interval count over
the last 64 candles, and the full quality assessment; the last
processing error (shape only, ≤200 chars); and **signals suppressed by
gate** — wired to the `signal_provenance` ledger's reason codes (the
Truth Layer's own record of gated emissions; a seeded DATA_STALE row
shows up in the count — test D8).

No real-time OTC feed reliability is claimed anywhere: the source is
unauthorized and the diagnostics say so on every response.

## E5 — Acceptance gate

| Criterion | Status |
|---|---|
| Synthetic + authorized historical fixtures demonstrate expected behavior | PASS — E2 battery uses synthetic ticks through the real path; the Binance datasets carry provenance records |
| No real-time OTC feed reliability claim without a verifiable source | PASS — constant `unauthorized` labeling, no such claim made |
| Duplicate candles after restart behave correctly | PASS **after fixing a real engine defect** (documented above) |
| Gates documented, configurable, reject-only | PASS |
| Diagnostics read-only + secret-free | PASS |

**Phase E verdict: PASS WITH LIMITATIONS** — the limitation is the same
one every phase carries: the only live data source is unauthorized, so
all live-side diagnostics describe an UNVERIFIED data channel; nothing
here is evidence of OTC feed reliability.

## Commands + results (recorded)

```
python3 scripts/test_phase_e_data_quality.py -> 43 passed, 0 failed, exit 0
Regression after the engine dedup fix:
  70/70 + 25/25 + 54/54 + 93/93 + 38/38 (+ JS 59/59)  all exit 0
clean_eval single-asset spot-check: identical pre/post-fix metrics
```
