"""Candlestick pattern labels — Phase L3 research module (Agent C).

Deterministic, no-lookahead labelling of classic candlestick formations on
CLOSED candles. This module is RESEARCH-ONLY: every Detection below is a
descriptive label of past price action. These labels are NOT predictive
signals, carry no validated edge, and imply nothing about the outcome of
any future candle. All numeric thresholds are explicit parameters taken
from common textbook definitions; they are not tuned and are not evidence
of any anomaly.

Contract (core/ict/types.py, frozen at L0):
  * pure function — no I/O, no global state, fully deterministic;
  * a Detection with confirm_index == c depends only on candles[:c + 1];
  * ts == candles[confirm_index].ts; meta is JSON-serializable.
"""
from __future__ import annotations

from typing import List, Sequence

from .types import (
    Candle,
    ContractError,
    Detection,
    validate_candles,
    validate_detections,
)

__all__ = ["detect_patterns", "PATTERN_KINDS"]

#: Every kind emitted by detect_patterns (11 distinct labels).
PATTERN_KINDS = (
    "bullish_engulfing",
    "bearish_engulfing",
    "hammer",
    "shooting_star",
    "doji",
    "morning_star",
    "evening_star",
    "inside_bar",
    "outside_bar",
    "tweezer_top",
    "tweezer_bottom",
)

_TWO_THIRDS = 2.0 / 3.0


def _detection(
    kind: str,
    idxs: Sequence[int],
    candles: Sequence[Candle],
    side: str,
    extra: dict,
) -> Detection:
    """Build one Detection spanning idxs (start=first, confirm=last).

    meta is always {"candles": [idxs...]} merged with the kind-specific
    numbers in `extra`. level_low/level_high span the lows/highs of the
    involved candles — the pattern's price extent, a descriptive bound
    (not a zone claim).
    """
    lo = min(candles[j].low for j in idxs)
    hi = max(candles[j].high for j in idxs)
    meta: dict = {"candles": list(idxs)}
    meta.update(extra)
    return Detection(
        kind=kind,
        start_index=idxs[0],
        confirm_index=idxs[-1],
        ts=candles[idxs[-1]].ts,
        side=side,
        level_low=lo,
        level_high=hi,
        meta=meta,
    )


