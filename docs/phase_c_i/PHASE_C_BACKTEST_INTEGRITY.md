# PHASE C — BACKTEST INTEGRITY AND OUT-OF-SAMPLE EVALUATION

> Implemented 2026-10 on branch `rebuild/phase-c-i` (from the verified
> Phase B checkpoint `f950b66`). New evaluation package:
> `core/eval_clean/` (+ the runner `scripts/backtest_clean_eval.py`).
> Everything below ran on authorized historical data (public Binance 1m
> candles committed in the repo), isolated synthetic fixtures, or nothing
> at all (provenance source → INSUFFICIENT_DATA). No broker contact, no
> credentials, no trades, no retraining, no production-numerics changes.

## C1 — Data integrity

`core/eval_clean/integrity.py` validates every observation before it can
enter any metric. Invalid observations are quarantined with a stable
machine-readable reason; nothing is silently repaired; every exclusion is
recorded and reported (`quarantined_by_reason` in every report).

**Candle rules** (each has an adversarial test — A2–A19):
asset identity; period validity; open timestamp validity + period-grid
alignment; OHLC presence/finiteness/positivity; OHLC consistency
(`high >= max(open,close)`, `low <= min(open,close)`, `low <= high`);
duplicate candles (first kept); out-of-order arrivals; missing intervals
(gap count + sizes recorded, `gap_before` flag set on the post-gap
candle); zero-range candles (default policy: quarantine — configurable to
flag); tie candles (`open == close`) ACCEPTED and labeled FLAT.

**Prediction rules** (tests B1–B13): malformed records; invalid
directions; duplicate predictions for the same target (FIRST kept —
frozen-first-write semantics matching the `signal_provenance` UNIQUE
constraint); missing target candle; quarantined target candle;
**evidence leakage** (evidence timestamp after the prediction cutoff);
post-gap targets (discontinuous feature window); FLAT outcomes
(`tie_outcome` — counted, never graded); NO_SIGNAL (counted for
coverage, never an error); invalid probabilities.

**Outcome convention** (single source of truth, frozen in
`FROZEN["outcome_convention"]`):

| Outcome | Definition | Grading |
|---|---|---|
| GREEN | `close > open` | a CALL prediction is correct iff GREEN |
| RED | `close < open` | a PUT prediction is correct iff RED |
| FLAT | `close == open` | **never labeled GREEN or RED**; excluded from direction metrics; counted (`tie_count`, `n_tie_excluded`, `flat_call`/`flat_put` in the confusion matrix); tie-targeted emissions still count in **coverage** |

**Timezone convention**: epoch SECONDS (UTC) everywhere inside the
pipeline. The Binance loader (`ms → s`) is the single documented
conversion point; the validator rejects what misalignment can catch, and
the loader contract is test-verified (A19).

**Data quality of the actual datasets** (measured, not assumed):
4 × 8,999 candles, zero duplicates/gaps/out-of-order/bad-OHLC/
misalignment; ties (o==c): BTC 451, ETH 201, XRP 355, BNB 341.

## C2 — Look-ahead and leakage audit

`core/eval_clean/leakage.py` + the adversarial battery in
`scripts/test_phase_c_eval.py` (section C, 16 checks). Bad examples are
CONSTRUCTED ON PURPOSE and the evaluator must REJECT each one:

| Guard | Bad example that must be rejected | Test |
|---|---|---|
| `EvidenceLedger.violations` | any feature with evidence_ts > prediction cutoff | C1 |
| boundary rule | evidence_ts == cutoff is legitimate (source candle closes as the target opens) | C2, B8 |
| `SplitScaler` | transform before fit (normalization stats not from train only) | C4, C5 |
| `verify_split_independence` | test target before/inside train; embargo violated (label overlap); duplicate observation across splits | C7–C9 |
| `detect_label_echo` | a feature that mirrors the label in ≥99% of rows | C11 |
| `future_perturbation_unchanged` | a function that reads the future | C14 |
| engine causality | mutate candle j → any prediction for targets ≤ j changes | C15 |

**Engine causal proof** (run inside every report): the CSE v1 engine is
replayed on the first asset; candle 2000 of 2500 is adversarially
inverted. Result on the reported run: **0 mismatches** among the 2000
predictions made before the mutation; 7 predictions after it changed —
mutation propagates forward only, never backward. Combined with the
structural argument (features for candle T are built strictly after
ingesting candle T−1), this proves the engine cannot read the future.

