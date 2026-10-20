"""PHASE-L2 POI ZONES — offline battery for core/ict/poi_zones.py (Agent B).

Run:  python3 scripts/test_phase_l2_poi_zones.py     (from repo root)

Covers (Phase-L2 authorization requirements):
  * hand-crafted FVG fixtures with exact zone bounds, min_gap filtering,
    touch/overlap rejection;
  * order-block fixtures (bearish + bullish, exact zones and meta), the
    no-opposite-candle skip path, hand-computed median gating, the
    inclusive >= threshold boundary;
  * supply/demand fixtures (exact demand + supply zones, non-overlapping
    base rejection, consolidation rejection, longest-base selection both
    ways), equal highs/lows (tolerance boundaries, window boundary), key
    levels (cluster of 3 swings, touches count, window/min_touches gates);
  * classify_zone_status freshness transitions (untested -> touched ->
    broken), gap-break without touch, priority rules, and the hard
    upto_index bound (never reads beyond it; the bound is actually used);
  * MECHANICAL no-lookahead proofs via perturbation (synthetic series AND
    hand-crafted fixtures) for every detector;
  * determinism (identical signature across repeated runs) for all five
    detectors;
  * fuzz (seeds 1..5) — every detector runs and validates without raising;
  * input validation (ContractError on bad params / empty candles);
  * source honesty: research disclaimer present, no predictive language.

No network. No broker. No DB. Pure in-process detector calls only.
"""
import inspect
import os
import re
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.ict import poi_zones  # noqa: E402
from core.ict.helpers import (  # noqa: E402
    assert_deterministic,
    assert_no_lookahead,
    synthetic_series,
)
from core.ict.poi_zones import (  # noqa: E402
    classify_zone_status,
    detect_equal_levels,
    detect_fvg,
    detect_key_levels,
    detect_order_blocks,
    detect_supply_demand,
)
from core.ict.types import (  # noqa: E402
    Candle,
    ContractError,
    Detection,
    validate_detections,
)

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


def check_fn(fn, label):
    """check(True) when fn() returns without raising; record the error otherwise."""
    try:
        fn()
    except Exception as exc:  # battery records, never crashes
        check(False, label, repr(exc))
    else:
        check(True, label)


def raises_contract_error(fn):
    try:
        fn()
    except ContractError:
        return True
    except Exception:
        return False
    return False


T0 = 1_700_000_000


def mkc(spec, t0=T0):
    """Candles from (open, high, low, close) tuples, 60s spacing, no gaps."""
    return [
        Candle(ts=t0 + i * 60, open=float(o), high=float(h), low=float(lo), close=float(c))
        for i, (o, h, lo, c) in enumerate(spec)
    ]


def near(a, b, tol=1e-9):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


# ── fixtures (all arithmetic hand-verified before writing this file) ────

FVG_BULL = mkc([
    (100.0, 101.0, 99.0, 100.5),
    (100.5, 102.0, 100.0, 101.5),
    (103.5, 104.5, 103.0, 104.0),   # bullish FVG [101, 103] confirmed here
    (102.5, 103.5, 102.0, 103.0),
    (104.5, 105.5, 104.0, 105.0),
    (104.0, 105.0, 103.5, 104.5),
])

FVG_BEAR = mkc([
    (100.0, 101.0, 99.0, 100.5),
    (99.5, 100.5, 99.0, 100.0),
    (97.0, 98.0, 96.5, 97.5),       # bearish FVG [98, 99] confirmed here
    (97.5, 99.0, 97.0, 98.5),
    (98.5, 99.5, 98.0, 99.0),
    (99.0, 100.0, 98.5, 99.5),
])

FVG_TOUCH = mkc([
    (100.0, 101.0, 99.0, 100.5),
    (100.5, 101.5, 100.0, 101.0),
    (103.0, 104.0, 101.0, 103.5),   # high[0]=101 == low[2]=101 -> touch, no gap
])

FVG_OVERLAP = mkc([
    (100.0, 101.0, 99.0, 100.5),
    (100.5, 101.5, 100.0, 101.0),
    (102.0, 103.0, 100.5, 102.5),   # low[2]=100.5 < high[0]=101 -> overlap
])

OB_BEAR = mkc([
    (100.0, 102.0, 99.5, 101.5),
    (101.5, 102.0, 100.0, 100.5),
    (100.5, 101.8, 100.2, 101.5),   # <- order block (last bullish before impulse)
    (101.5, 102.0, 101.0, 101.2),
    (101.2, 101.7, 97.5, 98.0),     # bearish impulse: body 3.2, close 98 < 99.5
    (98.0, 99.0, 97.5, 98.5),
    (98.5, 99.5, 98.0, 99.0),
    (99.0, 100.0, 98.5, 99.5),
])

