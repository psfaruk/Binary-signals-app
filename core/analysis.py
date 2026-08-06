"""core/analysis.py — Pure-function technical analysis library."""
import math

def _atr(candles, n=20):
    """True Range ATR — properly accounts for overnight gaps."""
    if not candles or len(candles) < 2:
        return 0.0001
    recent = candles[-n:] if len(candles) >= n else candles
    trs = []
    for i in range(1, len(recent)):
        c, prev = recent[i], recent[i - 1]
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - prev["close"]),
            abs(c["low"] - prev["close"]),
        )
        trs.append(tr)
    return (sum(trs) / len(trs)) if trs else 0.0001
def _ema(values, period):
    """Exponential Moving Average, seeded with SMA of first `period` values."""
    if not values:
        return 0
    k = 2 / (period + 1)
    seed_n = min(period, len(values))
    ema = sum(values[:seed_n]) / seed_n
    for v in values[seed_n:]:
        ema = v * k + ema * (1 - k)
    return ema
def _body(c):
    """Signed body of a candle (close - open)."""
    return c["close"] - c["open"]
def _abs_body(c):
    return abs(_body(c))
def _range(c):
    return c["high"] - c["low"]
def detect_candle_patterns(candles):
    """Detect multi-candle reversal/continuation patterns."""
    patterns = []
    if len(candles) < 3:
        return patterns
    c1 = candles[-3]
    c2 = candles[-2]
    c3 = candles[-1]
    atr = _atr(candles)
    b1 = _body(c1)
    b2 = _body(c2)
    b3 = _body(c3)
    r1, r2, r3 = _range(c1), _range(c2), _range(c3)
    tweezer_tol = atr * 0.08  # within 8% of ATR
    if abs(c2["low"] - c3["low"]) < tweezer_tol and b2 < 0 and b3 > 0:
        patterns.append({
            "name": "TWEEZER_BOTTOM",
            "direction": "CALL",
            "score": 2,
            "reason": f"Tweezer Bottom (same low {c3['low']:.5f}) → CALL (60% win rate)"
        })
    c2_mid = (c2["open"] + c2["close"]) / 2
    if b2 < 0 and b3 > 0:
        if c3["open"] < c2["close"] and c3["close"] > c2_mid and c3["close"] < c2["open"]:
            patterns.append({
                "name": "PIERCING_LINE",
                "direction": "CALL",
                "score": 3,
                "reason": "Piercing Line (bullish close above bearish midpoint) → CALL (63% win rate)"
            })
    if b2 > 0 and b3 < 0:
        if c3["open"] > c2["close"] and c3["close"] < c2_mid and c3["close"] > c2["open"]:
            patterns.append({
                "name": "DARK_CLOUD",
                "direction": "PUT",
                "score": 3,
                "reason": "Dark Cloud Cover (bearish close below bullish midpoint) → PUT (63% win rate)"
            })
    if b2 > 0 and b3 < 0 and _abs_body(c3) < _abs_body(c2) * 0.5:
        if c3["open"] <= c2["close"] and c3["close"] >= c2["open"]:
            patterns.append({
                "name": "BEAR_HARAMI",
                "direction": "CALL",
                "score": 2,
                "reason": "Bearish Harami (small bearish inside big bullish) → CALL (continuation, 51.8% measured n=501)"
            })
    if r3 > 0 and atr > 0:
        uw3 = c3["high"] - max(c3["open"], c3["close"])
        lw3 = min(c3["open"], c3["close"]) - c3["low"]
        uw_pct3 = uw3 / r3 * 100
        lw_pct3 = lw3 / r3 * 100
        body_pct3 = _abs_body(c3) / r3 * 100
        if uw_pct3 >= 66 and body_pct3 <= 33 and b3 <= 0:
            patterns.append({
                "name": "BEAR_PIN_BAR",
                "direction": "CALL",
                "score": 3,
                "reason": f"Bearish Pin Bar (upper wick {uw_pct3:.0f}%, body {body_pct3:.0f}%) → CALL (continuation, 51.9% measured n=294)"
            })
    if atr > 0 and _abs_body(c2) > atr * 0.3 and _abs_body(c3) > atr * 0.3:
        if b2 < 0 and b3 > 0 and abs(c3["close"] - c2["open"]) < atr * 0.15:
            if _abs_body(c3) > _abs_body(c2) * 0.5:
                patterns.append({
                    "name": "BULL_TWO_BAR_REV",
                    "direction": "CALL",
                    "score": 3,
                    "reason": f"Bullish Two-Bar Reversal (c2 down, c3 up, close near c2 open) → CALL (62% win rate)"
                })
        if b2 > 0 and b3 < 0 and abs(c3["close"] - c2["open"]) < atr * 0.15:
            if _abs_body(c3) > _abs_body(c2) * 0.5:
                patterns.append({
                    "name": "BEAR_TWO_BAR_REV",
                    "direction": "CALL",
                    "score": 3,
                    "reason": f"Bearish Two-Bar Reversal (c2 up, c3 down, close near c2 open) → CALL (continuation, 53.2% measured n=231)"
                })
    if r3 > 0 and atr > 0:
        body_pct3 = _abs_body(c3) / r3 * 100
        if body_pct3 < 10:
            if b1 > 0 and b2 > 0 and c3["close"] < c3["open"] + (r3 * 0.05):
                patterns.append({
                    "name": "DOJI_BEARISH",
                    "direction": "PUT",
                    "score": 2,
                    "reason": f"Doji after uptrend (body {body_pct3:.0f}%) → PUT reversal (58% win rate)"
                })
    return patterns
