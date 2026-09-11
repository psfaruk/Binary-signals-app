"""core/otc_predict/regime.py — market regime detection (PART 23).

USER SPEC (PART 23 — Market Regime Detection):

    System আগে বুঝবে market কেমন: TRENDING / RANGING / HIGH VOLATILITY /
    LOW VOLATILITY। তারপর model ব্যবহার করবে।
      Strong trend        → continuation strategy
      Range               → rejection strategy
      Extreme volatility  → NO SIGNAL

Pure rule-based classifier computed from the CLOSED-candle window only
(PART 19 leak-safety applies here too). The regime feeds the signal
filter's quality gates and the price-action scorer's bias.
"""

import math

__all__ = ["detect_regime", "REGIME_NAMES"]

REGIME_NAMES = ("TRENDING_UP", "TRENDING_DOWN", "RANGING",
                "HIGH_VOL", "LOW_VOL")


def _ema(values, n):
    if len(values) < n:
        return None
    k = 2.0 / (n + 1.0)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1.0 - k)
    return e


def detect_regime(window):
    """Classify the current market regime from closed candles.

    Returns dict:
        regime       — one of REGIME_NAMES
        trend_score  — |ema10-ema20|/ATR  (signed by direction)
        vol_state    — "normal" | "high" | "low"
        extreme_vol  — bool (PART 23: extreme volatility → NO SIGNAL)
    Rules (deliberately simple, testable, and tunable):
      * HIGH_VOL  : last-10 mean range > 2.5 × last-30 mean range
                    (a volatility explosion — feed shock / algorithm switch)
      * TRENDING  : |ema_gap|/ATR >= 0.60 AND directional structure
                    dominance (hh+hl vs lh+ll) agrees with the gap
      * LOW_VOL   : last-10 mean range < 0.55 × last-30 mean range
      * RANGING   : everything else
    """
    n = len(window)
    if n < 24:
        return {"regime": "RANGING", "trend_score": 0.0,
                "vol_state": "normal", "extreme_vol": False,
                "reason": "window too short"}

    closes = [x["close"] for x in window]
    a = _atr14(window)
    e10, e20 = _ema(closes, 10), _ema(closes, 20)
    gap = (e10 - e20) if (e10 is not None and e20 is not None) else 0.0
    trend_score = (gap / a) if a > 1e-12 else 0.0

    r10 = [(x["high"] - x["low"]) for x in window[-10:]]
    r30 = [(x["high"] - x["low"]) for x in window[-30:]]
    m10 = sum(r10) / len(r10)
    m30 = sum(r30) / len(r30) or 1e-12
    ratio = m10 / m30

    # structure dominance over the last 10 candles
    w = window[-11:]
    up_dom = down_dom = 0
    for j in range(1, len(w)):
        if w[j]["high"] > w[j - 1]["high"]:
            up_dom += 1
        else:
            down_dom += 1
        if w[j]["low"] > w[j - 1]["low"]:
            up_dom += 1
        else:
            down_dom += 1

    extreme_vol = ratio > 2.5
    if extreme_vol:
        regime, vol_state = "HIGH_VOL", "high"
    elif ratio < 0.55:
        regime, vol_state = "LOW_VOL", "low"
    elif abs(trend_score) >= 0.60:
        # trend needs structure agreement (PART 23: "Strong trend")
        if trend_score > 0 and up_dom >= down_dom:
            regime = "TRENDING_UP"
        elif trend_score < 0 and down_dom >= up_dom:
            regime = "TRENDING_DOWN"
        else:
            regime = "RANGING"
        vol_state = "normal"
    else:
        regime, vol_state = "RANGING", "normal"

    return {"regime": regime, "trend_score": round(trend_score, 4),
            "vol_state": vol_state, "extreme_vol": extreme_vol,
            "range_ratio": round(ratio, 4)}


def _atr14(window):
    trs = []
    for j in range(max(1, len(window) - 14), len(window)):
        h, l = window[j]["high"], window[j]["low"]
        pc = window[j - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0
