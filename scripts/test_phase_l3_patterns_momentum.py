"""PHASE-L3 PATTERNS+MOMENTUM tests (Agent C — Pattern/Momentum Engineer).

Run:  python3 scripts/test_phase_l3_patterns_momentum.py
      (from the repository root)

Covers:
  A. All 11 candlestick kinds: hand-crafted POSITIVE fixture (exact
     kind/side/start_index/confirm_index/meta["candles"]) + hand-crafted
     NEAR-MISS negative fixture per kind (exactly one condition violated,
     e.g. body dominance 0.59 vs 0.6) — 22 checks.
  B. Indicator hand-values: strictly rising closes -> RSI exactly 100.0,
     strictly falling -> exactly 0.0; a 15-candle series whose RSI-14 is
     computed BY HAND (arithmetic shown in comments below) and asserted
     to 1e-6; ROC exact value; stoch_k exactly 1.0 / 0.0 at window
     extremes; flat-series rules (all-zero changes seed RSI 50.0, HH==LL
     window -> stoch_k 0.0); velocity exact values.
  C. Warmup boundaries per key (roc i<5 / rsi i<14 / stoch_k i<13 /
     velocity i<1) and 1:1 alignment with candle indices.
  D. Divergence fixtures: crafted price higher-high + RSI lower-high ->
     exact div_bearish with exact confirm index; mirrored div_bullish;
     a case where RSI also makes a higher-high -> NO detection; min_gap
     violation -> none; lookback violation -> none; peaks inside RSI
     warmup -> pair skipped.
  E. MECHANICAL NO-LOOKAHEAD: assert_no_lookahead (multi-variant
     perturbation) for detect_patterns and detect_divergences; per-element
     proof for compute_indicators (perturb candle j -> indicators[:j]
     identical within 1e-12).
  F. DETERMINISM: assert_deterministic for both detectors; double-run
     equality for compute_indicators.
  G. FUZZ: seeds 1..5 on synthetic_series(200) — detect_patterns (and
     detect_divergences) never raise and their outputs pass
     validate_detections.
  H. HONESTY + INPUT VALIDATION: research disclaimer present in both
     module sources; no overclaim vocabulary ("guaranteed"/"sure"/
     "profitable"/"win" as whole words); empty/invalid inputs rejected
     with ContractError; degenerate flat series yields zero patterns.

No DB, no network, no broker, no credentials. Pure offline checks.
"""
import os
import re
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_PASS = 0
_FAIL = 0
_FAILURES = []


