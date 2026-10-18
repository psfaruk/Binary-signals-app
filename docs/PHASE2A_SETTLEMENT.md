# PHASE-2A — Settlement Lifecycle + Time Provider

> Rebuilt 2026-10 (Phase B) on top of the Phase-A Truth Layer
> (CP-P1-1). Modules: `core/candle_lifecycle.py`,
> `core/time_provider.py`; integration in `feed.py`, `db.py`,
> `quotex_ws.py`, `server.py`, `core/retention.py`.

## Governing rules (fail-closed)

1. **Finalized OHLC is immutable.** A late tick, a duplicate close, a
   watchdog replay — none of them can edit a finalized candle. Evidence
   that arrives late is appended to a **correction ledger**
   (`candle_corrections`) with the original values preserved verbatim.
2. **The production ledger is never touched by lifecycle events.**
   `signal_log` (the win-rate ledger written by the Phase-0 grader) and
   the Phase-A `signal_provenance` research ledger are never modified
   by corrections; the research **settlement** row records its outcome
   from the FROZEN values only.
3. **Local time is never labeled as broker time; sync evidence expires
   on reconnect; a connection or a previously-set boolean is never
   accepted as synchronization.**

## Candle identity

```
identity = (asset, period, period_start)
period_start = floor(ts / period) * period      [epoch seconds]
```

Stable across restarts (derived only from tick timestamp + stream
period). A tick **exactly at a period boundary belongs to the NEW
candle** (right-edge), matching `feed._floor_to_period`.

## Lifecycle states (`candle_settlement.state`)

| State | Meaning | Written by |
|---|---|---|
| `open` | running candle, ticks accumulating | (in-memory only, no row) |
| `pending_finalization` | close initiated; crash window — OHLC not yet frozen, row is recoverable | `begin_finalization` (top of the close path) |
| `finalized` | OHLC **frozen** (immutable from here on) | `finalize` |
| `settled` | research outcome recorded from frozen values + the `signal_provenance` emission row | `settle` |

Allowed transitions: `(none) → pending → finalized → settled`;
`settled → settled` only as an idempotent retry (attempts counted,
outcome columns never rewritten). A second finalization with the SAME
OHLC is an idempotent no-op; with DIFFERENT OHLC it is a **conflict**:
rejected + audited into `candle_corrections`
(reason `conflicting_finalization`), frozen row untouched.

## Late-tick policy

| Candle state | Late tick handling |
|---|---|
| `open` / `pending` / unknown | NOT late for the lifecycle — the feed's live path incorporates it normally (no correction row). |
| `finalized` / `settled` | **Correction ledger only.** Row records: original OHLC (verbatim), implied corrected OHLC, original vs corrected outcome, `outcome_flipped` flag, tick timestamp + price, UTC stamp. Idempotent on duplicate ticks (UNIQUE on asset+period+period_start+reason+tick_ts+tick_price). |

The Phase-0 "A6" post-scoring patch (in-place mutation of closed-candle
OHLC after grading — RC-06) is **removed**; `feed._note_late_ticks`
records corrections instead.

## Research vs production ledgers

* **Research ledger**: `signal_provenance` (Phase A emissions) joined
  at settlement time by `target_candle_open_epoch == period_start`.
  The settlement row records `research_direction`, `outcome` (up/down/
  flat from the FROZEN close), `research_correct`, and the joined
  `signal_id`.
* **Production ledger**: `signal_log` — written only by the baseline
  grader for actionable CALL/PUT predictions. Corrections NEVER touch
  it. `production_accuracy` on the settlement row is an audit COPY of
  the grader's return value, never a source of truth.

## Time provider (`core/time_provider.py`)

| Provider | Backend | sync behavior |
|---|---|---|
| `LocalClockProvider` | pyquotex path (no server-time mechanism) | UNVERIFIED forever, `local_only: true`, `now()` = device UTC — never presented as broker time. |
| `SourceTimeProvider` | raw-WS timesync frames (the only measured source) | Evidence-based, see below. |

**SourceTimeProvider evidence policy** (all fail-closed):

* `SYNCED` requires **≥ 3 accepted samples** (a median needs 3 points;
  1 sample is an anecdote), **fresh** within the 120 s staleness window
  (Quotex sends timesync every ~5–30 s; 120 s silence = 4–24× the
  expected cadence = expired → `STALE`, which the gate treats as
  unverified).
* **Reconnect invalidation**: every (re)connection clears ALL evidence;
  SYNCED must be re-established by new measured samples.
* **Offset jump > 5 s** between consecutive accepted samples resets the
  evidence (re-verify from scratch; never blend the jump into the
  estimate). Samples implying |offset| > 1 day are **malformed**
  (rejected), not jumps.
* **Malformed samples rejected + counted** (12 kinds): None, bool,
  non-numeric, NaN, ±inf, ≤ 0, absurd offset. Out-of-order and exact
  duplicate server timestamps are rejected + counted separately.
* Offset estimate = **median** of per-sample offsets
  (`server_ts − local_wall_at_receipt`); uncertainty = **max deviation
  from the median** (conservative).
* **Monotonic elapsed time**: staleness/freshness math uses
  `time.monotonic()` exclusively — wall-clock steps (NTP, suspend/
  resume) cannot corrupt freshness verdicts. Wall clocks are used only
  for the offset estimate itself and human-readable UTC labels.