OB_BULL = mkc([
    (100.0, 100.5, 98.0, 98.5),     # <- order block (last bearish before impulse)
    (98.5, 99.0, 98.0, 98.8),
    (98.8, 99.2, 98.5, 99.0),
    (99.0, 99.5, 99.0, 99.2),
    (99.0, 103.0, 98.8, 102.8),     # bullish impulse: body 3.8, close 102.8 > 100.5
])

OB_NO_BEAR = mkc([
    (101.0, 102.0, 100.0, 100.5),
    (100.5, 101.0, 99.5, 100.0),
    (100.0, 100.5, 97.5, 98.0),     # bearish impulse but no bullish candle before
])

OB_NO_BULL = mkc([
    (98.0, 99.0, 97.5, 98.5),
    (98.5, 99.5, 98.0, 99.0),
    (99.0, 102.0, 98.8, 101.5),     # bullish impulse but no bearish candle before
])

OB_SUB = mkc([
    (100.0, 102.0, 99.5, 101.5),
    (101.5, 102.0, 100.0, 100.5),
    (100.5, 101.8, 100.2, 101.5),
    (101.5, 102.0, 101.0, 101.2),
    (101.2, 101.5, 99.4, 99.9),     # body 1.3 < 1.5 * median(1.0) -> no OB
])

OB_EDGE = mkc([
    (101.0, 101.5, 99.5, 100.0),    # bearish OB candle
    (100.0, 101.5, 99.5, 101.0),
    (101.0, 103.0, 100.5, 103.0),   # body exactly 2.0 == 2.0 * median(1.0)
])

SD_DEMAND = mkc([
    (100.0, 101.0, 99.5, 100.5),
    (100.5, 101.5, 100.0, 101.0),
    (101.0, 102.0, 100.5, 101.5),   # base candles 0..2
    (101.5, 104.0, 101.2, 103.5),   # impulse: body 2.0, close 103.5 > 102
    (103.5, 104.5, 103.2, 104.0),
    (104.0, 104.8, 103.8, 104.5),
])

SD_SUPPLY = mkc([
    (100.0, 100.5, 99.0, 99.5),
    (99.5, 100.0, 99.2, 99.6),
    (99.6, 100.1, 99.3, 99.4),      # base candles 0..2
    (99.4, 99.6, 96.8, 97.0),       # impulse: body 2.4, close 97.0 < 99.0
])

SD_NO_OVERLAP = mkc([
    (100.0, 101.0, 99.5, 100.5),
    (100.5, 101.5, 100.0, 101.0),
    (102.5, 103.5, 102.0, 103.0),   # no common band with the first two
    (103.0, 105.5, 102.8, 105.0),   # impulse, but no valid 3-candle base
])

SD_LONG4 = mkc([
    (100.0, 100.8, 99.5, 100.3),
    (100.3, 101.0, 99.8, 100.6),
    (100.6, 101.3, 100.1, 100.9),
    (100.9, 101.6, 100.4, 101.2),   # base 0..3 -> longest valid base (k=4)
    (101.2, 104.0, 101.0, 103.5),   # impulse
])

SD_CAP3 = mkc([
    (105.0, 106.0, 104.5, 105.5),   # breaks the common band -> base capped at k=3
    (100.0, 101.0, 99.5, 100.5),
    (100.5, 101.5, 100.0, 101.0),
    (101.0, 102.0, 100.5, 101.5),   # base 1..3 (k=3)
    (101.5, 104.0, 101.2, 103.5),   # impulse
])

SD_NO_CONSOL = mkc([
    (100.0, 100.45, 99.95, 100.4),
    (100.2, 100.65, 100.15, 100.6),
    (100.4, 100.85, 100.35, 100.8),  # common band ok but net move 0.8 > 0.5*1.5
    (100.8, 102.9, 100.6, 102.5),    # impulse, but base fails consolidation
])


def eq_highs_fixture(h1, h2):
    """Two swing highs at indices 1 and 5 (confirmed at 2 and 6), level h1/h2."""
    return mkc([
        (99.5, 100.0, 99.0, 99.8),
        (100.0, h1, 99.2, 100.5),    # swing high 1 at index 1
        (99.6, 100.0, 99.0, 99.7),   # confirms swing 1
        (99.7, 100.0, 99.0, 99.6),
        (99.6, 100.0, 99.0, 99.7),
        (99.7, h2, 99.2, 100.4),     # swing high 2 at index 5
        (99.6, 100.0, 99.0, 99.5),   # confirms swing 2
        (99.5, 100.0, 99.0, 99.7),
    ])


