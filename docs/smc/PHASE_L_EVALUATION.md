# PHASE L6 — ICT/SMC COMPOSITE-ARM EVALUATION (PRE-COMMITTED HOLDOUT)

> 2026-10-20, branch `rebuild/smc-research`. Script:
> `scripts/backtest_ict_eval.py` (arms written into the file BEFORE the
> first run — no tuning on the holdout). Machine-readable report:
> `scripts/out/phase_l_ict_eval_20261020T235016.json`.
> Holdout manifest (sha256-frozen at CP-L0, before any ICT arm ran):
> `docs/smc/HOLDOUT_MANIFEST.json` — all 8 dataset hashes verified
> matching at run time (`manifest_match: true` for every asset).

## 1. Protocol (as frozen)

* Data: authorized 30-day Binance 1m public data, 8 assets. The trailing
  PARTIAL candle per file was dropped (fetch-time open candle, verified
  against the live API in Phase K).
* Holdout: the last **8,640 CLOSED candles** (6 days) per asset
  (2026-10-13 19:40 UTC → 2026-10-19 19:39 UTC), 69,120 target rows
  total; feature context = full preceding series (≫ 600-candle warmup).
* Arms: 8 pre-specified rule functions (baseline + 7 ICT composites),
  zero fitted parameters.
* Grading: CALL correct iff target GREEN (close>open), PUT iff RED;
  FLAT excluded from accuracy (repo tie policy) but directional
  emissions count toward coverage.
* Pre-committed acceptance rule: Wilson-95%-lower(pooled accuracy) >
  prev-candle baseline accuracy AND coverage ≥ 0.5.

## 2. Results (pooled across 8 assets)

| Arm | Accuracy | 95% CI | Coverage | Graded | Rule verdict |
|---|---|---|---|---|---|
| baseline_prev_candle | 49.11% | [48.70, 49.52] | 0.913 | 58,134 | reference |
| ict1_ob_retest | 49.92% | [49.43, 50.40] | 0.633 | 40,178 | ACCEPTED* |
| **ict2_sweep_reversal** | **50.53%** | **[50.14, 50.93]** | 0.963 | 61,218 | **ACCEPTED*** |
| ict3_amd_continuation | 47.13% | [43.58, 50.71] | 0.011 | 749 | REJECTED (coverage) |
| ict4_fvg_fill | 49.60% | [49.15, 50.04] | 0.771 | 48,462 | ACCEPTED* |
| ict5_divergence | 50.25% | [49.10, 51.40] | 0.112 | 7,236 | REJECTED (coverage) |
| ict6_kz_structure | 49.76% | [48.06, 51.46] | 0.051 | 3,324 | REJECTED (coverage) |
| ict7_vote | 49.99% | [49.41, 50.56] | 0.460 | 29,171 | REJECTED (coverage 0.46<0.5) |

\* "ACCEPTED" = passes the LETTER of the pre-committed rule. Read §3
before attaching any meaning to that word.

## 3. Honest interpretation — READ THIS BEFORE §2

1. **No arm is an actionable trading edge.** At the repo's standard
   explicit-payout assumption (0.85), break-even win rate = 1/(1+0.85) =
   **54.05%**. Every arm — including the three that passed the rule — is
   FAR below break-even. Expected value per unit staked at 0.85 payout:
   ict1 −7.7%, ict2 −6.5%, ict4 −8.2%. **Trading any of these loses
   money under the stated payout.** The Truth Layer gate stays closed.
2. **Why "ACCEPTED" then?** The pre-committed rule compares each arm's
   CI-lower to the prev-candle baseline — and on this holdout the
   baseline itself came in at **49.11%, below a coin flip**. A
   coin-flip arm (50.0%) mechanically beats a 49.11% baseline. This is a
   weakness of the rule's baseline-relative formulation, recorded here
   honestly rather than silently reinterpreted. The rule was fixed
   before the run and is reported exactly as it performed.
3. **Only one result is even statistically notable:** ict2
   (liquidity-sweep reversal) — 50.53% with CI-lower 50.14% > 50%,
   consistent across all 8 assets (per-asset range 49.96%–51.06%) and
   with near-full coverage (0.963). This is a small, consistent tilt,
   NOT an edge. Caveats: (a) 7 arms shared one holdout — with ~7
   independent near-50% tests, one CI excluding 50% is not compelling
   family-wise evidence (no multiple-comparison correction was
   pre-committed); (b) exchange crypto data, NOT OTC — transfer to
   Quotex OTC is unverified; (c) the effect size (+0.5pp) is an order of
   magnitude below what payout-adjusted profitability requires.
4. **Consequences:** all ICT modules stay research-only with weight 0.
   Nothing is registered in `core/model_validation.json`. No UI element
   may present any arm as validated. The ict2 tilt is recorded as a
   research finding for future (authorized-data, break-even-aware,
   family-wise-corrected) campaigns.

## 4. Reproduction

```
python3 scripts/backtest_ict_eval.py            # cached assets aggregate
python3 scripts/backtest_ict_eval.py --refresh  # full recompute (~25 min)
```

Deterministic (no RNG anywhere); report JSON records per-asset hashes,
holdout spans, per-arm specs, per-asset metrics, verdicts, and the four
honest limitations (OTC transfer unverified; fixed a-priori thresholds;
no authorized news calendar; shared holdout without family-wise
correction).

## 5. What this phase proves

* The full ICT/SMC research stack works end-to-end on authorized data:
  feature extraction → no-lookahead-proven snapshots → pre-specified
  composite arms → frozen-protocol evaluation with pre-committed
  acceptance — all reproducible.
* The evaluation delivered a TRUE negative with one small recorded
  research tilt — exactly what an honest research pipeline is for.
* No claims about Quotex OTC performance are made or implied anywhere.