* `now()` = device clock + offset **only while SYNCED**; otherwise the
  device clock alone (no evidence → no claim).

**Timing sync ≠ source authorization.** A SYNCED provider only means
the broker clock is measurable on the raw-WS backend — an UNAUTHORIZED
data source. The Phase-1 Truth Layer still gates every emission on
authorization, validation registry and timing; synced-but-unauthorized
remains NO_SIGNAL / DATA_SOURCE_UNAUTHORIZED with research fields
preserved (test-verified).

## Gate wiring (Phase A regression guarantee)

`feed._timing_verified()` consults the provider ONLY. The Phase-A
boolean latch (`client._server_time_synced`) is **superseded**: it
remains as an attribute for backward compatibility but is NOT read by
the gate — a previously-set boolean is not evidence (directive).
`quotex_ws.py` feeds measured samples to the provider in its timesync
handler and calls `on_reconnect()` on every (re)connection.
`feed._srv_now()` applies the offset only while SYNCED (the old
single-sample trust is removed).

## Schema (additive, idempotent — no existing table altered)

* `candle_settlement` — one row per candle identity: state, frozen
  OHLC, tick count, finalize source + UTC stamps, research settlement
  columns (signal_id, research_direction, research_strategy, outcome,
  research_correct, production_accuracy copy, settled UTC),
  `settle_attempts` / `conflict_count` / `correction_count`.
  UNIQUE(asset, period, period_start).
* `candle_corrections` — the additive correction ledger: reason
  (`late_tick` | `conflicting_finalization`), original + implied
  corrected OHLC (JSON), late tick ts/price, original vs corrected
  outcome, `outcome_flipped`, `stale_replay`, ledger label, UTC stamps.
  UNIQUE(asset, period, period_start, reason, late_tick_epoch,
  late_tick_price).

INSERT column parity is **structural** (`_CS_COLUMNS` / `_CC_COLUMNS`
drive both schema and SQL). State transitions are conditional
`UPDATE … WHERE state=…` inside one `_write_cursor` transaction
(commit-on-success / rollback-on-exception).

## Concurrency (honest statement)

SQLite is in WAL mode with `busy_timeout=10 s`. Writers: the **single
server process** (uvicorn event loop + `asyncio.to_thread` helpers +
retention daemon thread + local training daemon thread). Conditional
UPDATEs make stale writers unable to move a state backwards, and the
short transaction windows + WAL handle thread contention.
**Cross-process writers are NOT supported**: the row-ops' read-decide-
write pattern is safe within one process; two independent processes
writing the same candle concurrently could interleave in ways SQLite
alone does not serialize beyond the final conditional UPDATE. This is
documented, not hidden — a multi-writer architecture (advisory locks /
queue / single-writer service) would be a new process architecture and
is explicitly out of scope (stop condition honored by NOT building it).

## Retention

`candle_settlement` and `candle_corrections` get the SAME count-based
policy as `signal_log` / `signal_provenance` (newest 200 rows per
asset+period, ordered by `period_start` / `created_at_epoch`). They are
NEVER added to the 60-minute time purge `_POLICY` — the audit trail
must outlive a candle-close by hours; the count prune keeps storage
bounded and aligned with the audit window of the ledgers it joins.

## Diagnostic endpoints

* `GET /api/settlement-integrity` — lifecycle version, state counts,
  correction aggregates (late ticks, flips, conflicts), binding
  policies, live DB counts.
* `GET /api/time-sync` — active backend, provider describe (sync
  status, offset, uncertainty, staleness, sample/rejection/jump/
  reconnect counters, last sync UTC), `timing_verified_for_active_
  backend` (False for pyquotex even if leftover raw-WS samples exist),
  and the explicit authorization caveat.

## Tests

* `scripts/test_phase2_settlement.py` — 54 checks: identity/boundary
  math, full lifecycle, duplicate close/ticks, late before/after,
  conflicts, subprocess restart recovery (pending AND
  finalized-not-settled), ledger separation, retention safety, the
  E2E late-flip acceptance scenario through the REAL feed close path,
  and the two HTTP endpoints (TestClient, no lifespan → no broker).
* `scripts/test_phase2_time_provider.py` — 40 checks: local-clock
  honesty, evidence thresholds, staleness, reconnect invalidation,
  12-kind malformed battery, out-of-order/duplicate counting, offset
  jumps, wall-clock-step immunity (monotonic), `now()` honesty,
  pyquotex-like gate behavior, synced-but-unauthorized contract
  integration, describe completeness.

Temp DBs only; no network; no broker; no credentials.

## Known limitations

* `settle_pending` recovery is invoked opportunistically after each
  close (and directly by tests); there is no background sweeper thread.
* The correction ledger records the LATE EVIDENCE but does not feed it
  back into engine context — downstream consumers (Phase C clean-eval)
  decide how to use corrections. This is by design (no silent
  rewriting), not an oversight.
* `_srv_now()` no longer trusts a single timesync sample (Phase-A
  behavior). While UNSYNCED, candle-boundary timing runs on the local
  clock — an honest degradation, matching the pre-B1 baseline.
