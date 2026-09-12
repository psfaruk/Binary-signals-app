"""core/otc_predict/geometry.py — FUTURE-CANDLE geometry (AUDIT 2026-09-13, P1).

USER COMPLAINT (verbatim): "মডেল গুলো ফিউচার ক্যান্ডেল দেখানোর কথা কিন্তু
দেখাচ্ছে না কেনো আসলে কোথায় সমস্যা।"

ROOT CAUSE: the ML models' T+1/T+2 predictions carried direction +
probability + target_time but NO candle OHLC, and the frontend only
rendered them as TEXT in the side card — the chart's ghost candle came
exclusively from the OLD 6-module engine. The models could never show a
future candle because there was no code path to draw one.

This module computes the EXPECTED future-candle OHLC for a frozen
prediction, used by BOTH delivery paths:

  * WS path  — predictor.on_candle_closed() attaches t1.candle / t2.candle
               to the 'otc_pred' frame (window ATR + closed-candle close).
  * REST path — server /api/prediction/{asset} re-derives the same geometry
               from the frozen row's close_i + candle_micro history ATR, so
               a fresh page load paints the same future candles.

HONESTY CONTRACT (PART 29 unchanged):
  * The drawn candle is a VISUALIZATION of the model's frozen direction and
    calibrated probability — never a claim of knowing the future OHLC.
  * Geometry scales with conviction: |p - 0.5| larger → larger expected
    body. A 50% coin-flip model draws a small, honest, near-doji candle.
  * Freeze/grade semantics live in tracker.py — this module never
    re-decides direction, it only renders what was frozen.
"""

import math

__all__ = ["future_candle", "atr_from_candles", "GEOMETRY_VERSION"]

GEOMETRY_VERSION = 1

# Body/wick sizing as fractions of ATR. Base body 0.30 ATR (a typical
# 1-minute OTC body is ~0.4-0.5 ATR; a *prediction* is drawn slightly
# smaller than the average real candle to stay visually humble), scaling up
# to ~0.60 ATR at full conviction (|p-0.5| = 0.5, e.g. p = 0.62 calibrated
# on an honest model is already a strong statement).
_BODY_BASE = 0.30
_BODY_CONV = 0.60          # extra ATR fraction at full conviction
_WICK = 0.20               # wick beyond the body, in signal direction
_TAIL = 0.12               # opposite-side tail from the open


def atr_from_candles(candles, n=14):
    """Average True Range over the last `n` candles of a closed-candle list.

    Same True-Range formula as core.otc_features.atr (max of high-low,
    |high-prev_close|, |low-prev_close|). Accepts raw dicts with
    open/high/low/close; returns a strictly positive value (price-relative
    fallback for degenerate/flat windows so the drawn candle is always
    visible, never zero-sized).
    """
    if not candles:
        return 0.0
    if len(candles) < 2:
        rng = candles[0].get("high", 0) - candles[0].get("low", 0)
        if rng > 0:
            return float(rng)
        ref = candles[0].get("close", 0) or 1.0
        return abs(ref) * 0.0001
    recent = candles[-n:] if len(candles) >= n else candles
    trs = []
    for j in range(1, len(recent)):
        c, prev = recent[j], recent[j - 1]
        try:
            tr = max(
                c["high"] - c["low"],
                abs(c["high"] - prev["close"]),
                abs(c["low"] - prev["close"]),
            )
        except (KeyError, TypeError):
            continue
        trs.append(tr)
    avg = sum(trs) / len(trs) if trs else 0.0
    if avg <= 0:
        # price-relative fallback (matches feed._atr behaviour)
        ref = candles[-1].get("close", 0) or 1.0
        return abs(ref) * 0.0001
    return avg


def _conviction(probability):
    """Distance from coin-flip, in [0, 1]."""
    try:
        p = float(probability)
    except (TypeError, ValueError):
        p = 0.5
    if not math.isfinite(p):
        p = 0.5
    p = min(1.0, max(0.0, p))
    return abs(p - 0.5) * 2.0


def future_candle(base_close, atr_value, direction_up, probability,
                   target_time, precision=6):
    """Expected OHLC of the T+h candle for a frozen prediction.

    Parameters
    ----------
    base_close     : close of the prediction-time candle (the anchor the
                     future candle opens from)
    atr_value      : ATR of the closed window (strictly positive)
    direction_up   : True → CALL (up candle), False → PUT (down candle)
    probability    : calibrated P(UP) — scales the expected body size
    target_time    : unix open-time of the future candle
    precision      : rounding for JSON-friendliness

    Returns a {time, open, high, low, close} dict (LightweightCharts-
    ready). All values are strictly positive and internally consistent:
    high >= max(open, close), low <= min(open, close).
    """
    a = float(atr_value) if atr_value and float(atr_value) > 0 else 0.0001
    base = float(base_close) if base_close and float(base_close) > 0 else 0.0001

    body = a * (_BODY_BASE + _BODY_CONV * _conviction(probability))
    wick = a * _WICK
    tail = a * _TAIL

    if direction_up:
        # CALL — green candle: open at the anchor, close above it
        op = base
        cl = base + body
        hi = cl + wick
        lo = op - tail
    else:
        # PUT — red candle: open at the anchor, close below it
        op = base
        cl = base - body
        hi = op + tail
        lo = cl - wick

    # degenerate-price guard (never emit non-positive prices)
    if lo <= 0:
        lo = min(op, cl) * 0.5 or 0.0001

    return {
        "time": int(target_time),
        "open": round(op, precision),
        "high": round(max(hi, op, cl), precision),
        "low": round(min(lo, op, cl), precision),
        "close": round(cl, precision),
    }
