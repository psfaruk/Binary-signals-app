"""PHASE-L4 LIQUIDITY/SESSIONS/NEWS/ORDERFLOW battery (Phase L, Agent D2 / Task L4).

Run:  python3 scripts/test_phase_l4_liquidity_sessions_news.py   (from repo root)

Self-contained, offline, deterministic. No network, no broker, no
credentials, no database (tempfiles under tempfile.TemporaryDirectory are
used ONLY to exercise news.import_calendar — the sanctioned loader; the
detectors themselves stay pure). Hand-crafted candle fixtures with EXACT
hand-computed expectations, adversarial edge cases, mechanical
no-lookahead proofs (multi-variant perturbation via
core.ict.helpers.assert_no_lookahead), determinism proofs, and source
honesty checks on all four modules.

RUNTIME NOTE: ~2 minutes total. The single expensive check is the mandated
mechanical no-lookahead proof for detect_session_events on the 2880-candle
2-day series (2880 perturbation points x 4 variants, each a full re-scan
plus contract validation) — that cost is the proof's teeth, not waste.

DOCUMENTED SPEC-AMBIGUITY RESOLUTION pinned by these checks (see also the
module docstring + worklog):
  * detect_ranges confirm_index: the phase spec's "confirm_index = last run
    candle" is read as "the candle at which the maximal run becomes
    knowable" = the BREAKER candle b (run s..e, breaker b = e+1), or e when
    the run terminates at end of data. The literal reading (stamp e while
    deciding on candle b) is mechanically incompatible with the frozen
    no-lookahead contract: perturbing the breaker back inside the band would
    delete a detection confirmed BEFORE the perturbed candle. Checks A1-A3
    assert the breaker semantics; G1 proves the contract mechanically.
  * detect_amd emission rule: the spec's "range -> sweep of range LOW ->
    within 5 candles a candle with body >= ... AND close > mid; one per
    (range, side)" is implemented as a 5-candle LOOKBACK from each candidate
    distribution candle d (sweep index in [max(range.confirm, d-5), d-1],
    the LATEST sweep reported when several qualify) rather than a
    first-sweep-only rule, which silently missed valid sequences whenever
    the first sweep's 5-candle window held no qualifying expansion. The
    lookback form reads only candles[:d+1] (no-lookahead by construction);
    checks C5b/C5c pin the retry + latest-sweep-pairing semantics and G2b
    proves them mechanically on the retry/pairing fixtures.

Coverage (all checks; exit code 0 iff all pass):
  P. fixture sanity                                              (1 check)
  A. detect_ranges: exact runs, boundaries, min_bars             (9 checks)
  B. detect_sweeps: levels/ranges/fractals, confirm, wick_min    (12 checks)
  C. detect_amd: bullish/bearish sequences + negatives           (9 checks)
  D. sessions: boundaries, DST, kill zones, events               (20 checks)
  E. news: import/dedup/sha256/CSV, minutes, gate                (19 checks)
  F. orderflow: deltas, z-scores, boundaries, warmup             (15 checks)
  G. mechanical no-lookahead (synthetic + crafted)               (9 checks)
  H. determinism                                                (9 checks)
  I. module source honesty + frozen API surface                 (15 checks)
"""
import dataclasses
import datetime as dt
import hashlib
import inspect
import json
import os
import re
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.ict.types import (  # noqa: E402
    Candle,
    ContractError,
    Detection,
    validate_candles,
)
from core.ict.helpers import (  # noqa: E402
    assert_deterministic,
    assert_no_lookahead,
    synthetic_series,
)
from core.ict import liquidity as lq  # noqa: E402
from core.ict import sessions as ss  # noqa: E402
from core.ict import news as nw  # noqa: E402
from core.ict import orderflow as of  # noqa: E402

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


def _close(a, b, eps=1e-9):
    return a is not None and b is not None and abs(a - b) <= eps