def classify_market_regime(candles, lookback=30):
    """Classify market state into one of four regimes."""
    if len(candles) < 10:
        return {
            "regime": "RANGE", "trend_strength": 0.0, "volatility_pct": 1.0,
            "ema9": 0, "ema21": 0,
            "is_trending": False, "is_ranging": True, "is_volatile": False,
        }
    lookback = min(lookback, len(candles))
    recent = candles[-lookback:]
    closes = [c["close"] for c in recent]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    ema_diff = (ema9 - ema21) / ema21 if ema21 > 0 else 0
    atr_val = _atr(candles, 20)
    price_mid = (ema9 + ema21) / 2 if (ema9 + ema21) > 0 else 1.0
    atr_norm = max(atr_val * 4.0, price_mid * 0.0005)
    trend_strength = min(abs(ema_diff * price_mid) / atr_norm, 1.0)
    hh_hl = 0
    lh_ll = 0
    prev_swing_high = None
    prev_swing_low = None
    for i in range(2, len(recent) - 2):
        c = recent[i]
        is_swing_high = (c["high"] >= recent[i - 1]["high"] and c["high"] > recent[i - 2]["high"]
                         and c["high"] >= recent[i + 1]["high"] and c["high"] > recent[i + 2]["high"])
        is_swing_low = (c["low"] <= recent[i - 1]["low"] and c["low"] < recent[i - 2]["low"]
                        and c["low"] <= recent[i + 1]["low"] and c["low"] < recent[i + 2]["low"])
        if is_swing_high:
            if prev_swing_high is not None:
                if c["high"] > prev_swing_high:
                    hh_hl += 1   # Higher High
                else:
                    lh_ll += 1   # Lower High
            prev_swing_high = c["high"]
        if is_swing_low:
            if prev_swing_low is not None:
                if c["low"] > prev_swing_low:
                    hh_hl += 1   # Higher Low
                else:
                    lh_ll += 1   # Lower Low
            prev_swing_low = c["low"]
    atr_now = _atr(candles, 10)
    atr_hist = atr_val
    vol_pct = (atr_now / atr_hist) if atr_hist > 0 else 1.0
    if vol_pct > 1.5:
        regime = "VOLATILE"
    elif ema9 > ema21 and trend_strength > 0.25 and hh_hl > lh_ll:
        regime = "TREND_UP"
    elif ema9 < ema21 and trend_strength > 0.25 and lh_ll > hh_hl:
        regime = "TREND_DOWN"
    else:
        regime = "RANGE"
    return {
        "regime": regime,
        "trend_strength": round(trend_strength, 3),
        "volatility_pct": round(vol_pct, 3),
        "ema9": round(ema9, 6),
        "ema21": round(ema21, 6),
        "is_trending": regime in ("TREND_UP", "TREND_DOWN"),
        "is_ranging": regime == "RANGE",
        "is_volatile": regime == "VOLATILE",
    }
