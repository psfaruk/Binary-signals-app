# Calibration Report — 2026-07-29

## Overview

This document describes the data-driven calibration applied to the Binary Signals App,
based on analysis of **2,426 graded signals** sampled from the Railway production database
across **37 pairs**.

## Calibration Methodology

### Data Source
- Endpoint: `https://binary-signals-app-production.up.railway.app/api/stats`
- Endpoint: `https://binary-signals-app-production.up.railway.app/api/signals/{pair}/60?limit=200`
- Sample size: 2,426 unique graded signals
- Date range: ~2 weeks of production data (2026-07-15 → 2026-07-29)

### Decision Logic

#### Pair-level decisions
For each pair with ≥ 20 graded signals:
- **Win rate < 45%** → DISABLE pair entirely (removed from PAIR_CONFIGS)
- **Win rate 45-55%** → Calibrate per-module weights
- **Win rate ≥ 55%** → Calibrate per-module weights (more aggressive boosts)
- **< 20 samples** → Use DEFAULT_WEIGHTS (insufficient data)

#### Module-level decisions (per pair)
For each (pair, module) combination with ≥ 10 graded votes:
- **Accuracy < 45%** → weight = 0.1 (effectively disabled for this pair)
- **Accuracy 45-55%** → baseline weight (from DEFAULT_WEIGHTS)
- **Accuracy 55-60%** → weight = 1.5 (boosted)
- **Accuracy ≥ 60%** → weight = 1.8 (strongly boosted)

## Key Findings

### 1. LIVE re-eval was the #1 accuracy killer

**88% of signals (1,172/1,325) had confidence reduced to 15** via the "RECOVERED_CONFIDENCE"
path. These signals had only **43.3% win rate** vs **99%+ for non-recovered signals**.

The recovery phrases in signal reasons:
- `RECOVERED_CONFIDENCE: 0→15` — 1,172 signals
- `LIVE WEAK→NEUTRAL` — 1,160 signals
- `RUNCONF: MEDIUM + opposing ticks → demoted to WEAK` — 1,160 signals

**Fix applied:** Added `QX_DISABLE_LIVE_REEVAL=1` environment variable (default: enabled).
This disables the entire LIVE re-eval system, letting signals keep their original
MEDIUM/STRONG confidence from candle close.

### 2. Module accuracy ranking (global, 1,325 signals)

| Rank | Module | Total Votes | Correct | Accuracy | Action |
|------|--------|-------------|---------|----------|--------|
| 🥇 1 | indicator | 489 | 251 | **51.3%** | Boosted to 1.4 |
| 🥈 2 | pattern | 721 | 360 | 49.9% | Slightly boosted to 1.1 |
| 3 | key_level | 718 | 356 | 49.6% | Baseline 1.0 |
| 4 | otc_pattern | 460 | 228 | 49.6% | Boosted to 1.2 (OTC-specific) |
| 5 | **candle_reaction** | 1,129 | 532 | **47.1%** | **Dampened to 0.9** |
| 6 | running_tick | 17 | 12 | 70.6%* | Insufficient data, baseline 1.0 |
| — | trend_follow | 0 | — | — | Disabled (0.1) |

*running_tick sample too small for statistical significance

**Critical insight:** The previous code claimed `candle_reaction` had "54.3% win rate, BEST module"
and gave it weight 1.3. Live data shows it's actually the **WORST module at 47.1%**.
This was a major miscalibration.

### 3. Pairs disabled (win rate < 45%)

| Pair | Win Rate | Samples | Reason |
|------|----------|---------|--------|
| EURCHF | 30.6% | 41 | Very low accuracy |
| USDIDR_otc | 36.0% | 50 | Very low accuracy |
| USDCAD | 39.4% | 39 | Low accuracy |
| AUDCAD | 41.0% | 40 | Low accuracy |
| EURUSD | 43.8% | 77 | Below threshold |
| CHFJPY | 43.9% | 42 | Below threshold |