def detect_patterns(
    candles: Sequence[Candle],
    body_dominance: float = 0.6,
    doji_ratio: float = 0.1,
    wick_mult: float = 2.0,
    star_shrink: float = 0.3,
    tweezer_tol: float = 0.001,
) -> List[Detection]:
    """Scan closed candles for 11 classic candlestick formations.

    Every kind is evaluated independently at every eligible index, so one
    candle may appear in several detections (e.g. an engulfing pair that
    is also an outside bar). All rules use::

        body       = |close - open|
        range      = high - low
        lower_wick = min(open, close) - low
        upper_wick = high - max(open, close)

    Kinds and their exact rules (span = start_index..confirm_index):

    ``bullish_engulfing`` / ``bearish_engulfing`` (candles i-1, i)
        Prior candle strictly opposite colour with body > 0 (a zero-body
        prior is skipped); the current body STRICTLY engulfs the prior
        body — bullish: open[i] < close[i-1] AND close[i] > open[i-1]
        (bearish mirrored: open[i] > close[i-1] AND close[i] < open[i-1]);
        body[i] > body[i-1] (mathematically implied by strict engulfing,
        asserted explicitly anyway); body[i]/range[i] >= body_dominance.
        The current candle's colour follows from the engulfing inequalities.

    ``hammer`` (single candle i, side "bullish")
        range > 0, body > 0, lower_wick >= wick_mult * body,
        upper_wick <= body, and the body's upper extreme in the top third
        of the range: (close - low)/range >= 2/3 for a bullish close,
        (open - low)/range >= 2/3 for a bearish close.

    ``shooting_star`` (single candle i, side "bearish") — mirror:
        upper_wick >= wick_mult * body, lower_wick <= body, body's lower
        extreme in the bottom third: (high - open)/range >= 2/3 for a
        bullish close, (high - close)/range >= 2/3 for a bearish close.

    ``doji`` (single candle i, side "neutral")
        range > 0 AND body <= doji_ratio * range.

    ``morning_star`` (candles i-2, i-1, i, side "bullish")
        Candle i-2 bearish with body >= 0.5 * range[i-2]; candle i-1 body
        <= star_shrink * body[i-2] AND max(open,close)[i-1] <
        min(open,close)[i-2] (star body gaps below the prior body);
        candle i bullish AND close[i] > (open[i-2] + close[i-2]) / 2.
        meta carries ``mid_threshold`` (the midpoint used), ``first_body``
        and ``star_body``. ``evening_star`` is the exact mirror (prior
        bullish, star body gaps above, close below the midpoint).

    ``inside_bar`` (candles i-1, i, side "neutral")
        high[i] < high[i-1] AND low[i] > low[i-1] (strict both sides).

    ``outside_bar`` (candles i-1, i)
        high[i] > high[i-1] AND low[i] < low[i-1]; side is candle i's
        close-vs-open direction ("neutral" when body == 0).

    ``tweezer_top`` (candles i-1, i, side "bearish")
        |high[i] - high[i-1]| <= tweezer_tol * high[i-1], candle i-1
        bullish, candle i bearish. ``tweezer_bottom`` mirrored on lows
        (prior bearish, current bullish, tolerance relative to low[i-1]).

    meta always contains ``"candles": [involved indices]`` plus the
    kind-specific numbers named above.

    Parameters:
        candles: closed candles; validated via validate_candles (raises
            ContractError on invariant violations).
        body_dominance: minimum body[i]/range[i] for engulfing candles,
            in (0, 1] (default 0.6).
        doji_ratio: maximum body/range ratio for a doji, in (0, 1]
            (default 0.1).
        wick_mult: minimum wick-to-body multiple for hammer /
            shooting star, > 0 (default 2.0).
        star_shrink: maximum star body as a fraction of the first
            candle's body for morning/evening star, in (0, 1]
            (default 0.3).
        tweezer_tol: equal-extreme tolerance as a fraction of the
            reference high/low, >= 0 (default 0.001).

    Returns:
        Deterministic list[Detection], chronologically ordered by the
        scan index; validated against the contract before returning.

    Purity:
        A detection confirmed at candle i reads only candles[i-2 .. i]
        (at most), i.e. a subset of candles[:confirm_index + 1]. No
        look-ahead is possible by construction.
    """
    validate_candles(candles)
    if not 0.0 < body_dominance <= 1.0:
        raise ContractError(f"body_dominance must be in (0, 1]: {body_dominance}")
    if not 0.0 < doji_ratio <= 1.0:
        raise ContractError(f"doji_ratio must be in (0, 1]: {doji_ratio}")
    if wick_mult <= 0.0:
        raise ContractError(f"wick_mult must be > 0: {wick_mult}")
    if not 0.0 < star_shrink <= 1.0:
        raise ContractError(f"star_shrink must be in (0, 1]: {star_shrink}")
    if tweezer_tol < 0.0:
        raise ContractError(f"tweezer_tol must be >= 0: {tweezer_tol}")

    out: List[Detection] = []
    n = len(candles)
    for i in range(n):
        c = candles[i]
        rng = c.high - c.low
        body = c.body
        lower_wick = min(c.open, c.close) - c.low
        upper_wick = c.high - max(c.open, c.close)

        # ── single-candle kinds ────────────────────────────────────────
        if rng > 0.0 and body <= doji_ratio * rng:
            out.append(
                _detection(
                    "doji",
                    [i],
                    candles,
                    "neutral",
                    {"body": body, "range": rng, "body_to_range": body / rng},
                )
            )

        if (
            rng > 0.0
            and body > 0.0
            and lower_wick >= wick_mult * body
            and upper_wick <= body
        ):
            # body's upper extreme must sit in the top third of the range
            if c.close > c.open:
                top = (c.close - c.low) / rng
            else:  # body > 0 guarantees a bearish close here
                top = (c.open - c.low) / rng
            if top >= _TWO_THIRDS:
                out.append(
                    _detection(
                        "hammer",
                        [i],
                        candles,
                        "bullish",
                        {
                            "body": body,
                            "lower_wick": lower_wick,
                            "upper_wick": upper_wick,
                            "wick_to_body": lower_wick / body,
                        },
                    )
                )

        if (
            rng > 0.0
            and body > 0.0
            and upper_wick >= wick_mult * body
            and lower_wick <= body
        ):
            # body's lower extreme must sit in the bottom third of the range
            if c.close > c.open:
                bottom = (c.high - c.open) / rng
            else:  # body > 0 guarantees a bearish close here
                bottom = (c.high - c.close) / rng
            if bottom >= _TWO_THIRDS:
                out.append(
                    _detection(
                        "shooting_star",
                        [i],
                        candles,
                        "bearish",
                        {
                            "body": body,
                            "lower_wick": lower_wick,
                            "upper_wick": upper_wick,
                            "wick_to_body": upper_wick / body,
                        },
                    )
                )

        # ── two-candle kinds ───────────────────────────────────────────
        if i >= 1:
            p = candles[i - 1]
            p_rng = p.high - p.low
            p_body = p.body
            body_dom = body / rng if rng > 0.0 else 0.0

            # bullish engulfing: prior bearish, current body swallows it
            if (
                p.close < p.open
                and p_body > 0.0
                and c.open < p.close
                and c.close > p.open
                and body > p_body
                and body_dom >= body_dominance
            ):
                out.append(
                    _detection(
                        "bullish_engulfing",
                        [i - 1, i],
                        candles,
                        "bullish",
                        {
                            "body_prior": p_body,
                            "body_current": body,
                            "body_dominance": body_dom,
                        },
                    )
                )

            # bearish engulfing: prior bullish, mirrored
            if (
                p.close > p.open
                and p_body > 0.0
                and c.open > p.close
                and c.close < p.open
                and body > p_body
                and body_dom >= body_dominance
            ):
                out.append(
                    _detection(
                        "bearish_engulfing",
                        [i - 1, i],
                        candles,
                        "bearish",
                        {
                            "body_prior": p_body,
                            "body_current": body,
                            "body_dominance": body_dom,
                        },
                    )
                )

            # inside bar: strictly narrower on both sides
            if c.high < p.high and c.low > p.low:
                out.append(
                    _detection(
                        "inside_bar",
                        [i - 1, i],
                        candles,
                        "neutral",
                        {"prior_range": p_rng, "current_range": rng},
                    )
                )

            # outside bar: strictly wider on both sides
            if c.high > p.high and c.low < p.low:
                out.append(
                    _detection(
                        "outside_bar",
                        [i - 1, i],
                        candles,
                        c.direction,  # bullish / bearish / neutral
                        {"prior_range": p_rng, "current_range": rng},
                    )
                )

            # tweezer top: near-equal highs, bullish -> bearish flip
            if (
                abs(c.high - p.high) <= tweezer_tol * p.high
                and p.close > p.open
                and c.close < c.open
            ):
                out.append(
                    _detection(
                        "tweezer_top",
                        [i - 1, i],
                        candles,
                        "bearish",
                        {
                            "high_prior": p.high,
                            "high_current": c.high,
                            "high_diff": abs(c.high - p.high),
                        },
                    )
                )

            # tweezer bottom: near-equal lows, bearish -> bullish flip
            if (
                abs(c.low - p.low) <= tweezer_tol * p.low
                and p.close < p.open
                and c.close > c.open
            ):
                out.append(
                    _detection(
                        "tweezer_bottom",
                        [i - 1, i],
                        candles,
                        "bullish",
                        {
                            "low_prior": p.low,
                            "low_current": c.low,
                            "low_diff": abs(c.low - p.low),
                        },
                    )
                )

        # ── three-candle kinds ─────────────────────────────────────────
        if i >= 2:
            a = candles[i - 2]
            b = candles[i - 1]
            a_body = a.body
            b_body = b.body
            mid = (a.open + a.close) / 2.0

            # morning star: decline, small gap-down star, strong recovery
            if (
                a.close < a.open
                and a_body >= 0.5 * a.range
                and b_body <= star_shrink * a_body
                and max(b.open, b.close) < min(a.open, a.close)
                and c.close > c.open
                and c.close > mid
            ):
                out.append(
                    _detection(
                        "morning_star",
                        [i - 2, i - 1, i],
                        candles,
                        "bullish",
                        {
                            "mid_threshold": mid,
                            "first_body": a_body,
                            "star_body": b_body,
                        },
                    )
                )

            # evening star: rally, small gap-up star, strong rejection
            if (
                a.close > a.open
                and a_body >= 0.5 * a.range
                and b_body <= star_shrink * a_body
                and min(b.open, b.close) > max(a.open, a.close)
                and c.close < c.open
                and c.close < mid
            ):
                out.append(
                    _detection(
                        "evening_star",
                        [i - 2, i - 1, i],
                        candles,
                        "bearish",
                        {
                            "mid_threshold": mid,
                            "first_body": a_body,
                            "star_body": b_body,
                        },
                    )
                )

    validate_detections(out, candles)
    return out