def find_key_levels(candles, lookback=50):
    """Find recent swing highs/lows as key support/resistance levels."""
    if len(candles) < 5:
        return []
    recent = candles[-lookback:] if len(candles) > lookback else candles
    offset = len(candles) - len(recent)
    levels = []
    for i in range(2, len(recent) - 2):
        c = recent[i]
        if (c["high"] >= recent[i - 1]["high"] and c["high"] > recent[i - 2]["high"]
                and c["high"] >= recent[i + 1]["high"] and c["high"] > recent[i + 2]["high"]):
            levels.append({"price": c["high"], "type": "resistance",
                           "idx": i + offset})
        if (c["low"] <= recent[i - 1]["low"] and c["low"] < recent[i - 2]["low"]
                and c["low"] <= recent[i + 1]["low"] and c["low"] < recent[i + 2]["low"]):
            levels.append({"price": c["low"], "type": "support",
                           "idx": i + offset})
    resistances = [l for l in levels if l["type"] == "resistance"][-8:]
    supports = [l for l in levels if l["type"] == "support"][-8:]
    return resistances + supports
def check_level_confluence(candles, levels, atr):
    """Check if the last candle's close is near a key S/R level."""
    if not levels or not candles or len(candles) < 2 or atr <= 0:
        return {"near_level": False, "level_type": None,
                "level_price": None, "action": None, "distance_atr": 0}
    last = candles[-1]
    prev = candles[-2]
    close = last["close"]
    prev_close = prev["close"]
    open_ = last["open"]
    high = last["high"]
    low = last["low"]
    tol = atr * 0.30
    nearest = None
    nearest_dist = float("inf")
    for lvl in levels:
        dist = abs(close - lvl["price"])
        if dist < tol and dist < nearest_dist:
            nearest = lvl
            nearest_dist = dist
    if not nearest:
        return {"near_level": False, "level_type": None,
                "level_price": None, "action": None, "distance_atr": 0}
    level_price = nearest["price"]
    if nearest["type"] == "resistance":
        if close > level_price:
            action = "breakout"
        elif high > level_price and close < level_price:
            action = "wick_rejection"
        else:
            action = "bounce"
    else:  # support
        if close < level_price:
            action = "breakdown"
        elif low < level_price and close > level_price:
            action = "wick_rejection"
        else:
            action = "bounce"
    return {
        "near_level": True,
        "level_type": nearest["type"],
        "level_price": level_price,
        "action": action,
        "distance_atr": round(nearest_dist / atr, 3),
    }