def check(cond, label, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {label}")
    else:
        _FAIL += 1
        _FAILURES.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


from core.ict.types import Candle, ContractError, validate_detections  # noqa: E402
from core.ict.helpers import (  # noqa: E402
    assert_deterministic,
    assert_no_lookahead,
    perturb,
    synthetic_series,
)
from core.ict.candlestick import detect_patterns, PATTERN_KINDS  # noqa: E402
from core.ict.momentum import compute_indicators, detect_divergences  # noqa: E402

T0 = 1_700_000_000


def mkser(rows):
    """Build a candle list from (open, high, low, close) tuples."""
    return [
        Candle(ts=T0 + i * 60, open=float(o), high=float(h),
               low=float(l), close=float(c))
        for i, (o, h, l, c) in enumerate(rows)
    ]


def candles_from_closes(closes, wick=0.125):
    """Candles whose closes are exactly `closes` (wicks are inert filler).

    Candle 0 opens slightly below closes[0]; candle i>0 opens at
    closes[i-1]. Only closes matter for RSI/ROC/velocity/divergences.
    """
    out = []
    prev = closes[0] - 0.25
    for i, c in enumerate(closes):
        o = prev if i else closes[0] - 0.25
        out.append(Candle(ts=T0 + i * 60, open=float(o),
                          high=float(max(o, c) + wick),
                          low=float(min(o, c) - wick), close=float(c)))
        prev = c
    return out


def hits(dets, kind):
    return [d for d in dets if d.kind == kind]


def check_positive(label, rows, kind, start, confirm, side):
    """Positive fixture: exactly one detection of `kind` with exact fields."""
    candles = mkser(rows)
    try:
        dets = detect_patterns(candles)
        validate_detections(dets, candles)
        found = hits(dets, kind)
        ok = (
            len(found) == 1
            and found[0].start_index == start
            and found[0].confirm_index == confirm
            and found[0].side == side
            and found[0].ts == candles[confirm].ts
            and found[0].meta.get("candles") == list(range(start, confirm + 1))
        )
        detail = f"kinds_seen={[d.kind for d in dets]}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"raised {exc!r}"
    check(ok, label, detail)


def check_negative(label, rows, kind):
    """Near-miss fixture: the violated condition must suppress `kind`."""
    candles = mkser(rows)
    try:
        dets = detect_patterns(candles)
        validate_detections(dets, candles)
        found = hits(dets, kind)
        ok = not found
        detail = f"unexpected {kind}: {[(d.start_index, d.confirm_index) for d in found]}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"raised {exc!r}"
    check(ok, label, detail)


# ════════════════════════════════════════════════════════════════════════
print("\nA. PATTERN FIXTURES (11 kinds x positive + near-miss)")

# 1. bullish_engulfing — prior bearish, current body strictly engulfs it.
check_positive(
    "A1  bullish_engulfing positive (start=0 confirm=1 side=bullish)",
    [(102, 102.2, 100.8, 101), (100.5, 103.2, 100.3, 103)],
    "bullish_engulfing", 0, 1, "bullish")
# near-miss: body dominance 2.36/4.00 = 0.59 < 0.6 — ONLY that fails
check_negative(
    "A2  bullish_engulfing near-miss (body dominance 0.59 vs 0.6)",
    [(102, 102.2, 100.8, 101), (100.82, 103.4, 99.4, 103.18)],
    "bullish_engulfing")

# 2. bearish_engulfing — prior bullish, mirrored engulfing.
check_positive(
    "A3  bearish_engulfing positive (start=0 confirm=1 side=bearish)",
    [(100, 101.2, 99.8, 101), (102, 102.2, 99.3, 99.5)],
    "bearish_engulfing", 0, 1, "bearish")
# near-miss: prior candle same colour (bearish) — ONLY opposite-colour fails
check_negative(
    "A4  bearish_engulfing near-miss (prior candle same colour)",
    [(101, 101.2, 99.8, 100), (102, 102.2, 99.3, 99.5)],
    "bearish_engulfing")

# 3. hammer — lower wick >= 2x body, upper wick <= body, body top third.
check_positive(
    "A5  hammer positive (start=0 confirm=0 side=bullish)",
    [(100, 101.5, 98, 101)],
    "hammer", 0, 0, "bullish")
# near-miss: lower wick 1.9 vs wick_mult 2.0 — ONLY that fails
check_negative(
    "A6  hammer near-miss (lower wick 1.9x body vs 2.0x required)",
    [(100, 101.5, 98.1, 101)],
    "hammer")

# 4. shooting_star — mirrored hammer.
check_positive(
    "A7  shooting_star positive (start=0 confirm=0 side=bearish)",
    [(101, 103, 99.5, 100)],
    "shooting_star", 0, 0, "bearish")
# near-miss: upper wick 1.9 vs 2.0 — ONLY that fails
check_negative(
    "A8  shooting_star near-miss (upper wick 1.9x body vs 2.0x required)",
    [(101, 102.9, 99.5, 100)],
    "shooting_star")

# 5. doji — body <= doji_ratio * range.
check_positive(
    "A9  doji positive (start=0 confirm=0 side=neutral)",
    [(100, 101, 99, 100.05)],
    "doji", 0, 0, "neutral")
# near-miss: body ratio 0.105 vs 0.1 — ONLY that fails
check_negative(
    "A10 doji near-miss (body/range 0.105 vs 0.1 required)",
    [(100, 101, 99, 100.21)],
    "doji")

# 6. morning_star — decline, small gap-down star, close above midpoint.
check_positive(
    "A11 morning_star positive (start=0 confirm=2 side=bullish)",
    [(105, 105.5, 99.5, 100), (99.5, 99.8, 98.7, 99.0), (100, 103.5, 99.5, 103)],
    "morning_star", 0, 2, "bullish")
# near-miss: close == midpoint 102.5 exactly (strict > required) — ONLY that fails
check_negative(
    "A12 morning_star near-miss (close == midpoint 102.5, strict > required)",
    [(105, 105.5, 99.5, 100), (99.5, 99.8, 98.7, 99.0), (100, 103.0, 99.5, 102.5)],
    "morning_star")

# 7. evening_star — mirrored.
check_positive(
    "A13 evening_star positive (start=0 confirm=2 side=bearish)",
    [(100, 105.5, 99.5, 105), (105.5, 106.2, 105.3, 106.0), (105, 105.2, 101.5, 102.0)],
    "evening_star", 0, 2, "bearish")
# near-miss: star body NOT fully gapped above prior body — ONLY that fails
check_negative(
    "A14 evening_star near-miss (star body not gap-up above prior body)",
    [(100, 105.5, 99.5, 105), (104.5, 105.4, 104.3, 105.2), (105, 105.2, 101.5, 102.0)],
    "evening_star")

# 8. inside_bar — strict narrower on both sides.
check_positive(
    "A15 inside_bar positive (start=0 confirm=1 side=neutral)",
    [(100, 101, 99, 100.5), (100, 100.5, 99.5, 100.2)],
    "inside_bar", 0, 1, "neutral")
# near-miss: high[1] == high[0] (strict < required) — ONLY that fails
check_negative(
    "A16 inside_bar near-miss (equal highs, strict < required)",
    [(100, 101, 99, 100.5), (100, 101.0, 99.5, 100.2)],
    "inside_bar")

# 9. outside_bar — strict wider on both sides.
check_positive(
    "A17 outside_bar positive (start=0 confirm=1 side=bullish)",
    [(100, 100, 99, 99.5), (99.8, 100.5, 98.5, 100.2)],
    "outside_bar", 0, 1, "bullish")
# near-miss: low[1] == low[0] (strict < required) — ONLY that fails
check_negative(
    "A18 outside_bar near-miss (equal lows, strict < required)",
    [(100, 100, 99, 99.5), (99.8, 100.5, 99.0, 100.2)],
    "outside_bar")

# 10. tweezer_top — equal highs, bullish -> bearish flip.
check_positive(
    "A19 tweezer_top positive (start=0 confirm=1 side=bearish)",
    [(100, 102.0, 99.5, 101), (101.5, 102.0, 100.0, 100.5)],
    "tweezer_top", 0, 1, "bearish")
# near-miss: |high diff| 0.3 > tol 0.001*102 — ONLY that fails
check_negative(
    "A20 tweezer_top near-miss (high diff 0.3 vs tolerance 0.102)",
    [(100, 102.0, 99.5, 101), (101.5, 102.3, 100.0, 100.5)],
    "tweezer_top")

# 11. tweezer_bottom — mirrored on lows.
check_positive(
    "A21 tweezer_bottom positive (start=0 confirm=1 side=bullish)",
    [(101, 101.5, 99.0, 100), (100.5, 102.0, 99.0, 101.5)],
    "tweezer_bottom", 0, 1, "bullish")
# near-miss: |low diff| 0.2 > tol 0.001*99 — ONLY that fails
check_negative(
    "A22 tweezer_bottom near-miss (low diff 0.2 vs tolerance 0.099)",
    [(101, 101.5, 99.0, 100), (100.5, 102.0, 99.2, 101.5)],
    "tweezer_bottom")

# independence: one pair may carry several kinds at once
_pair = mkser([(102, 102.2, 100.8, 101), (100.5, 103.2, 100.3, 103)])
_kinds = {d.kind for d in detect_patterns(_pair)}
check("bullish_engulfing" in _kinds and "outside_bar" in _kinds,
      "A23 kinds are independent: same pair labels engulfing AND outside_bar",
      f"kinds={sorted(_kinds)}")

# spec: morning/evening star meta carries the mid threshold used
_ms = hits(detect_patterns(mkser(
    [(105, 105.5, 99.5, 100), (99.5, 99.8, 98.7, 99.0), (100, 103.5, 99.5, 103)])),
    "morning_star")
check(len(_ms) == 1 and _ms[0].meta["mid_threshold"] == 102.5,
      "A24 morning_star meta carries mid_threshold == 102.5",
      f"meta={_ms[0].meta if _ms else None}")

# ════════════════════════════════════════════════════════════════════════
print("\nB. INDICATOR HAND-VALUES")

# B1/B6: strictly rising closes -> every gain, avg_loss stays exactly 0
# -> RSI exactly 100.0; close is the window high -> stoch_k exactly 1.0.
_rising = []
_prev = 99.75
for _i in range(20):
    _cl = 100.0 + 0.5 * _i
    _o = _prev if _i else 99.75
    _rising.append(Candle(ts=T0 + _i * 60, open=_o, high=_cl,
                          low=min(_o, _cl) - 0.125, close=_cl))
    _prev = _cl
indr = compute_indicators(_rising)
check(all(indr[i]["rsi"] == 100.0 for i in range(14, 20)),
      "B1 strictly rising closes -> RSI exactly 100.0 from i=14",
      f"rsi={[indr[i]['rsi'] for i in range(14, 20)]}")
check(all(indr[i]["stoch_k"] == 1.0 for i in range(13, 20)),
      "B2 close at window high -> stoch_k exactly 1.0 from i=13",
      f"stoch={[indr[i]['stoch_k'] for i in range(13, 20)]}")

_falling = []
_prev = 100.25
for _i in range(20):
    _cl = 100.0 - 0.5 * _i
    _o = _prev if _i else 100.25
    _falling.append(Candle(ts=T0 + _i * 60, open=_o,
                           high=max(_o, _cl) + 0.125, low=_cl, close=_cl))
    _prev = _cl
indf = compute_indicators(_falling)
check(all(indf[i]["rsi"] == 0.0 for i in range(14, 20)),
      "B3 strictly falling closes -> RSI exactly 0.0 from i=14",
      f"rsi={[indf[i]['rsi'] for i in range(14, 20)]}")
check(all(indf[i]["stoch_k"] == 0.0 for i in range(13, 20)),
      "B4 close at window low -> stoch_k exactly 0.0 from i=13",
      f"stoch={[indf[i]['stoch_k'] for i in range(13, 20)]}")

# B5: hand-computed Wilder RSI-14 on a 15-candle series.
#
#   closes:      100 102 101 103 102 104 103 105 104 106 105 107 106 108 107
#   changes d1..d14: +2  -1 +2  -1 +2  -1 +2  -1 +2  -1 +2  -1 +2  -1
#   gains : 7 changes of +2 -> total 14 -> avg_gain = 14/14 = 1.0
#   losses: 7 changes of -1 -> total  7 -> avg_loss =  7/14 = 0.5
#   RSI(14) at i=14 = 100 * avg_gain / (avg_gain + avg_loss)
#                   = 100 * 1.0 / 1.5 = 66.666666... = 200/3
_hand_closes = [100, 102, 101, 103, 102, 104, 103, 105, 104,
                106, 105, 107, 106, 108, 107]
indh = compute_indicators(candles_from_closes(_hand_closes, wick=0.25))
check(abs(indh[14]["rsi"] - 200.0 / 3.0) <= 1e-6,
      "B5 hand-computed Wilder RSI-14 == 200/3 (66.666..., 1e-6)",
      f"got {indh[14]['rsi']!r}")

# B6: ROC exact — close[5]/close[0] - 1 = 104/100 - 1 = 0.04
check(abs(indh[5]["roc"] - 0.04) <= 1e-12,
      "B6 ROC[5] == close[5]/close[0]-1 == 0.04 (1e-12)",
      f"got {indh[5]['roc']!r}")

# B7: velocity exact values from the same series
check(indh[1]["velocity"] == 2.0 and indh[2]["velocity"] == -1.0,
      "B7 velocity exact (d1 = +2.0, d2 = -1.0)",
      f"vel1={indh[1]['velocity']!r} vel2={indh[2]['velocity']!r}")

# B8-B10: flat series rules
_flat = mkser([(100.0, 100.0, 100.0, 100.0)] * 20)
indflat = compute_indicators(_flat)
check(all(indflat[i]["rsi"] == 50.0 for i in range(14, 20)),
      "B8 all-zero changes seed RSI 50.0 (contract rule)",
      f"rsi={[indflat[i]['rsi'] for i in range(14, 20)]}")
check(all(indflat[i]["stoch_k"] == 0.0 for i in range(13, 20)),
      "B9 HH==LL window -> stoch_k 0.0 (contract rule)",
      f"stoch={[indflat[i]['stoch_k'] for i in range(13, 20)]}")
check(indflat[5]["roc"] == 0.0 and indflat[19]["roc"] == 0.0
      and indflat[7]["velocity"] == 0.0,
      "B10 flat series: ROC 0.0 and velocity 0.0",
      f"roc5={indflat[5]['roc']!r} vel7={indflat[7]['velocity']!r}")

check(all(set(row.keys()) == {"roc", "rsi", "stoch_k", "velocity"}
          for row in indr),
      "B11 every indicator row exposes exactly the 4 contract keys")

# ════════════════════════════════════════════════════════════════════════
print("\nC. WARMUP BOUNDARIES (rising 20-candle series)")

check(all(indr[i]["roc"] is None for i in range(5))
      and indr[5]["roc"] is not None,
      "C1 roc: None for i<5, first value AT i=5")
check(all(indr[i]["rsi"] is None for i in range(14))
      and indr[14]["rsi"] is not None,
      "C2 rsi: None for i<14, first value AT i=14")
check(all(indr[i]["stoch_k"] is None for i in range(13))
      and indr[13]["stoch_k"] is not None,
      "C3 stoch_k: None for i<13, first value AT i=13")
check(indr[0]["velocity"] is None
      and all(indr[i]["velocity"] is not None for i in range(1, 20)),
      "C4 velocity: None at i=0 only")
check(len(indr) == 20,
      "C5 indicator vector aligned 1:1 with candle indices",
      f"len={len(indr)}")

# ════════════════════════════════════════════════════════════════════════
print("\nD. DIVERGENCE FIXTURES")


def bear_div_closes():
    """Peak 19 = 111.5 (sharp +3 rally), peak 38 = 112.5 (slow +1 climb).

    The sharp rally leaves RSI(19) high (~77); the pullback plus slow
    grind leaves RSI(38) lower (~71) despite the higher close — the
    crafted RSI lower-high path.
    """
    cs = [100.0 - 0.5 * i for i in range(14)]          # 0..13 drift down
    cs += [cs[-1] + 3.0 * k for k in range(1, 7)]      # 14..19 sharp rally
    cs += [cs[-1] - 2.0 * k for k in range(1, 7)]      # 20..25 pullback
    cs += [cs[-1] + 1.0 * k for k in range(1, 14)]     # 26..38 slow climb
    cs += [cs[-1] - 1.5 * k for k in range(1, 7)]      # 39..44 fade
    return cs


def bull_div_closes():
    """Trough 19 = 88.5 (sharp -3 drop), trough 38 = 87.5 (slow -1 slide).

    Mirror of the bearish fixture: RSI(19) ~23, RSI(38) ~29 (higher low
    on momentum) while price makes a lower low.
    """
    cs = [100.0 + 0.5 * i for i in range(14)]          # 0..13 drift up
    cs += [cs[-1] - 3.0 * k for k in range(1, 7)]      # 14..19 sharp drop
    cs += [cs[-1] + 2.0 * k for k in range(1, 7)]      # 20..25 rally
    cs += [cs[-1] - 1.0 * k for k in range(1, 14)]     # 26..38 slow slide
    cs += [cs[-1] + 1.5 * k for k in range(1, 7)]      # 39..44 recovery
    return cs


def rsi_hh_closes():
    """Peak 19 = 103.4 (gentle +1 rally), peak 36 = 124.9 (strong +2 rally).

    Momentum accelerates into the second peak, so RSI(36) > RSI(19) —
    RSI also makes a higher high -> no bearish divergence.
    """
    cs = [100.0 - 0.2 * i for i in range(14)]          # 0..13 drift down
    cs += [cs[-1] + 1.0 * k for k in range(1, 7)]      # 14..19 gentle rally
    cs += [cs[-1] - 0.5 * k for k in range(1, 6)]      # 20..24 shallow dip
    cs += [cs[-1] + 2.0 * k for k in range(1, 13)]     # 25..36 strong rally
    cs += [cs[-1] - 1.5 * k for k in range(1, 9)]      # 37..44 fade
    return cs


cd_bear = candles_from_closes(bear_div_closes())
indb = compute_indicators(cd_bear)
dets_bear = detect_divergences(cd_bear)
_d = dets_bear[0] if len(dets_bear) == 1 else None
check(
    _d is not None
    and _d.kind == "div_bearish"
    and _d.start_index == 19
    and _d.confirm_index == 43          # second peak 38 + swing_window 5
    and _d.side == "bearish"
    and _d.ts == cd_bear[43].ts
    and _d.meta["candles"] == [19, 38]
    and _d.meta["price_high_1"] == 111.5
    and _d.meta["price_high_2"] == 112.5,
    "D1 price HH + RSI LH -> exactly one div_bearish (start=19, confirm=43)",
    f"dets={[(x.kind, x.start_index, x.confirm_index) for x in dets_bear]}")

check(
    len(dets_bear) == 1
    and dets_bear[0].meta["rsi_1"] == indb[19]["rsi"]
    and dets_bear[0].meta["rsi_2"] == indb[38]["rsi"]
    and dets_bear[0].meta["rsi_1"] > dets_bear[0].meta["rsi_2"],
    "D2 div_bearish meta RSI equals compute_indicators at the peaks and is LH",
    f"rsi1={dets_bear[0].meta['rsi_1']:.4f} rsi2={dets_bear[0].meta['rsi_2']:.4f}"
    if dets_bear else "no detection")

cd_bull = candles_from_closes(bull_div_closes())
indu = compute_indicators(cd_bull)
dets_bull = detect_divergences(cd_bull)
_d = dets_bull[0] if len(dets_bull) == 1 else None
check(
    _d is not None
    and _d.kind == "div_bullish"
    and _d.start_index == 19
    and _d.confirm_index == 43
    and _d.side == "bullish"
    and _d.ts == cd_bull[43].ts
    and _d.meta["candles"] == [19, 38]
    and _d.meta["price_low_1"] == 88.5
    and _d.meta["price_low_2"] == 87.5
    and _d.meta["rsi_1"] == indu[19]["rsi"]
    and _d.meta["rsi_2"] == indu[38]["rsi"]
    and _d.meta["rsi_2"] > _d.meta["rsi_1"],
    "D3 price LL + RSI HL -> exactly one div_bullish (start=19, confirm=43)",
    f"dets={[(x.kind, x.start_index, x.confirm_index) for x in dets_bull]}")

cd_hh = candles_from_closes(rsi_hh_closes())
indh2 = compute_indicators(cd_hh)
dets_hh = detect_divergences(cd_hh)
check(len(dets_hh) == 0 and indh2[36]["rsi"] > indh2[19]["rsi"],
      "D4 price HH but RSI also HH -> NO divergence of either kind",
      f"rsi19={indh2[19]['rsi']:.3f} rsi36={indh2[36]['rsi']:.3f} "
      f"dets={len(dets_hh)}")

check(detect_divergences(cd_bear, min_gap=25) == [],
      "D5 min_gap violation (gap 19 < min_gap 25) -> no detection")

check(detect_divergences(cd_bear, lookback=10) == [],
      "D6 lookback violation (gap 19 > lookback 10) -> no detection")

# peaks at 6 and 12 (close 103 -> 103.5, higher high, gap 6 >= 3) but both
# sit inside the RSI-14 warmup, so the pair is skipped (RSI is None there)
_warm = candles_from_closes(
    [100, 100.5, 101, 101.5, 102, 102.5, 103.0,
     102.0, 101.5, 101.0, 101.5, 102.0, 103.5,
     102.5, 102.0, 101.5, 101.0, 100.5, 100.0, 99.5])
check(detect_divergences(_warm) == [],
      "D7 peaks inside RSI warmup (6, 12) -> pair skipped (RSI None)")

# ════════════════════════════════════════════════════════════════════════
print("\nE. MECHANICAL NO-LOOKAHEAD")

try:
    assert_no_lookahead(detect_patterns, synthetic_series(150, seed=7))
    check(True, "E1 detect_patterns passes multi-variant perturbation "
                "(synthetic_series(150, seed=7))")
except AssertionError as exc:
    check(False, "E1 detect_patterns no-lookahead", str(exc)[:300])

try:
    assert_no_lookahead(detect_divergences, synthetic_series(200, seed=8))
    check(True, "E2 detect_divergences passes multi-variant perturbation "
                "(synthetic_series(200, seed=8))")
except AssertionError as exc:
    check(False, "E2 detect_divergences no-lookahead", str(exc)[:300])


def _ind_no_lookahead():
    """Perturb candle j (all 4 variants) -> indicators[:j] identical."""
    series = synthetic_series(60, seed=11)
    base = compute_indicators(series)
    for j in range(len(series)):
        for variant in (0, 1, 2, 3):
            after = compute_indicators(perturb(series, j, variant))
            for i in range(j):
                for key in ("roc", "rsi", "stoch_k", "velocity"):
                    a, b = base[i][key], after[i][key]
                    if (a is None) != (b is None):
                        return False, f"j={j} v={variant} i={i} {key}: {a!r} vs {b!r}"
                    if a is not None and abs(a - b) > 1e-12:
                        return False, f"j={j} v={variant} i={i} {key}: {a} vs {b}"
    return True, ""


try:
    _ok, _detail = _ind_no_lookahead()
    check(_ok, "E3 compute_indicators per-element no-lookahead "
               "(perturb candle j -> indicators[:j] identical within 1e-12)",
          _detail)
except Exception as exc:  # noqa: BLE001
    check(False, "E3 compute_indicators no-lookahead", repr(exc))

# ════════════════════════════════════════════════════════════════════════
print("\nF. DETERMINISM")

try:
    assert_deterministic(detect_patterns, synthetic_series(200, seed=6))
    check(True, "F1 detect_patterns deterministic (200 candles, seed 6, 3 runs)")
except AssertionError as exc:
    check(False, "F1 detect_patterns determinism", str(exc)[:300])

try:
    assert_deterministic(detect_divergences, synthetic_series(200, seed=6))
    check(True, "F2 detect_divergences deterministic (200 candles, seed 6)")
except AssertionError as exc:
    check(False, "F2 detect_divergences determinism", str(exc)[:300])

_series6 = synthetic_series(200, seed=6)
check(compute_indicators(_series6) == compute_indicators(_series6),
      "F3 compute_indicators byte-identical across repeated runs")

# ════════════════════════════════════════════════════════════════════════
print("\nG. FUZZ (seeds 1..5, synthetic_series(200))")

for _seed in (1, 2, 3, 4, 5):
    try:
        _cs = synthetic_series(200, seed=_seed)
        _dp = detect_patterns(_cs)
        validate_detections(_dp, _cs)
        _dd = detect_divergences(_cs)
        validate_detections(_dd, _cs)
        check(isinstance(_dp, list) and isinstance(_dd, list),
              f"G{_seed} seed={_seed}: detectors run without raising and "
              f"outputs validate",
              f"patterns={len(_dp)} divs={len(_dd)}")
    except Exception as exc:  # noqa: BLE001
        check(False, f"G{_seed} seed={_seed} fuzz", repr(exc))

# ════════════════════════════════════════════════════════════════════════
print("\nH. HONESTY + INPUT VALIDATION")

_src_c = open(os.path.join(_REPO, "core", "ict", "candlestick.py"),
              encoding="utf-8").read()
_src_m = open(os.path.join(_REPO, "core", "ict", "momentum.py"),
              encoding="utf-8").read()

check("RESEARCH-ONLY" in _src_c and "NOT predictive" in _src_c,
      "H1 candlestick.py source carries the research disclaimer")
check("RESEARCH-ONLY" in _src_m and "NOT predictive" in _src_m,
      "H2 momentum.py source carries the research disclaimer")

_overclaim = re.compile(r"\b(?:guaranteed|sure|profitable|win|wins|winning)\b",
                        re.IGNORECASE)
check(_overclaim.search(_src_c) is None,
      "H3 candlestick.py free of overclaim vocabulary "
      "(guaranteed/sure/profitable/win as whole words)")
check(_overclaim.search(_src_m) is None,
      "H4 momentum.py free of overclaim vocabulary")


def _raises_contract(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return False
    except ContractError:
        return True


check(_raises_contract(detect_patterns, []),
      "H5 detect_patterns rejects empty candle list (ContractError)")
check(_raises_contract(detect_divergences, []),
      "H6 detect_divergences rejects empty candle list (ContractError)")
check(_raises_contract(compute_indicators, []),
      "H7 compute_indicators rejects empty candle list (ContractError)")

check(_raises_contract(detect_patterns, synthetic_series(3),
                       body_dominance=0.0),
      "H8 detect_patterns rejects body_dominance=0.0")
check(_raises_contract(detect_divergences, synthetic_series(30),
                       swing_window=0),
      "H9 detect_divergences rejects swing_window=0")
check(_raises_contract(compute_indicators, synthetic_series(30), rsi_n=0),
      "H10 compute_indicators rejects rsi_n=0")

check(len(PATTERN_KINDS) == 11 and len(set(PATTERN_KINDS)) == 11,
      "H11 exactly 11 distinct pattern kinds exported",
      str(PATTERN_KINDS))

check(detect_patterns(mkser([(100.0, 100.0, 100.0, 100.0)] * 5)) == [],
      "H12 degenerate flat series yields zero pattern detections")

# ════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 72)
print(f"PHASE-L3 PATTERNS+MOMENTUM: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for f in _FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
