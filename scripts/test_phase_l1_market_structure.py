"""PHASE-L1 MARKET STRUCTURE battery (Phase L, Agent A / Task L1).

Run:  python3 scripts/test_phase_l1_market_structure.py   (from repo root)

Self-contained, offline, deterministic. No network, no broker, no
credentials, no database. Hand-crafted candle fixtures with EXACT expected
detections (computed by hand from the frozen definitions), adversarial edge
cases, mechanical no-lookahead proofs (multi-variant perturbation via
core.ict.helpers.assert_no_lookahead), determinism proofs, fuzz property
checks over synthetic series, and source honesty checks on the module.

Coverage (all checks; exit code 0 iff all pass):
  P. fixture sanity (validate_candles, catch nothing)          (1 check)
  A. detect_swings: hand-crafted fixtures + tie cancellation  (11 checks)
  B. detect_structure_breaks: BOS/CHoCH fixtures               (16 checks)
  C. detect_trend_transitions: fixtures                        (7 checks)
  D. input validation edges                                   (6 checks)
  E. mechanical no-lookahead (synthetic + fixtures)            (8 checks)
  F. determinism                                              (3 checks)
  G. fuzz properties (seeds 1..5, n=150)                       (6 checks)
  H. module source honesty                                    (4 checks)
"""
import inspect
import os
import re
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.ict.types import (  # noqa: E402
    Candle,
    ContractError,
    Detection,
    validate_candles,
    validate_detections,
)
from core.ict.helpers import (  # noqa: E402
    assert_deterministic,
    assert_no_lookahead,
    synthetic_series,
)
from core.ict import market_structure as ms  # noqa: E402

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


def _raises_contract_error(fn, *args, **kwargs):
    """True iff fn(*args, **kwargs) raises ContractError (nothing else)."""
    try:
        fn(*args, **kwargs)
        return False
    except ContractError:
        return True
    except Exception:
        return False


