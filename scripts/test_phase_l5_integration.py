"""PHASE-L5 battery — ICT/SMC integration feature snapshot.

Proves at integration level:
  * three computation paths are IDENTICAL (reference prefix context,
    full-series context, incremental matrix build);
  * the TARGET candle's OHLC is never read (perturbation proof);
  * future candles never change earlier snapshots (future-immutable
    matrix extension);
  * incremental zone status == poi_zones.classify_zone_status exactly;
  * ex-ante time features (session/kill-zone, news distance) are
    legitimately knowable in advance and are labeled as such;
  * determinism, warmup behavior, content/type contracts;
  * scale: matrix build over 2,000 candles and a REAL-data slice.

Run: python3 scripts/test_phase_l5_integration.py
"""
import json
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.ict.features import (FeatureContext, build_feature_matrix,
                               snapshot_at)
from core.ict.helpers import perturb, synthetic_series
from core.ict.news import import_calendar, minutes_to_event
from core.ict.poi_zones import classify_zone_status
from core.ict import features as F

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
        _FAILURES.append(label)
        print(f"  FAIL {label} {detail}")


EXPECTED_KEYS = [
    "structure.trend", "structure.trend_age",
    "structure.last_swing_high", "structure.swing_high_age",
    "structure.last_swing_low", "structure.swing_low_age",
    "structure.last_break", "structure.break_age",
    "poi.nearest_supply", "poi.nearest_demand",
    "poi.demand_zone_active", "poi.supply_zone_active",
    "poi.order_block_bullish_active", "poi.order_block_bearish_active",
    "poi.fvg_bullish_active", "poi.fvg_bearish_active",
    "pattern.last_kind", "pattern.last_side", "pattern.last_age",
    "pattern.bull_count_5", "pattern.bear_count_5",
    "mom.rsi", "mom.roc", "mom.stoch_k", "mom.velocity",
    "mom.div_side", "mom.div_age",
    "liq.in_range", "liq.range_low", "liq.range_high",
    "liq.last_sweep", "liq.sweep_age", "liq.last_amd", "liq.amd_age",
    "session.target_session", "session.target_kill_zone",
    "flow.last_delta", "flow.absorption_age", "flow.effort_age",
    "flow.activity_spike_age",
    "news.minutes_to_high_impact", "news.provided",
]

print("A. THREE-PATH EQUIVALENCE (integration no-lookahead)")
cs = synthetic_series(300, seed=31)
ctx = FeatureContext(cs)
m = build_feature_matrix(cs, start=1, end=299)
mrows = {r["t"]: r["features"] for r in m}
all_ok = True
first_diff = None
for t in range(1, 300):
    a = snapshot_at(cs, t)
    b = ctx.snapshot(t)
    c = mrows[t]
    if not (a == b == c):
        all_ok = False
        first_diff = (t, [k for k in a if a[k] != b.get(k) or a[k] != c.get(k)][:3])
        break
check(all_ok, "A1 snapshot_at == full-context snapshot == matrix row for EVERY t in 1..299",
      str(first_diff))

print("B. TARGET-OHLC NEVER READ (perturbation proof)")
ok = True
for t in (60, 150, 250):
    base = ctx.snapshot(t)
    for variant in (0, 1, 2, 3):
        mutated = perturb(cs, t, variant)
        mctx = FeatureContext(mutated)
        if mctx.snapshot(t) != base:
            ok = False
check(ok, "B1 perturbing the TARGET candle (4 variants x 3 positions) leaves snapshot(t) identical")
ok = True
for t in (50, 120):
    base = ctx.snapshot(t)
    mutated = perturb(cs, t + 40, 1)
    if FeatureContext(mutated).snapshot(t) != base:
        ok = False
check(ok, "B2 perturbing a FUTURE candle leaves earlier snapshot identical")

