"""Module: MARKET STATE — Deep-analysis main predictor (5-state classification)."""
from engines.base.types import ModuleResult, MarketContext

__all__ = ["analyze"]


def _market_regime_simple(candles):
    """Detect regime + zone from 20-candle window."""
    lookback = 20
    recent = candles[-lookback:]
    if len(recent) < 6:
        return "SIDEWAYS", "NEUTRAL"

    mid = len(recent) // 2
    first, second = recent[:mid], recent[mid:]
    f_hi = max(x["high"] for x in first)
    f_lo = min(x["low"] for x in first)
    s_hi = max(x["high"] for x in second)
    s_lo = min(x["low"] for x in second)

    if s_hi > f_hi and s_lo > f_lo:
        regime = "UPTREND"
    elif s_hi < f_hi and s_lo < f_lo:
        regime = "DOWNTREND"
    else:
        regime = "SIDEWAYS"

    full_hi = max(x["high"] for x in recent)
    full_lo = min(x["low"] for x in recent)
    rng = full_hi - full_lo
    if rng == 0:
        return regime, "NEUTRAL"
    pos = (candles[-1]["close"] - full_lo) / rng
    if pos <= 0.25:
        zone = "SUPPORT"
    elif pos >= 0.75:
        zone = "RESISTANCE"
    else:
        zone = "NEUTRAL"
    return regime, zone


def _key_touches(candles, price, lookback=40):
    """Count how many times `price` has been tested as a pivot in recent candles."""
    recent = candles[-lookback:]
    if len(recent) < 5:
        return 0
    pivots = []
    for i in range(1, len(recent) - 1):
        hi, lo = recent[i]["high"], recent[i]["low"]
        if hi >= recent[i - 1]["high"] and hi >= recent[i + 1]["high"]:
            pivots.append(hi)
        if lo <= recent[i - 1]["low"] and lo <= recent[i + 1]["low"]:
            pivots.append(lo)
    best = 0
    for p in pivots:
        if abs(p - price) <= price * 0.0006:
            best += 1
    return best