EQ_HIGHS = eq_highs_fixture(103.0, 103.02)   # diff 0.02 <= 0.1% of 103.01
EQ_OUT = eq_highs_fixture(103.0, 103.25)     # diff 0.25 > 0.1% of 103.125

EQ_LOWS = mkc([
    (99.5, 100.0, 99.0, 99.8),
    (99.5, 100.0, 97.0, 99.8),      # swing low 1 (97.0) at index 1
    (99.5, 100.0, 99.0, 99.7),
    (99.5, 100.0, 99.0, 99.6),
    (99.5, 100.0, 99.0, 99.7),
    (99.5, 100.0, 96.98, 99.8),     # swing low 2 (96.98) at index 5
    (99.5, 100.0, 99.0, 99.5),
    (99.5, 100.0, 99.0, 99.7),
])

KL_RES = mkc([
    (97.6, 98.0, 97.5, 97.8),
    (97.8, 100.0, 97.5, 98.5),      # swing high 100.0 at index 1
    (98.5, 99.0, 97.5, 98.8),
    (98.8, 100.15, 97.5, 99.0),     # swing high 100.15 at index 3
    (99.0, 99.2, 97.5, 98.9),
    (98.9, 100.1, 97.5, 99.2),      # swing high 100.1 at index 5
    (98.3, 98.5, 97.5, 98.0),       # confirms the cluster
    (98.0, 98.4, 97.5, 98.2),       # trailing (no-lookahead headroom)
])

KL_SUP = mkc([
    (101.5, 102.5, 101.0, 101.8),
    (101.8, 102.5, 100.0, 101.5),   # swing low 100.0 at index 1
    (101.5, 102.5, 101.0, 101.8),
    (101.8, 102.5, 99.85, 101.5),   # swing low 99.85 at index 3
    (101.5, 102.5, 101.0, 101.8),
    (101.8, 102.5, 99.9, 101.5),    # swing low 99.9 at index 5
    (101.5, 102.5, 101.0, 101.2),   # confirms the cluster
    (101.2, 102.5, 101.0, 101.0),   # trailing (no-lookahead headroom)
])

CL_SERIES = mkc([
    (100.0, 101.0, 99.0, 100.5),
    (100.5, 102.0, 100.0, 101.5),
    (103.5, 104.5, 103.0, 104.0),   # bullish FVG [101, 103] confirmed here
    (103.0, 104.0, 102.5, 103.5),   # touches the zone
    (102.0, 103.0, 101.5, 102.5),   # touches again, no break
    (101.0, 102.0, 100.0, 100.5),   # closes below 101 -> broken (bullish zone)
])

CL_GAP = mkc([
    (100.0, 101.0, 99.0, 100.5),
    (100.5, 102.0, 100.0, 101.5),
    (103.5, 104.5, 103.0, 104.0),   # same bullish FVG [101, 103]
    (100.5, 100.8, 100.2, 100.4),   # gapped below: no overlap, close < 101
])

CL_BEAR = mkc([
    (100.0, 101.0, 99.0, 100.5),
    (99.5, 100.5, 99.0, 100.0),
    (97.0, 98.0, 96.5, 97.5),       # bearish FVG [98, 99] confirmed here
    (98.5, 99.5, 98.2, 99.0),       # overlaps; close 99.0 == high -> not broken
    (99.5, 100.0, 99.2, 99.8),      # closes above 99 -> broken (bearish zone)
])

SHORT2 = mkc([(100.0, 101.0, 99.0, 100.5), (100.5, 102.0, 100.0, 101.5)])


def first(dets, **fields):
    for d in dets:
        if all(getattr(d, k) == v for k, v in fields.items()):
            return d
    return None


# ════════════════════════════════════════════════════════════════════════
print("== Phase-L2 POI zones: fair value gaps ==")

dets = detect_fvg(FVG_BULL)
check(len(dets) == 1, "FVG-BULL: exactly one detection", f"got {len(dets)}")
d = dets[0] if dets else None
check(
    d is not None
    and d.kind == "fvg_bullish"
    and d.side == "bullish"
    and d.start_index == 0
    and d.confirm_index == 2
    and d.ts == FVG_BULL[2].ts
    and near(d.level_low, 101.0)
    and near(d.level_high, 103.0)
    and near(d.meta.get("gap"), 2.0)
    and d.level_low <= d.level_high,
    "FVG-BULL: exact kind/side/indices/ts/zone/meta",
    "" if d is None else f"zone=[{d.level_low},{d.level_high}] meta={d.meta}",
)

