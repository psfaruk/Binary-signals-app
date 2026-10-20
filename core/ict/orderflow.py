"""Order-flow proxies for Phase L4 (volume delta, absorption, activity).

HONEST PROXY NATURE — read first: volume delta here is a close-position
proxy, NOT true bid/ask classification; OTC feeds have no volume at all.
On feeds that do carry volume, "delta" is computed from where the close
sits inside the candle's range, weighted by volume — a heuristic footprint
proxy. Nothing in this module observes actual orders, participants, or
"institutional" behaviour (unobservable from OHLCV). No predictive claim
is made: these are research features only.

No-lookahead: every detection depends only on candles[:confirm_index + 1].
Z-scores compare candle i against the PREVIOUS ``window`` candles
(indices i-window .. i-1), never against i itself or the future.
"""
from __future__ import annotations

import math
from typing import Sequence

from .types import (
    Candle,
    ContractError,
    Detection,
    validate_candles,
    validate_detections,
)

__all__ = ["volume_delta_series", "detect_flow_anomalies", "tick_intensity_flags"]

# Effort anomaly range z-score threshold (fixed by spec; documented constant).
_EFFORT_RANGE_Z = 1.5


def _zscore(x: float, window_values: Sequence[float]) -> float | None:
    """Population z-score of x vs window_values; None when the statistic is
    undefined (empty window, or zero standard deviation — a degenerate
    window honestly supports no orderflow claim)."""
    n = len(window_values)
    if n == 0:
        return None
    mean = sum(window_values) / n
    var = sum((v - mean) ** 2 for v in window_values) / n
    std = math.sqrt(var)
    if std == 0.0:
        return None
    return (x - mean) / std


def volume_delta_series(
    candles: Sequence[Candle],
    window: int = 20,  # accepted for API symmetry; NOT used for the delta values
) -> list[float | None]:
    """Per-candle signed close-position-weighted volume proxy.

    delta_i = ((close - open) / (high - low, or 1e-9 when the range is 0))
              * (volume, or 1.0 when the feed has no volume)

    Index 0 is None by contract (the series needs a candle before it);
    there is NO warmup-None for i < window — the delta is per-candle, the
    rolling window matters only to detect_flow_anomalies below.
    """
    validate_candles(candles)
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise ContractError(f"window must be int >= 1, got {window!r}")
    out: list[float | None] = [None]
    for c in candles[1:]:
        rng = c.high - c.low
        denom = rng if rng > 0 else 1e-9
        vol = c.volume if c.volume is not None else 1.0
        out.append(((c.close - c.open) / denom) * vol)
    return out


def detect_flow_anomalies(
    candles: Sequence[Candle],
    window: int = 20,
    z_vol: float = 1.0,
    z_range: float = -0.5,
) -> list[Detection]:
    """Absorption / effort-vs-result anomaly labels (research proxies).

    For candle i >= window, with volume z and range z computed against the
    previous ``window`` candles (excluding i):
      * absorption     : vol_z >= z_vol AND range_z <= z_range
                         (effort in, no result — proxy for passive fill)
      * effort_anomaly : vol_z >= z_vol AND range_z >= +1.5 (fixed constant)
                         (effort with outsized result)
    confirm_index = i, side "neutral", meta {"vol_z", "range_z"}.
    If BOTH thresholds can hold at once (only possible when the caller
    passes z_range >= 1.5, outside the default -0.5), absorption is
    checked first and takes precedence (deterministic rule, documented).

    HONESTY RULES: if any volume in the comparison window or candle i
    itself is None, i is SKIPPED (no volume -> no orderflow claim). If a
    window's standard deviation is zero the z-score is undefined and i is
    skipped as well (degenerate statistics support no claim).
    """
    validate_candles(candles)
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise ContractError(f"window must be int >= 1, got {window!r}")
    n = len(candles)
    out: list[Detection] = []
    for i in range(window, n):
        window_slice = candles[i - window : i]
        c = candles[i]
        if c.volume is None or any(w.volume is None for w in window_slice):
            continue
        vol_z = _zscore(c.volume, [w.volume for w in window_slice])
        range_z = _zscore(c.range, [w.range for w in window_slice])
        if vol_z is None or range_z is None:
            continue
        kind = None
        if vol_z >= z_vol and range_z <= z_range:
            kind = "absorption"
        elif vol_z >= z_vol and range_z >= _EFFORT_RANGE_Z:
            kind = "effort_anomaly"
        if kind is not None:
            out.append(
                Detection(
                    kind=kind,
                    start_index=i,
                    confirm_index=i,
                    ts=c.ts,
                    side="neutral",
                    meta={"vol_z": vol_z, "range_z": range_z},
                )
            )
    validate_detections(out, candles)
    return out


def tick_intensity_flags(
    candles: Sequence[Candle],
    window: int = 20,
    z: float = 2.0,
) -> list[Detection]:
    """Activity-spike labels for feeds WITHOUT volume.

    Candle COUNT is constant per unit time on 1-minute candles, so it is
    meaningless as an intensity measure; instead the candle RANGE is used
    as an activity proxy (assumes 1-minute candles, per repo convention —
    range == range-per-minute; no duration normalisation is attempted).
    Candle i >= window with range z-score (vs the previous window candles)
    >= z emits kind "activity_spike", side "neutral",
    meta {"range_z": ...}. This is an ACTIVITY proxy, not a tick count and
    not an orderflow claim.
    """
    validate_candles(candles)
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise ContractError(f"window must be int >= 1, got {window!r}")
    n = len(candles)
    out: list[Detection] = []
    for i in range(window, n):
        window_slice = candles[i - window : i]
        c = candles[i]
        range_z = _zscore(c.range, [w.range for w in window_slice])
        if range_z is None:
            continue
        if range_z >= z:
            out.append(
                Detection(
                    kind="activity_spike",
                    start_index=i,
                    confirm_index=i,
                    ts=c.ts,
                    side="neutral",
                    meta={"range_z": range_z},
                )
            )
    validate_detections(out, candles)
    return out