def analyze(candles, ctx: MarketContext) -> list:
    """Run MARKET STATE deep analysis. Returns 0 or 1 ModuleResult."""
    if len(candles) < 4:
        return []

    cur = candles[-1]
    prev = candles[-2]
    o, h, l, c = cur["open"], cur["high"], cur["low"], cur["close"]
    total_range = h - l
    if total_range == 0:
        return []

    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    is_bull = c >= o

    _regime, _zone = _market_regime_simple(candles)
    _trend_dir = (+1 if _regime == "UPTREND"
                  else -1 if _regime == "DOWNTREND" else 0)
    _cand_dir = +1 if is_bull else -1
    _close_pos = (c - l) / total_range

    _avg_body10 = (sum(abs(x["close"] - x["open"]) for x in candles[-10:])
                   / min(10, len(candles))) or 1e-9

    _streak = 0
    for _i in range(len(candles) - 1, 0, -1):
        _d = (candles[_i]["close"] >= candles[_i]["open"]) == is_bull
        if _d:
            _streak += 1
        else:
            break

    _st_pts = {"CONTINUATION": 0.0, "EXHAUSTION": 0.0,
               "REVERSAL": 0.0, "TRAP": 0.0, "RANGE": 0.0}
    _st_dir = {k: 0.0 for k in _st_pts}
    _st_ev = {k: [] for k in _st_pts}

    def _st(state, pts, direction, why):
        _st_pts[state] += pts
        _st_dir[state] += direction * pts
        _st_ev[state].append(why)

    # CONTINUATION
    if _trend_dir:
        _st("CONTINUATION", 2, _trend_dir,
            f"20-candle {_regime.lower()} structure")
        if _cand_dir == _trend_dir and body / total_range >= 0.55:
            _st("CONTINUATION", 2, _trend_dir,
                f"Impulse candle with the trend (body {body/total_range:.0%})")
        elif _cand_dir != _trend_dir and body <= _avg_body10 * 0.6 and (
                (lower_wick >= body and lower_wick > upper_wick)
                if _trend_dir > 0 else
                (upper_wick >= body and upper_wick > lower_wick)):
            _st("CONTINUATION", 2, _trend_dir,
                "Healthy pullback wicked back in trend direction")

    # EXHAUSTION
    if _streak >= 4:
        _st("EXHAUSTION", 2 + (1 if _streak >= 6 else 0), -_cand_dir,
            f"{_streak} same-color candles in a row")
    if len(candles) >= 3:
        _b3 = candles[-3:]
        _dir3 = [1 if x["close"] >= x["open"] else -1 for x in _b3]
        _bod3 = [abs(x["close"] - x["open"]) for x in _b3]
        if _dir3[0] == _dir3[1] == _dir3[2] and _bod3[0] > _bod3[1] > _bod3[2] > 0:
            _st("EXHAUSTION", 2, -_dir3[2],
                "Three pushes, each body smaller - momentum fading")
    if body / total_range >= 0.75:
        _touches = _key_touches(candles, h if is_bull else l)
        if _touches >= 2:
            _st("EXHAUSTION", 2, -_cand_dir,
                f"Full-power candle hit tested level (x{_touches})")
    if _trend_dir > 0 and _zone == "RESISTANCE" and upper_wick > total_range * 0.45:
        _st("EXHAUSTION", 2, -1,
            "Long upper rejection wick at top of up-move")
    elif _trend_dir < 0 and _zone == "SUPPORT" and lower_wick > total_range * 0.45:
        _st("EXHAUSTION", 2, +1,
            "Long lower rejection wick at bottom of down-move")

    # REVERSAL
    _rev_conf = 0
    if upper_wick / total_range > 0.55 and body / total_range < 0.25:
        _anch = _zone == "RESISTANCE"
        _st("REVERSAL", 3 if _anch else 2, -1,
            "Shooting star: push above rejected"
            + (" - at resistance zone" if _anch else ""))
        _rev_conf += 1
    elif lower_wick / total_range > 0.55 and body / total_range < 0.25:
        _anch = _zone == "SUPPORT"
        _st("REVERSAL", 3 if _anch else 2, +1,
            "Hammer: push below rejected"
            + (" - at support zone" if _anch else ""))
        _rev_conf += 1
    prev_body = abs(prev["close"] - prev["open"])
    prev_bull = prev["close"] >= prev["open"]
    if (prev_body > 0 and is_bull != prev_bull and body / prev_body >= 1.0
            and _trend_dir and _cand_dir != _trend_dir):
        _st("REVERSAL", 2, _cand_dir,
            "Counter-trend engulfing candle")
        _rev_conf += 1
    if _rev_conf and _st_pts["EXHAUSTION"] < 2 and _zone == "NEUTRAL":
        _st_pts["REVERSAL"] *= 0.5
        _st_dir["REVERSAL"] *= 0.5
        _st_ev["REVERSAL"].append("(unanchored: weight halved)")

    # TRAP
    if body / total_range >= 0.68 and (
            (is_bull and upper_wick < total_range * 0.10)
            or (not is_bull and lower_wick < total_range * 0.10)):
        _st("TRAP", 2, -_cand_dir,
            "Big one-sided candle invites chasers when fuel is spent")
    for _fb_lvl in [p for p, _ in [(p, _key_touches(candles, p))
                                   for p in [candles[-2]["high"], candles[-2]["low"]]]
                    if _ > 0]:
        if prev["close"] > _fb_lvl >= c:
            _st("TRAP", 2, -1, f"Failed breakout above {_fb_lvl:.5g}")
            break
        if prev["close"] < _fb_lvl <= c:
            _st("TRAP", 2, +1, f"Failed breakdown below {_fb_lvl:.5g}")
            break

    # RANGE
    if _trend_dir == 0:
        _st("RANGE", 2, 0, "No directional structure (sideways)")
        if _zone == "RESISTANCE":
            _st("RANGE", 1, -1, "Price at top of range - fade zone")
        elif _zone == "SUPPORT":
            _st("RANGE", 1, +1, "Price at bottom of range - fade zone")
    if _streak <= 1 and len(candles) >= 4:
        _zz_len = 1
        for _i in range(len(candles) - 2, max(len(candles) - 8, 0), -1):
            _d = (candles[_i]["close"] >= candles[_i]["open"])
            if _d != is_bull:
                _zz_len += 1
            else:
                break
        if _zz_len >= 4:
            _st("RANGE", 2, -_cand_dir,
                f"{_zz_len} alternating color candles - oscillation")
    if body / total_range <= 0.08 or (
            body / total_range <= 0.30 and upper_wick / total_range >= 0.28
            and lower_wick / total_range >= 0.28):
        _st("RANGE", 1, 0, "Indecision candle (doji / spinning top)")

    # Pick winner by points (TRAP > REVERSAL > EXHAUSTION > CONTINUATION > RANGE)
    _st_prio = ["TRAP", "REVERSAL", "EXHAUSTION", "CONTINUATION", "RANGE"]
    _st_win = max(_st_prio, key=lambda k: (_st_pts[k], -_st_prio.index(k)))
    _st_tot = sum(_st_pts.values())

    if _st_pts[_st_win] < 3:
        return []  # UNCLEAR

    _st_bd = _st_dir[_st_win]
    if _st_bd == 0:
        return []  # No directional bias

    _ms_bias = "CALL" if _st_bd > 0 else "PUT"
    _ms_conv = round(100 * _st_pts[_st_win] / _st_tot) if _st_tot else 0

    if _ms_conv < 25:
        return []  # Too low conviction

    _mag = min(4, max(1, _ms_conv // 25))
    direction = _ms_bias
    confidence = min(80, 30 + _ms_conv // 2)

    sig_type = "CONTINUATION" if _st_win == "CONTINUATION" else "REVERSAL"

    reasons_str = f"MARKET_STATE {_st_win} (bias {_ms_bias}, conv {_ms_conv}%) -> {_ms_bias} (x{_mag})"
    if _st_ev[_st_win]:
        reasons_str += " | " + "; ".join(_st_ev[_st_win][:2])

    return [ModuleResult(
        module_name="market_state",
        direction=direction,
        score=_mag,
        confidence=confidence,
        signal_type=sig_type,
        reliability="MICRO",
        group="MARKET_STATE",
        reasons=[reasons_str],
    )]