dets = detect_fvg(FVG_BEAR)
check(len(dets) == 1, "FVG-BEAR: exactly one detection", f"got {len(dets)}")
d = dets[0] if dets else None
check(
    d is not None
    and d.kind == "fvg_bearish"
    and d.side == "bearish"
    and d.start_index == 0
    and d.confirm_index == 2
    and d.ts == FVG_BEAR[2].ts
    and near(d.level_low, 98.0)
    and near(d.level_high, 99.0)
    and near(d.meta.get("gap"), 1.0),
    "FVG-BEAR: exact kind/side/indices/ts/zone/meta",
    "" if d is None else f"zone=[{d.level_low},{d.level_high}] meta={d.meta}",
)

check(
    detect_fvg(FVG_BULL, min_gap=2.5) == [] and detect_fvg(FVG_BEAR, min_gap=1.5) == [],
    "FVG-GAP-FILTER: min_gap above the actual gap size removes detections",
)
check(
    detect_fvg(FVG_BULL, min_gap=2.0) == [] and len(detect_fvg(FVG_BULL, min_gap=1.999)) == 1,
    "FVG-GAP-BOUNDARY: gap must be STRICTLY greater than min_gap (2.0 gap vs 2.0 filter)",
)
check(
    detect_fvg(FVG_TOUCH) == [] and detect_fvg(FVG_OVERLAP) == [],
    "FVG-TOUCH-OVERLAP: touching or overlapping bounds are not gaps",
)
check(
    detect_fvg(SHORT2) == []
    and detect_order_blocks(SHORT2) == []
    and detect_supply_demand(SHORT2) == []
    and detect_equal_levels(SHORT2) == []
    and detect_key_levels(SHORT2) == [],
    "FVG-SHORT: 2-candle series -> no detections, no exception (all detectors)",
)

# ════════════════════════════════════════════════════════════════════════
print("== Phase-L2 POI zones: order blocks ==")

dets = detect_order_blocks(OB_BEAR)
check(len(dets) == 1, "OB-BEAR: exactly one detection", f"got {len(dets)}")
d = dets[0] if dets else None
check(
    d is not None
    and d.kind == "order_block_bearish"
    and d.side == "bearish"
    and d.start_index == 2
    and d.confirm_index == 4
    and d.ts == OB_BEAR[4].ts
    and near(d.level_low, 100.2)
    and near(d.level_high, 101.8)
    and d.meta.get("body_zone") == [100.5, 101.5]
    and d.meta.get("impulse_index") == 4
    and near(d.meta.get("impulse_body"), 3.2),
    "OB-BEAR: exact zone/indices/meta (full candle range of last bullish candle)",
    "" if d is None else f"zone=[{d.level_low},{d.level_high}] meta={d.meta}",
)

dets = detect_order_blocks(OB_BULL)
check(len(dets) == 1, "OB-BULL: exactly one detection", f"got {len(dets)}")
d = dets[0] if dets else None
check(
    d is not None
    and d.kind == "order_block_bullish"
    and d.side == "bullish"
    and d.start_index == 0
    and d.confirm_index == 4
    and near(d.level_low, 98.0)
    and near(d.level_high, 100.5)
    and d.meta.get("body_zone") == [98.5, 100.0]
    and d.meta.get("impulse_index") == 4
    and near(d.meta.get("impulse_body"), 3.8),
    "OB-BULL: exact zone/indices/meta (full range of last bearish candle)",
    "" if d is None else f"zone=[{d.level_low},{d.level_high}] meta={d.meta}",
)

check(
    detect_order_blocks(OB_NO_BEAR) == [] and detect_order_blocks(OB_NO_BULL) == [],
    "OB-NO-OPPOSITE: impulse with no opposite-color candle before it -> skipped",
)

_bodies = [c.body for c in OB_BEAR[:4]]  # hand-computed: ~[1.5, 1.0, 1.0, 0.3]
check(
    len(_bodies) == 4
    and near(_bodies[0], 1.5)
    and near(_bodies[1], 1.0)
    and near(_bodies[2], 1.0)
    and near(_bodies[3], 0.3)
    and near(poi_zones._median(_bodies), 1.0)
    and near(poi_zones._median([0.5]), 0.5)
    and near(poi_zones._median([1.0, 0.3]), 0.65)
    and near(poi_zones._median([3.0, 1.0, 2.0]), 2.0)
    and _bodies[3] < 1.5 * 1.0 <= OB_BEAR[4].body,
    "OB-MEDIAN: hand-computed median gate (median [1.5,1.0,1.0,0.3]=1.0, threshold 1.5)",
    f"bodies={_bodies}",
)
check(
    detect_order_blocks(OB_SUB) == [],
    "OB-SUBTHRESHOLD: impulse body 1.3 < 1.5*median(1.0)=1.5 -> no detection",
)
check(
    len(detect_order_blocks(OB_EDGE, impulse_mult=2.0)) == 1
    and detect_order_blocks(OB_EDGE, impulse_mult=2.1) == [],
    "OB-BOUNDARY: body == impulse_mult*median is INCLUDED (>=), above it is not",
)