**Existing-pipeline leakage findings** (documented, labeled, NOT silently
fixed — fixing would change live training behavior, out of scope without
authorization):

| Site | Defect | Action taken |
|---|---|---|
| `core/otc_predict/walk_forward.py` `report["selected"]` | winner picked from TEST-fold stats (selection-on-test) | docstring integrity note + `integrity_note` field in the report (label only) |
| `scripts/train_otc_model.py` | `best_overall` = min logloss on the reported rows | same label treatment |
| `scripts/backtest_fast_train_real.py` | in-sample replay tail | loud INTEGRITY LABEL banner on every run |
| `scripts/backtest_model_pipeline.py` S4 | trained-on-then-graded replay | loud INTEGRITY LABEL banner |
| `scripts/backtest_persistence_replay.py` | contaminated + broken at HEAD | **DEPRECATED**: exits code 1 with an honest notice unless `--acknowledge-contaminated` is passed |

The CSE engine itself, `otc_dataset`/`otc_features`, and the research
logreg baseline all pass their adversarial no-future-access proofs
(C15/F3; the repo's own `verify_lock` covers the tabular path).

## C3 — Chronological splitting (frozen before holdout evaluation)

`core/eval_clean/splits.py`. FROZEN protocol constants
(`clean_eval_v1`, recorded in every report):

* warmup = 600 candles/asset (engine hard gate is 61 closes)
* chronological 50% train / 25% selection / 25% **holdout test** per
  asset over the post-warmup region
* embargo = 1 candle between train and evaluation regions (T+1 label
  horizon); embargo candles belong to no region
* walk-forward = 4 expanding-window folds over the post-warmup region
* the holdout is reported ONCE by this pre-committed method; the
  selection region exists so tuning never needs the holdout

Exact cutoffs are recorded per asset in the report (`splits.cutoffs`);
e.g. BTCUSDT: train 2026-09-23T09:38Z → 2026-09-26T07:36Z, embargo
candle 07:37Z, selection 07:38Z → 2026-09-27T18:36Z, test
2026-09-27T18:37Z → 2026-09-29T05:36Z. `SplitResult.verify()` re-runs
`verify_split_independence` on every produced split; a regression fails
loudly. Splits are never random; rows keep temporal order.

## C4 — Baselines (same eligible observations, same exclusions)

`core/eval_clean/baselines.py`:

| Baseline | Definition |
|---|---|
| majority (train) | majority class of the TRAIN region only (exact tie → None, never silently broken) |
| previous-candle | continuation: target closes in the direction of the immediately preceding candle (FLAT source → abstain) |
| always-GREEN / always-RED | diagnostic constants |
| research logreg (pre-specified) | 6 frozen simple features (`RESEARCH_BASELINE_FEATURES`), L2=1.0, fixed GD, deterministic; standardization fitted on TRAIN only via `SplitScaler` |

No baseline is assumed useful; the results below show two of them beating
the production engine on the holdout.

## C5 — Metrics

`core/eval_clean/metrics.py` reports, per arm and per region (train /
selection / test / walk-forward folds / pooled, plus per-asset test):
total observations, directional count, NO_SIGNAL count, tie-excluded
count, **coverage** (always next to accuracy), accuracy with Wilson 95%
CI, CALL precision, PUT precision, GREEN recall, RED recall, confusion
matrix (incl. flat_call/flat_put), balanced accuracy, Brier score (only
when every graded row carries a genuine probability; None + note
otherwise), and a 10-bucket calibration table. Performance by time period
is available via the walk-forward folds and the region breakdowns.

**Reported run** (`clean_eval_20261018T151342_6166730493.json`, 4 assets,
2,100 test targets per asset, 168 tie-excluded, holdout period
2026-09-27T18:37Z → 2026-09-29T05:36Z):

| Arm | Test accuracy | Wilson 95% CI | Coverage |
|---|---|---|---|
| **cse_v1 (production engine)** | **0.5204** | [0.5096, 0.5312] | 1.0000 |
| cse_v1_research_localprior | 0.5132 | [0.5024, 0.5240] | 1.0000 |
| baseline majority (train) | 0.4978 | [0.4870, 0.5086] | 1.0000 |
| **baseline previous-candle** | **0.5274** | [0.5165, 0.5382] | 0.9800 |
| baseline always-GREEN | 0.4889 | [0.4782, 0.4997] | 1.0000 |
| baseline always-RED | 0.5111 | [0.5003, 0.5218] | 1.0000 |
| baseline research logreg | 0.5138 | [0.5030, 0.5246] | 1.0000 |

Walk-forward pooled (stability): cse_v1 0.5186, prev-candle 0.5257,
logreg 0.5134, localprior 0.5105 — same ordering as the holdout.

Calibration (cse_v1, test): predictions concentrate in [0.4,0.6) —
mean_pred 0.4807 → observed 0.4671 (n=3980); mean_pred 0.5207 → observed
0.5094 (n=4252). Brier 0.24936 (coin-flip reference 0.25). The engine's
`p_green` is weakly informative and NOT established as a calibrated
probability — it must be displayed as an internal score, not "the chance
of being correct" (Phase F handles presentation).

**Honest headline: on this dataset the production engine does NOT beat the
trivial previous-candle continuation baseline** (0.5204 vs 0.5274; the
CIs overlap, and prev-candle abstains on 2% of candles). The engine's CI
barely excludes 50% — a ~2pp edge over coin-flip on real exchange 1m
data, far below the 54.05% refund-tie break-even at an illustrative 0.85
payout. No accuracy claim beyond this is made.

## C6 — Payout-aware break-even

`core/eval_clean/breakeven.py`. `break_even_win_rate(r, tau, tie_outcome)`
implements the documented simplified model (win = +r, loss = −1,
tie = refund or loss — caller must state which, broker rules UNVERIFIED):

* refund ties: `(1 − tau) / (1 + r)` — with measured tie rate 3.74%
  (pooled) and the operator-supplied illustrative payout 0.85:
  **0.5203**
* ties are losses: `1 / (1 + r)` = **0.5405**

The payout is **explicitly supplied** (`--payout`), never hardcoded as a
guaranteed platform rate; the report block carries the formula, the
inputs' provenance, and the disclaimer that accuracy above break-even is
not guaranteed profitability (strike-vs-open grading, varying payouts,
slippage and platform tie rules are unmodelled).

## C7 — Acceptance gate

| Criterion | Status |
|---|---|
| Leakage checks implemented and passing | PASS — 16 adversarial checks + engine causal proof + split verification, all green |
| Split methodology reproducible | PASS — frozen constants + recorded cutoffs + `verify()` on every split |
| Metrics independently recomputable | PASS — pure functions, hand-computed fixtures (E1–E13), machine-readable JSON |
| Exclusions transparent | PASS — `quarantined_by_reason` / `excluded_by_reason` in every report |
| Synthetic and historical evidence separated | PASS — distinct verdicts (`SYNTHETIC_VALIDATION_ONLY` vs `OOS_EVALUATED_WITH_LIMITATIONS`); synthetic run shows ~48.9% (no fake edge on noise) |
| No unexplained data corruption | PASS — datasets read-only; sha256-stamped; committed JSONs untouched |

**Phase C verdict: PASS WITH LIMITATIONS** — the limitations are inherent
to the evidence (single 6.25-day window, public exchange data ≠ OTC,
unverifiable global-prior provenance) and are listed in every report.
The evaluation of the app's own recorded emissions is INCONCLUSIVE
(empty DB → INSUFFICIENT_DATA — honestly reported, not padded).

## Deliverables

* `core/eval_clean/` — integrity / leakage / splits / baselines / metrics
  / breakeven (+ package docs)
* `scripts/backtest_clean_eval.py` — reproducible runner (binance /
  synthetic / provenance sources; subprocess-isolated engine arms;
  machine-readable JSON to gitignored `scripts/out/`)
* `scripts/test_phase_c_eval.py` — 93 adversarial + correctness checks
  (all passing)
* Machine-readable reports in `scripts/out/` (timestamped, unique);
  committed report JSONs never overwritten
* Legacy contamination labels on the five flagged harnesses (above)
* This document

## Commands + results (recorded)

```
python3 scripts/test_phase_c_eval.py          -> 93 passed, 0 failed, exit 0
python3 scripts/backtest_clean_eval.py --source binance \
    --arms cse_v1,cse_v1_research_localprior --payout 0.85
    -> OOS_EVALUATED_WITH_LIMITATIONS, report written, exit 0
python3 scripts/backtest_clean_eval.py --source synthetic --seed 7
    -> SYNTHETIC_VALIDATION_ONLY, exit 0
python3 scripts/backtest_clean_eval.py --source provenance --db <missing>
    -> INSUFFICIENT_DATA, exit 0
Phase A+B regression after all Phase C edits:
    70/70 + 59/59 + 25/25 + 54/54 + 40/40 all exit 0 (248/248)
```
