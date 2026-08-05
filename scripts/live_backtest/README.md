# live_backtest — walk-forward validation against real Quotex history

Answers one question honestly: **does the signal engine actually beat a coin flip?**

Unlike `scripts/backtest_*.py` (which assert behaviour on hand-built synthetic
candles), this harness pulls real 1-minute history from Quotex for every live
pair and replays the production engine over it with no lookahead.

## Run it

```bash
cd <repo root>                       # imports need repo root as cwd
S=/tmp/bt

# 1. pull 7 days of real 1m candles for every pair (~15 min, ~10k candles/pair)
python -u scripts/live_backtest/fetch_history.py "<QX_SESSION_TOKEN>" $S/hist 168

# 2. replay the live engine, walk-forward  (~25 min)
python -u scripts/live_backtest/backtest_all.py $S/hist $S/bt_all.json
curl -s https://binary-signals-app-production.up.railway.app/api/pairs -o $S/pairs.json
python scripts/live_backtest/report.py $S/bt_all.json $S/pairs.json

# 3. each theory measured on its own, out-of-sample  (~5 min)
python -u scripts/live_backtest/backtest_theories.py $S/hist $S/bt_theories.json

# 4. is there ANY edge in the raw data?  (~3 min)
python scripts/live_backtest/edge_scan.py $S/hist
```

## What each script does

| script | question it answers |
| --- | --- |
| `fetch_history.py` | pulls real 1m OHLC per pair via the vendored pyquotex client (same backend production uses) |
| `backtest_all.py` | replays `engines.predict()` candle-by-candle; prediction for candle *i* sees only `candles[:i]` |
| `report.py` | win rate per pair vs. that pair's **break-even** = `100/(1+payout)`, plus slices by confidence / quality / regime |
| `backtest_theories.py` | runs each module standalone and grades every individual theory vote — Wilson 95% lower bound included |
| `edge_scan.py` | baseline: textbook one-line rules + run-length distribution, to see whether the target is predictable at all |

## Fidelity notes

* Grading uses feed.py's exact rule (`close>open` = UP, `close==open` = draw and
  excluded, degenerate candle = skipped).
* `htf_trend` is a direct port of `feed._get_htf_trend`'s maths.
* `recent_accuracy` is fed back walk-forward from the backtest's own results,
  mirroring `_run_eoc`'s per-candle accuracy cache.
* `DB_PATH` points at a throwaway empty DB so the engine starts with zero
  learned adaptation — the backtest measures the engine, not a fitted snapshot.
* **The `running_tick` module cannot be replayed**: it needs per-tick data, and
  Quotex's history endpoint only returns OHLC. It is silent throughout these
  runs. In production it fires on 96% of signals, so the live signal mix is
  *not* the backtested mix — see the 2026-08-05 findings.

## Train/test discipline

Never derive a parameter and validate it on the same candles — that is how every
previous tuning round in this repo produced a different "best" theory set. Split
by timestamp first (last 2 days held out), decide on train, and let the holdout
be the only arbiter:

```bash
# in Python: write candles with time < cut to train/, >= cut to test/
cut = newest_candle_time - 2*86400
```

Then run `backtest_all.py` on `test/` before and after your change and compare.
A change that improves train but not holdout is noise — revert it and leave a
comment saying so, as was done for the `agree=0` fallback inversion.

## Results, 2026-08-05 (7 days × 19 pairs, 191,514 candles)

* Walk-forward engine: **49.75%** over 56,085 signals, 95% CI [49.3, 50.2].
  Every single pair sits below its own break-even.
* All 13 active theories land in 48.1–52.3%; none has a 95% lower bound > 50%.
* Raw data has no exploitable 1-minute structure: OTC run-lengths match a fair
  coin to within 0.2pp; every simple rule sits at 49.5–50.2%.

### Logic fixes derived from this run (5d train / 2d holdout)

| change | holdout effect |
| --- | --- |
| monotonicity bug in `_apply_calibration_caps` | ordering restored (was 2 inversions) |
| confidence calibrated off `agree` | calibration error 14.82 → **0.66** points |
| | best-vs-worst decile spread −0.06 → **+3.75pp** |
| removed `Close below prev low` theory | 48.0% pooled, n=3578, CI upper 49.7 |
| **overall win rate** | 50.31% → 50.23% — **unchanged, as expected** |

The fixes make the output honest and filterable. They do not create an edge, and
nothing here changes the conclusion that no pair clears its break-even.

Rejected by the holdout: inverting the `agree=0` fallback (train 46.45% n=973
looked like a confirmed anti-edge; holdout said 50.99% following vs 46.56%
fading). Reverted — see the comment in `blender.py`.
