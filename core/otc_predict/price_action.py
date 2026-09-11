"""core/otc_predict/price_action.py — price action confirmation (PART 12).

USER SPEC (PART 12 — Price Action Confirmation):

    ML = UP 72% হলে system দেখবে:
      Trend → UP, Momentum → UP, Support → confirmed, Rejection → bullish,
      Volatility → acceptable   তাহলে confidence বাড়বে।
    কিন্তু ML = UP 72%, Trend = DOWN, Resistance = nearby, Momentum = weak
      হলে signal বাতিল হতে পারে।

The scorer is DIRECTION-CONDITIONAL: every component answers "does the
closed-candle evidence agree with the ML direction?" and returns a 0..1
agreement score. `agreed` requires a majority AND no hard veto
(volatility shock). All inputs are closed candles — leak-safe by design.
"""

import math

from core.otc_predict.regime import detect_regime

__all__ = ["price_action_confirm", "pa_against_count"]

_COMPONENTS = ("trend", "momentum", "level", "rejection", "structure")


def price_action_confirm(window, direction_up, features=None, regime=None):
    """Score price-action agreement with the ML direction.

    Parameters
    ----------
    window       : closed candles (oldest→newest, ends at prediction candle)
    direction_up : True when the ML prediction is UP/CALL
    features     : optional precomputed extended feature dict (saves recompute)
    regime       : optional precomputed regime dict

    Returns dict with per-component agreement in [0,1], a mean pa_score,
    `agreed` (majority + no veto) and an `against_count`.
    """
    d = 1 if direction_up else -1
    f = features or {}
    reg = regime or detect_regime(window)

    if len(window) < 24:
        return {"pa_score": 0.5, "agreed": False, "against_count": 0,
                "components": {}, "veto": "window_too_short"}

    n = len(window)
    cur = window[-1]

    # 1) TREND — EMA gap + slope direction (PART 12: Trend → UP/DOWN)
    trend = 0.5
    eg = f.get("ema_gap_atr", 0.0)
    sl = f.get("slope10_atr", 0.0)
    if eg * d > 0.15:
        trend += 0.3
    elif eg * d < -0.15:
        trend -= 0.3
    if sl * d > 0.0:
        trend += 0.2
    elif sl * d < 0.0:
        trend -= 0.2
    trend = min(1.0, max(0.0, trend))

    # 2) MOMENTUM — recent returns agree with the direction
    momentum = 0.5
    for k in ("mom_5", "mom_10"):
        v = f.get(k, 0.0)
        if v * d > 0:
            momentum += 0.25
        elif v * d < 0:
            momentum -= 0.25
    momentum = min(1.0, max(0.0, momentum))

    # 3) LEVEL — proximity to S/R (PART 12: Support confirmed / Resistance nearby)
    a = f.get("atr_norm", 0.0)
    ds = f.get("dist_support_atr", 0.0)
    dr = f.get("dist_resistance_atr", 0.0)
    level = 0.5
    if a > 0:
        if d == 1:
            # UP: room above is good; glued under resistance is bad;
            # sitting on support with a bullish candle is a bounce setup
            if dr <= 0.5:
                level = 0.1
            elif ds <= 0.5:
                level = 0.85
            elif dr >= 1.5:
                level = 0.7
        else:
            if ds <= 0.5:
                level = 0.1
            elif dr <= 0.5:
                level = 0.85
            elif ds >= 1.5:
                level = 0.7

    # 4) REJECTION — wick asymmetry + explicit rejection patterns
    rej = f.get("rejection_score", 0.0)
    rejection = 0.5 + 0.35 * (rej * d)          # rej in [-1,1]
    if d == 1 and (f.get("is_hammer") or f.get("engulf_bull")):
        rejection += 0.15
    if d == -1 and (f.get("is_star") or f.get("engulf_bear")):
        rejection += 0.15
    rejection = min(1.0, max(0.0, rejection))

    # 5) STRUCTURE — HH/HL vs LH/LL dominance + breakouts in direction
    structure = 0.5
    hh, hl = f.get("hh_count", 0.0), f.get("hl_count", 0.0)
    lh, ll = f.get("lh_count", 0.0), f.get("ll_count", 0.0)
    up_dom, down_dom = hh + hl, lh + ll
    if d == 1 and up_dom > down_dom + 2:
        structure += 0.25
    if d == -1 and down_dom > up_dom + 2:
        structure += 0.25
    if d == 1 and f.get("breakout_up"):
        structure += 0.2
    if d == -1 and f.get("breakout_down"):
        structure += 0.2
    # a FALSE break against us is evidence for us; a failed break in our
    # direction is a warning
    if d == 1 and f.get("false_break_down"):
        structure += 0.1
    if d == -1 and f.get("false_break_up"):
        structure += 0.1
    if d == 1 and f.get("false_break_up"):
        structure -= 0.2
    if d == -1 and f.get("false_break_down"):
        structure -= 0.2
    structure = min(1.0, max(0.0, structure))

    components = {"trend": round(trend, 3), "momentum": round(momentum, 3),
                  "level": round(level, 3), "rejection": round(rejection, 3),
                  "structure": round(structure, 3)}
    pa_score = sum(components.values()) / len(components)
    against_count = sum(1 for v in components.values() if v < 0.35)
    veto = None
    if reg.get("extreme_vol"):
        veto = "extreme_volatility"   # PART 23: extreme vol → NO SIGNAL

    return {"pa_score": round(pa_score, 3),
            "agreed": against_count <= 1 and veto is None and pa_score >= 0.5,
            "against_count": against_count,
            "components": components,
            "veto": veto}


def pa_against_count(pa):
    return int(pa.get("against_count", 0))
