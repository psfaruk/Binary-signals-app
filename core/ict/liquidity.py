"""Liquidity structures for Phase L4 — consolidation ranges, liquidity
sweeps, and AMD (accumulation -> manipulation -> distribution) sequences.

RESEARCH DISCLAIMER
-------------------
Everything in this module is a *deterministic price-pattern label* computed
from OHLC candles. A "range" is a run of closes inside a band, a "sweep" is
a wick beyond a level with the close back inside, and "AMD" is a
Wyckoff-lite sequence label. None of these are verified statements about
actual stop placement, liquidity pools, institutional intent, or future
price movement, and their validity on OTC feeds is unverified. No
predictive claim is made: these are research features only, exactly as
framed by docs/smc/PHASE_L_MASTER_PLAN.md §0.

No-lookahead contract: every Detection returned here depends only on
candles[:confirm_index + 1] (core/ict/types.py).

Exact range rule (detect_ranges)
--------------------------------
1. A candidate run starts at a seed candle ``s`` (the seed's own close is
   unconstrained — it defines the initial band).
2. Candle ``i > s`` JOINS the run iff its close lies inside the running
   band of the run-so-far (candles ``s..i-1``) widened by a tolerance:

       band = [min(low of s..i-1), max(high of s..i-1)]
       tol  = band_rel * mid(band),  mid = (band.low + band.high) / 2
       join iff band.low - tol <= close_i <= band.high + tol   (inclusive)

   The running band deliberately EXCLUDES the candle being tested: a close
   is always inside its own candle's high/low extremes, so including it
   would make the test vacuous. Once a candle joins, its own wicks may
   extend the running band (honest recomputation, band grows with the run).
3. The run TERMINATES at the first candle ``b`` whose close falls outside
   the band ("the breaker"), or at the end of the series.
4. If the run (``s..e``, ``e`` = last joined candle) has >= ``min_bars``
   candles it is emitted as kind "range", side "neutral", zone
   [min low, max high] over ``s..e`` (the breaker's wicks are NOT part of
   the zone), meta {"bars": e - s + 1}. ``confirm_index`` = ``b`` when a
   breaker exists — the completion (maximality) of the run only becomes
   knowable when the breaking close arrives — else ``confirm_index = e``
   (run terminated by end of data). Confirming at ``e`` while depending on
   candle ``b`` would violate the frozen no-lookahead contract, which the
   mandated perturbation battery enforces mechanically.
5. Scanning is greedy left-to-right (longest-maximal-run-per-start): after
   an emitted run the breaker seeds the next candidate (resume at ``b``);
   after a run shorter than ``min_bars`` the next candidate starts at
   ``s + 1``. Emitted runs never overlap.

   confirm_index DECISION (documented spec-ambiguity resolution): the
   phase spec's phrase "confirm_index = last candle of run" is read as
   "last candle of the run-as-TERMINATED", i.e. the breaker index b:
   the completion (maximality) of a longest run only becomes knowable
   when the breaking close arrives. The literal reading (confirm at e)
   is mechanically incompatible with the frozen no-lookahead contract —
   perturbing the breaker so its close re-enters the band would extend
   the run and delete a detection confirmed BEFORE the perturbed candle,
   exactly what core.ict.helpers.assert_no_lookahead forbids. When the
   run terminates at end-of-data there is no breaker and
   confirm_index = e (the last run candle).
"""
from __future__ import annotations

import statistics
from typing import Optional, Sequence

from .types import (
    Candle,
    ContractError,
    Detection,
    validate_candles,
    validate_detections,
)

__all__ = ["detect_ranges", "detect_sweeps", "detect_amd"]