T0 = (1_700_000_000 // 60) * 60


def _c(i, o, h, l, c):
    """One hand-crafted candle at index i (ts strictly increasing)."""
    return Candle(ts=T0 + i * 60, open=o, high=h, low=l, close=c, volume=100.0)


def _swing_tuples(ds):
    return [
        (d.kind, d.meta["swing_index"], d.confirm_index, float(d.meta["level"]))
        for d in ds
    ]


def _break_tuples(ds):
    return [
        (
            d.kind,
            d.confirm_index,
            d.meta["broken_swing_index"],
            float(d.meta["broken_level"]),
            d.meta["trend_before"],
        )
        for d in ds
    ]


def _trans_tuples(ds):
    return [
        (d.kind, d.confirm_index, d.meta["prev"], d.meta["new"], d.meta["swing_index"])
        for d in ds
    ]


# ── hand-crafted fixtures ────────────────────────────────────────────────
# F1: swing geometry with an equal-high tie (H[11] == H[12] == 106) that
#     CANCELS the otherwise-confirmed 106 swing high. o=c=97 everywhere.
F1 = [
    _c(0, 97, 100, 95, 97), _c(1, 97, 101, 95, 97), _c(2, 97, 103, 94, 97),
    _c(3, 97, 110, 96, 97), _c(4, 97, 104, 95, 97), _c(5, 97, 102, 94, 97),
    _c(6, 97, 100, 91, 97), _c(7, 97, 99, 93, 97), _c(8, 97, 98, 95, 97),
    _c(9, 97, 101, 96, 97), _c(10, 97, 104, 90, 97), _c(11, 97, 106, 92, 97),
    _c(12, 97, 106, 94, 97), _c(13, 97, 104, 96, 97), _c(14, 97, 102, 96, 97),
    _c(15, 97, 100, 95, 97),
]
F1_SWINGS = [
    ("swing_low", 2, 4, 94.0), ("swing_high", 3, 5, 110.0),
    ("swing_low", 6, 8, 91.0), ("swing_low", 10, 12, 90.0),
]

# F2: full market-structure narrative -- HH/HL uptrend, BOS above the last
#     swing high, reversal CHoCH below the last HL while trend is still
#     bullish, LH/LL downtrend, continuation BOS below the last swing low,
#     and a final upward CHoCH against the bearish trend.
F2 = [
    _c(0, 100, 101, 96, 99), _c(1, 99, 100, 95.5, 98), _c(2, 98, 99, 95, 97),
    _c(3, 97, 100.5, 96, 100), _c(4, 100, 101, 99, 101),
    _c(5, 101, 105, 100, 104), _c(6, 104, 104.5, 101, 102),
    _c(7, 102, 103, 99, 100), _c(8, 100, 101, 98, 99), _c(9, 99, 100, 98.5, 99.5),
    _c(10, 99.5, 100.5, 99, 100), _c(11, 100, 106, 99.5, 104),
    _c(12, 104, 107, 103, 106), _c(13, 106, 108, 105, 107),
    _c(14, 107, 107.5, 104, 105), _c(15, 105, 106, 103, 104),
    _c(16, 104, 108.5, 102.5, 108), _c(17, 108, 109.5, 107, 109),
    _c(18, 107, 107.5, 102, 103.5), _c(19, 103.5, 104, 96, 102),
    _c(20, 101, 102, 96.5, 97), _c(21, 97, 99, 96.2, 98.5),
    _c(22, 98.5, 101, 97.5, 100.5), _c(23, 100.5, 103, 99, 102),
    _c(24, 102, 102.5, 100, 100.5), _c(25, 100.5, 101.5, 98, 99),
    _c(26, 99, 100, 97, 97.5), _c(27, 97.5, 98, 95.2, 95.5),
    _c(28, 95.5, 97, 95.3, 96.5), _c(29, 96.5, 98, 95.4, 97.5),
    _c(30, 97.5, 104.5, 97, 104), _c(31, 104, 106, 96.5, 105.5),
    _c(32, 105.5, 106.5, 104, 106),
]
F2_SWINGS = [
    ("swing_low", 2, 4, 95.0), ("swing_high", 5, 7, 105.0),
    ("swing_low", 8, 10, 98.0), ("swing_high", 13, 15, 108.0),
    ("swing_high", 17, 19, 109.5), ("swing_low", 19, 21, 96.0),
    ("swing_high", 23, 25, 103.0), ("swing_low", 27, 29, 95.2),
]
F2_BREAKS = [
    ("bos_bullish", 12, 5, 105.0, "neutral"),
    ("bos_bullish", 17, 13, 108.0, "bullish"),
    ("choch_bearish", 20, 8, 98.0, "bullish"),
    ("bos_bearish", 27, 19, 96.0, "bearish"),
    ("choch_bullish", 30, 23, 103.0, "bearish"),
]
F2_TRANSITIONS = [
    ("trend_bullish", 15, "neutral", "bullish", 13),
    ("trend_neutral", 21, "bullish", "neutral", 19),
    ("trend_bearish", 25, "neutral", "bearish", 23),
]

# F3: direct neutral -> bullish -> bearish via an OUTSIDE BAR (candle 15 is
#     both swing high 108 and swing low 94; both confirm at candle 17, so
#     LH+LL are established simultaneously and the trend flips directly).
F3 = [
    _c(0, 100, 101, 96, 99), _c(1, 99, 100, 95.5, 98), _c(2, 98, 99, 95, 97),
    _c(3, 97, 100.5, 96, 100), _c(4, 100, 101, 99, 101),
    _c(5, 101, 105, 100, 104), _c(6, 104, 104.5, 101, 102),
    _c(7, 102, 103, 99, 100), _c(8, 100, 101, 98, 99), _c(9, 99, 100, 98.5, 99.5),
    _c(10, 99.5, 100.5, 99, 100), _c(11, 100, 110, 99.5, 109),
    _c(12, 109, 109.5, 105, 106), _c(13, 106, 107, 103, 104),
    _c(14, 104, 105, 101, 102), _c(15, 102, 108, 94, 96), _c(16, 96, 103, 95, 100),
    _c(17, 100, 102, 95.5, 101), _c(18, 101, 103, 96, 102),
]
F3_SWINGS = [
    ("swing_low", 2, 4, 95.0), ("swing_high", 5, 7, 105.0),
    ("swing_low", 8, 10, 98.0), ("swing_high", 11, 13, 110.0),
    ("swing_high", 15, 17, 108.0), ("swing_low", 15, 17, 94.0),
]
F3_BREAKS = [
    ("bos_bullish", 11, 5, 105.0, "neutral"),
    ("choch_bearish", 15, 8, 98.0, "bullish"),
]
F3_TRANSITIONS = [
    ("trend_bullish", 13, "neutral", "bullish", 11),
    ("trend_bearish", 17, "bullish", "bearish", 15),
]

# F4: re-break of the SAME price level (105.0) via a NEW swing occurrence
#     (idx 13) that formed after an intervening opposite-side update (swing
#     low idx 10). Both breaks must be emitted -- per-occurrence tracking.
F4 = [
    _c(0, 98, 100, 96, 99), _c(1, 99, 103, 96.5, 102), _c(2, 102, 104, 97, 103),
    _c(3, 103, 105, 97.5, 104), _c(4, 104, 104.5, 98, 103.5),
    _c(5, 103.5, 103.5, 98.5, 102.5), _c(6, 102.5, 102.5, 99, 101.5),
    _c(7, 101.5, 106.5, 99.5, 106), _c(8, 106, 106.5, 100.5, 104),
    _c(9, 104, 104.8, 101, 103), _c(10, 103, 103.6, 100, 101),
    _c(11, 101, 102.5, 101, 102), _c(12, 102, 103.5, 102, 103),
    _c(13, 103, 105, 102.5, 104), _c(14, 104, 104.5, 103, 103.5),
    _c(15, 103.5, 104, 103.5, 103.8), _c(16, 103.8, 103.8, 103, 103.2),
    _c(17, 103.2, 106.5, 102.8, 106), _c(18, 106, 106.5, 102.5, 104.5),
    _c(19, 104.5, 105, 103, 103.5),
]
F4_SWINGS = [
    ("swing_high", 3, 5, 105.0), ("swing_low", 10, 12, 100.0),
    ("swing_high", 13, 15, 105.0),
]
F4_BREAKS = [
    ("bos_bullish", 7, 3, 105.0, "neutral"),
    ("bos_bullish", 17, 13, 105.0, "neutral"),
]

# F5: confirmation gate. Candle 7 closes 99 -- ABOVE the older swing high
#     (98, confirmed at 5) but BELOW a newer not-yet-confirmed swing high
#     (100, swing idx 8, confirm 10). The break of 98 at candle 7 must NOT
#     be blocked by the unconfirmed level (an implementation that used the
#     full-series swing list as a cascade target would suppress it -- that
#     is lookahead). The 100 level itself breaks only at candle 11.
F5 = [
    _c(0, 94.5, 95, 90, 94), _c(1, 95.5, 96, 90.5, 95), _c(2, 96.5, 97, 91, 96),
    _c(3, 97.5, 98, 91.5, 97), _c(4, 96.8, 97, 92, 96.5), _c(5, 95.8, 96, 92.5, 95.5),
    _c(6, 96.5, 97, 93, 96.5), _c(7, 98.8, 99.5, 93.5, 99),
    _c(8, 99.2, 100, 94, 99.5), _c(9, 98.8, 99, 94.5, 98.5),
    _c(10, 98.3, 98.5, 95, 98), _c(11, 98.5, 101.5, 95.5, 101),
]
F5_SWINGS = [("swing_high", 3, 5, 98.0), ("swing_high", 8, 10, 100.0)]
F5_BREAKS = [
    ("bos_bullish", 7, 3, 98.0, "neutral"),
    ("bos_bullish", 11, 8, 100.0, "neutral"),
]

# ── P. fixture sanity ────────────────────────────────────────────────────
print("\nP. FIXTURE SANITY")
for _name, _fx in (("F1", F1), ("F2", F2), ("F3", F3), ("F4", F4), ("F5", F5)):
    validate_candles(_fx)  # raises on any invariant violation (catch nothing)
check(True, "P1 all five hand-crafted fixtures pass validate_candles")

# ── A. detect_swings ─────────────────────────────────────────────────────
print("\nA. DETECT_SWINGS")

sw1 = ms.detect_swings(F1)
validate_detections(sw1, F1)  # raises on violation (catch nothing)
check(_swing_tuples(sw1) == F1_SWINGS, "A1 F1 exact swing tuples",
      f"got {_swing_tuples(sw1)}")
check(
    all(d.meta["swing_index"] not in (11, 12) for d in sw1)
    and not any(d.kind == "swing_high" and float(d.meta["level"]) == 106.0 for d in sw1),
    "A2 F1 equal-high tie (106 == 106) CANCELS the swing -- not detected",
)
check(
    all(
        (d.side == "bearish" if d.kind == "swing_high" else d.side == "bullish")
        and d.level_low == d.level_high == d.meta["level"]
        and set(d.meta.keys()) == {"level", "swing_index"}
        for d in sw1
    ),
    "A3 F1 side mapping (high=bearish/low=bullish), level fields, meta keys",
)
check(
    all(
        d.start_index == d.meta["swing_index"]
        and d.ts == F1[d.confirm_index].ts
        for d in sw1
    ),
    "A4 F1 start_index == swing_index and ts == candles[confirm_index].ts",
)
check(
    all(d.confirm_index - d.meta["swing_index"] == 2 for d in sw1),
    "A5 F1 honest lag: confirm_index - swing_index == k for every swing",
)
check(ms.detect_swings(F1[:4]) == [], "A6 series shorter than 2k+1 -> []")
TIE = [
    _c(0, 99, 101, 95, 99), _c(1, 99, 102, 94, 99), _c(2, 99, 103, 94, 99),
    _c(3, 99, 103, 94, 99), _c(4, 99, 102, 95, 99), _c(5, 99, 101, 96, 99),
    _c(6, 99, 100, 96, 99),
]
check(ms.detect_swings(TIE) == [], "A7 duplicate highs AND duplicate lows never produce swings")
TIE_POS = [
    _c(0, 99, 101, 95, 99), _c(1, 99, 102, 94, 99), _c(2, 99, 103, 93.5, 99),
    _c(3, 99, 103, 94, 99), _c(4, 99, 102, 95, 99), _c(5, 99, 101, 96, 99),
    _c(6, 99, 100, 96, 99),
]
check(
    _swing_tuples(ms.detect_swings(TIE_POS)) == [("swing_low", 2, 4, 93.5)],
    "A8 tie-break positive control (non-vacuous): breaking the low tie yields the swing",
)
KD = [
    _c(0, 99, 100, 95, 99), _c(1, 99, 106, 96, 99), _c(2, 99, 100, 95, 99),
    _c(3, 99, 107, 96, 99), _c(4, 99, 100, 95, 99), _c(5, 99, 100, 95, 99),
    _c(6, 99, 100, 95, 99),
]
KD_K1 = [("swing_high", 1, 2, 106.0), ("swing_low", 2, 3, 95.0), ("swing_high", 3, 4, 107.0)]
KD_K2 = [("swing_high", 3, 5, 107.0)]
sw_k1 = ms.detect_swings(KD, k=1)
sw_k2 = ms.detect_swings(KD, k=2)
check(_swing_tuples(sw_k1) == KD_K1, "A9 k=1 exact tuples", f"got {_swing_tuples(sw_k1)}")
check(_swing_tuples(sw_k2) == KD_K2, "A10 k=2 exact tuples", f"got {_swing_tuples(sw_k2)}")
check(
    _swing_tuples(sw_k1) != _swing_tuples(sw_k2)
    and all(d.confirm_index - d.meta["swing_index"] == 1 for d in sw_k1)
    and all(d.confirm_index - d.meta["swing_index"] == 2 for d in sw_k2),
    "A11 k=1 vs k=2 differ on the same series; lag == k in both",
)

# ── B. detect_structure_breaks ───────────────────────────────────────────
print("\nB. DETECT_STRUCTURE_BREAKS")

sw2 = ms.detect_swings(F2)
br2 = ms.detect_structure_breaks(F2)
validate_detections(br2, F2)
check(_swing_tuples(sw2) == F2_SWINGS, "B1 F2 exact swing tuples (fixture preamble)",
      f"got {_swing_tuples(sw2)}")
check(_break_tuples(br2) == F2_BREAKS, "B2 F2 exact break tuples",
      f"got {_break_tuples(br2)}")
check(
    ("bos_bullish", 17, 13, 108.0, "bullish") in _break_tuples(br2),
    "B3 F2 HH/HL uptrend BOS above the last swing high (trend_before bullish)",
)
check(
    ("choch_bearish", 20, 8, 98.0, "bullish") in _break_tuples(br2),
    "B4 F2 reversal CHoCH: close below the last HL while trend still bullish",
)
check(
    all(
        (d.side == "bullish") == d.kind.endswith("bullish")
        and (d.side == "bearish") == d.kind.endswith("bearish")
        for d in br2
    ),
    "B5 F2 side mapping for all four break kinds",
)
check(
    all(
        d.start_index == d.meta["broken_swing_index"]
        and d.level_low == d.level_high == d.meta["broken_level"]
        and d.ts == F2[d.confirm_index].ts
        and set(d.meta.keys()) == {"broken_level", "broken_swing_index", "trend_before"}
        for d in br2
    ),
    "B6 F2 start_index/levels/ts/meta-keys contract on every break",
)
check(
    all(d.confirm_index != 16 for d in br2),
    "B7 F2 NO break at candle 16 (close 108 EQUALS the swing level -- strict inequality required)",
)
check(
    len({(d.side, d.meta["broken_swing_index"]) for d in br2}) == len(br2),
    "B8 F2 each swing occurrence broken at most once (no re-break)",
)
check(ms.detect_structure_breaks(F1) == [], "B9 F1 no breaks (price never exceeds any level)")
check(
    ms.detect_structure_breaks(F2, 2, swings=list(sw2)) == br2,
    "B10 precomputed swings parameter reproduces the default output exactly",
)
_bad = [Detection(kind="fvg", start_index=2, confirm_index=4, ts=F2[4].ts,
                  side="bullish", level_low=1, level_high=2, meta={})]
check(
    _raises_contract_error(ms.detect_structure_breaks, F2, 2, _bad),
    "B11 provided swings with a foreign kind -> ContractError",
)
check(
    _raises_contract_error(ms.detect_structure_breaks, KD, 2, ms.detect_swings(KD, k=1)),
    "B12 k=1 swings passed with k=2 -> ContractError (confirm_index mismatch)",
)
check(ms.detect_structure_breaks(F2[:4]) == [], "B13 series shorter than 2k+1 -> []")
br3 = ms.detect_structure_breaks(F3)
validate_detections(br3, F3)
check(
    _break_tuples(br3) == F3_BREAKS
    and _swing_tuples(ms.detect_swings(F3)) == F3_SWINGS,
    "B14 F3 exact swings + breaks (outside-bar fixture)",
)
br5 = ms.detect_structure_breaks(F5)
validate_detections(br5, F5)
check(
    _break_tuples(br5) == F5_BREAKS
    and _swing_tuples(ms.detect_swings(F5)) == F5_SWINGS,
    "B15 F5 confirmation gate: candle-7 break of 98 NOT blocked by the "
    "then-unconfirmed swing high (100, confirm 10); 100 breaks only at 11",
)
br4 = ms.detect_structure_breaks(F4)
validate_detections(br4, F4)
check(
    _break_tuples(br4) == F4_BREAKS
    and [float(d.meta["broken_level"]) for d in br4] == [105.0, 105.0]
    and {d.meta["broken_swing_index"] for d in br4} == {3, 13}
    and ms.detect_trend_transitions(F4) == [],
    "B16 F4 same price level 105 breaks TWICE via fresh swing occurrences "
    "(after the intervening swing-low update); trend stays neutral",
)

# ── C. detect_trend_transitions ──────────────────────────────────────────
print("\nC. DETECT_TREND_TRANSITIONS")

tr3 = ms.detect_trend_transitions(F3)
validate_detections(tr3, F3)
check(_trans_tuples(tr3) == F3_TRANSITIONS, "C1 F3 exact transitions: neutral->bullish->bearish",
      f"got {_trans_tuples(tr3)}")
check(
    all(
        d.kind == "trend_" + d.meta["new"]
        and d.side == d.meta["new"]
        and d.side in ("bullish", "bearish", "neutral")
        and set(d.meta.keys()) == {"prev", "new", "swing_index"}
        for d in tr3
    ),
    "C2 F3 kind/side/meta-key contract (side == new state)",
)
check(
    all(
        d.start_index == d.meta["swing_index"]
        and d.ts == F3[d.confirm_index].ts
        and d.level_low is None
        and d.level_high is None
        for d in tr3
    ),
    "C3 F3 start_index == swing_index, ts == candles[confirm].ts, levels None",
)
tr2 = ms.detect_trend_transitions(F2)
validate_detections(tr2, F2)
check(
    _trans_tuples(tr2) == F2_TRANSITIONS,
    "C4 F2 exact transitions (honest event-driven sequence incl. intermediate neutral)",
    f"got {_trans_tuples(tr2)}",
)
check(ms.detect_trend_transitions(F1) == [], "C5 F1 no transitions (never two swing highs)")
check(
    ms.detect_trend_transitions(F2, 2, swings=list(sw2)) == tr2,
    "C6 precomputed swings parameter reproduces the default transitions",
)
check(ms.detect_trend_transitions(F2[:4]) == [], "C7 series shorter than 2k+1 -> []")

# ── D. input validation edges ────────────────────────────────────────────
print("\nD. INPUT VALIDATION EDGES")

check(
    _raises_contract_error(ms.detect_swings, [])
    and _raises_contract_error(ms.detect_structure_breaks, [])
    and _raises_contract_error(ms.detect_trend_transitions, []),
    "D1 empty candle list -> ContractError for all three detectors",
)
check(
    _raises_contract_error(ms.detect_swings, [Candle(ts=T0, open=10, high=9, low=8, close=9.5)]),
    "D2 malformed candle (high < max(open, close)) -> ContractError",
)
check(
    _raises_contract_error(ms.detect_swings, F1, 0)
    and _raises_contract_error(ms.detect_swings, F1, -2),
    "D3 k=0 and k=-2 -> ContractError",
)
check(_raises_contract_error(ms.detect_swings, F1, True), "D4 bool k -> ContractError")
_dup = list(sw2) + [sw2[0]]
check(
    _raises_contract_error(ms.detect_structure_breaks, F2, 2, _dup),
    "D5 duplicate swing occurrence in provided swings -> ContractError",
)
_nonmono = [F1[0], Candle(ts=F1[0].ts, open=97, high=98, low=96, close=97)]
check(
    _raises_contract_error(ms.detect_swings, _nonmono),
    "D6 non-increasing ts -> ContractError (validate_candles strict order)",
)

# ── E. mechanical no-lookahead ───────────────────────────────────────────
print("\nE. MECHANICAL NO-LOOKAHEAD (perturbation proofs)")

_cs = synthetic_series(120, seed=11)
try:
    assert_no_lookahead(ms.detect_swings, _cs, {"k": 2})
    check(True, "E1 assert_no_lookahead detect_swings on synthetic(120, seed=11)")
except AssertionError as exc:
    check(False, "E1 assert_no_lookahead detect_swings on synthetic(120, seed=11)", str(exc)[:160])
try:
    assert_no_lookahead(ms.detect_structure_breaks, _cs, {"k": 2})
    check(True, "E2 assert_no_lookahead detect_structure_breaks on synthetic(120, seed=11)")
except AssertionError as exc:
    check(False, "E2 assert_no_lookahead detect_structure_breaks on synthetic(120, seed=11)", str(exc)[:160])
try:
    assert_no_lookahead(ms.detect_trend_transitions, _cs, {"k": 2})
    check(True, "E3 assert_no_lookahead detect_trend_transitions on synthetic(120, seed=11)")
except AssertionError as exc:
    check(False, "E3 assert_no_lookahead detect_trend_transitions on synthetic(120, seed=11)", str(exc)[:160])
try:
    assert_no_lookahead(ms.detect_swings, F2, {"k": 2})
    check(True, "E4 assert_no_lookahead detect_swings on fixture F2")
except AssertionError as exc:
    check(False, "E4 assert_no_lookahead detect_swings on fixture F2", str(exc)[:160])
try:
    assert_no_lookahead(ms.detect_structure_breaks, F2, {"k": 2})
    check(True, "E5 assert_no_lookahead detect_structure_breaks on fixture F2")
except AssertionError as exc:
    check(False, "E5 assert_no_lookahead detect_structure_breaks on fixture F2", str(exc)[:160])
try:
    assert_no_lookahead(ms.detect_trend_transitions, F2, {"k": 2})
    check(True, "E6 assert_no_lookahead detect_trend_transitions on fixture F2")
except AssertionError as exc:
    check(False, "E6 assert_no_lookahead detect_trend_transitions on fixture F2", str(exc)[:160])
try:
    for _fn in (ms.detect_swings, ms.detect_structure_breaks, ms.detect_trend_transitions):
        assert_no_lookahead(_fn, F3, {"k": 2})
    check(True, "E7 assert_no_lookahead all three on F3 (outside-bar double-confirm stress)")
except AssertionError as exc:
    check(False, "E7 assert_no_lookahead all three on F3 (outside-bar double-confirm stress)", str(exc)[:160])
try:
    for _fn in (ms.detect_swings, ms.detect_structure_breaks, ms.detect_trend_transitions):
        assert_no_lookahead(_fn, F5, {"k": 2})
    check(True, "E8 assert_no_lookahead all three on F5 (confirmation-gate stress)")
except AssertionError as exc:
    check(False, "E8 assert_no_lookahead all three on F5 (confirmation-gate stress)", str(exc)[:160])

# ── F. determinism ───────────────────────────────────────────────────────
print("\nF. DETERMINISM")

_det = synthetic_series(200, seed=5)
try:
    assert_deterministic(ms.detect_swings, _det, {"k": 2})
    check(True, "F1 assert_deterministic detect_swings on synthetic(200, seed=5)")
except AssertionError as exc:
    check(False, "F1 assert_deterministic detect_swings on synthetic(200, seed=5)", str(exc)[:160])
try:
    assert_deterministic(ms.detect_structure_breaks, _det, {"k": 2})
    check(True, "F2 assert_deterministic detect_structure_breaks on synthetic(200, seed=5)")
except AssertionError as exc:
    check(False, "F2 assert_deterministic detect_structure_breaks on synthetic(200, seed=5)", str(exc)[:160])
try:
    assert_deterministic(ms.detect_trend_transitions, _det, {"k": 2})
    check(True, "F3 assert_deterministic detect_trend_transitions on synthetic(200, seed=5)")
except AssertionError as exc:
    check(False, "F3 assert_deterministic detect_trend_transitions on synthetic(200, seed=5)", str(exc)[:160])

# ── G. fuzz properties (seeds 1..5, n=150) ───────────────────────────────
print("\nG. FUZZ PROPERTIES")

SEEDS = (1, 2, 3, 4, 5)
_fuzz = {}
for _seed in SEEDS:
    _fcs = synthetic_series(150, seed=_seed)
    _fsw = ms.detect_swings(_fcs)
    _fbr = ms.detect_structure_breaks(_fcs)
    _ftr = ms.detect_trend_transitions(_fcs)
    validate_detections(_fsw, _fcs)  # raises on violation (catch nothing)
    validate_detections(_fbr, _fcs)
    validate_detections(_ftr, _fcs)
    _fuzz[_seed] = (_fcs, _fsw, _fbr, _ftr)
check(True, "G1 all three detectors run + validate on synthetic(150) for seeds 1..5")
check(
    all(
        d.confirm_index - d.meta["swing_index"] == 2
        for (_c_, _sw, _br, _tr) in _fuzz.values()
        for d in _sw
    ),
    "G2 swing confirm_index - swing_index == k for every fuzz swing",
)
_counts = [len(_sw) for (_c_, _sw, _br, _tr) in _fuzz.values()]
check(sum(1 for x in _counts if x > 0) >= 3, "G3 swing count > 0 for at least 3 of 5 seeds",
      f"counts={_counts}")
check(
    all(
        len({(d.side, d.meta["broken_swing_index"]) for d in _br}) == len(_br)
        and all(d.meta["trend_before"] in ("bullish", "bearish", "neutral") for d in _br)
        and all(set(d.meta.keys()) == {"broken_level", "broken_swing_index", "trend_before"} for d in _br)
        for (_c_, _sw, _br, _tr) in _fuzz.values()
    ),
    "G4 breaks: unique (side, swing) occurrences, valid trend_before, exact meta keys",
)
check(
    all(
        d.meta["prev"] != d.meta["new"]
        and d.kind == "trend_" + d.meta["new"]
        and d.side == d.meta["new"]
        and set(d.meta.keys()) == {"prev", "new", "swing_index"}
        for (_c_, _sw, _br, _tr) in _fuzz.values()
        for d in _tr
    ),
    "G5 transitions: prev != new, kind/side derived from new state, exact meta keys",
)


def _nondecreasing(ds):
    return all(ds[i].confirm_index <= ds[i + 1].confirm_index for i in range(len(ds) - 1))


check(
    all(
        _nondecreasing(_sw) and _nondecreasing(_br) and _nondecreasing(_tr)
        for (_c_, _sw, _br, _tr) in _fuzz.values()
    ),
    "G6 all outputs ordered by non-decreasing confirm_index (chronological emission)",
)

# ── H. module source honesty ─────────────────────────────────────────────
print("\nH. MODULE SOURCE HONESTY")

_MODULE_PATH = os.path.join(_REPO, "core", "ict", "market_structure.py")
with open(_MODULE_PATH, "r", encoding="utf-8") as _fh:
    _SRC = _fh.read()
check(
    "no predictive claim" in _SRC and "research feature" in _SRC
    and "k-candle confirmation lag" in _SRC,
    "H1 module docstring carries the research disclaimer incl. the k-candle lag",
)
_forbidden = re.compile(r"\b(guaranteed|sure|profitable|win|wins|winning)\b", re.IGNORECASE)
check(
    _forbidden.search(_SRC) is None,
    "H2 module source contains none of: guaranteed / sure / profitable / win(s|ning)",
    f"found: {_forbidden.search(_SRC).group(0) if _forbidden.search(_SRC) else ''}",
)
_io_tokens = ("open(", "import os", "import json", "requests", "environ",
              "getenv", "print(", "write(", "socket", "urllib")
check(
    not any(_tok in _SRC for _tok in _io_tokens),
    "H3 no I/O tokens in the module source (pure detectors)",
)
_sig_sw = inspect.signature(ms.detect_swings)
_sig_br = inspect.signature(ms.detect_structure_breaks)
_sig_tr = inspect.signature(ms.detect_trend_transitions)
check(
    list(_sig_sw.parameters) == ["candles", "k"]
    and _sig_sw.parameters["k"].default == 2
    and list(_sig_br.parameters) == ["candles", "k", "swings"]
    and _sig_br.parameters["k"].default == 2
    and _sig_br.parameters["swings"].default is None
    and list(_sig_tr.parameters) == ["candles", "k", "swings"]
    and _sig_tr.parameters["k"].default == 2
    and _sig_tr.parameters["swings"].default is None,
    "H4 public API surface matches the frozen L1 spec (names + defaults)",
)

# ── summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"PHASE-L1 MARKET STRUCTURE: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for _f in _FAILURES:
        print(f"  - {_f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