def compute_statistical_edge(candles, lookback=50):
    """Compute Z-scores and percentiles for the last candle."""
    if len(candles) < 10:
        return {"z_body": 0, "z_range": 0, "close_percentile": 50,
                "streak_rarity": 0, "current_streak": 0, "streak_direction": 0}
    recent = candles[-lookback:] if len(candles) > lookback else candles
    prior_for_stats = recent[:-1] if len(recent) > 1 else recent
    bodies = [_abs_body(c) for c in prior_for_stats]
    ranges = [_range(c) for c in prior_for_stats]
    mean_body = sum(bodies) / len(bodies) if bodies else 0
    _n_body = len(bodies)
    var_body = (sum((b - mean_body) ** 2 for b in bodies) / (_n_body - 1)
                if _n_body > 1 else 1)
    std_body = math.sqrt(var_body) if var_body > 0 else 1
    mean_range = sum(ranges) / len(ranges) if ranges else 0
    _n_range = len(ranges)
    var_range = (sum((r - mean_range) ** 2 for r in ranges) / (_n_range - 1)
                 if _n_range > 1 else 1)
    std_range = math.sqrt(var_range) if var_range > 0 else 1
    last = candles[-1]
    last_body = _abs_body(last)
    last_range = _range(last)
    z_body = (last_body - mean_body) / std_body
    z_range = (last_range - mean_range) / std_range
    prior_closes = [c["close"] for c in recent[:-1]] if len(recent) > 1 else []
    if prior_closes:
        count_below = sum(1 for cl in prior_closes if cl < last["close"])
        count_equal = sum(1 for cl in prior_closes if cl == last["close"])
        close_percentile = ((count_below + 0.5 * count_equal) / len(prior_closes)) * 100
    else:
        close_percentile = 50
    last_body_signed = _body(last)
    direction = 1 if last_body_signed > 0 else (-1 if last_body_signed < 0 else 0)
    if direction == 0:
        streak = 0
        streak_rarity = 0
    else:
        streak = 1
        for i in range(len(recent) - 2, -1, -1):
            b = _body(recent[i])
            d = 1 if b > 0 else (-1 if b < 0 else 0)
            if d == direction:
                streak += 1
            else:
                break
        cutoff = len(recent) - streak  # index where current streak started
        historical = recent[:max(0, cutoff)]
        all_streaks = []
        cur_dir = 0
        cur_len = 0
        for c in historical:
            b = _body(c)
            d = 1 if b > 0 else (-1 if b < 0 else 0)
            if d == 0:
                if cur_len >= 1:
                    all_streaks.append(cur_len)
                cur_dir, cur_len = 0, 0
            elif d == cur_dir:
                cur_len += 1
            else:
                if cur_len >= 1:
                    all_streaks.append(cur_len)
                cur_dir, cur_len = d, 1
        if cur_len >= 1:
            all_streaks.append(cur_len)
        if all_streaks:
            longer = sum(1 for s in all_streaks if s >= streak)
            streak_rarity = longer / len(all_streaks)
        else:
            streak_rarity = 0.5
    return {
        "z_body": round(z_body, 2),
        "z_range": round(z_range, 2),
        "close_percentile": round(close_percentile, 1),
        "streak_rarity": round(streak_rarity, 3),
        "current_streak": streak,
        "streak_direction": direction,
    }
def round_level(price):
    """Classify how close a price is to a 'round' psychological level."""
    if price <= 0:
        return None, 0, "NONE"
    magnitude = math.floor(math.log10(price))  # 0 for 1.05, 2 for 150
    big_step = 10 ** (magnitude - 1)   # 0.1 for forex, 10 for JPY, 1000 for BTC
    mid_step = 10 ** (magnitude - 2)   # one digit finer
    big = round(price / big_step) * big_step
    mid = round(price / mid_step) * mid_step
    d_big = abs(price - big)
    d_mid = abs(price - mid)
    tol_big = price * 0.0005
    tol_mid = price * 0.0002
    if d_big < d_mid and d_big < tol_big:
        return big, d_big, "BIG"
    if d_mid < tol_mid:
        return mid, d_mid, "MID"
    if d_big < tol_big:
        return big, d_big, "BIG"
    return None, 0, "NONE"
_round_level = round_level

def key_levels_rich(candles, lookback=60):
    """Extract recent swing highs/lows as key levels (last ``lookback`` candles)."""
    if len(candles) < 5:
        return []
    recent = candles[-lookback:] if len(candles) > lookback else candles
    offset = len(candles) - len(recent)
    levels = []
    for i in range(2, len(recent) - 2):
        c = recent[i]
        if (c["high"] >= recent[i - 1]["high"] and c["high"] > recent[i - 2]["high"]
                and c["high"] >= recent[i + 1]["high"] and c["high"] > recent[i + 2]["high"]):
            levels.append({"type": "swing_high", "price": c["high"],
                           "idx": i + offset, "time": c.get("time", 0)})
        if (c["low"] <= recent[i - 1]["low"] and c["low"] < recent[i - 2]["low"]
                and c["low"] <= recent[i + 1]["low"] and c["low"] < recent[i + 2]["low"]):
            levels.append({"type": "swing_low", "price": c["low"],
                           "idx": i + offset, "time": c.get("time", 0)})
    swing_highs = [lv for lv in levels if lv["type"] == "swing_high"][-10:]
    swing_lows = [lv for lv in levels if lv["type"] == "swing_low"][-10:]
    return sorted(swing_highs + swing_lows, key=lambda x: x["idx"])
_key_levels = key_levels_rich
