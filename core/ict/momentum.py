"""Momentum indicators and RSI divergence labels — Phase L3 research module.

RESEARCH-ONLY: every value computed here is a descriptive statistic of
past closes. Indicators and divergences are NOT predictive signals, carry
no validated edge, and imply nothing about future prices.

Contract (core/ict/types.py, frozen at L0):
  * pure functions — no I/O, no global state, fully deterministic;
  * indicator[i] is a function of candles[:i + 1] only;
  * a Detection with confirm_index == c depends only on candles[:c + 1].
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .types import (
    Candle,
    ContractError,
    Detection,
    validate_candles,
    validate_detections,
)

__all__ = ["compute_indicators", "detect_divergences"]


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    """RSI from Wilder-smoothed averages; defined 50.0 when both are zero.

    Exactly 100.0 while avg_loss == 0 and exactly 0.0 while avg_gain == 0.
    """
    total = avg_gain + avg_loss
    if total <= 0.0:
        return 50.0
    return 100.0 * avg_gain / total


def compute_indicators(
    candles: Sequence[Candle],
    roc_n: int = 5,
    rsi_n: int = 14,
    stoch_n: int = 14,
) -> List[dict]:
    """Per-candle momentum indicators, aligned 1:1 with candle index i.

    Returns a list of dicts (one per candle), each with EXACTLY the keys:

    ``"roc"``
        close[i] / close[i - roc_n] - 1.0.  None while i < roc_n.
        (Prices are validated > 0, so the division is safe.)
    ``"rsi"``
        Wilder RSI of closes with period rsi_n.  None while i < rsi_n;
        the first value sits AT i == rsi_n.
    ``"stoch_k"``
        (close[i] - LL) / (HH - LL) where HH/LL are the high/low extremes
        of the LAST stoch_n candles INCLUSIVE of i.  Defined as 0.0 when
        HH == LL (degenerate flat window).  None while i < stoch_n - 1.
    ``"velocity"``
        close[i] - close[i - 1].  None at i == 0 only.

    RSI construction (standard Wilder):
      * seed at i == rsi_n: avg_gain = gain_sum / rsi_n and
        avg_loss = loss_sum / rsi_n over the first rsi_n close changes
        (simple average, dividing by the number of changes);
      * every later candle applies Wilder smoothing::

            avg_gain <- (avg_gain * (rsi_n - 1) + gain) / rsi_n
            avg_loss <- (avg_loss * (rsi_n - 1) + loss) / rsi_n

        with gain = max(change, 0), loss = max(-change, 0);
      * RSI = 100 * avg_gain / (avg_gain + avg_loss); when both averages
        are exactly zero (the all-flat seed) RSI is defined as 50.0.

    Purity: the computation rolls strictly forward, so element i reads
    only candles[:i + 1]; perturbing candle j can never change any
    element with index < j.

    Parameters:
        candles: closed candles; validated via validate_candles.
        roc_n: ROC lookback in candles, >= 1.
        rsi_n: RSI period (number of close changes in the seed), >= 1.
        stoch_n: stochastic %K window size INCLUSIVE of the current
            candle, >= 1.

    Returns:
        list[dict] of length len(candles); deterministic.
    """
    validate_candles(candles)
    if roc_n < 1:
        raise ContractError(f"roc_n must be >= 1: {roc_n}")
    if rsi_n < 1:
        raise ContractError(f"rsi_n must be >= 1: {rsi_n}")
    if stoch_n < 1:
        raise ContractError(f"stoch_n must be >= 1: {stoch_n}")

    n = len(candles)
    closes = [c.close for c in candles]
    out: List[dict] = []
    avg_gain: Optional[float] = None
    avg_loss: Optional[float] = None

    for i in range(n):
        # ROC — rate of change over the last roc_n closes
        roc = (closes[i] / closes[i - roc_n] - 1.0) if i >= roc_n else None

        # velocity — one-candle close delta
        velocity = (closes[i] - closes[i - 1]) if i >= 1 else None

        # stochastic %K — last stoch_n candles, inclusive of i
        if i >= stoch_n - 1:
            window = candles[i - stoch_n + 1 : i + 1]
            hh = max(w.high for w in window)
            ll = min(w.low for w in window)
            stoch_k = 0.0 if hh == ll else (candles[i].close - ll) / (hh - ll)
        else:
            stoch_k = None

        # RSI — Wilder smoothing, seeded with the simple average of the
        # first rsi_n changes
        if i < rsi_n:
            rsi = None
        else:
            if avg_gain is None or avg_loss is None:
                gain_sum = 0.0
                loss_sum = 0.0
                for j in range(1, rsi_n + 1):
                    ch = closes[j] - closes[j - 1]
                    if ch > 0.0:
                        gain_sum += ch
                    elif ch < 0.0:
                        loss_sum -= ch
                avg_gain = gain_sum / rsi_n
                avg_loss = loss_sum / rsi_n
            else:
                ch = closes[i] - closes[i - 1]
                gain = ch if ch > 0.0 else 0.0
                loss = -ch if ch < 0.0 else 0.0
                avg_gain = (avg_gain * (rsi_n - 1) + gain) / rsi_n
                avg_loss = (avg_loss * (rsi_n - 1) + loss) / rsi_n
            rsi = _rsi_from(avg_gain, avg_loss)

        out.append(
            {"roc": roc, "rsi": rsi, "stoch_k": stoch_k, "velocity": velocity}
        )
    return out


def detect_divergences(
    candles: Sequence[Candle],
    rsi_n: int = 14,
    swing_window: int = 5,
    min_gap: int = 3,
    lookback: int = 60,
) -> List[Detection]:
    """Label classic RSI divergences between confirmed swing points.

    Swing points:
        A swing PEAK at index i requires close[i] > close[i ± k] for
        k = 1..swing_window on BOTH sides (strict).  It is only knowable
        (confirmed) swing_window candles later, so a peak at i carries
        confirm_index = i + swing_window.  A swing TROUGH mirrors with
        strict <.  Only indices with the full swing_window margin on both
        sides are considered (i.e. swing_window <= i <= n - 1 -
        swing_window), so every emitted detection is fully confirmed
        inside the series.

    A BEARISH divergence is a peak pair (p1 < p2) with all of:
      * p2 - p1 >= min_gap  AND  p2 - p1 <= lookback
        ("within lookback" is read as the pair-distance bound);
      * close[p2] > close[p1]  (price makes a higher high);
      * RSI[p2] < RSI[p1]      (momentum makes a lower high);
      * RSI defined at BOTH peaks — pairs whose peaks sit inside the RSI
        warmup (RSI is None there) are skipped.

    A BULLISH divergence mirrors on troughs: close[p2] < close[p1]
    (lower low) while RSI[p2] > RSI[p1] (higher low).

    meta carries ``"candles": [p1, p2]``, ``"price_high_1"`` /
    ``"price_low_1"`` = close[p1], ``"price_high_2"`` / ``"price_low_2"``
    = close[p2], ``"rsi_1"`` and ``"rsi_2"`` (RSI values taken from
    compute_indicators with the same rsi_n).  level_low/level_high span
    the two swing closes.

    Purity: the detection for pair (p1, p2) reads only
    candles[p1 - swing_window .. p2 + swing_window] (the swing extremes)
    plus closes[:p2 + 1] (the RSI path) — a subset of
    candles[:confirm_index + 1].  No look-ahead is possible.

    Parameters:
        candles: closed candles; validated via validate_candles.
        rsi_n: RSI period for the momentum leg (>= 1).
        swing_window: strict-local-extreme margin AND confirmation lag
            in candles (>= 1).
        min_gap: minimum distance between the two swing points (>= 1).
        lookback: maximum distance between the two swing points (>= 1).

    Returns:
        Deterministic list[Detection] sorted by
        (confirm_index, start_index, kind); validated against the
        contract before returning.
    """
    validate_candles(candles)
    if rsi_n < 1:
        raise ContractError(f"rsi_n must be >= 1: {rsi_n}")
    if swing_window < 1:
        raise ContractError(f"swing_window must be >= 1: {swing_window}")
    if min_gap < 1:
        raise ContractError(f"min_gap must be >= 1: {min_gap}")
    if lookback < 1:
        raise ContractError(f"lookback must be >= 1: {lookback}")

    n = len(candles)
    closes = [c.close for c in candles]
    rsi = [row["rsi"] for row in compute_indicators(candles, rsi_n=rsi_n)]

    peaks: List[int] = []
    troughs: List[int] = []
    for i in range(swing_window, n - swing_window):
        if all(
            closes[i] > closes[i - k] and closes[i] > closes[i + k]
            for k in range(1, swing_window + 1)
        ):
            peaks.append(i)
        if all(
            closes[i] < closes[i - k] and closes[i] < closes[i + k]
            for k in range(1, swing_window + 1)
        ):
            troughs.append(i)

    out: List[Detection] = []

    # bearish: price higher high while RSI makes a lower high
    for a in range(len(peaks)):
        i1 = peaks[a]
        for b in range(a + 1, len(peaks)):
            i2 = peaks[b]
            gap = i2 - i1
            if gap < min_gap or gap > lookback:
                continue
            if closes[i2] <= closes[i1]:
                continue
            r1 = rsi[i1]
            r2 = rsi[i2]
            if r1 is None or r2 is None or r2 >= r1:
                continue
            out.append(
                Detection(
                    kind="div_bearish",
                    start_index=i1,
                    confirm_index=i2 + swing_window,
                    ts=candles[i2 + swing_window].ts,
                    side="bearish",
                    level_low=min(closes[i1], closes[i2]),
                    level_high=max(closes[i1], closes[i2]),
                    meta={
                        "candles": [i1, i2],
                        "price_high_1": closes[i1],
                        "price_high_2": closes[i2],
                        "rsi_1": r1,
                        "rsi_2": r2,
                    },
                )
            )

    # bullish: price lower low while RSI makes a higher low
    for a in range(len(troughs)):
        i1 = troughs[a]
        for b in range(a + 1, len(troughs)):
            i2 = troughs[b]
            gap = i2 - i1
            if gap < min_gap or gap > lookback:
                continue
            if closes[i2] >= closes[i1]:
                continue
            r1 = rsi[i1]
            r2 = rsi[i2]
            if r1 is None or r2 is None or r2 <= r1:
                continue
            out.append(
                Detection(
                    kind="div_bullish",
                    start_index=i1,
                    confirm_index=i2 + swing_window,
                    ts=candles[i2 + swing_window].ts,
                    side="bullish",
                    level_low=min(closes[i1], closes[i2]),
                    level_high=max(closes[i1], closes[i2]),
                    meta={
                        "candles": [i1, i2],
                        "price_low_1": closes[i1],
                        "price_low_2": closes[i2],
                        "rsi_1": r1,
                        "rsi_2": r2,
                    },
                )
            )

    out.sort(key=lambda d: (d.confirm_index, d.start_index, d.kind))
    validate_detections(out, candles)
    return out