# ---------------------------------------------------------------------------
# ranges
# ---------------------------------------------------------------------------
def detect_ranges(
    candles: Sequence[Candle],
    min_bars: int = 8,
    band_rel: float = 0.0015,
) -> list[Detection]:
    """Detect consolidation ranges per the module rule above.

    Parameters
    ----------
    min_bars : minimum candles in a qualifying run (>= 2; 1-candle ranges
        are rejected as meaningless).
    band_rel : band tolerance as a fraction of the running band mid.
    """
    validate_candles(candles)
    if not isinstance(min_bars, int) or isinstance(min_bars, bool):
        raise ContractError(f"min_bars must be int, got {type(min_bars).__name__}")
    if min_bars < 2:
        raise ContractError(f"min_bars must be >= 2, got {min_bars}")
    if not (band_rel > 0):
        raise ContractError(f"band_rel must be > 0, got {band_rel}")

    n = len(candles)
    out: list[Detection] = []
    s = 0
    while s < n:
        e = s
        min_lo = candles[s].low
        max_hi = candles[s].high
        breaker: Optional[int] = None
        for i in range(s + 1, n):
            mid = (min_lo + max_hi) / 2.0
            tol = band_rel * mid
            close_i = candles[i].close
            if close_i < min_lo - tol or close_i > max_hi + tol:
                breaker = i
                break
            e = i
            if candles[i].low < min_lo:
                min_lo = candles[i].low
            if candles[i].high > max_hi:
                max_hi = candles[i].high
        run_len = e - s + 1
        if run_len >= min_bars:
            confirm = breaker if breaker is not None else n - 1
            out.append(
                Detection(
                    kind="range",
                    start_index=s,
                    confirm_index=confirm,
                    ts=candles[confirm].ts,
                    side="neutral",
                    level_low=min_lo,
                    level_high=max_hi,
                    meta={"bars": run_len},
                )
            )
            s = breaker if breaker is not None else n
        else:
            s += 1
    validate_detections(out, candles)
    return out


# ---------------------------------------------------------------------------
# sweeps
# ---------------------------------------------------------------------------
_LEVEL_TAGS = ("high", "low")