# ════════════════════════════════════════════════════════════════════════
print("== Phase-L2 POI zones: supply / demand ==")

dets = detect_supply_demand(SD_DEMAND)
check(len(dets) == 1, "SD-DEMAND: exactly one detection", f"got {len(dets)}")
d = dets[0] if dets else None
check(
    d is not None
    and d.kind == "demand_zone"
    and d.side == "bullish"
    and d.start_index == 0
    and d.confirm_index == 3
    and d.ts == SD_DEMAND[3].ts
    and near(d.level_low, 99.5)
    and near(d.level_high, 102.0)
    and d.meta.get("base_len") == 3
    and d.meta.get("impulse_index") == 3
    and near(d.meta.get("impulse_body"), 2.0),
    "SD-DEMAND: exact zone/indices/meta ([min low, max high] of the base)",
    "" if d is None else f"zone=[{d.level_low},{d.level_high}] meta={d.meta}",
)

dets = detect_supply_demand(SD_SUPPLY)
check(len(dets) == 1, "SD-SUPPLY: exactly one detection", f"got {len(dets)}")
d = dets[0] if dets else None
check(
    d is not None
    and d.kind == "supply_zone"
    and d.side == "bearish"
    and d.start_index == 0
    and d.confirm_index == 3
    and near(d.level_low, 99.0)
    and near(d.level_high, 100.5)
    and d.meta.get("base_len") == 3,
    "SD-SUPPLY: exact mirrored zone/indices/meta",
    "" if d is None else f"zone=[{d.level_low},{d.level_high}] meta={d.meta}",
)

check(
    detect_supply_demand(SD_NO_OVERLAP) == [],
    "SD-NO-OVERLAP: base candles without a common band -> rejected",
)
check(
    detect_supply_demand(SD_NO_CONSOL) == [],
    "SD-NO-CONSOLIDATION: net move 0.8 > 0.5 * total range 1.5 -> rejected",
)

dets = detect_supply_demand(SD_LONG4)
check(
    len(dets) == 1
    and dets[0].start_index == 0
    and near(dets[0].level_low, 99.5)
    and near(dets[0].level_high, 101.6)
    and dets[0].meta.get("base_len") == 4,
    "SD-LONG-BASE: longest valid base (k=4) is selected",
    f"got {[(d.start_index, d.level_low, d.level_high, d.meta) for d in dets]}",
)
dets = detect_supply_demand(SD_CAP3)
check(
    len(dets) == 1
    and dets[0].start_index == 1
    and near(dets[0].level_low, 99.5)
    and near(dets[0].level_high, 102.0)
    and dets[0].meta.get("base_len") == 3,
    "SD-CAP-BASE: earlier candle breaking the common band caps the base at k=3",
    f"got {[(d.start_index, d.level_low, d.level_high, d.meta) for d in dets]}",
)

# ════════════════════════════════════════════════════════════════════════
print("== Phase-L2 POI zones: equal highs / lows ==")

dets = detect_equal_levels(EQ_HIGHS)
check(len(dets) == 1, "EQ-HIGH: exactly one detection", f"got {len(dets)}")
d = dets[0] if dets else None
check(
    d is not None
    and d.kind == "eq_highs"
    and d.side == "bearish"
    and d.start_index == 1
    and d.confirm_index == 6
    and d.ts == EQ_HIGHS[6].ts
    and near(d.level_low, (103.0 + 103.02) / 2.0)
    and near(d.level_high, (103.0 + 103.02) / 2.0)
    and d.meta.get("levels") == [103.0, 103.02]
    and d.meta.get("swing_indices") == [1, 5],
    "EQ-HIGH: exact level(mean)/indices/meta, side bearish (resistance label)",
    "" if d is None else f"level={d.level_low} meta={d.meta}",
)

check(
    detect_equal_levels(EQ_OUT) == [],
    "EQ-HIGH-OUT: levels 0.25 apart exceed tol_rel*mean (0.103125) -> none",
)
check(
    detect_equal_levels(EQ_HIGHS, window=3) == [],
    "EQ-WINDOW: swings 4 candles apart with window=3 -> none (window binds)",
)

dets = detect_equal_levels(EQ_LOWS)
check(
    len(dets) == 1
    and dets[0].kind == "eq_lows"
    and dets[0].side == "bullish"
    and dets[0].start_index == 1
    and dets[0].confirm_index == 6
    and near(dets[0].level_low, (97.0 + 96.98) / 2.0)
    and dets[0].meta.get("levels") == [97.0, 96.98]
    and dets[0].meta.get("swing_indices") == [1, 5],
    "EQ-LOW: mirrored detection, side bullish, exact level/meta",
    f"got {dets}",
)

