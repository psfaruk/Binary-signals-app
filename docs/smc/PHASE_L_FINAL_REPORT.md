# PHASE L — FINAL REPORT (SMC/ICT DEEP RESEARCH BUILD)

> 2026-10-20, branch `rebuild/smc-research`. This report closes the
> owner-authorized Phase L directive: build the ICT/SMC/price-action/news
> research stack end-to-end, evaluate honestly, surface in the UI, and
> never fabricate an edge.

## 1. Repository state

| Item | Value |
|---|---|
| Branch | `rebuild/smc-research` (from `rebuild/resolve` @ `a6df1e1`) |
| Checkpoints | CP-L0 `35c3e8f` → CP-L1 `2106465` → CP-L2 `fafee18` → CP-L3 `f8081c6` → CP-L4 `183d1d5` → CP-K1 `09149fc` (interleaved Phase K completion) → CP-L5 `8d6d98c` → CP-L5b `c44cb56` → CP-L6 `d54a6d8` → CP-L7 `93b5e75` |
| Working tree | clean at every checkpoint; no push (no authorization) |
| Offline suite | **1071/1071 checks green** (`bash scripts/run_offline_checks.sh`, exit 0) |

## 2. What was delivered (per phase, with evidence)

* **L0 — plan + frozen contract** (`docs/smc/PHASE_L_MASTER_PLAN.md`):
  honest framing (no "sure signal" promise), full concept→module mapping
  for every owner-listed concept, 5-agent assignment plan,
  `core/ict/types.py` (Candle/Detection, confirm_index bookkeeping),
  multi-variant no-lookahead perturbation helpers,
  **pre-committed holdout manifest** (`docs/smc/HOLDOUT_MANIFEST.json`,
  sha256-frozen BEFORE any evaluation).
* **L1–L4 — detector modules** (4 parallel subagents + 1 recovery
  agent, disjoint file ownership): market structure
  (swings/BOS/CHoCH/trend, 62 checks), POI zones (FVG/order
  blocks/supply-demand/equal levels/key levels/zone status, 66),
  candlestick patterns + momentum (11 pattern kinds, Wilder RSI,
  divergences, 70), liquidity/AMD + sessions-killzones-DST + news
  calendar policy + orderflow proxies (118; a real `detect_amd`
  first-sweep bug was found and fixed by the recovery agent).
  Every module: pure, deterministic, mechanically no-lookahead-proven
  (perturbation guard-rail proven non-vacuous).
* **Phase K (interleaved, completing an interrupted session)** —
  provenance-clean retraining of the CSE global prior on the authorized
  30-day dataset: **verdict REJECTED per the pre-committed rule**
  (brier-vs-null clause unsatisfiable); production weights byte-identical;
  two real O(n²) harness performance bugs fixed (results identical);
  dataset refresh verified against the LIVE Binance public API (trailing
  partial candle + 801-min day-1 gap documented); 26-check battery.
* **L5 — integration** (`core/ict/features.py`): 41-feature snapshot at
  target-candle time; three computation paths proven IDENTICAL;
  target-OHLC perturbation proof; incremental zone status ==
  `classify_zone_status`; a real unprimed-tracker bug in late-start
  matrices was caught and fixed with a pinning check (31 checks).
* **L6 — honest evaluation** (`scripts/backtest_ict_eval.py` +
  `docs/smc/PHASE_L_EVALUATION.md`): 7 pre-specified ICT composite arms +
  prev-candle baseline on the pre-committed 6-day holdout (8 assets,
  69,120 rows). **Result: 3 arms pass the rule's letter only because the
  baseline itself is 49.11% (below coin-flip); the only statistically
  notable tilt is ict2 sweep-reversal 50.53% [50.14–50.93], consistent
  across all 8 assets — and EVERY arm is far below the 54.05%
  break-even (negative expectancy −6.5% to −8.2% per unit at 0.85
  payout). NO module registered; weights stay 0.**
* **L7 — UI research overlay**: `/api/ict-research/{asset}`
  (auto mode = honest no-data without an authorized live feed; replay
  mode = authorized 30-day dataset with sha256 provenance and L6
  research votes labeled "ভ্যালিডেটেড নয়, ওজন ০, প্রযোজ্য নয়"),
  collapsible Bengali panel, 27-check battery + real-Chromium E2E +
  VLM-verified honest rendering; Truth Layer gate untouched
  (still `actionable_signals_possible: false`).

## 3. Real defects found and fixed in this phase set

1. `detect_amd` first-sweep-only rule silently missed valid sequences
   (L4 recovery agent; fixed + pinned).
2. Two O(n²) performance bugs in the frozen evaluation harness
   (`by_t` rebuilt per candle; `_index_of` linear scans) — semantics
   preserved, Phase-C battery green, 550s → 68s per asset (Phase K).
3. Unprimed zone-tracker bug in `build_feature_matrix(start>1)` —
   early zones kept stale "untested" status (L5; caught in lead
   self-review, fixed + pinned by battery check C2).
4. Docstring inaccuracy: engine has 35 features, not 34 (Phase K).
5. Phase-K interrupted-session uncommitted work + silent dataset refresh
   recovered, verified against the live API, and committed with full
   provenance documentation.

## 4. What the owner asked vs. what is honestly true

| Owner's wish | Honest engineering answer |
|---|---|
| "আরও sure signal / perfect entry" | **Does not exist here.** All ICT arms measure at ~49–50.5% — below break-even. The system will not pretend otherwise: every overlay is labeled NOT validated; the Truth Layer still emits NO_SIGNAL without an authorized source and a validated model. |
| ICT/SMC/market structure/candlestick/price action/news/AMD/retail-institutional/banks | Every concept has a tested, deterministic, no-lookahead implementation (see the mapping table in the master plan). What is NOT measurable (bank intent, true order classification) is implemented only as clearly-labeled proxies or explicitly documented as unobservable. |
| Multi-agent build | Executed for real: 1 lead + 4 parallel module agents + 1 recovery agent; disjoint ownership; every battery re-run independently by the lead before each checkpoint. |
| Where/what/how verified | 1071 offline checks + mechanical no-lookahead proofs at module AND integration level + pre-committed holdout evaluation + real-browser E2E + VLM visual verification — all re-runnable via `bash scripts/run_offline_checks.sh`. |

## 5. Standing limitations (unchanged from Phase I unless noted)

1. **B1**: no authorized live data source (Quotex channels remain
   unauthorized; Binance public data is authorized but is exchange
   crypto, NOT OTC).
2. **B2**: no validated model — now WITH the added evidence that the
   pre-specified ICT arms also fail break-even on the pre-committed
   holdout.
3. **B5 (bounded)**: a provenance-complete retrained prior exists
   (Phase K artifact) with honest measured performance; the old
   weights' unverifiable lineage can no longer inflate any claim.
4. Family-wise error is not controlled across arms (documented in the
   L6 report); any future acceptance campaign must pre-commit a
   multiplicity correction and a break-even-aware rule.
5. The dataset's 801-minute day-1 gap and trailing partial candle are
   documented and handled (integrity reason codes / explicit drop).

## 6. Final verdict

> **PASS WITH LIMITATIONS — as a PREDICTION RESEARCH APPLICATION.**
> **NOT READY — as a trading signal tool** (unchanged: no authorized
> source, no validated model, every measured arm below break-even).

Safe to use as-is: the complete ICT/SMC research stack (detectors,
integration snapshots, frozen-protocol evaluation, replay overlay,
1071-check suite). NOT safe for any purpose that treats an overlay vote
as a signal — the evidence in this phase set directly refutes that use.