def detect_sweeps(
    candles: Sequence[Candle],
    levels: Optional[list[tuple[float, str]]] = None,
    ranges: Optional[list[Detection]] = None,
    confirm: bool = True,
    wick_min: float = 0.0,
) -> list[Detection]:
    """Detect liquidity sweeps of price levels.

    SMC naming convention (research labels, not facts): liquidity resting
    ABOVE price action (swing highs / range tops — stops of shorts) is
    buy-side liquidity, so sweeping it emits kind "sweep_buy_side" with
    side "bearish" (potential reversal down). Sweeping a level BELOW price
    action emits kind "sweep_sell_side" with side "bullish".

    Top-side sweep of level L at candle i:
        high_i  - L > wick_min  (strictly)   AND   close_i < L
    Bottom-side sweep of level L at candle i:
        L - low_i   > wick_min  (strictly)   AND   close_i > L
    With ``confirm=True`` candle i+1 must ALSO close back on the inside
    (below L / above L); confirm_index is then i+1, else i.

    Level sources (precedence):
      1. ``levels`` — explicit (price, "high"|"low") tuples supplied by the
         caller (external constants, e.g. prior-day extremes). Used as-is.
      2. ``ranges`` — Detections from detect_ranges: each range contributes
         its high (tag "high") and low (tag "low"). A range's bounds are
         only usable from candles at index >= range.confirm_index (the
         range is complete then; earlier use would peek at the range's
         breaker candle — lookahead).
      3. Otherwise — 3-point fractal swing highs/lows (k=1): swing at j is
         strictly greater/less than BOTH immediate neighbours, confirmed at
         j+1. A swing may be swept at candle i only if it was CONFIRMED by
         index <= i-1 (i.e. swing index <= i-2): no lookahead.

    meta = {"level": L, "wick": penetration size, "confirmed": confirm}.
    """
    validate_candles(candles)
    if not isinstance(confirm, bool):
        raise ContractError(f"confirm must be bool, got {type(confirm).__name__}")
    if (
        isinstance(wick_min, bool)
        or not isinstance(wick_min, (int, float))
        or wick_min != wick_min  # NaN
        or wick_min == float("inf")
        or wick_min < 0
    ):
        raise ContractError(f"wick_min must be a finite number >= 0, got {wick_min!r}")

    n = len(candles)

    resolved: list[tuple[float, str, int]] = []  # (level, tag, min_sweep_index)
    if levels is not None:
        for entry in levels:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ContractError(f"level entry must be (price, 'high'|'low'), got {entry!r}")
            price, tag = entry
            if not isinstance(price, (int, float)) or isinstance(price, bool):
                raise ContractError(f"level price must be numeric, got {price!r}")
            if tag not in _LEVEL_TAGS:
                raise ContractError(f"level tag must be 'high' or 'low', got {tag!r}")
            if price <= 0:
                raise ContractError(f"level price must be > 0, got {price}")
            resolved.append((float(price), tag, 0))
    elif ranges is not None:
        for r in ranges:
            if not isinstance(r, Detection):
                raise ContractError(f"ranges entries must be Detection, got {type(r).__name__}")
            if r.level_low is None or r.level_high is None:
                raise ContractError(f"range detection lacks bounds: {r!r}")
            resolved.append((float(r.level_high), "high", r.confirm_index))
            resolved.append((float(r.level_low), "low", r.confirm_index))
    else:
        resolved = _fractal_levels(candles)

    out: list[Detection] = []
    for i in range(n):
        c = candles[i]
        for level, tag, min_i in resolved:
            if i < min_i:
                continue
            if tag == "high":
                wick = c.high - level
                if wick > wick_min and c.close < level:
                    if confirm:
                        if i + 1 >= n or candles[i + 1].close >= level:
                            continue
                        ci = i + 1
                    else:
                        ci = i
                    out.append(
                        Detection(
                            kind="sweep_buy_side",
                            start_index=i,
                            confirm_index=ci,
                            ts=candles[ci].ts,
                            side="bearish",
                            level_low=level,
                            level_high=level,
                            meta={"level": level, "wick": wick, "confirmed": confirm},
                        )
                    )
            else:
                wick = level - c.low
                if wick > wick_min and c.close > level:
                    if confirm:
                        if i + 1 >= n or candles[i + 1].close <= level:
                            continue
                        ci = i + 1
                    else:
                        ci = i
                    out.append(
                        Detection(
                            kind="sweep_sell_side",
                            start_index=i,
                            confirm_index=ci,
                            ts=candles[ci].ts,
                            side="bullish",
                            level_low=level,
                            level_high=level,
                            meta={"level": level, "wick": wick, "confirmed": confirm},
                        )
                    )
    validate_detections(out, candles)
    return out


def _fractal_levels(candles: Sequence[Candle]) -> list[tuple[float, str, int]]:
    """3-point fractal swings (k=1) as sweep levels, no-lookahead safe.

    Swing high at j: high_j strictly greater than both immediate
    neighbours; confirmed at j+1. Usable as a sweep level from candle
    j+2 onward (confirmed by index <= i-1 for sweep candle i).
    """
    n = len(candles)
    lv: list[tuple[float, str, int]] = []
    for j in range(1, n - 1):
        if candles[j].high > candles[j - 1].high and candles[j].high > candles[j + 1].high:
            lv.append((candles[j].high, "high", j + 2))
        if candles[j].low < candles[j - 1].low and candles[j].low < candles[j + 1].low:
            lv.append((candles[j].low, "low", j + 2))
    return lv


# ---------------------------------------------------------------------------
# AMD
# ---------------------------------------------------------------------------
_DIST_WINDOW = 5  # distribution candle must be within this many candles after the sweep
_MEDIAN_WINDOW = 20  # median body of the previous <=20 candles