print("C. FUTURE-IMMUTABLE MATRIX EXTENSION")
cs_big = synthetic_series(460, seed=77)
m1 = build_feature_matrix(cs_big[:400], start=1, end=399)
m2 = build_feature_matrix(cs_big, start=1, end=399)
same = all(m1[i]["features"] == m2[i]["features"] for i in range(len(m1)))
check(same, "C1 extending the series does not change any earlier snapshot row")

print("D. ZONE STATUS == classify_zone_status EXACTLY")
ok = True
detail = ""
for t in (80, 160, 240):
    trk, avail = F._fold_statuses(ctx.zones, cs, t)
    for j, z in enumerate(avail[-8:]):
        ref = classify_zone_status(z, cs, t - 1)
        inc = trk.status.get(len(avail) - 8 + j, "untested")
        if ref != inc:
            ok = False
            detail = f"t={t} zone={z.kind} ref={ref} inc={inc}"
            break
    if not ok:
        break
check(ok, "D1 incremental tracker status == classify_zone_status for sampled zones/t", detail)

print("E. CONTENT & TYPE CONTRACTS")
f100 = ctx.snapshot(100)
check(all(k in f100 for k in EXPECTED_KEYS),
      "E1 all 41 feature keys present",
      str([k for k in EXPECTED_KEYS if k not in f100]))
check(f100["mom.rsi"] is None or 0.0 <= f100["mom.rsi"] <= 100.0,
      "E2 rsi in [0,100] or None")
check(ctx.snapshot(13)["mom.rsi"] is None and ctx.snapshot(15)["mom.rsi"] is not None,
      "E3 rsi warmup boundary (None at 13, present at 15)")
check(f100["structure.trend"] in ("bullish", "bearish", "neutral", None),
      "E4 trend value in enum")
check(f100["session.target_session"] in ("asia", "london", "newyork", "off"),
      "E5 session value in enum")
check(f100["session.target_kill_zone"] in ("london_kz", "ny_kz", None),
      "E6 kill zone value in enum")
sup = f100["poi.nearest_supply"]
dem = f100["poi.nearest_demand"]
sup_ok = sup is None or (sup["dist_atr"] > 0 and set(sup) >=
                         {"kind", "level_low", "level_high", "mid",
                          "dist_atr", "status", "age"})
dem_ok = dem is None or (dem["dist_atr"] < 0 and set(dem) >=
                         {"kind", "level_low", "level_high", "mid",
                          "dist_atr", "status", "age"})
check(sup_ok and dem_ok, "E7 nearest-zone dicts: supply above (dist>0), demand below (dist<0)",
      f"sup={sup and sup['dist_atr']} dem={dem and dem['dist_atr']}")
check(isinstance(f100["pattern.bull_count_5"], int)
      and 0 <= f100["pattern.bull_count_5"] <= 5,
      "E8 pattern window counts are ints in [0,5]")
check(f100["news.provided"] is False and f100["news.minutes_to_high_impact"] is None,
      "E9 news absent -> provided=False")
zacts = [k for k in EXPECTED_KEYS if k.endswith("_active")]
check(all(isinstance(f100[k], int) and f100[k] >= 0 for k in zacts),
      "E10 zone active counts are non-negative ints")

print("F. EX-ANTE TIME FEATURES (legitimately known in advance)")
# target ts in NY kill zone -> feature says so although OHLC unread
from core.ict.types import Candle
hand = [Candle(ts=1735689900 + i * 60, open=100.0, high=101.0,
               low=99.0, close=100.5) for i in range(120)]
# 1735689900 = 2025-01-01 03:25 UTC... craft explicit NY-kz target instead
from core.ict.sessions import session_at
ny_ts = None
base = 1767225600  # 2026-01-01 00:00 UTC (winter)
for off in range(0, 1440):
    ts = base + off * 60
    if session_at(ts)["kill_zone"] == "ny_kz":
        ny_ts = ts
        break