T0 = (1_700_000_000 // 60) * 60  # minute-aligned epoch base


def _c(i, o, h, l, c, v=100.0):
    """One hand-crafted candle at index i (ts strictly increasing, 1-min)."""
    return Candle(ts=T0 + i * 60, open=o, high=h, low=l, close=c, volume=v)


def _T(y, m, d, H, M=0, S=0):
    """Independent epoch-seconds helper (UTC) — not the module's calendar."""
    return int(dt.datetime(y, m, d, H, M, S, tzinfo=dt.timezone.utc).timestamp())


def _last_sunday(y, m, last_day):
    d = dt.date(y, m, last_day)
    while d.weekday() != 6:  # Sunday
        d -= dt.timedelta(days=1)
    return d


def _nth_sunday(y, m, n):
    d = dt.date(y, m, 1)
    while d.weekday() != 6:
        d += dt.timedelta(days=1)
    return d + dt.timedelta(weeks=n - 1)


# ── hand-crafted fixtures ────────────────────────────────────────────────
# R1: 8-bar consolidation (indices 0..7, zone [99.15, 100.9]) broken by an
#     outside CLOSE at index 8 (close 101.5 > band_high + tol = 101.05);
#     the breaker also wicks BELOW the zone low (98.9) but that wick is not
#     part of the range. Fillers 9..11 form no second run.
R1 = [
    _c(0, 100.0, 100.6, 99.4, 100.2), _c(1, 100.2, 100.7, 99.3, 100.1),
    _c(2, 100.1, 100.8, 99.2, 99.9), _c(3, 99.9, 100.75, 99.25, 100.0),
    _c(4, 100.0, 100.85, 99.3, 100.3), _c(5, 100.3, 100.7, 99.15, 99.8),
    _c(6, 99.8, 100.8, 99.2, 100.05), _c(7, 100.05, 100.9, 99.25, 100.4),
    _c(8, 100.4, 101.8, 98.9, 101.5),  # breaker: close outside the band
    _c(9, 101.5, 102.6, 101.2, 102.2), _c(10, 102.2, 102.4, 102.0, 102.3),
    _c(11, 102.3, 102.5, 102.1, 102.2),
]
# R2: the SAME run shortened to 7 bars (index 3 dropped, all else equal).
R2 = [R1[i] for i in (0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11)]

# S1: explicit top-side level 106.0; candle 1 sweeps (high 106.5, close
#     105.4 back below), candle 2 confirms (close 105.1 below 106).
S1 = [
    _c(0, 105.0, 105.5, 104.5, 105.2), _c(1, 105.2, 106.5, 105.0, 105.4),
    _c(2, 105.4, 105.8, 104.9, 105.1), _c(3, 105.1, 105.6, 104.8, 105.0),
]
# S3: like S1 but the next candle closes ABOVE the level -> confirm=True
#     must emit nothing; candle 3 sweeps again (high 106.6, close 105.8).
S3 = [
    _c(0, 105.0, 105.5, 104.5, 105.2), _c(1, 105.2, 106.5, 105.0, 105.4),
    _c(2, 105.4, 106.4, 105.0, 106.2), _c(3, 106.2, 106.6, 105.5, 105.8),
]
# S4: explicit bottom-side level 104.0; candle 1 sweeps (low 103.4, close
#     104.5 back above), candle 2 confirms (close 104.2 above 104).
S4 = [
    _c(0, 105.0, 105.5, 104.5, 105.2), _c(1, 105.0, 105.4, 103.4, 104.5),
    _c(2, 104.5, 104.9, 103.9, 104.2), _c(3, 104.2, 104.6, 103.8, 104.0),
]
# S5: pre-range sweep attempt (candle 2 wicks high but predates the range),
#     range at 3..10 (zone [98.2, 100.9], breaker/confirm at 11), then a
#     genuine post-confirmation sweep of the range high at 12 (confirmed 13).
S5 = [
    _c(0, 103.2, 103.7, 102.6, 103.3), _c(1, 103.3, 103.6, 102.7, 103.2),
    _c(2, 103.0, 103.4, 98.7, 99.0),     # pre-range sweep attempt (high 103.4)
    _c(3, 99.0, 100.4, 98.2, 98.3),
    _c(4, 98.4, 100.8, 98.3, 100.3), _c(5, 100.3, 100.9, 98.6, 100.1),
    _c(6, 100.1, 100.7, 98.4, 99.8), _c(7, 99.8, 100.75, 98.25, 100.0),
    _c(8, 100.0, 100.85, 98.3, 100.2), _c(9, 100.2, 100.6, 98.35, 99.9),
    _c(10, 99.9, 100.65, 98.4, 100.05),
    _c(11, 100.05, 101.6, 99.9, 101.3),  # breaker of the range (close outside)
    _c(12, 101.3, 101.5, 100.3, 100.5),  # SWEEP of range high 100.9 (wick 0.6)
    _c(13, 100.5, 100.8, 100.1, 100.3),  # confirmation close back below 100.9
]
# S6: fractal fallback. Swing high at index 3 (high 106 > 105 and > 105.8,
#     confirmed by candle 4). Candle 4 (the confirmation candle) CAN NEVER
#     sweep a k=1 swing (its high is below the swing by definition); candle
#     5 — the first index at which the swing is sweepable (j+2) — sweeps it.
S6 = [
    _c(0, 100.0, 101.0, 99.0, 100.5), _c(1, 100.5, 102.0, 100.0, 101.0),
    _c(2, 101.0, 105.0, 100.5, 104.0), _c(3, 104.0, 106.0, 103.5, 105.5),
    _c(4, 105.5, 105.8, 104.5, 105.0),  # swing confirmation candle
    _c(5, 105.0, 106.5, 104.8, 105.2),  # sweep of swing level 106.0
    _c(6, 105.2, 105.6, 104.9, 105.1),  # confirmation close below 106.0
]
# B12: candle 1 closes EXACTLY at the level (106.0) -> close is not strictly
#      back below -> no sweep (strict inequality boundary).
B12 = [
    _c(0, 105.0, 105.5, 104.5, 105.2), _c(1, 105.2, 106.5, 105.0, 106.0),
    _c(2, 106.0, 106.4, 105.0, 105.5),
]

# C1: AMD bullish. Range 0..7 (zone [99.15, 100.9], mid 100.025) confirmed
#     by breaker 8 (close 101.5). Candle 8 ALSO sweeps the range LOW
#     (low 98.9 < 99.15 by 0.25, close 101.5 > 99.15) -> sweep_index 8.
#     Distribution candle 9: body 0.7 >= 1.5 * median(bodies 0..8) =
#     1.5 * 0.25 = 0.375, close 102.2 > mid 100.025 -> dist_index 9.
C1 = [
    _c(0, 100.0, 100.6, 99.4, 100.2), _c(1, 100.2, 100.7, 99.3, 100.1),
    _c(2, 100.1, 100.8, 99.2, 99.9), _c(3, 99.9, 100.75, 99.25, 100.0),
    _c(4, 100.0, 100.85, 99.3, 100.3), _c(5, 100.3, 100.7, 99.15, 99.8),
    _c(6, 99.8, 100.8, 99.2, 100.05), _c(7, 100.05, 100.9, 99.25, 100.4),
    _c(8, 100.4, 101.8, 98.9, 101.5),   # breaker + sweep of the range low
    _c(9, 101.5, 102.6, 101.2, 102.2),  # distribution (body 0.7, close > mid)
    _c(10, 102.2, 102.4, 102.0, 102.3), _c(11, 102.3, 102.5, 102.1, 102.2),
    _c(12, 102.2, 102.45, 102.05, 102.35), _c(13, 102.35, 102.5, 102.2, 102.25),
]


def _mirror(cs):
    """Point-mirror every candle around 100.025 (swaps high/low) — an exact
    bullish<->bearish mirror that preserves validity, ranges, bodies, ts."""
    k = 200.05
    return [
        Candle(ts=c.ts, open=k - c.open, high=k - c.low, low=k - c.high,
               close=k - c.close, volume=c.volume)
        for c in cs
    ]


C2 = _mirror(C1)  # bearish AMD with identical indices (self-mirror zone)
# C3: weak expansion — candle 9 body 0.2 < 0.375, fillers weak too.
C3 = list(C1)
C3[9] = _c(9, 101.5, 102.0, 101.3, 101.7)
# C4: qualifying distribution pushed to sweep+6 (outside the 5-candle window).
C4 = list(C3) + [_c(14, 102.2, 103.5, 102.0, 103.2)]
# C5: TWO qualifying distribution candles -> only the first (index 9) emitted.
C5 = list(C1)
C5[10] = _c(10, 102.2, 103.4, 102.0, 103.1)
# C6: big body but close BELOW the range mid -> bullish side must reject it.
C6 = list(C1[:9]) + [_c(9, 101.5, 102.6, 99.0, 99.5)] + C1[10:]
# C7R: RETRY semantics. Range 0..7 (zone [99.15, 100.9], mid 100.025,
#      breaker+sweep#1 at 8). Candles 9..13 are zero-body fillers (body 0
#      vs thresholds 0.375/0.3375/0.3/0.3/0.3 -> sweep#1's window holds NO
#      qualifying distribution). A SECOND sweep of the range low at 14
#      (low 98.5 < 99.15, close 101.6 > 99.15) is followed by a qualifying
#      distribution at 15 (body 0.5 >= 1.5*median 0.1 = 0.15, close 102.1
#      > mid) -> amd_bullish sweep_index=14 dist_index=15. Hand-computed
#      median at d=15 over the 15 bodies of candles 0..14, sorted
#      0,0,0,0,0,0.1,0.1,0.1,0.2,0.2,0.25,0.3,0.35,0.5,1.1 -> 8th = 0.1.
#      Trailing candles 16..17 exist so the no-lookahead proof (G2b) can
#      perturb BEYOND the confirm index 15.
C7R = C1[:9] + [_c(i, 101.5, 101.6, 101.4, 101.5) for i in range(9, 14)]
C7R += [
    _c(14, 101.5, 101.7, 98.5, 101.6),  # second sweep of the range low
    _c(15, 101.6, 102.3, 101.5, 102.1),  # qualifying distribution
    _c(16, 102.1, 102.4, 102.0, 102.2), _c(17, 102.2, 102.5, 102.1, 102.3),
]
# C7P: LATEST-sweep pairing. Like C7R but TWO sweeps (13 AND 14) inside the
#      5-candle lookback of the distribution candle 15: lookback =
#      [max(8, 15-5), 14] = [10, 14]; sweep 13 (low 98.6 < 99.15, close
#      101.6 > 99.15) and sweep 14 (low 98.5) both qualify ->
#      meta["sweep_index"] must be 14 (the LATEST manipulation before the
#      expansion — documented deterministic rule).
C7P = C1[:9] + [_c(i, 101.5, 101.6, 101.4, 101.5) for i in range(9, 13)]
C7P += [
    _c(13, 101.5, 101.7, 98.6, 101.6),  # sweep (earlier)
    _c(14, 101.5, 101.7, 98.5, 101.6),  # sweep (latest)
    _c(15, 101.6, 102.3, 101.5, 102.1),  # qualifying distribution
    _c(16, 102.1, 102.4, 102.0, 102.2), _c(17, 102.2, 102.5, 102.1, 102.3),
]

# F1: delta hand-check. delta_i = ((c-o)/(h-l or 1e-9)) * (v or 1.0):
#     i1: (101-100)/(102-98) * 200   = 0.25  * 200 = 50.0
#     i2: (101-101)/(101-101 -> 1e-9) * 1.0 = 0.0        (zero-range candle)
#     i3: (102.5-101)/(103-100) * 1.0 = 0.5
#     i4: (103.5-102.5)/(104-102) * 50 = 0.5 * 50 = 25.0
F1 = [
    _c(0, 100.0, 101.0, 99.0, 100.5, 100.0),
    _c(1, 100.0, 102.0, 98.0, 101.0, 200.0),
    Candle(ts=T0 + 2 * 60, open=101.0, high=101.0, low=101.0, close=101.0,
           volume=None),
    Candle(ts=T0 + 3 * 60, open=101.0, high=103.0, low=100.0, close=102.5,
           volume=None),
    _c(4, 102.5, 104.0, 102.0, 103.5, 50.0),
]
# F3: absorption hand-check (window=4). Window vols [8,10,12,10]:
#     mean 10, var (4+0+4+0)/4 = 2, std sqrt(2)  -> vol_z = (20-10)/sqrt(2)
#     = 5*sqrt(2) ~ 7.0710678.  Window ranges [1,2,3,2]: mean 2, var
#     (1+0+1+0)/4 = 0.5, std sqrt(0.5) -> range_z = (1-2)/sqrt(0.5)
#     = -sqrt(2) ~ -1.4142136  <= -0.5  -> absorption.
F3 = [
    _c(0, 100.0, 100.5, 99.5, 100.2, 8.0),
    _c(1, 100.2, 101.2, 99.2, 100.8, 10.0),
    _c(2, 100.8, 102.3, 99.3, 101.5, 12.0),
    _c(3, 101.5, 102.5, 100.5, 102.0, 10.0),
    _c(4, 102.0, 102.5, 101.5, 102.3, 20.0),  # vol 20, range 1.0
]
# F4: same window, candle 4 range 4.0 -> range_z = (4-2)/sqrt(0.5) =
#     2*sqrt(2) ~ 2.8284271 >= 1.5 -> effort_anomaly (not absorption).
F4 = list(F3)
F4[4] = _c(4, 102.0, 104.5, 100.5, 104.0, 20.0)
# F5: one None volume INSIDE the window -> candle 4 skipped entirely.
F5 = list(F3)
F5[1] = Candle(ts=F3[1].ts, open=100.2, high=101.2, low=99.2, close=100.8,
               volume=None)
# FBf: exact z boundary (window=2). Window vols [1,2]: mean 1.5, var 0.25,
#      std 0.5 -> candle-2 vol 2.0 gives vol_z EXACTLY 1.0; window ranges
#      [1,2] -> candle-2 range 1.0 gives range_z EXACTLY -1.0.
#      Candle 3 sees window vols [2,2] -> std 0 -> skipped (degenerate).
FBf = [
    _c(0, 100.0, 100.5, 99.5, 100.2, 1.0),
    _c(1, 100.2, 101.2, 99.2, 100.8, 2.0),
    _c(2, 100.8, 101.3, 100.3, 101.0, 2.0),
    _c(3, 101.0, 101.5, 100.5, 101.2, 2.0),
]
# FBt: same shape for ticks (volume=None everywhere): candle-2 range 2.0 ->
#      range_z EXACTLY +1.0; candle 3 window ranges [2,2] -> std 0 -> skip.
FBt = [
    Candle(ts=T0 + i * 60, open=o, high=h, low=l, close=c, volume=None)
    for i, (o, h, l, c) in enumerate([
        (100.0, 100.5, 99.5, 100.2), (100.2, 101.2, 99.2, 100.8),
        (100.8, 101.8, 99.8, 101.0), (101.0, 101.5, 100.5, 101.2),
    ])
]
# F6: activity spike. 20 window candles: nineteen range 2.0 + one (index 5)
#     range 4.0 -> mean 2.1, var (19*0.01 + 3.61)/20 = 0.19, std sqrt(0.19);
#     candle 20 range 4.0 -> z = 1.9/sqrt(0.19) ~ 4.3588989 >= 2.0.
#     ALL volumes None (OTC-style feed): ticks must not care.
F6 = []
for i in range(21):
    if i == 5:
        F6.append(Candle(ts=T0 + i * 60, open=100.0, high=102.0, low=98.0,
                         close=101.0, volume=None))
    elif i == 20:
        F6.append(Candle(ts=T0 + i * 60, open=100.0, high=102.0, low=98.0,
                         close=101.5, volume=None))
    else:
        F6.append(Candle(ts=T0 + i * 60, open=100.0, high=101.0, low=99.0,
                         close=100.4, volume=None))

_ALL_FIXTURES = [R1, R2, S1, S3, S4, S5, S6, B12, C1, C2, C3, C4, C5, C6,
                 C7R, C7P, F1, F3, F4, F5, FBf, FBt, F6]

# ── P. fixture sanity ────────────────────────────────────────────────────
print("P. FIXTURE SANITY")
check(
    all(isinstance(c, Candle) for fx in _ALL_FIXTURES for c in fx),
    "P1 all fixture candles are Candle instances",
)
for fx in _ALL_FIXTURES:
    validate_candles(fx)  # raises -> battery crashes loudly (fixture bug)

# ── A. detect_ranges ─────────────────────────────────────────────────────
print("\nA. DETECT_RANGES")
_r1 = lq.detect_ranges(R1)
check(
    len(_r1) == 1
    and _r1[0].kind == "range" and _r1[0].side == "neutral"
    and _r1[0].start_index == 0 and _r1[0].confirm_index == 8
    and _r1[0].ts == R1[8].ts
    and _close(_r1[0].level_low, 99.15) and _close(_r1[0].level_high, 100.9)
    and _r1[0].meta == {"bars": 8},
    "A1 exact 8-bar run: kind/side/start/confirm(ts)/zone [99.15,100.9]/bars",
    f"got {[_r1[0]] if _r1 else []}",
)
_tol = 0.0015 * (99.15 + 100.9) / 2.0  # 0.1500375
check(
    _r1 and R1[7].close <= _r1[0].level_high + _tol
    and R1[8].close > _r1[0].level_high + _tol
    and _r1[0].confirm_index == 8,
    "A2 run ends at the first OUTSIDE close (breaker = confirmation candle)",
)
check(
    _r1 and _r1[0].level_high < R1[8].high and _r1[0].level_low > R1[8].low,
    "A3 zone excludes the breaker's wicks (min low / max high of the run only)",
)
check(lq.detect_ranges(R2) == [], "A4 7-bar run with min_bars=8 -> no detection")
check(
    lq.detect_ranges(R2, min_bars=7) and len(lq.detect_ranges(R2, min_bars=7)) == 1
    and lq.detect_ranges(R2, min_bars=7)[0].meta == {"bars": 7}
    and lq.detect_ranges(R2, min_bars=7)[0].confirm_index == 7,
    "A5 the same 7-bar run with min_bars=7 -> detected (bars=7, breaker 7)",
)
check(
    lq.detect_ranges(R1, min_bars=9) == [],
    "A6 8-bar run with min_bars=9 -> no detection",
)
check(
    len(lq.detect_ranges(R1, min_bars=7)) == 1
    and lq.detect_ranges(R1, min_bars=7)[0].meta == {"bars": 8},
    "A7 8-bar run with min_bars=7 -> the SAME maximal run (bars=8)",
)
_flat = [_c(i, 100.0, 100.0, 100.0, 100.0) for i in range(10)]
_rf = lq.detect_ranges(_flat)
check(
    len(_rf) == 1 and _rf[0].start_index == 0 and _rf[0].confirm_index == 9
    and _rf[0].meta == {"bars": 10} and _close(_rf[0].level_low, 100.0)
    and _close(_rf[0].level_high, 100.0),
    "A8 flat series: one end-of-data run, confirm = last candle (no breaker)",
)
check(
    _raises_contract_error(lq.detect_ranges, [])
    and _raises_contract_error(lq.detect_ranges, R1, min_bars=1)
    and _raises_contract_error(lq.detect_ranges, R1, band_rel=0.0)
    and _raises_contract_error(lq.detect_ranges, R1, min_bars=True),
    "A9 validation: empty candles / min_bars<2 / band_rel<=0 / bool min_bars",
)

# ── B. detect_sweeps ─────────────────────────────────────────────────────
print("\nB. DETECT_SWEEPS")
_b1 = lq.detect_sweeps(S1, levels=[(106.0, "high")])
check(
    len(_b1) == 1
    and _b1[0].kind == "sweep_buy_side" and _b1[0].side == "bearish"
    and _b1[0].start_index == 1 and _b1[0].confirm_index == 2
    and _b1[0].ts == S1[2].ts
    and _close(_b1[0].level_low, 106.0) and _close(_b1[0].level_high, 106.0)
    and set(_b1[0].meta) == {"level", "wick", "confirmed"}
    and _close(_b1[0].meta["level"], 106.0)
    and _close(_b1[0].meta["wick"], 0.5)
    and _b1[0].meta["confirmed"] is True,
    "B1 top-side confirm=True: exact kind/side/indices/ts/level/wick/meta",
    f"got {[_b1[0]] if _b1 else []}",
)
check(
    lq.detect_sweeps(S1, levels=[(106.0, "high")], wick_min=0.5) == [],
    "B2 wick_min == penetration (0.5) -> excluded (strictly greater required)",
)
check(
    len(lq.detect_sweeps(S1, levels=[(106.0, "high")], wick_min=0.4)) == 1,
    "B3 wick_min 0.4 < penetration 0.5 -> detected",
)
check(
    lq.detect_sweeps(S3, levels=[(106.0, "high")]) == [],
    "B4 confirm=True and next close NOT back below the level -> nothing",
)
_b5 = lq.detect_sweeps(S3, levels=[(106.0, "high")], confirm=False)
check(
    len(_b5) == 2
    and _b5[0].start_index == 1 and _b5[0].confirm_index == 1
    and _b5[1].start_index == 3 and _b5[1].confirm_index == 3
    and all(d.meta["confirmed"] is False for d in _b5),
    "B5 confirm=False: unconfirmed sweeps at i (candles 1 and 3)",
)
_b6 = lq.detect_sweeps(S4, levels=[(104.0, "low")])
check(
    len(_b6) == 1 and _b6[0].kind == "sweep_sell_side" and _b6[0].side == "bullish"
    and _b6[0].start_index == 1 and _b6[0].confirm_index == 2
    and _close(_b6[0].meta["wick"], 0.6) and _b6[0].meta["level"] == 104.0,
    "B6 bottom-side mirror: sweep_sell_side / bullish / wick 0.6",
)
_r5 = lq.detect_ranges(S5)
_b7 = lq.detect_sweeps(S5, ranges=_r5)
check(
    len(_r5) == 1 and _r5[0].start_index == 3 and _r5[0].confirm_index == 11
    and _close(_r5[0].level_low, 98.2) and _close(_r5[0].level_high, 100.9)
    and len(_b7) == 1 and _b7[0].kind == "sweep_buy_side"
    and _b7[0].start_index == 12 and _b7[0].confirm_index == 13
    and _close(_b7[0].meta["level"], 100.9)
    and _close(_b7[0].meta["wick"], 0.6)
    and all(d.start_index >= _r5[0].confirm_index for d in _b7),
    "B7 ranges-derived levels: only sweepable from range confirmation on "
    "(pre-range attempt at candle 2 ignored)",
    f"got {[(d.kind, d.start_index) for d in _b7]}",
)
_b8 = lq.detect_sweeps(S5, levels=[(100.9, "high")])
check(
    len(_b8) == 2 and _b8[0].start_index == 2 and _b8[0].confirm_index == 3
    and _b8[1].start_index == 12 and _b8[1].confirm_index == 13
    and _close(_b8[0].meta["wick"], 2.5),
    "B8 explicit levels on the SAME series: the candle-2 sweep IS detected "
    "(gating is a ranges-mode property, not a global one)",
)
_b9 = lq.detect_sweeps(S6)
check(
    len(_b9) == 1 and _b9[0].kind == "sweep_buy_side" and _b9[0].side == "bearish"
    and _b9[0].start_index == 5 and _b9[0].confirm_index == 6
    and _close(_b9[0].meta["level"], 106.0)
    and _close(_b9[0].meta["wick"], 0.5)
    and _b9[0].start_index >= 3 + 2,
    "B9 fractal fallback: swing at 3 swept at the EARLIEST legal index 5 "
    "(j+2; the confirmation candle 4 cannot sweep a k=1 swing by geometry)",
    f"got {[(d.kind, d.start_index) for d in _b9]}",
)
_b10 = lq.detect_sweeps(S6, confirm=False)
check(
    len(_b10) == 1 and _b10[0].start_index == 5 and _b10[0].confirm_index == 5
    and _b10[0].meta["confirmed"] is False,
    "B10 fractal sweep with confirm=False: confirmed at the sweep candle",
)
check(
    lq.detect_sweeps(B12, levels=[(106.0, "high")]) == [],
    "B11 close EXACTLY at the level is not 'back below' -> no sweep (strict)",
)
check(
    _raises_contract_error(lq.detect_sweeps, S1, levels=[(106.0, "up")])
    and _raises_contract_error(lq.detect_sweeps, S1, levels=[(106.0,)])
    and _raises_contract_error(lq.detect_sweeps, S1, levels=[[106.0, "high"]])
    and _raises_contract_error(lq.detect_sweeps, S1, levels=[(0.0, "high")])
    and _raises_contract_error(lq.detect_sweeps, S1, levels=[(106.0, "high")], wick_min=-1.0)
    and _raises_contract_error(lq.detect_sweeps, S1, confirm="yes")
    and _raises_contract_error(lq.detect_sweeps, S5, ranges=[_r5[0], "not-a-detection"]),
    "B12 level/ranges/confirm/wick_min validation raises ContractError",
)

# ── C. detect_amd ────────────────────────────────────────────────────────
print("\nC. DETECT_AMD")
_a1 = lq.detect_amd(C1)
check(
    len(_a1) == 1
    and _a1[0].kind == "amd_bullish" and _a1[0].side == "bullish"
    and _a1[0].start_index == 0 and _a1[0].confirm_index == 9
    and _a1[0].ts == C1[9].ts
    and _close(_a1[0].level_low, 99.15) and _close(_a1[0].level_high, 100.9)
    and set(_a1[0].meta) == {"range_low", "range_high", "sweep_index", "dist_index"}
    and _close(_a1[0].meta["range_low"], 99.15)
    and _close(_a1[0].meta["range_high"], 100.9)
    and _a1[0].meta["sweep_index"] == 8 and _a1[0].meta["dist_index"] == 9,
    "C1 bullish sequence: range 0..7 -> sweep of range low at 8 -> "
    "distribution at 9 (body 0.7 >= 1.5*median 0.25, close > mid 100.025)",
    f"got {[_a1[0]] if _a1 else []}",
)
_a2 = lq.detect_amd(C2)
check(
    len(_a2) == 1
    and _a2[0].kind == "amd_bearish" and _a2[0].side == "bearish"
    and _a2[0].start_index == 0 and _a2[0].confirm_index == 9
    and _a2[0].meta["sweep_index"] == 8 and _a2[0].meta["dist_index"] == 9
    and _close(_a2[0].meta["range_low"], 99.15)
    and _close(_a2[0].meta["range_high"], 100.9),
    "C2 bearish mirror: sweep of range HIGH at 8 -> distribution closes "
    "below the range mid at 9 (identical indices — exact point mirror)",
)
check(
    lq.detect_amd(C3) == [],
    "C3 weak expansion (body 0.2 < 1.5*median) -> no AMD",
)
check(
    lq.detect_amd(C4) == [],
    "C4 distribution 6 candles after the sweep (window is sweep+1..sweep+5) "
    "-> no AMD",
)
_a5 = lq.detect_amd(C5)
check(
    len(_a5) == 1 and _a5[0].meta["dist_index"] == 9,
    "C5 two qualifying distribution candles -> only the FIRST (one per "
    "range+side)",
)
_a5b = lq.detect_amd(C7R)
check(
    len(_a5b) == 1
    and _a5b[0].kind == "amd_bullish" and _a5b[0].side == "bullish"
    and _a5b[0].start_index == 0 and _a5b[0].confirm_index == 15
    and _a5b[0].ts == C7R[15].ts
    and _a5b[0].meta["sweep_index"] == 14 and _a5b[0].meta["dist_index"] == 15
    and _close(_a5b[0].meta["range_low"], 99.15)
    and _close(_a5b[0].meta["range_high"], 100.9),
    "C5b RETRY: the first sweep (8) has no qualifying distribution in its "
    "window but the SECOND sweep (14) is followed by one at 15 -> the "
    "sequence still fires (a first-sweep-only rule would have missed it); "
    "sweep_index 14 / dist_index 15 / confirm 15",
    f"got {[_a5b[0]] if _a5b else []}",
)
_a5c = lq.detect_amd(C7P)
check(
    len(_a5c) == 1 and _a5c[0].confirm_index == 15
    and _a5c[0].meta["sweep_index"] == 14 and _a5c[0].meta["dist_index"] == 15,
    "C5c two sweeps (13, 14) inside the distribution's 5-candle lookback -> "
    "meta['sweep_index'] reports the LATEST (14) — deterministic pairing",
    f"got {[_a5c[0]] if _a5c else []}",
)
check(
    lq.detect_amd(C6) == [],
    "C6 big body but close BELOW the range mid -> bullish side rejects it",
)
check(
    _raises_contract_error(lq.detect_amd, [])
    and _raises_contract_error(lq.detect_amd, C1, impulse_mult=0.0)
    and _raises_contract_error(lq.detect_amd, C1, min_bars=1)
    and _raises_contract_error(lq.detect_amd, C1, band_rel=-0.1),
    "C7 validation: empty candles / impulse_mult<=0 / min_bars<2 / band_rel<0",
)

# ── D. sessions ──────────────────────────────────────────────────────────
print("\nD. SESSIONS")
_s = ss.session_at(_T(2026, 1, 15, 8, 0))
check(
    _s["session"] == "london" and _s["kill_zone"] == "london_kz"
    and ss.session_at(_T(2026, 1, 15, 7, 59, 59))["session"] == "off"
    and ss.session_at(_T(2026, 1, 15, 16, 29, 59))["session"] == "london"
    and ss.session_at(_T(2026, 1, 15, 16, 30))["session"] == "newyork",
    "D1 London winter: opens 08:00:00, closes 16:30 (half-open), NY follows",
)
check(
    ss.session_at(_T(2026, 7, 15, 7, 0))["session"] == "london"
    and ss.session_at(_T(2026, 7, 15, 6, 59, 59))["session"] == "off"
    and ss.session_at(_T(2026, 7, 15, 15, 29, 59))["session"] == "london"
    and ss.session_at(_T(2026, 7, 15, 15, 30))["session"] == "newyork"
    and ss.session_at(_T(2026, 7, 15, 7, 0))["dst_london"] is True,
    "D2 London summer (DST): opens 07:00:00 UTC, closes 15:30",
)
check(
    ss.session_at(_T(2026, 1, 15, 14, 30))["session"] == "london"
    and ss.session_at(_T(2026, 1, 15, 16, 30))["session"] == "newyork"
    and ss.session_at(_T(2026, 1, 15, 20, 59, 59))["session"] == "newyork"
    and ss.session_at(_T(2026, 1, 15, 21, 0))["session"] == "asia",
    "D3 NY winter: 14:30-21:00 with london > newyork > asia priority",
)
check(
    ss.session_at(_T(2026, 1, 15, 14, 30))["kill_zone"] == "ny_kz"
    and ss.session_at(_T(2026, 1, 15, 14, 30))["session"] == "london"
    and ss.session_at(_T(2026, 1, 15, 17, 29, 59))["kill_zone"] == "ny_kz"
    and ss.session_at(_T(2026, 1, 15, 17, 30))["kill_zone"] is None,
    "D4 NY kill zone winter 14:30-17:30 INDEPENDENT of session priority "
    "(session=london while ny_kz runs)",
)
check(
    ss.session_at(_T(2026, 7, 15, 13, 30))["kill_zone"] == "ny_kz"
    and ss.session_at(_T(2026, 7, 15, 16, 29, 59))["kill_zone"] == "ny_kz"
    and ss.session_at(_T(2026, 7, 15, 16, 30))["kill_zone"] is None
    and ss.session_at(_T(2026, 7, 15, 7, 0))["kill_zone"] == "london_kz"
    and ss.session_at(_T(2026, 7, 15, 9, 59, 59))["kill_zone"] == "london_kz"
    and ss.session_at(_T(2026, 7, 15, 10, 0))["kill_zone"] is None,
    "D5 kill zones summer: NY 13:30-16:30, London 07:00-10:00 (first 3h)",
)
check(
    ss.session_at(_T(2026, 1, 15, 22, 0))["session"] == "asia"
    and ss.session_at(_T(2026, 1, 15, 23, 59, 59))["session"] == "asia"
    and ss.session_at(_T(2026, 1, 16, 0, 0))["session"] == "off"
    and ss.session_at(_T(2026, 1, 16, 3, 0))["session"] == "off",
    "D6 Asia 15:00-24:00 wraps at UTC midnight: 23:59:59 asia, 00:00 off",
)
check(
    ss.session_at(_T(2026, 1, 15, 15, 30))["session"] == "london",
    "D7 triple overlap (winter 15:30: london+newyork+asia) -> london",
)
_lon_start = int(dt.datetime.combine(_last_sunday(2026, 3, 31), dt.time(1, 0),
                                     tzinfo=dt.timezone.utc).timestamp())
check(
    _last_sunday(2026, 3, 31) == dt.date(2026, 3, 29)
    and ss.session_at(_lon_start - 1)["dst_london"] is False
    and ss.session_at(_lon_start)["dst_london"] is True
    and ss.session_at(_lon_start - 1)["session"] == "off"
    and ss.session_at(_T(2026, 3, 29, 7, 0))["session"] == "london",
    "D8 London DST starts 2026-03-29 01:00 UTC (independently computed); "
    "summer schedule active from the flip (open 07:00)",
)
_ny_start = int(dt.datetime.combine(_nth_sunday(2026, 3, 2), dt.time(7, 0),
                                    tzinfo=dt.timezone.utc).timestamp())
check(
    _nth_sunday(2026, 3, 2) == dt.date(2026, 3, 8)
    and ss.session_at(_ny_start - 1)["dst_ny"] is False
    and ss.session_at(_ny_start)["dst_ny"] is True
    and ss.session_at(_ny_start - 1)["dst_london"] is False,
    "D9 NY DST starts 2026-03-08 07:00 UTC while London is still on winter "
    "time (independent flags)",
)
check(
    ss.session_at(_T(2026, 3, 10, 13, 30))["dst_ny"] is True
    and ss.session_at(_T(2026, 3, 10, 13, 30))["dst_london"] is False
    and ss.session_at(_T(2026, 3, 10, 13, 30))["kill_zone"] == "ny_kz"
    and ss.session_at(_T(2026, 3, 10, 13, 30))["session"] == "london",
    "D10 asymmetric DST window (2026-03-10): NY summer KZ 13:30 while "
    "London winter session still running",
)
_lon_end = int(dt.datetime.combine(_last_sunday(2026, 10, 31), dt.time(1, 0),
                                   tzinfo=dt.timezone.utc).timestamp())
_ny_end = int(dt.datetime.combine(_nth_sunday(2026, 11, 1), dt.time(6, 0),
                                  tzinfo=dt.timezone.utc).timestamp())
check(
    _last_sunday(2026, 10, 31) == dt.date(2026, 10, 25)
    and _nth_sunday(2026, 11, 1) == dt.date(2026, 11, 1)
    and ss.session_at(_lon_end - 1)["dst_london"] is True
    and ss.session_at(_lon_end)["dst_london"] is False
    and ss.session_at(_ny_end - 1)["dst_ny"] is True
    and ss.session_at(_ny_end)["dst_ny"] is False,
    "D11 DST ends: London 2026-10-25 01:00 UTC, NY 2026-11-01 06:00 UTC",
)
check(
    ss.session_at(_T(2026, 7, 15, 7, 30), dst_mode="never")["session"] == "off"
    and ss.session_at(_T(2026, 7, 15, 7, 30), dst_mode="never")["dst_london"] is False
    and ss.session_at(_T(2026, 7, 15, 8, 0), dst_mode="never")["session"] == "london"
    and ss.session_at(_T(2026, 7, 15, 7, 0))["session"] == "london",
    "D12 dst_mode='never' forces winter offsets in July (auto = london at "
    "07:00, never = off until 08:00)",
)
check(
    ss.session_at(_T(2026, 1, 15, 7, 30), dst_mode="always")["session"] == "london"
    and ss.session_at(_T(2026, 1, 15, 7, 30), dst_mode="always")["dst_london"] is True
    and ss.session_at(_T(2026, 1, 15, 7, 30), dst_mode="always")["dst_ny"] is True,
    "D13 dst_mode='always' forces summer offsets in January",
)
check(
    set(_s) == {"session", "kill_zone", "dst_london", "dst_ny", "note"}
    and isinstance(_s["note"], str) and len(_s["note"]) > 0
    and _s["note"] == "session boundaries are a research convention; "
    "validity on OTC feeds is unverified",
    "D14 exact dict keys + the mandated non-empty honesty note verbatim",
)
check(
    _raises_contract_error(ss.session_at, _T(2026, 1, 15, 8, 0), dst_mode="sometimes")
    and _raises_contract_error(ss.session_at, True)
    and _raises_contract_error(ss.session_at, 1768473600.0),
    "D15 validation: bad dst_mode / bool ts / float ts",
)
_2D0 = _T(2026, 1, 15, 0, 0)
_FLAT2D = [Candle(ts=_2D0 + i * 60, open=100.0, high=101.0, low=99.0,
                  close=100.5) for i in range(2880)]
_tags = ss.tag_sessions(_FLAT2D)
_direct = [
    {"session": ss.session_at(c.ts)["session"],
     "kill_zone": ss.session_at(c.ts)["kill_zone"]}
    for c in _FLAT2D
]
check(
    len(_tags) == 2880 and _tags == _direct
    and all(set(t) == {"session", "kill_zone"} for t in _tags[:50]),
    "D16 tag_sessions == per-candle session_at on the 2-day series "
    "(skipping optimisation provably equivalent), exact keys",
)
_ev_open = ss.detect_session_events(
    [Candle(ts=_T(2026, 1, 15, 7, 55) + i * 60, open=100.0, high=101.0,
            low=99.0, close=100.5) for i in range(11)]
)
check(
    len(_ev_open) == 2
    and _ev_open[0].kind == "session_open"
    and _ev_open[0].meta == {"session": "london", "event": "session_open"}
    and _ev_open[0].start_index == _ev_open[0].confirm_index == 5
    and _ev_open[0].ts == _T(2026, 1, 15, 8, 0)
    and _ev_open[0].side == "neutral"
    and _ev_open[1].kind == "kill_zone_open"
    and _ev_open[1].meta == {"session": "london_kz", "event": "kill_zone_open"}
    and _ev_open[1].start_index == 5,
    "D17 session_open + kill_zone_open at the 08:00 candle (index 5), "
    "side neutral, meta exact",
)
_ev_mid = ss.detect_session_events(
    [Candle(ts=_T(2026, 1, 15, 23, 56) + i * 60, open=100.0, high=101.0,
            low=99.0, close=100.5) for i in range(8)]
)
check(
    len(_ev_mid) == 1 and _ev_mid[0].kind == "session_close"
    and _ev_mid[0].meta == {"session": "asia", "event": "session_close"}
    and _ev_mid[0].start_index == 4 and _ev_mid[0].side == "neutral",
    "D18 Asia session_close at the UTC-midnight candle (index 4)",
)
_ev_kz = ss.detect_session_events(
    [Candle(ts=_T(2026, 1, 15, 10, 58) + i * 60, open=100.0, high=101.0,
            low=99.0, close=100.5) for i in range(6)]
)
check(
    len(_ev_kz) == 1 and _ev_kz[0].kind == "kill_zone_close"
    and _ev_kz[0].meta == {"session": "london_kz", "event": "kill_zone_close"}
    and _ev_kz[0].start_index == 2,
    "D19 kill_zone_close at 11:00 with NO session events (components "
    "compared independently — session did not re-emit)",
)
check(
    _raises_contract_error(ss.detect_session_events, [])
    and _raises_contract_error(ss.tag_sessions, []),
    "D20 empty candles raise ContractError (validate_candles authoritative)",
)

# ── E. news ──────────────────────────────────────────────────────────────
print("\nE. NEWS")
_SAMPLE = os.path.join(_REPO, "scripts", "data", "news_sample_calendar.json")
_ev = nw.import_calendar(_SAMPLE)
_NFP = "SYNTHETIC-TEST: Nonfarm Payrolls (fabricated fixture event — not real calendar data)"
check(
    len(_ev) == 6 and all(isinstance(e, nw.NewsEvent) for e in _ev),
    "E1 sample calendar imports exactly 6 NewsEvents",
)
check(
    _ev[0].ts == _T(2026, 1, 15, 13, 30) and _ev[0].currency == "USD"
    and _ev[0].impact == "high" and _ev[0].title == _NFP
    and _ev[0].source_file == _SAMPLE
    and _ev[1].ts == 1768489200 and _ev[1].impact == "medium"
    and _ev[2].ts == _T(2026, 1, 16, 9, 0) and _ev[2].currency == "EUR"
    and _ev[3].ts == 1768558500 and _ev[3].impact == "low"
    and _ev[4].ts == _T(2026, 1, 19, 13, 30) and _ev[4].impact == "high"
    and _ev[5].ts == 1768836600 and _ev[5].impact == "medium",
    "E2 ISO rows -> independent datetime epochs; ts rows verbatim; fields "
    "exact; import order preserved",
)
_sha = hashlib.sha256(open(_SAMPLE, "rb").read()).hexdigest()
check(
    all(e.source_sha256 == _sha for e in _ev) and len(_sha) == 64,
    "E3 sha256 of the file bytes recomputed independently matches every "
    "event's source_sha256",
)
with tempfile.TemporaryDirectory() as _td:
    _dup = os.path.join(_td, "dup.json")
    _rows = json.load(open(_SAMPLE, "r", encoding="utf-8"))
    _rows.append(dict(_rows[0]))  # exact duplicate: ts+currency+title
    with open(_dup, "w", encoding="utf-8") as _fh:
        json.dump(_rows, _fh)
    check(
        len(nw.import_calendar(_dup)) == 6,
        "E4 tempfile copy with one exact duplicate row -> still 6 events "
        "(dedup on ts+currency+title, first kept)",
    )
    _csv = os.path.join(_td, "cal.csv")
    with open(_csv, "w", encoding="utf-8") as _fh:
        _fh.write("ts,time_utc,currency,impact,title\n"
                  "1768483800,,USD,high,CSV NFP\n"
                  ",2026-01-16T09:00:00Z,EUR,medium,CSV ECB\n"
                  "1768554000,,USD,low,CSV misc\n")
    _cev = nw.import_calendar(_csv)
    check(
        len(_cev) == 3 and _cev[0].ts == 1768483800 and _cev[0].impact == "high"
        and _cev[1].ts == _T(2026, 1, 16, 9, 0) and _cev[1].currency == "EUR"
        and _cev[2].currency == "USD" and _cev[2].impact == "low"
        and all(e.source_sha256 == hashlib.sha256(
            open(_csv, "rb").read()).hexdigest() for e in _cev),
        "E5 CSV: 3 rows (epoch ts + ISO time_utc mixed), fields and sha256 "
        "correct",
    )
    _bad_impact = os.path.join(_td, "bad_impact.json")
    with open(_bad_impact, "w", encoding="utf-8") as _fh:
        json.dump([{"ts": 1768483800, "currency": "USD", "impact": "critical",
                    "title": "x"}], _fh)
    check(
        _raises_contract_error(nw.import_calendar, _bad_impact),
        "E6 bad impact ('critical') raises ContractError via the import path",
    )
    _naive = os.path.join(_td, "naive.json")
    with open(_naive, "w", encoding="utf-8") as _fh:
        json.dump([{"time": "2026-01-15T13:30:00", "currency": "USD",
                    "impact": "high", "title": "x"}], _fh)
    check(
        _raises_contract_error(nw.import_calendar, _naive),
        "E7 naive ISO (no Z/+00:00) raises ContractError",
    )
    _offset = os.path.join(_td, "offset.json")
    with open(_offset, "w", encoding="utf-8") as _fh:
        json.dump([{"time": "2026-01-15T13:30:00+01:00", "currency": "USD",
                    "impact": "high", "title": "x"}], _fh)
    check(
        _raises_contract_error(nw.import_calendar, _offset),
        "E8 non-UTC offset (+01:00) raises ContractError",
    )
    _ms = os.path.join(_td, "ms.json")
    with open(_ms, "w", encoding="utf-8") as _fh:
        json.dump([{"ts": 1768483800000, "currency": "USD", "impact": "high",
                    "title": "ms"}], _fh)
    check(
        nw.import_calendar(_ms)[0].ts == 1768483800,
        "E9 epoch milliseconds detected and converted to seconds",
    )
    _both = os.path.join(_td, "both.json")
    with open(_both, "w", encoding="utf-8") as _fh:
        json.dump([{"ts": 1768483800, "time": "2026-01-15T13:30:00Z",
                    "currency": "USD", "impact": "high", "title": "x"}], _fh)
    _neither = os.path.join(_td, "neither.json")
    with open(_neither, "w", encoding="utf-8") as _fh:
        json.dump([{"currency": "USD", "impact": "high", "title": "x"}], _fh)
    check(
        _raises_contract_error(nw.import_calendar, _both)
        and _raises_contract_error(nw.import_calendar, _neither),
        "E10 both ts+time / neither ts nor time raise ContractError",
    )
    _badhdr = os.path.join(_td, "badhdr.csv")
    with open(_badhdr, "w", encoding="utf-8") as _fh:
        _fh.write("ts,time,currency,impact,title\n")
    check(
        _raises_contract_error(nw.import_calendar, _badhdr)
        and _raises_contract_error(nw.import_calendar, "/nonexistent.txt")
        and _raises_contract_error(nw.import_calendar, "/nonexistent.json"),
        "E11 bad CSV header / unsupported suffix / missing file all raise "
        "ContractError (never a raw OSError)",
    )
check(
    _raises_contract_error(
        nw.NewsEvent, ts=100, currency="USD", impact="critical", title="x",
        source_file="f", source_sha256="h")
    and _raises_contract_error(
        nw.NewsEvent, ts=0, currency="USD", impact="high", title="x",
        source_file="f", source_sha256="h")
    and _raises_contract_error(
        nw.NewsEvent, ts=100, currency="  ", impact="high", title="x",
        source_file="f", source_sha256="h")
    and _raises_contract_error(
        nw.NewsEvent, ts=100, currency="USD", impact="high", title=" ",
        source_file="f", source_sha256="h")
    and _raises_contract_error(
        nw.NewsEvent, ts=True, currency="USD", impact="high", title="x",
        source_file="f", source_sha256="h"),
    "E12 NewsEvent.__post_init__: bad impact / ts<=0 / blank currency / "
    "blank title / bool ts",
)
try:
    _ev[0].impact = "low"
    _frozen = False
except Exception:
    _frozen = True
check(
    _frozen and nw.NewsEvent.__dataclass_fields__["ts"].name == "ts"
    and [f.name for f in dataclasses.fields(nw.NewsEvent)]
    == ["ts", "currency", "impact", "title", "source_file", "source_sha256"],
    "E13 NewsEvent is frozen (assignment raises) with the exact field order",
)
check(
    nw.minutes_to_event(1768483800 + 30, _ev, "high") == (0, _NFP)
    and nw.minutes_to_event(1768483800 - 90, _ev, "high") == (1, _NFP),
    "E14 minutes_to_event: 30s after -> (0, title); 90s before -> (1, title)",
)
check(
    nw.minutes_to_event(1768489200, _ev, "high") == (90, _NFP)
    and nw.minutes_to_event(1768489200, _ev, "medium")[0] == 0,
    "E15 exact impact match only: at the MEDIUM event's ts the nearest HIGH "
    "event is 90 min away (medium ignored for impact='high')",
)
check(
    nw.minutes_to_event(1768483800, [], "high") is None
    and nw.minutes_to_event(
        1768483800,
        [nw.NewsEvent(ts=1768483800, currency="USD", impact="medium",
                      title="m", source_file="f", source_sha256="h")],
        "high") is None,
    "E16 minutes_to_event -> None when no event matches the impact class",
)
check(
    nw.gate_for_news(1768483800 + 1800, _ev, window_min=30) is True
    and nw.gate_for_news(1768483800 + 1801, _ev, window_min=30) is False
    and nw.gate_for_news(1768483800 - 1800, _ev, window_min=30) is True
    and nw.gate_for_news(1768483800 - 1801, _ev, window_min=30) is False
    and nw.gate_for_news(1768483800, _ev, window_min=0) is True,
    "E17 gate_for_news window is INCLUSIVE (+/-30min -> True; 30min+1s -> "
    "False; window_min=0 -> exact ts only)",
)
check(
    nw.gate_for_news(1768489200, _ev, window_min=30) is False
    and nw.gate_for_news(1768489200, _ev, window_min=30,
                         impacts=("high", "medium")) is True,
    "E18 gate_for_news impacts filter: medium event gated only when medium "
    "is listed (risk policy hook)",
)
check(
    _raises_contract_error(nw.minutes_to_event, 1768483800, _ev, "critical")
    and _raises_contract_error(nw.gate_for_news, 1768483800, _ev, 30,
                               ("critical",))
    and _raises_contract_error(nw.gate_for_news, 1768483800, _ev, -1),
    "E19 minutes/gate param validation raises ContractError",
)

# ── F. orderflow ─────────────────────────────────────────────────────────
print("\nF. ORDERFLOW")
_d1 = of.volume_delta_series(F1)
check(
    len(_d1) == 5 and _d1[0] is None
    and _close(_d1[1], 50.0, 1e-12) and _close(_d1[2], 0.0, 1e-12)
    and _close(_d1[3], 0.5, 1e-12) and _close(_d1[4], 25.0, 1e-12),
    "F1 hand-computed deltas: [None, 50.0, 0.0, 0.5, 25.0] "
    "(0.25*200; zero-range -> 0/1e-9; 0.5*1.0; 0.5*50)",
    f"got {_d1}",
)
check(
    of.volume_delta_series(F1[:1]) == [None]
    and len(of.volume_delta_series(F3)) == 5
    and of.volume_delta_series(F1)[0] is None,
    "F2 delta warmup: index 0 is None for any n (None for i<1 only, no "
    "window warmup), length == n",
)
_f3 = of.detect_flow_anomalies(F3, window=4)
check(
    len(_f3) == 1 and _f3[0].kind == "absorption" and _f3[0].side == "neutral"
    and _f3[0].start_index == 4 and _f3[0].confirm_index == 4
    and _f3[0].ts == F3[4].ts
    and set(_f3[0].meta) == {"vol_z", "range_z"}
    and _close(_f3[0].meta["vol_z"], 5 * 2 ** 0.5)
    and _close(_f3[0].meta["range_z"], -(2 ** 0.5)),
    "F3 absorption hand-computed: vol_z = 10/sqrt(2) = 5*sqrt(2), "
    "range_z = -sqrt(2) (population z vs previous window, i excluded)",
    f"got {_f3[0].meta if _f3 else []}",
)
_f4 = of.detect_flow_anomalies(F4, window=4)
check(
    len(_f4) == 1 and _f4[0].kind == "effort_anomaly"
    and _f4[0].start_index == 4 and _f4[0].side == "neutral"
    and _close(_f4[0].meta["range_z"], 2 * 2 ** 0.5),
    "F4 effort_anomaly: vol_z >= 1.0 AND range_z = 2*sqrt(2) >= 1.5 "
    "(not absorption)",
)
check(
    of.detect_flow_anomalies(F5, window=4) == [],
    "F5 volume None anywhere in the comparison window -> candle skipped, "
    "no detections",
)
_fbf = of.detect_flow_anomalies(FBf, window=2)
check(
    len(_fbf) == 1 and _fbf[0].kind == "absorption" and _fbf[0].start_index == 2
    and _fbf[0].meta["vol_z"] == 1.0 and _fbf[0].meta["range_z"] == -1.0
    and of.detect_flow_anomalies(FBf, window=2, z_vol=1.5) == [],
    "F6 EXACT z boundary: vol_z == 1.0 detected at z_vol=1.0 (>= inclusive), "
    "rejected at z_vol=1.5; degenerate std-0 window at candle 3 skipped",
)
_f6 = of.tick_intensity_flags(F6, window=20, z=2.0)
check(
    len(_f6) == 1 and _f6[0].kind == "activity_spike"
    and _f6[0].side == "neutral"
    and _f6[0].start_index == 20 and _f6[0].confirm_index == 20
    and _f6[0].ts == F6[20].ts
    and _close(_f6[0].meta["range_z"], 1.9 / (0.19 ** 0.5))
    and all(c.volume is None for c in F6),
    "F7 activity_spike at the exact index 20 with hand-computed "
    "range_z = 1.9/sqrt(0.19) on a volume-LESS feed (activity proxy, no "
    "volume needed)",
    f"got {[_f6[0].meta] if _f6 else []}",
)
_fbt = of.tick_intensity_flags(FBt, window=2, z=1.0)
check(
    len(_fbt) == 1 and _fbt[0].kind == "activity_spike"
    and _fbt[0].start_index == 2 and _fbt[0].meta["range_z"] == 1.0
    and of.tick_intensity_flags(FBt, window=2, z=1.5) == [],
    "F8 tick z boundary: range_z == 1.0 detected at z=1.0 (>= inclusive), "
    "rejected at z=1.5",
)
check(
    of.detect_flow_anomalies(F3[:4], window=4) == []
    and of.tick_intensity_flags(F3[:4], window=4) == []
    and len(of.detect_flow_anomalies(F3, window=4)) == 1
    and _f3[0].start_index == 4,
    "F9 warmup: n == window -> zero detections; first eligible index is "
    "exactly i == window",
)
check(
    _raises_contract_error(of.detect_flow_anomalies, [])
    and _raises_contract_error(of.tick_intensity_flags, [])
    and _raises_contract_error(of.volume_delta_series, [])
    and _raises_contract_error(of.detect_flow_anomalies, F3, window=0)
    and _raises_contract_error(of.tick_intensity_flags, F3, window=0)
    and _raises_contract_error(of.volume_delta_series, F3, window=0)
    and _raises_contract_error(of.detect_flow_anomalies, F3, window=True)
    and _raises_contract_error(of.tick_intensity_flags, F3, window=True),
    "F10 validation: empty candles and bad window (0 / bool) raise "
    "ContractError for all three functions",
)
_d2 = of.volume_delta_series(F3)
check(
    _d2 == of.volume_delta_series(F3)
    and all(x is None or isinstance(x, float) for x in _d2),
    "F11 volume_delta_series is a pure float|None series, stable across "
    "runs",
)
_f1b = of.detect_flow_anomalies(F1, window=2)
check(
    _f1b == [] or all(d.kind in ("absorption", "effort_anomaly") for d in _f1b),
    "F12 kinds are exactly 'absorption' | 'effort_anomaly' (no other label)",
)
# F13: volume None on candle i itself (not the window) -> skipped too.
_f13 = list(F3)
_f13[4] = Candle(ts=F3[4].ts, open=102.0, high=102.5, low=101.5, close=102.3,
                 volume=None)
check(
    of.detect_flow_anomalies(_f13, window=4) == [],
    "F13 volume None on the TEST candle itself -> skipped (no orderflow "
    "claim without volume)",
)
_f14 = of.detect_flow_anomalies(F3, window=4, z_vol=10.0)
check(
    _f14 == [] and len(of.detect_flow_anomalies(F3, window=4, z_vol=7.0)) == 1,
    "F14 z_vol threshold is honoured exactly (7.0710678 >= 7.0 yes, >= 10 no)",
)
_f15 = of.detect_flow_anomalies(FBf, window=2, z_vol=1.0, z_range=1.5)
check(
    len(_f15) == 1 and _f15[0].kind == "absorption",
    "F15 overlapping-threshold precedence: with z_range=1.5 the candle "
    "satisfies both and absorption is emitted (deterministic precedence)",
)

# ── G. mechanical no-lookahead ───────────────────────────────────────────
print("\nG. MECHANICAL NO-LOOKAHEAD (assert_no_lookahead, 4 variants x every index)")
_s13 = synthetic_series(180, seed=13)
_s14 = synthetic_series(200, seed=14)
try:
    assert_no_lookahead(lq.detect_ranges, _s13)
    _g1 = True
    _g1d = f"{len(lq.detect_ranges(_s13))} base ranges"
except AssertionError as _e:
    _g1, _g1d = False, str(_e)[:200]
check(
    _g1 and len(lq.detect_ranges(_s13)) > 0,
    "G1 detect_ranges on synthetic_series(180, seed=13) — non-vacuous "
    f"({_g1d})",
    _g1d,
)
try:
    assert_no_lookahead(lq.detect_amd, _s13)
    _g2 = True
except AssertionError as _e:
    _g2, _g2d = False, str(_e)[:200]
    check(False, "G2 detect_amd on synthetic_series(180, seed=13)", _g2d)
else:
    check(_g2, f"G2 detect_amd on synthetic_series(180, seed=13) (mandated "
    f"seed; {len(lq.detect_amd(_s13))} AMDs there — the crafted-fixture "
    f"proof below supplies the teeth)")
try:
    assert_no_lookahead(lq.detect_amd, C1)
    assert_no_lookahead(lq.detect_amd, C2)
    assert_no_lookahead(lq.detect_amd, C7R)
    assert_no_lookahead(lq.detect_amd, C7P)
    _g2b = True
except AssertionError as _e:
    _g2b, _g2bd = False, str(_e)[:200]
    check(False, "G2b detect_amd no-lookahead on the C1/C2/C7R/C7P AMD fixtures", _g2bd)
else:
    check(
        _g2b and len(lq.detect_amd(C1)) == 1 and len(lq.detect_amd(C2)) == 1
        and len(lq.detect_amd(C7R)) == 1 and len(lq.detect_amd(C7P)) == 1,
        "G2b detect_amd no-lookahead on crafted fixtures — non-vacuous "
        "(1 AMD each on C1/C2 and on the retry/pairing fixtures C7R/C7P; "
        "perturbing post-confirm candles 16..17 cannot touch the "
        "confirm-15 detections)",
    )
try:
    assert_no_lookahead(of.detect_flow_anomalies, _s13)
    _g3 = True
except AssertionError as _e:
    _g3, _g3d = False, str(_e)[:200]
    check(False, "G3 detect_flow_anomalies on synthetic_series(180, seed=13)", _g3d)
else:
    check(
        _g3 and len(of.detect_flow_anomalies(_s13)) > 0,
        f"G3 detect_flow_anomalies on synthetic_series(180, seed=13) — "
        f"non-vacuous ({len(of.detect_flow_anomalies(_s13))} base detections)",
    )
_LV = [(98.0, "low"), (102.0, "high")]
try:
    assert_no_lookahead(lq.detect_sweeps, _s14, params={"levels": _LV})
    _g4 = True
except AssertionError as _e:
    _g4, _g4d = False, str(_e)[:200]
    check(False, "G4 detect_sweeps explicit levels on synthetic_series(200, seed=14)", _g4d)
else:
    check(
        _g4 and len(lq.detect_sweeps(_s14, levels=_LV)) > 0,
        f"G4 detect_sweeps EXPLICIT levels on synthetic_series(200, seed=14) "
        f"— non-vacuous ({len(lq.detect_sweeps(_s14, levels=_LV))} sweeps)",
    )
try:
    assert_no_lookahead(lq.detect_sweeps, _s14)
    _g5 = True
except AssertionError as _e:
    _g5, _g5d = False, str(_e)[:200]
    check(False, "G5 detect_sweeps fractal fallback on synthetic_series(200, seed=14)", _g5d)
else:
    check(
        _g5 and len(lq.detect_sweeps(_s14)) > 0,
        f"G5 detect_sweeps FRACTAL fallback on synthetic_series(200, "
        f"seed=14) — non-vacuous ({len(lq.detect_sweeps(_s14))} sweeps)",
    )
try:
    assert_no_lookahead(of.tick_intensity_flags, _s13)
    _g6 = True
except AssertionError as _e:
    _g6, _g6d = False, str(_e)[:200]
    check(False, "G6 tick_intensity_flags on synthetic_series(180, seed=13)", _g6d)
else:
    check(
        _g6 and len(of.tick_intensity_flags(_s13)) > 0,
        f"G6 tick_intensity_flags on synthetic_series(180, seed=13) — "
        f"non-vacuous ({len(of.tick_intensity_flags(_s13))} spikes)",
    )
print("  ... G7: detect_session_events on the flat 2880-candle 2-day series "
    "(ts-only dependence) — this one takes ~90s by design ...")
try:
    assert_no_lookahead(ss.detect_session_events, _FLAT2D)
    _g7 = True
except AssertionError as _e:
    _g7, _g7d = False, str(_e)[:200]
    check(False, "G7 detect_session_events on flat 2880-candle 2-day series", _g7d)
else:
    check(
        _g7 and len(ss.detect_session_events(_FLAT2D)) > 0,
        f"G7 detect_session_events on flat 2880-candle 2-day series — "
        f"non-vacuous ({len(ss.detect_session_events(_FLAT2D))} events)",
    )
try:
    assert_no_lookahead(lq.detect_sweeps, S5, params={"ranges": lq.detect_ranges(S5)})
    assert_no_lookahead(lq.detect_sweeps, S6)
    assert_no_lookahead(lq.detect_ranges, S5)
    assert_no_lookahead(ss.detect_session_events,
                        [Candle(ts=_T(2026, 1, 15, 7, 55) + i * 60, open=100.0,
                                high=101.0, low=99.0, close=100.5)
                         for i in range(11)])
    _g8 = True
except AssertionError as _e:
    _g8, _g8d = False, str(_e)[:200]
    check(False, "G8 crafted-fixture no-lookahead (sweeps both modes, ranges, session events)", _g8d)
else:
    check(
        _g8,
        "G8 crafted-fixture no-lookahead: sweeps (ranges-derived + fractal), "
        "ranges, session events on boundary-crossing series",
    )

# ── H. determinism ───────────────────────────────────────────────────────
print("\nH. DETERMINISM (assert_deterministic, synthetic_series(200, seed=15))")
_s15 = synthetic_series(200, seed=15)
try:
    assert_deterministic(lq.detect_ranges, _s15)
    check(True, f"H1 detect_ranges deterministic ({len(lq.detect_ranges(_s15))} base)")
except AssertionError as _e:
    check(False, "H1 detect_ranges deterministic", str(_e)[:200])
try:
    assert_deterministic(lq.detect_sweeps, _s15)
    check(True, f"H2 detect_sweeps (fractal) deterministic ({len(lq.detect_sweeps(_s15))} base)")
except AssertionError as _e:
    check(False, "H2 detect_sweeps (fractal) deterministic", str(_e)[:200])
try:
    assert_deterministic(lq.detect_sweeps, _s15, params={"levels": _LV})
    check(True, "H3 detect_sweeps (explicit levels) deterministic")
except AssertionError as _e:
    check(False, "H3 detect_sweeps (explicit levels) deterministic", str(_e)[:200])
try:
    assert_deterministic(lq.detect_amd, _s15)
    check(True, f"H4 detect_amd deterministic ({len(lq.detect_amd(_s15))} base)")
except AssertionError as _e:
    check(False, "H4 detect_amd deterministic", str(_e)[:200])
try:
    assert_deterministic(of.detect_flow_anomalies, _s15)
    check(True, f"H5 detect_flow_anomalies deterministic ({len(of.detect_flow_anomalies(_s15))} base)")
except AssertionError as _e:
    check(False, "H5 detect_flow_anomalies deterministic", str(_e)[:200])
try:
    assert_deterministic(of.tick_intensity_flags, _s15)
    check(True, f"H6 tick_intensity_flags deterministic ({len(of.tick_intensity_flags(_s15))} base)")
except AssertionError as _e:
    check(False, "H6 tick_intensity_flags deterministic", str(_e)[:200])
try:
    assert_deterministic(ss.detect_session_events, _s15)
    check(True, "H7 detect_session_events deterministic on the synthetic series")
except AssertionError as _e:
    check(False, "H7 detect_session_events deterministic", str(_e)[:200])
check(
    of.volume_delta_series(_s15) == of.volume_delta_series(_s15)
    and ss.tag_sessions(_FLAT2D) == ss.tag_sessions(_FLAT2D)
    and [ss.session_at(t) for t in (_T(2026, 1, 15, 8, 0), _T(2026, 3, 29, 1, 0))]
    == [ss.session_at(t) for t in (_T(2026, 1, 15, 8, 0), _T(2026, 3, 29, 1, 0))],
    "H8 non-Detection outputs (delta series, tags, session_at) byte-stable "
    "across repeated calls",
)
try:
    assert_deterministic(ss.detect_session_events, _FLAT2D, runs=2)
    check(True, "H9 detect_session_events deterministic on the 2-day crafted series")
except AssertionError as _e:
    check(False, "H9 detect_session_events deterministic (2-day)", str(_e)[:200])

# ── I. module source honesty + frozen API surface ────────────────────────
print("\nI. MODULE SOURCE HONESTY + API")
_SRC = {}
for _name in ("liquidity", "sessions", "news", "orderflow"):
    with open(os.path.join(_REPO, "core", "ict", f"{_name}.py"),
              "r", encoding="utf-8") as _fh:
        _SRC[_name] = _fh.read()
# whitespace-normalised view: phrase checks must survive docstring line wraps
_SRCN = {k: " ".join(v.split()) for k, v in _SRC.items()}
_forbidden = re.compile(r"\b(guaranteed|sure|profitable|win|wins|winning)\b",
                        re.IGNORECASE)
check(
    "RESEARCH DISCLAIMER" in _SRC["liquidity"]
    and "validity on OTC feeds is unverified" in _SRCN["liquidity"]
    and "No predictive claim is made" in _SRCN["liquidity"],
    "I1 liquidity.py carries the research disclaimer (unverified on OTC, "
    "no predictive claim)",
)
check(
    _forbidden.search(_SRC["liquidity"]) is None,
    "I2 liquidity.py free of guaranteed/sure/profitable/win(s|ning)",
)
check(
    "session boundaries are a research convention; validity on OTC feeds "
    "is unverified" in _SRCN["sessions"],
    "I3 sessions.py carries the mandated honesty note",
)
check(
    _forbidden.search(_SRC["sessions"]) is None,
    "I4 sessions.py free of forbidden vocabulary",
)
check(
    "SYNTHETIC TEST FIXTURE ONLY" in _SRCN["news"]
    and "NO NETWORK" in _SRC["news"]
    and "RISK-POLICY helper" in _SRCN["news"],
    "I5 news.py: sample labelled synthetic-fixture-only, no-network, "
    "gate documented as risk policy",
)
check(
    _forbidden.search(_SRC["news"]) is None,
    "I6 news.py free of forbidden vocabulary",
)
check(
    "close-position" in _SRC["orderflow"]
    and "NOT true bid/ask classification" in _SRCN["orderflow"]
    and "OTC feeds have no volume" in _SRCN["orderflow"],
    "I7 orderflow.py: prominent close-position-PROXY disclaimer",
)
check(
    _forbidden.search(_SRC["orderflow"]) is None,
    "I8 orderflow.py free of forbidden vocabulary",
)
_io_tokens = ("open(", ".read(", ".write(", "print(", "import os",
              "import csv", "import json", "import hashlib", "import io",
              "import datetime", "from datetime", "requests", "urllib",
              "socket", "environ", "getenv", "subprocess", "pathlib")
check(
    not any(_t in _SRC[m] for m in ("liquidity", "sessions", "orderflow")
            for _t in _io_tokens),
    "I9 no I/O tokens in the three detector modules (pure; news.py is the "
    "sanctioned loader)",
)
check(
    not any(_t in _SRC["news"] for _t in
            ("requests", "urllib", "socket", "http://", "https://")),
    "I10 news.py contains no network tokens (loader reads local files only)",
)
def _sig(fn):
    """(parameter names, {name: default}) for params that HAVE defaults."""
    params = inspect.signature(fn).parameters
    return (list(params),
            {k: v.default for k, v in params.items()
             if v.default is not inspect.Parameter.empty})
check(
    _sig(lq.detect_ranges) == (["candles", "min_bars", "band_rel"],
                               {"min_bars": 8, "band_rel": 0.0015})
    and _sig(lq.detect_sweeps) == (["candles", "levels", "ranges", "confirm", "wick_min"],
                                   {"levels": None, "ranges": None, "confirm": True,
                                    "wick_min": 0.0})
    and _sig(lq.detect_amd) == (["candles", "min_bars", "band_rel", "impulse_mult"],
                                {"min_bars": 8, "band_rel": 0.0015, "impulse_mult": 1.5}),
    "I11 liquidity API surface + defaults match the frozen L4 spec",
)
check(
    _sig(ss.session_at) == (["ts", "dst_mode"], {"dst_mode": "auto"})
    and _sig(ss.tag_sessions) == (["candles"], {})
    and _sig(ss.detect_session_events) == (["candles"], {}),
    "I12 sessions API surface + defaults match the spec",
)
check(
    _sig(nw.import_calendar) == (["path"], {})
    and _sig(nw.minutes_to_event) == (["ts", "events", "impact"], {"impact": "high"})
    and _sig(nw.gate_for_news) == (["ts", "events", "window_min", "impacts"],
                                   {"window_min": 30, "impacts": ("high",)}),
    "I13 news API surface + defaults match the spec",
)
check(
    _sig(of.volume_delta_series) == (["candles", "window"], {"window": 20})
    and _sig(of.detect_flow_anomalies) == (["candles", "window", "z_vol", "z_range"],
                                           {"window": 20, "z_vol": 1.0, "z_range": -0.5})
    and _sig(of.tick_intensity_flags) == (["candles", "window", "z"],
                                          {"window": 20, "z": 2.0}),
    "I14 orderflow API surface + defaults match the spec",
)
check(
    all(isinstance(d, Detection) for d in
        lq.detect_ranges(_s13) + lq.detect_sweeps(_s14) + lq.detect_amd(C1)
        + of.detect_flow_anomalies(_s13) + of.tick_intensity_flags(_s13)
        + ss.detect_session_events(_FLAT2D)),
    "I15 every detector returns Detection objects honouring the frozen "
    "contract (validated inside each detector)",
)

# ── summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"PHASE-L4 LIQ/SESSION/NEWS/FLOW: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for _f in _FAILURES:
        print(f"  - {_f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