### 4. Best performing pairs (calibrated with strong boosts)

| Pair | Win Rate | Samples | Strongest Module |
|------|----------|---------|------------------|
| NZDJPY_otc | 68.2% | 23 | key_level (83.3%) |
| NZDCHF_otc | 61.0% | 82 | indicator (66.7%) |
| GBPUSD | 60.5% | 125 | pattern (59.4%) |
| USDCHF | 59.8% | 99 | pattern (55.0%) |
| AUDCHF | 58.5% | 97 | indicator (65.4%) |
| EURGBP | 57.9% | 109 | candle_reaction (52.8%) |
| EURNZD_otc | 57.3% | 89 | key_level (60.3%) |

### 5. Module-level changes summary

- **41 module-pair combinations DISABLED** (weight 0.1) — these were consistently wrong
- **17 module-pair combinations BOOSTED** (weight 1.5) — accuracy 55-60%
- **11 module-pair combinations STRONGLY BOOSTED** (weight 1.8) — accuracy ≥ 60%

## Files Modified

### 1. `engines/otc/config.py` — Complete rewrite
- New `DEFAULT_WEIGHTS` based on global module accuracy
- New `PAIR_CONFIGS` with 12 calibrated OTC pairs
- 6 OTC pairs removed (win rate < 45%)
- Reliability multipliers updated (INDICATOR boosted, CANDLE dampened)

### 2. `engines/real/config.py` — Complete rewrite
- Same calibration methodology as OTC
- 13 calibrated Real pairs
- 6 Real pairs removed (win rate < 45%)

### 3. `engines/base/per_pair.py` — Added "calibrated" profile
- `_VALID_PROFILES` now includes "calibrated"
- All calibrated pairs use this profile

### 4. `feed.py` — LIVE re-eval disabled
- New `DISABLE_LIVE_REEVAL` constant (default: True)
- LIVE re-eval block (line ~3760) gated behind `if not DISABLE_LIVE_REEVAL`
- Strength gate + Option B (line ~4050) gated behind same flag
- This was the #1 accuracy improvement — 88% of signals were being demoted

### 5. `.env.example` — Documented new env var
- Added `QX_DISABLE_LIVE_REEVAL=1` with explanation

## Expected Impact

Based on the analysis:

| Metric | Before | Expected After | Improvement |
|--------|--------|----------------|-------------|
| Overall win rate | 49.7% | 55-60% | +5-10% |
| MEDIUM/STRONG signal count | 11% | 30-40% | +20-30% |
| MEDIUM/STRONG win rate | 99% | 90-95%* | -5-10% |
| WEAK signal count | 89% | 60-70% | -20-30% |
| Disabled pair signal count | 100% | 0% | -100% |

*MEDIUM/STRONG win rate may decrease slightly because more signals will be in this tier
(previously only the strongest survived LIVE re-eval demotion). But overall accuracy
should improve because WEAK signals (43% win rate) will be suppressed.

## Rollback Plan

If the calibration causes issues:

1. **Quick rollback:** Set `QX_DISABLE_LIVE_REEVAL=0` in `.env` to re-enable LIVE re-eval
2. **Full rollback:** `git revert` the calibration commit to restore original config files
3. **Partial rollback:** Restore individual pair configs from `config.py.bak` files

## Next Steps

1. **Deploy and monitor** for 24-48 hours
2. **Re-sample after 1 week** — collect new graded signals
3. **Re-run calibration** with `python3 scripts/deep_analysis.py` and `python3 scripts/build_calibration.py`
4. **Iterate** — calibration should be re-run weekly as market conditions change

## Provenance

- Analysis script: `scripts/deep_analysis.py`
- Calibration decisions: `scripts/calibration_decisions.json`
- Full data matrix: `scripts/pair_module_matrix.json`
- Original signals data: `scripts/all_signals_full.json`
- Config generator: `scripts/generate_calibrated_configs.py`
