# PHASE L — SMC/ICT DEEP RESEARCH BUILD: MASTER PLAN

> Branch `rebuild/smc-research` (cut from `rebuild/resolve` @ `a6df1e1`,
> 671/671 offline checks green at base). Owner authorization: "signal logic
> upgrade — ICT, SMC, market structure, candlestick patterns, price action,
> news event management, accumulation/manipulation, retail vs institutional,
> banks, POI zones, support/supply/resistance, key levels, momentum — deep
> plan + line-by-line implementation."
>
> **STATUS OF PRIOR BLOCKERS:** B3+B4 CLOSED (Phase J, adversarially
> verified). B1 partially unblocked (authorized Binance public feed
> confirmed reachable; 30-day dataset staged). B2/B5 are what this phase
> attacks: build the research capacity, measure honestly, retrain only with
> recorded provenance.

---

## 0. HONEST FRAMING — READ FIRST (non-negotiable)

The owner asked for "more sure signals" and "perfect entry points."
An honest engineering answer:

1. **"Sure signal" does not exist** — especially at 1-minute OTC granularity,
   where the current evidence says the production engine (52.04%) does NOT
   beat a previous-candle coin-flip baseline (52.74%). Any promise of "sure"
   signals would be fabrication, which this repository is forbidden to do
   (it already failed one audit for exactly that).
2. What CAN be built, and what this plan builds:
   - **Deterministic, unit-tested, no-lookahead feature extractors** for
     every ICT/SMC/price-action concept listed by the owner.
   - **Zone/level/pattern detection** surfaced as research overlays
     (order blocks, FVG, supply/demand, key levels, liquidity pools).
   - **Honest evaluation** of every composite signal idea through the
     frozen, leakage-guarded protocol with a pre-committed acceptance rule.
   - **Provenance-clean retraining** so future models have verifiable
     training-window provenance (attacks B5).
3. The Truth Layer gating does not change: **no validated edge → NO_SIGNAL**.
   ICT modules enter the module engine with weight 0.0 (research-only)
   until and unless they pass the pre-committed acceptance rule on the
   pre-committed holdout.
4. Banks/institutions' intent is **not directly observable** from 1m OHLCV.
   We implement only measurable footprint proxies (volume delta, effort vs
   result, absorption, sweep-and-reclaim) and label them as proxies. No
   claims about "smart money" intent are emitted anywhere.

## 1. CONCEPT → MEASURABLE IMPLEMENTATION MAP

| Owner's concept | Module (file) | Measurable proxy | Data required | Honesty caveat |
|---|---|---|---|---|
| ICT / SMC market structure | `core/ict/market_structure.py` | Fractal swing highs/lows (k-candle confirmation), BOS (break of structure), CHoCH (change of character), HH/HL–LH/LL trend states, ranges | OHLC | Swing confirmation needs k future candles — detection timestamp is honest (`confirm_index`) |
| POI zones (order blocks) | `core/ict/poi_zones.py` | Last opposite-color candle before an impulsive move that breaks structure; zone = its body/wicks | OHLC | Zone is a hypothesis region, not an oracle |
| Fair value gaps (FVG) | `core/ict/poi_zones.py` | 3-candle imbalance: `c1.high < c3.low` (bullish) / `c1.low > c3.high` (bearish) | OHLC | Strict numeric definition; fill tracking |
| Support / supply / resistance | `core/ict/poi_zones.py` | Demand = base before rally; supply = base before drop; resistance = swing-high clusters with ≥2 touches; zone freshness (untested/touched/broken) | OHLC | Cluster tolerance is a documented parameter |
| Key levels, liquidity pools | `core/ict/poi_zones.py` | Equal highs/lows within `eq_tol` (stop clusters), session/period extremes | OHLC | Equal-high ≠ guaranteed liquidity |
| Candlestick patterns | `core/ict/candlestick.py` | Engulfing, hammer/shooting star, doji, morning/evening star, inside/outside bar, tweezers — strict ratio definitions | OHLC | Fixed thresholds documented as parameters |
| Price action | composite | structure + pattern + momentum snapshot at candle close | OHLC | — |
| Momentum | `core/ict/momentum.py` | ROC, RSI-14, stoch-%K, velocity; divergence (price HH vs RSI LH etc.) | OHLC | Standard indicators, no smoothing lookahead |
| Accumulation → manipulation → distribution (AMD) | `core/ict/liquidity.py` | Range detection (N-candle consolidation) → sweep of range extreme → expansion candle closing back inside → direction | OHLC | Wyckoff-lite heuristic; phases are labels not facts |
| Liquidity sweeps / stop hunts | `core/ict/liquidity.py` | Wick beyond equal-high/low or range extreme, close back within, next-candle confirmation | OHLC | Proxy for stop runs |
| Retail vs institutional | `core/ict/orderflow.py` | Volume delta (needs real volume), tick-intensity proxy (OTC), effort-vs-result (range vs volume), absorption flag | OHLCV / tick counts | PROXY ONLY — no actual order classification; volume absent on OTC |
| Banks / big institutions | (not separable) | Only the orderflow proxies above | — | Explicitly documented as unobservable |
| Kill zones / sessions | `core/ict/sessions.py` | UTC session tagging (Asia/London/NY) + ICT kill-zone windows, DST-aware approximation | timestamps | OTC is 24/7 synthetic — session validity on OTC unverified; on exchange data sessions are real |
| News event management | `core/ict/news.py` | Owner-supplied economic calendar import (CSV/JSON, provenance-labeled), minutes-to-nearest-event features, configurable signal-gating policy (NO_SIGNAL ± N min around high-impact) | authorized calendar file | No auto-fetch from unauthorized sources; no auto-trading; on crypto OTC the relevance is itself a research question |

