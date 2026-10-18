# PHASE G — FRONTEND, API, AND END-TO-END VALIDATION

> Executed 2026-10 on `rebuild/phase-c-i` (after CP-PCI-4). API battery:
> `scripts/test_phase_g_api.py` (39 checks through the real FastAPI
> app, no lifespan → zero broker contact). Browser E2E: REAL Chromium
> (agent-browser) against a REAL uvicorn server (temp DB, NO
> credentials — the feed returns before any client creation, verified
> in feed.py `_connect`: `if not env_token: return False`), plus VLM
> visual verification of screenshots. No broker contact; no
> credentials; no trades.

## G3 — API tests (39/39 green)

| Area | Verified |
|---|---|
| Health | `/healthz` 200, JSON object, feed-task-alive flag |
| Signal endpoint | `/api/signals/latest` no-stream fallback rows are NON-directional (`PENDING`) with `reason_code=DATA_UNAVAILABLE`, `research_only=true`, `data_source_authorization="unauthorized"`, `confidence=null` (never fabricated 0/50) |
| Truth Layer diagnostics | `actionable_signals_possible=false`, authorization `unauthorized`, full reason-code vocabulary exposed |
| Time-sync diagnostics | valid sync state, backend identity labeled, pyquotex honestly never timing-verified |
| Settlement-integrity diagnostics | lifecycle snapshot present |
| Data-quality diagnostics (Phase E) | honest unauthorized source label |
| Error/unavailable states | unknown asset → empty candles/None prediction, no crash |
| Invalid payloads | malformed JSON / non-numeric period / non-numeric limit → handled, no 500s |
| Empty database | all read endpoints respond; DB verified genuinely empty (nothing fabricated) |
| Database errors | broken DB path → honest error status; **truth-layer stays fail-closed even under DB failure** |
| Authorization | `/api/db-export`, `/api/signals/clear`, `/api/debug` denied without credentials (Phase D gates) |
| No trade endpoints | route inventory contains ZERO trade/order/position/execution/bet endpoints |

## G4 — Browser end-to-end (REAL browser, REAL server)

Server: `uvicorn server:app` on a temp DB with NO credentials (the
tokenless `_connect` returns before any client is created — no broker
contact). Browser: Chromium via agent-browser; states verified from
the accessibility tree + screenshots + in-page `fetch` checks.

| Scenario | Result |
|---|---|
| First load | full render ("Binary Signal — Aurora Terminal"), nav, all panels; **zero page errors** |
| Loading state | chart shows "চার্ট লোড হচ্ছে…" (loading); future-candle panels "সংগ্রহ চলছে…" (collecting); fingerprint panel waiting |
| NO_SIGNAL / no-data state | signal panel "➖ NEUTRAL", SCORE 0, STRENGTH/REGIME/PRICE/Payout "—", "ক্যান্ডেল শুরুর অপেক্ষায়…" (waiting for candle), "টিকের অপেক্ষায়…" (waiting for ticks) — **no fabricated CALL/PUT anywhere** |
| Unauthorized data | in-page `/api/truth-layer` fetch returns `actionable_signals_possible=false`; research-only labels attached (Phase D) |
| Empty history | chart area empty, no crash, honest placeholders |
| Server error | server killed mid-session → page stays intact, no unhandled errors surfaced |
| Page refresh & recovery | reload after server RESTART → full recovery, title stable, zero errors |
| Responsive mobile | 390×844 viewport renders with the same honest states (30 "—"/NEUTRAL markers) |
| Visual verification | VLM inspection of the full-page screenshot: fully rendered, professional dark dashboard, placeholder/waiting states with dashes and NEUTRAL — no fabricated predictions |

**Untested-in-browser scenarios (documented honestly):** valid live
prediction display, new-candle event, reconnect-with-data, and
historical-correction display all require a live/authorized data feed
(or fixture injection into the server process, which has no such
endpoint). These flows are verified at the MODULE and API level
instead (Phase A 70-check suite incl. E2E through the real feed path;
Phase B 54-check settlement suite incl. late-flip + restart; Phase E
43-check candle-construction battery). No browser E2E claim is made
for them.

## G1/G5 — UI states & accessibility

* The full state vocabulary renders through the pure mapping module
  (`signal_states.js`, 59 JS checks): CALL/PUT/NO_SIGNAL, missing
  confidence → "—", stale demotion, reason badges.
* Honest countdown/clock: device clock labeled "🖥 UTC" (NOT "Broker
  UTC" — Phase A fix, re-verified in the E2E text extraction);
  countdown "--:--" until real data exists.
* p_green displayed as "ইঞ্জিন স্কোর" (engine score — Phase F4).
* Historical-pattern probability keeps its "not a guarantee"
  disclaimer; no "probability of being correct" phrasing anywhere.
* Color-independent state indicators: NO_SIGNAL renders with a "⛔"
  glyph + text label, not color alone (signal_states.js mapping);
  templates declare `lang` and carry ARIA/role attributes (G5a/b).
* Bengali/English mixed labels render correctly (verified in the
  accessibility tree).
* Prediction vs confirmed outcome: distinct panels (signal panel vs
  settlement/result panels); suppressed-signal reasons visible via the
  reason badges + `/api/data-quality` suppression counts.
* No specific tick-frequency claim exists in the UI or docs.

## Offline runner + CI

`scripts/run_offline_checks.sh` (rebuilt this phase) runs EVERY suite
in dependency order with truthful exit propagation —
**ALL OFFLINE CHECKS PASSED: 589 checks**
(Phase A 154 + Phase B 94 + Phase C 93 + Phase D 38 + Phase E 43 +
Phase F 28 + Phase G 39). `.github/workflows/ci.yml` mirrors the
runner; GitHub execution is honestly NOT claimable until a real run is
observed (no push has occurred).

## Verdict

**PHASE G: PASS WITH LIMITATIONS** — the limitation is the documented
set of data-dependent browser scenarios (above) that cannot be driven
without an authorized live feed; everything verifiable without one was
verified in a real browser.

## Commands + results (recorded)

```
python3 scripts/test_phase_g_api.py            -> 39 passed, 0 failed
bash scripts/run_offline_checks.sh             -> ALL OFFLINE CHECKS
                                                  PASSED (589 checks)
agent-browser E2E (3 sessions)                 -> results above;
                                                  screenshots saved
                                                  under /tmp/phase_g_e2e/
```