# ════════════════════════════════════════════════════════════════════════
print("== Phase-L2 POI zones: key levels ==")

dets = detect_key_levels(KL_RES)
check(len(dets) == 1, "KL-RES: exactly one detection", f"got {len(dets)}")
d = dets[0] if dets else None
_mean3 = (100.0 + 100.15 + 100.1) / 3.0
check(
    d is not None
    and d.kind == "resistance"
    and d.side == "bearish"
    and d.start_index == 1
    and d.confirm_index == 6
    and d.ts == KL_RES[6].ts
    and near(d.level_low, _mean3)
    and near(d.level_high, _mean3)
    and d.meta.get("touches") == 3
    and near(d.meta.get("level"), _mean3)
    and d.meta.get("swing_indices") == [1, 3, 5],
    "KL-RES: 3 clustered swing highs -> resistance at mean, touches=3, confirm at 3rd swing+1",
    "" if d is None else f"level={d.level_low} meta={d.meta}",
)

dets = detect_key_levels(KL_SUP)
_mean3s = (100.0 + 99.85 + 99.9) / 3.0
check(
    len(dets) == 1
    and dets[0].kind == "support"
    and dets[0].side == "bullish"
    and dets[0].start_index == 1
    and dets[0].confirm_index == 6
    and near(dets[0].level_low, _mean3s)
    and dets[0].meta.get("touches") == 3
    and dets[0].meta.get("swing_indices") == [1, 3, 5],
    "KL-SUP: mirrored 3-swing-low cluster -> support at mean, touches=3",
    f"got {dets}",
)

check(
    detect_key_levels(KL_RES, window=2) == [],
    "KL-WINDOW: cluster span 4 > window=2 -> no level emitted",
)
check(
    detect_key_levels(KL_RES, min_touches=4) == [],
    "KL-MIN-TOUCHES: only 3 swings available with min_touches=4 -> none",
)

# ════════════════════════════════════════════════════════════════════════
print("== Phase-L2 POI zones: classify_zone_status ==")

zone = first(detect_fvg(CL_SERIES), kind="fvg_bullish", confirm_index=2)
check(
    zone is not None and near(zone.level_low, 101.0) and near(zone.level_high, 103.0),
    "CL-ZONE-SANITY: classify fixture zone is the [101, 103] bullish FVG",
)
check(
    zone is not None
    and classify_zone_status(zone, CL_SERIES, 2) == "untested"
    and classify_zone_status(zone, CL_SERIES, 1) == "untested"
    and classify_zone_status(zone, CL_SERIES, -1) == "untested",
    "CL-UNTESTED: nothing scanned at/below confirm_index (upto 2, 1, -1)",
)
check(
    zone is not None
    and classify_zone_status(zone, CL_SERIES, 3) == "touched"
    and classify_zone_status(zone, CL_SERIES, 4) == "touched",
    "CL-TOUCHED: overlapping candles after confirmation -> touched (upto 3, 4)",
)
check(
    zone is not None and classify_zone_status(zone, CL_SERIES, 5) == "broken",
    "CL-BROKEN: close 100.5 < level_low 101 (bullish zone) -> broken at upto 5",
)
check(
    zone is not None and classify_zone_status(zone, CL_SERIES, 4) == "touched",
    "CL-NEVER-BEYOND: breaking candle sits at upto_index+1; upto_index=4 -> 'touched' not 'broken'",
)
check(
    zone is not None
    and classify_zone_status(zone, CL_SERIES, 5) != classify_zone_status(zone, CL_SERIES, 4)
    and classify_zone_status(zone, CL_SERIES, 5) == "broken"
    and classify_zone_status(zone, CL_SERIES, 4) == "touched",
    "CL-BOUND-USED: breaking candle AT upto_index -> upto=5 'broken' vs upto=4 'touched'",
)

zone_gap = first(detect_fvg(CL_GAP), kind="fvg_bullish", confirm_index=2)
check(
    zone_gap is not None
    and classify_zone_status(zone_gap, CL_GAP, 2) == "untested"
    and classify_zone_status(zone_gap, CL_GAP, 3) == "broken",
    "CL-GAP-BREAK: candle closing beyond with NO overlap still reports 'broken' (priority)",
)

zone_bear = first(detect_fvg(CL_BEAR), kind="fvg_bearish")
check(
    zone_bear is not None
    and classify_zone_status(zone_bear, CL_BEAR, 3) == "touched"
    and classify_zone_status(zone_bear, CL_BEAR, 4) == "broken",
    "CL-BEARISH: close 99.8 > level_high 99 -> broken; earlier touch-only candle -> touched",
)