## 2. ARCHITECTURE

```
core/ict/
  types.py          # Candle, Detection, ParamSet — THE shared contract (written by lead)
  helpers.py        # validation, perturbation utils shared by tests
  market_structure.py   # L1 agent
  poi_zones.py           # L2 agent
  candlestick.py         # L3 agent
  momentum.py            # L3 agent
  liquidity.py           # L4 agent (AMD + sweeps)
  sessions.py            # L4 agent (kill zones)
  news.py                # L4 agent (calendar import + gating policy)
  orderflow.py           # L4 agent (volume-delta/absorption proxies)
  features.py            # L5 lead — snapshot_at(candles, t): features for
                         #   predicting candle t from candles[:t] ONLY
scripts/
  test_phase_l1_market_structure.py   # per-module batteries
  test_phase_l2_poi_zones.py
  test_phase_l3_patterns_momentum.py
  test_phase_l4_liquidity_sessions_news.py
  backtest_ict_eval.py                # L6 — frozen-protocol evaluation of
                                      #   pre-specified composite ICT arms
  ict_research_server.py (route additions in server.py, lead-owned)
static/js/  chart research overlay (lead + frontend agent, L7)
```

**Shared contract (locked in `types.py` before any module work starts):**

- `Candle(ts, open, high, low, close, volume=None)` — ts = unix **seconds**
  (matches `load_binance_1m`); frozen dataclass; invariant `high ≥ max(o,c)`,
  `low ≤ min(o,c)` validated by `validate_candles()`.
- `Detection(kind, start_index, confirm_index, ts, side, level_low,
  level_high, meta)` — every detection records the candle index at which it
  is **confirmed** (candle closed). Hard rule: a detection with
  `confirm_index = c` may depend ONLY on `candles[:c+1]`.
- Every detector: `detect_*(candles: Sequence[Candle], **params) ->
  list[Detection]`, pure function, deterministic, no I/O.

## 3. PHASES (L0–L8), ACCEPTANCE & VERIFICATION

| Phase | Deliverable | Key files | Verification (all must pass) | Acceptance |
|---|---|---|---|---|
| **L0** | This plan + locked contract | `docs/smc/PHASE_L_MASTER_PLAN.md`, `core/ict/types.py`, `helpers.py` | `ast.parse` gate; contract unit tests; 671 regression green | Contract frozen; any change needs lead approval |
| **L1** | Market structure | `market_structure.py`, `test_phase_l1_*.py` | ≥25 unit checks incl. hand-crafted BOS/CHoCH/trend fixtures; perturbation test (mutating candle j must not alter detections with confirm_index < j); determinism (same input → identical output) | All green; no lookahead |
| **L2** | POI zones | `poi_zones.py`, `test_phase_l2_*.py` | ≥30 checks: OB/FVG/S&D/equal-level fixtures with exact expected zones; freshness transitions; perturbation; determinism | Same |
| **L3** | Patterns + momentum | `candlestick.py`, `momentum.py`, `test_phase_l3_*.py` | ≥30 checks: each pattern exact fixture; RSI/ROC hand-computed values; divergence fixtures; perturbation; determinism | Same |
| **L4** | Liquidity/AMD + sessions + news + orderflow | `liquidity.py`, `sessions.py`, `news.py`, `orderflow.py`, `test_phase_l4_*.py` | ≥35 checks: range→sweep→expansion sequence; kill-zone/DST boundaries; calendar import + provenance + gating policy; volume-delta/absorption math; perturbation; determinism | Same |
| **L5** | Integration snapshot | `features.py`, `test_phase_l5_*.py` | `snapshot_at` no-lookahead proof via full-series future-perturbation (change ANY candle ≥ t → snapshot(t) unchanged); feature matrix build on 30-day dataset; full 671+new regression green | Snapshot immutability proven |
| **L6** | Honest evaluation campaign | `backtest_ict_eval.py`, `docs/smc/PHASE_L_EVALUATION.md` | Frozen protocol: chronological split on 30-day authorized data (last 6 days = pre-committed holdout, embargo 1); arms = 6 pre-specified ICT composite rules + prev-candle baseline + majority; coverage-aware metrics, Wilson CI; **pre-committed rule: an arm is ACCEPTED only if CI-lower > baseline accuracy AND coverage ≥ 0.5**; seeds fixed; report JSON with sha256 provenance | Whatever the numbers are — recorded honestly. REJECTED arms stay weight-0 |
| **L7** | UI research overlay | `server.py` route `/api/ict-research/{asset}`, `static/js` overlay, module panel | API battery additions; real-Chromium E2E: zones/levels/patterns render with "রিসার্চ (ভ্যালিডেটেড নয়)" labels; VLM visual check; zero fabricated direction on empty data | No state where UI implies validation that doesn't exist |
| **L8** | Final report + checkpoint | `docs/smc/PHASE_L_FINAL_REPORT.md`, worklog, CP tag | Full suite green; every claim traced to a command output | Honest verdict (likely: research capacity READY; trading readiness unchanged until an arm passes) |