check(ny_ts is not None, "F1 found an NY kill-zone timestamp (winter 14:30 UTC)")
if ny_ts is not None:
    idx = (ny_ts - base) // 60
    candles = [Candle(ts=base + i * 60, open=100.0, high=101.0, low=99.0,
                      close=100.5) for i in range(idx + 1)]
    snap = snapshot_at(candles, len(candles) - 1)
    check(snap["session.target_kill_zone"] == "ny_kz",
          "F2 target kill-zone tagged ex-ante from TARGET ts (OHLC unread)")
events = import_calendar(os.path.join(_REPO, "scripts", "data",
                                      "news_sample_calendar.json"))
ev = sorted(events, key=lambda e: e.ts)[0]
candles = [Candle(ts=ev.ts - (120 - i) * 60, open=100.0, high=101.0,
                  low=99.0, close=100.5) for i in range(120)]
snap = snapshot_at(candles, 119, events=events)
m = minutes_to_event(candles[119].ts, events, impact="high")
check(snap["news.minutes_to_high_impact"] == (m[0] if m else None),
          "F3 news distance matches minutes_to_event for the target ts")
check(snap["news.provided"] is True, "F4 news.provided=True with events")

print("G. WARMUP & ERRORS")
try:
    ctx.snapshot(0)
    check(False, "G1 t=0 raises IndexError")
except IndexError:
    check(True, "G1 t=0 raises IndexError")
try:
    ctx.snapshot(len(cs))
    check(False, "G2 t=len raises IndexError")
except IndexError:
    check(True, "G2 t=len raises IndexError")
try:
    FeatureContext([])
    check(False, "G3 empty candles raise ContractError")
except Exception:
    check(True, "G3 empty candles raise ContractError")
f1 = ctx.snapshot(1)
check(f1["mom.rsi"] is None and f1["pattern.last_kind"] is None
      and f1["liq.in_range"] is None,
      "G4 t=1 graceful Nones during warmup")

print("H. DETERMINISM")
m_a = build_feature_matrix(cs, start=1, end=60)
m_b = build_feature_matrix(cs, start=1, end=60)
check(json.dumps(m_a, sort_keys=True, default=str)
      == json.dumps(m_b, sort_keys=True, default=str),
      "H1 two matrix builds byte-identical")

print("I. SCALE (timing)")
t0 = time.time()
big = synthetic_series(2000, seed=99)
mb = build_feature_matrix(big, start=1, end=1999)
el = time.time() - t0
check(el < 60.0, f"I1 matrix over 2,000 candles in {el:.1f}s (<60s)")
check(len(mb) == 1999, "I2 row count exact")

print("J. REAL AUTHORIZED DATA SLICE (BTCUSDT 30-day)")
raw = json.load(open(os.path.join(_REPO, "scripts", "data",
                                  "binance_30d", "BTCUSDT_1m.json")))[:1500]
from core.ict.types import candle_from_row
real = [candle_from_row(r) for r in raw]
t0 = time.time()
mr = build_feature_matrix(real, start=1, end=1499)
el = time.time() - t0
mid = mr[750]["features"]
check(len(mr) == 1499 and el < 60.0,
      f"J1 real-slice matrix 1,499 rows in {el:.1f}s")
check(mid["mom.rsi"] is not None, "J2 mid-series rsi present on real data")
check(mid["session.target_session"] in ("asia", "london", "newyork", "off"),
      "J3 real-data session tagged")
# spot no-lookahead on real data: perturb target candle
rctx = FeatureContext(real)
t = 900
base_snap = rctx.snapshot(t)
ok = True
for variant in (0, 1):
    mut = perturb(real, t, variant)
    if FeatureContext(mut).snapshot(t) != base_snap:
        ok = False
check(ok, "J4 real-data target-OHLC perturbation leaves snapshot identical")

print()
print("=" * 72)
print(f"PHASE-L5 INTEGRATION: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