neutral_zone = Detection(
    kind="range_hypothesis", start_index=0, confirm_index=2,
    ts=CL_SERIES[2].ts, side="neutral",
    level_low=101.0, level_high=103.0, meta={},
)
check(
    classify_zone_status(neutral_zone, CL_SERIES, 2) == "untested"
    and classify_zone_status(neutral_zone, CL_SERIES, 3) == "broken",
    "CL-NEUTRAL: neutral-kind zone breaks on EITHER side (close 103.5 > 103)",
)

_bad_zone_inv = Detection(kind="z", start_index=0, confirm_index=2,
                          ts=CL_SERIES[2].ts, side="bullish",
                          level_low=103.0, level_high=101.0, meta={})
_bad_zone_nolvl = Detection(kind="z", start_index=0, confirm_index=2,
                            ts=CL_SERIES[2].ts, side="bullish", meta={})
_bad_zone_ci = Detection(kind="z", start_index=0, confirm_index=6,
                         ts=CL_SERIES[6 - 1].ts, side="bullish",
                         level_low=101.0, level_high=103.0, meta={})
check(
    raises_contract_error(lambda: classify_zone_status(_bad_zone_inv, CL_SERIES, 4))
    and raises_contract_error(lambda: classify_zone_status(_bad_zone_nolvl, CL_SERIES, 4))
    and raises_contract_error(lambda: classify_zone_status(zone, CL_SERIES, len(CL_SERIES)))
    and raises_contract_error(lambda: classify_zone_status(zone, CL_SERIES, -2))
    and raises_contract_error(lambda: classify_zone_status(_bad_zone_ci, CL_SERIES, 4))
    and raises_contract_error(lambda: classify_zone_status({"kind": "z"}, CL_SERIES, 4)),
    "CL-VALIDATION: inverted/missing bounds, bad upto_index, bad confirm_index, non-Detection -> ContractError",
)

# ════════════════════════════════════════════════════════════════════════
print("== Phase-L2 POI zones: mechanical no-lookahead ==")

S150 = synthetic_series(150, seed=3)
S200S9 = synthetic_series(200, seed=9)
check_fn(lambda: assert_no_lookahead(detect_fvg, S150), "NL-FVG-SYN: synthetic(150, seed=3)")
check_fn(lambda: assert_no_lookahead(detect_order_blocks, S150), "NL-OB-SYN: synthetic(150, seed=3)")
check_fn(lambda: assert_no_lookahead(detect_equal_levels, S150), "NL-EQ-SYN: synthetic(150, seed=3)")
check_fn(lambda: assert_no_lookahead(detect_supply_demand, S200S9), "NL-SD-SYN: synthetic(200, seed=9)")
check_fn(lambda: assert_no_lookahead(detect_key_levels, S200S9), "NL-KL-SYN: synthetic(200, seed=9)")
check_fn(lambda: assert_no_lookahead(detect_fvg, FVG_BULL), "NL-FVG-FIX: hand-crafted bullish FVG fixture")
check_fn(lambda: assert_no_lookahead(detect_order_blocks, OB_BEAR), "NL-OB-FIX: hand-crafted bearish OB fixture")
check_fn(lambda: assert_no_lookahead(detect_equal_levels, EQ_HIGHS), "NL-EQ-FIX: hand-crafted equal-highs fixture")
check_fn(lambda: assert_no_lookahead(detect_supply_demand, SD_DEMAND), "NL-SD-FIX: hand-crafted demand fixture")
check_fn(lambda: assert_no_lookahead(detect_key_levels, KL_RES), "NL-KL-FIX: hand-crafted resistance fixture")

# ════════════════════════════════════════════════════════════════════════
print("== Phase-L2 POI zones: determinism ==")

S200S4 = synthetic_series(200, seed=4)
for name, fn in (
    ("FVG", detect_fvg),
    ("OB", detect_order_blocks),
    ("SD", detect_supply_demand),
    ("EQ", detect_equal_levels),
    ("KL", detect_key_levels),
):
    check_fn(
        lambda fn=fn: assert_deterministic(fn, S200S4),
        f"DET-{name}: identical output signature across 3 runs on synthetic(200, seed=4)",
    )

# ════════════════════════════════════════════════════════════════════════
print("== Phase-L2 POI zones: fuzz (seeds 1..5) ==")

DETECTORS = (
    ("detect_fvg", detect_fvg),
    ("detect_order_blocks", detect_order_blocks),
    ("detect_supply_demand", detect_supply_demand),
    ("detect_equal_levels", detect_equal_levels),
    ("detect_key_levels", detect_key_levels),
)
fvg_counts = {}
fuzz_ok = True
for seed in (1, 2, 3, 4, 5):
    cs = synthetic_series(200, seed=seed)
    try:
        for _name, fn in DETECTORS:
            res = fn(list(cs))
            if not isinstance(res, list):
                fuzz_ok = False
            validate_detections(res, cs)
        fvg_counts[seed] = len(detect_fvg(cs))
    except Exception as exc:
        fuzz_ok = False
        print(f"    fuzz seed {seed} error: {exc!r}")