def detect_amd(
    candles: Sequence[Candle],
    min_bars: int = 8,
    band_rel: float = 0.0015,
    impulse_mult: float = 1.5,
) -> list[Detection]:
    """Accumulation -> Manipulation -> Distribution sequence labels.

    Sequence (bullish variant; bearish is the mirror):
      (a) accumulation: a range per detect_ranges(min_bars, band_rel);
      (b) manipulation: a sweep of the range LOW — detect_sweeps
          confirm=False semantics with wick_min = 0 (low_i < range_low AND
          close_i > range_low) — at an index i >= range.confirm_index;
      (c) distribution: a candle d with i < d <= i + 5 (within 5 candles
          AFTER the sweep) whose body >= impulse_mult * median body of its
          previous <= 20 candles AND whose close is above the range mid.

    Emission rule — ONE detection per (range, side): the chronologically
    FIRST candle d (scanned left to right from range.confirm_index + 1)
    that satisfies (c) while at least one qualifying sweep exists at an
    index in [max(range.confirm_index, d - 5), d - 1] (the 5-candle
    lookback). When several sweeps fall inside that lookback window the
    LATEST one (the most recent manipulation before the expansion) is
    reported as meta["sweep_index"] — a documented deterministic choice.
    A strong expansion candle with no sweep inside its lookback pairs
    with nothing and is skipped. This lookback formulation reads only
    candles[:d + 1] (the range itself reads candles[:confirm_index + 1]),
    so the frozen no-lookahead contract holds by construction — it never
    has to inspect a LATER sweep's window before deciding an EARLIER
    emission (a forward "try each sweep in turn" loop would need to read
    past candle d to reject an earlier sweep, which would leak).

    Honesty: these are Wyckoff-lite *labels*, not verified statements about
    manipulation or institutional behaviour.
    """
    validate_candles(candles)
    if not isinstance(min_bars, int) or isinstance(min_bars, bool) or min_bars < 2:
        raise ContractError(f"min_bars must be int >= 2, got {min_bars}")
    if not (band_rel > 0):
        raise ContractError(f"band_rel must be > 0, got {band_rel}")
    if not (impulse_mult > 0):
        raise ContractError(f"impulse_mult must be > 0, got {impulse_mult}")

    n = len(candles)
    ranges = detect_ranges(candles, min_bars=min_bars, band_rel=band_rel)
    out: list[Detection] = []

    # median body of the previous <= _MEDIAN_WINDOW candles, per index —
    # depends only on d, so it is precomputed once and shared by all ranges
    bodies = [c.body for c in candles]
    med: list[float] = [0.0] * n  # med[d] meaningful for d >= 1 only
    for d in range(1, n):
        med[d] = statistics.median(bodies[max(0, d - _MEDIAN_WINDOW):d])

    for r in ranges:
        lo = float(r.level_low)
        hi = float(r.level_high)
        mid = (lo + hi) / 2.0
        b = r.confirm_index
        for side, kind, want_above in (
            ("bullish", "amd_bullish", True),
            ("bearish", "amd_bearish", False),
        ):
            for d in range(b + 1, n):
                cd = candles[d]
                if d < 1 or cd.body < impulse_mult * med[d]:
                    continue
                if not ((cd.close > mid) if want_above else (cd.close < mid)):
                    continue
                # 5-candle lookback, DESCENDING: the first qualifying sweep
                # found is the LATEST manipulation before the expansion.
                sweep_index: Optional[int] = None
                for i in range(d - 1, max(b, d - _DIST_WINDOW) - 1, -1):
                    ci = candles[i]
                    if want_above:
                        if ci.low < lo and ci.close > lo:
                            sweep_index = i
                            break
                    else:
                        if ci.high > hi and ci.close < hi:
                            sweep_index = i
                            break
                if sweep_index is None:
                    continue  # expansion without a manipulation nearby
                out.append(
                    Detection(
                        kind=kind,
                        start_index=r.start_index,
                        confirm_index=d,
                        ts=cd.ts,
                        side=side,
                        level_low=r.level_low,
                        level_high=r.level_high,
                        meta={
                            "range_low": r.level_low,
                            "range_high": r.level_high,
                            "sweep_index": sweep_index,
                            "dist_index": d,
                        },
                    )
                )
                break  # one per (range, side)
    validate_detections(out, candles)
    return out
