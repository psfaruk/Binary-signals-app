# PHASE F — PREDICTION ENGINE EVALUATION AND RESEARCH

> Executed 2026-10 on `rebuild/phase-c-i` (after CP-PCI-3). Runner:
> `scripts/research_experiments.py` (pre-specified experiments through
> the Phase-C frozen protocol) + `scripts/test_phase_f_research.py`
> (28 checks). No production model was replaced, retrained or promoted.
> No broker contact. No credentials. No trades.

## F1 — Audit of the existing predictor (no numerics changed)

**The production predictor is CSE v1** (`core/next_candle.py`,
`next_candle_v1`): a 35-feature candle-anatomy logistic engine
(`FEATURE_NAMES`: color/streak/body anatomy, pattern one-hots,
empirical 2/3-candle transition probabilities, ATR/volatility, EMA gap,
5m-HTF trend, hour sin/cos), blended global-prior + online-local model
(`w = n/(n+3000)`; local = standardized L2 logistic, full-batch GD),
with a fixed calibration slope (0.636) shrinking the displayed score.
Thresholds (production defaults, test-verified A2): BLEND_K=3000,
FIT_MIN_ROWS=300, REFIT_EVERY=50. The live feed requires ≥64 closed
candles (A5) and consumes `engine.current()` at candle open through the
truth-gated `_run_eoc` boundary.

**NO_SIGNAL conditions**: the 13-code contract vocabulary
(`ALL_REASON_CODES`, A4) — under current policy every live emission is
NO_SIGNAL (unauthorized source / no validated model / unverified
timing).

**Baseline metrics** (Phase C pre-committed holdout, pooled 4 assets,
n=8232 graded): accuracy **0.5204** [0.5096, 0.5312], coverage 1.0,
Brier 0.2494. **Known limitations**: global prior trained on an
unverifiable window (possibly this very dataset — the localprior arm
scored 0.5132, so the prior contributes but is not the whole story);
public exchange data ≠ OTC; single 6.25-day window; strike-vs-open
grading gap. Production numerics untouched (weight artifact
byte-identical to the committed version — test F1).

## F2/F3 — Controlled experiments (isolated research path)

Six pre-specified experiments (EXP-000…EXP-005) ran through the frozen
Phase-C protocol (identical splits/embargo/cutoffs; manifests with
feature definitions, hyperparameters, seeds, dataset sha256
provenance; selection region reported; holdout evaluated once). All
candidates are deterministic (GD, no RNG) and reproducible — EXP-002's
recorded test accuracy reproduces EXACTLY in a fresh subprocess (C1).

**Pre-committed acceptance rule (F5, frozen before the holdout run,
identical in every manifest — D5): eligible iff TEST Wilson-CI lower
bound > prev-candle baseline point estimate AND TEST coverage ≥ 0.5.**

| Experiment | Test accuracy | 95% CI | Coverage | Decision |
|---|---|---|---|---|
| EXP-000 prev-candle (reference) | 0.5274 | [0.5165, 0.5382] | 0.98 | reference |
| EXP-001 CSE v1 production | 0.5204 | [0.5096, 0.5312] | 1.00 | REJECTED |
| EXP-002 logreg 6-feature | 0.5138 | [0.5030, 0.5246] | 1.00 | REJECTED |
| EXP-003 logreg 12-feature extended | 0.5066 | [0.4958, 0.5174] | 1.00 | REJECTED |
| EXP-004a engine+prev agreement filter | 0.5342 | [0.5213, 0.5470] | 0.70 | REJECTED |
| EXP-004b engine conviction ≥0.02 | 0.5350 | [0.5187, 0.5513] | 0.43 | REJECTED |

**Honest readings (negative results recorded, D3):**

* The production engine does NOT beat the trivial continuation
  baseline on the pre-committed holdout.
* Adding features HURT on this window (12-feature 50.66% < 6-feature
  51.38% < prev-candle 52.74%) — classic small-sample overfitting
  direction; recorded as a negative result, not tuned away.
* The rule-based filters raise raw accuracy (53.4–53.5%) but trade it
  against coverage (0.70 / 0.43) and still fail the pre-committed CI
  rule (EXP-004a's lower bound 0.5213 < 0.5274; EXP-004b's coverage
  0.43 < 0.5). Because the rule was frozen in advance, these are
  REJECTED — exactly the discipline F3 demands (no post-hoc threshold
  relaxation to claim a win).

## F4 — Confidence honesty

The engine's `p_green` is an **internal score**, not an established
probability: the test-region calibration table (EXP-005) shows
predictions confined to [0.4, 0.6) — mean_pred 0.4807 → observed
0.4671 (n=3980); 0.5207 → 0.5094 (n=4252); no bucket outside that
range is ever populated. Verdict recorded in the experiment report:
**CALIBRATION_NOT_ESTABLISHED**.

UI consequences (applied): the next-candle panel's `p_green` display no
longer claims "(calibrated)" — it renders as "X% গ্রিন (ইঞ্জিন স্কোর)"
(engine score); unmeasured confidence still renders "—" (Phase A);
the historical-pattern probability keeps its "not a guarantee"
disclaimer; and there is no "probability of being correct" phrasing
anywhere (tests E3–E5). The live-candle panel's P(close>open) values
remain model-defined event estimates displayed alongside the
research-only truth labels (Phase D).

## F5 — Candidate acceptance

**Final decision: RETAIN BASELINE / NO_SIGNAL.** No candidate —
including the production engine — convincingly beats the
previous-candle baseline on the pre-committed holdout. Nothing was
promoted; the model-validation registry stays empty (the only path to
actionable signals remains an authorized source + signed OOS evidence,
neither of which exists). Production impact: NONE (tests F1/F2).

## Commands + results (recorded)

```
python3 scripts/research_experiments.py          -> report written, exit 0
python3 scripts/test_phase_f_research.py         -> 28 passed, 0 failed
JS regression after the label change: 59/59; Phase A: 70/70, 25/25
```