check(fuzz_ok, "FUZZ-RUN: seeds 1..5 — all five detectors run + validate, no exception")
check(
    sum(1 for c in fvg_counts.values() if c > 0) >= 3,
    "FUZZ-FVG: FVG count > 0 on at least 3 of 5 seeds",
    f"counts={fvg_counts}",
)

# ════════════════════════════════════════════════════════════════════════
print("== Phase-L2 POI zones: honesty + API surface ==")

with open(os.path.join(_REPO, "core", "ict", "poi_zones.py"), "r", encoding="utf-8") as fh:
    MODULE_SRC = fh.read()
check(
    "RESEARCH DISCLAIMER" in MODULE_SRC and "hypothesis" in MODULE_SRC.lower(),
    "HONESTY-DISCLAIMER: module states zones are hypothesis regions (research framing)",
)
_FORBIDDEN = re.compile(r"\b(?:guaranteed|sure|profitable|win|wins|winning)\b", re.IGNORECASE)
check(
    _FORBIDDEN.search(MODULE_SRC) is None,
    "HONESTY-LANGUAGE: no 'guaranteed'/'sure'/'profitable'/'win' wording in module source",
)

_EXPECTED_DEFAULTS = {
    "detect_fvg": {"min_gap": 0.0},
    "detect_order_blocks": {"impulse_mult": 1.5, "lookback": 10},
    "detect_supply_demand": {"base_min": 3, "impulse_mult": 2.0, "lookback": 20},
    "detect_equal_levels": {"tol_rel": 0.001, "window": 60},
    "detect_key_levels": {"window": 100, "min_touches": 3, "tol_rel": 0.002},
}
api_ok = True
api_detail = []
for fn_name, expected in _EXPECTED_DEFAULTS.items():
    sig = inspect.signature(getattr(poi_zones, fn_name))
    for pname, pdefault in expected.items():
        param = sig.parameters.get(pname)
        if param is None or param.default != pdefault:
            api_ok = False
            api_detail.append(f"{fn_name}.{pname}!={pdefault}")
check(api_ok, "API-DEFAULTS: detector signatures expose the specified explicit defaults", str(api_detail))

# ════════════════════════════════════════════════════════════════════════
print("== Phase-L2 POI zones: input validation ==")

VC = FVG_BULL[:3]
bad_param_cases = [
    lambda: detect_fvg(VC, min_gap=-0.1),
    lambda: detect_fvg(VC, min_gap=float("nan")),
    lambda: detect_order_blocks(VC, impulse_mult=0),
    lambda: detect_order_blocks(VC, impulse_mult=-1.5),
    lambda: detect_order_blocks(VC, lookback=0),
    lambda: detect_order_blocks(VC, lookback=True),
    lambda: detect_supply_demand(VC, base_min=0),
    lambda: detect_supply_demand(VC, impulse_mult=0.0),
    lambda: detect_supply_demand(VC, lookback=-3),
    lambda: detect_equal_levels(VC, tol_rel=-0.001),
    lambda: detect_equal_levels(VC, window=0),
    lambda: detect_key_levels(VC, window=0),
    lambda: detect_key_levels(VC, min_touches=1),
    lambda: detect_key_levels(VC, tol_rel=-1.0),
]
n_bad = sum(1 for case in bad_param_cases if raises_contract_error(case))
check(
    n_bad == len(bad_param_cases),
    "PARAM-VALIDATION: invalid parameters raise ContractError for every detector",
    f"{n_bad}/{len(bad_param_cases)}",
)

empty_ok = sum(1 for _n, fn in DETECTORS if raises_contract_error(lambda fn=fn: fn([])))
empty_ok += 1 if raises_contract_error(lambda: classify_zone_status(zone, [], 0)) else 0
check(
    empty_ok == len(DETECTORS) + 1,
    "EMPTY-INPUT: empty candle list raises ContractError (all detectors + classify)",
    f"{empty_ok}/{len(DETECTORS) + 1}",
)

# ── summary ─────────────────────────────────────────────────────────────
print()
if _FAILURES:
    print("Failures:")
    for f in _FAILURES:
        print(f"  - {f}")
print(f"PHASE-L2 POI ZONES: {_PASS} passed, {_FAIL} failed, {_PASS + _FAIL} total")
sys.exit(0 if _FAIL == 0 else 1)