**Rollback:** every phase = one clean checkpoint commit (CP-L*n); module
files are additive (new package `core/ict/`), server.py touched only in L7
behind a single route — revert = `git revert` of the phase commit. No
history rewrite, no force push, no remote push (no authorization).

## 4. ENGINEERING AGENT ASSIGNMENTS (multi-agent, real)

| Agent (subagent_type) | Task ID | Owns (exclusive write) | Forbidden |
|---|---|---|---|
| **Lead Architect** (me, orchestrator) | L0, L5, L6, L7, L8 | `types.py`, `helpers.py`, `features.py`, `server.py`, `backtest_ict_eval.py`, docs, checkpoints, integration | Module internals |
| **Agent A — Market Structure Engineer** | L1 | `core/ict/market_structure.py`, `scripts/test_phase_l1_market_structure.py` | server.py, other modules |
| **Agent B — POI/Zone Engineer** | L2 | `core/ict/poi_zones.py`, `scripts/test_phase_l2_poi_zones.py` | same |
| **Agent C — Pattern/Momentum Engineer** | L3 | `core/ict/candlestick.py`, `core/ict/momentum.py`, `scripts/test_phase_l3_patterns_momentum.py` | same |
| **Agent D — Liquidity/Session/News Engineer** | L4 | `core/ict/liquidity.py`, `sessions.py`, `news.py`, `orderflow.py`, `scripts/test_phase_l4_liquidity_sessions_news.py` | same |

5 engineering agents total (1 lead + 4 parallel builders). All read the
worklog first; all append their section after; all run their own batteries
and report exact pass counts; lead independently re-runs everything before
each checkpoint. Shared-ownership conflicts are impossible by construction
(disjoint file sets). L1–L4 run **in parallel** after L0 locks the contract.

## 5. IN-APP "AGENT" DESIGN (module engine integration)

The existing মডিউল ইঞ্জিন (module voting) gets ICT module adapters in L7:

- Each ICT detector group registers as a module producing `(direction,
  strength)` votes for the **next** candle — computed ONLY from closed
  candles via `features.snapshot_at`.
- Default weight = **0.0** with explicit `research_only=True` flag. Weights
  change ONLY if the L6 campaign accepts an arm (owner-informed, separate
  authorization, re-checkpointed).
- The coordination engine's output continues to flow through the Truth
  Layer (Phase A contract): unauthorized source / unverified timing /
  no validated model → **NO_SIGNAL**. Nothing in this plan can emit an
  actionable CALL/PUT.
- UI labels every ICT module "রিসার্চ মডিউল — ভ্যালিডেটেড নয়" until validated.

## 6. DATA & PROVENANCE

| Asset | Class | Use |
|---|---|---|
| `scripts/data/binance_30d/*.json` (8 assets × 43,200 1m, volume) | AUTHORIZED historical exchange (public API, fetched Phase J) | L5 feature matrices, L6 evaluation (train/holdout) |
| Original 8,999-row files | immutable history | untouched (holdout evidence binding) |
| Synthetic OTC-like generator | SYNTHETIC | control arm only, labeled |
| Quotex live channels | UNAUTHORIZED | never used by this phase |
| Economic calendar files (L4) | owner-supplied, provenance-labeled | research + gating policy only |

**Pre-committed holdout (frozen now, before any L6 run):** last **6 days**
(8,640 candles) of each 30-day series; embargo 1 candle; warmup 600.
Written to `docs/smc/HOLDOUT_MANIFEST.json` at L0 with sha256 of the
dataset files — cannot be silently re-chosen later.

## 7. WHAT "SOLVED" MEANS AT THE END OF PHASE L

- Every owner-listed concept has a tested, deterministic, no-lookahead,
  honest implementation ✅ (verifiable by re-running batteries)
- Zones/levels/patterns visible in the UI as clearly-labeled research ✅
- Every composite ICT signal idea measured against baselines with
  pre-committed acceptance ✅ — **whatever the result is**
- B5 attacked: any future training happens through `features.py` with
  recorded provenance windows ✅
- B1/B2 unchanged in status unless an arm passes; no false success ✅

*Plan written by the Lead Architect. Contract locks next; agents dispatch
immediately after.*
