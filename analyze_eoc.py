"""
End-of-Candle analysis engine  —  REWRITE (2026-07-10)

Multiple theories vote CALL/PUT. The blend produces the final signal.

KEY CHANGES vs previous version:
  1. TICK-WEIGHTED buyer/seller pressure (bigger moves = more weight)
  2. TICK-SPEED / acceleration analysis (momentum building or dying?)
  3. VOLUME PROFILE (where did price spend the most time?)
  4. MOMENTUM SHIFT detection (late direction change in running candle)
  5. CON theory now checks exhaustion (RSI extreme + candle size decay)
  6. Tighter dead band (|net|<1 instead of <2, confidence<40 instead of <45)
  7. REV theory checks if wick is at a KEY LEVEL (swing high/low)
  8. TRAP theory uses tick-weighted analysis too
  9. MICRO_BUILD theory — new, pure microstructure vote from closed candle ticks

Theories:
  CON   - Continuation (trend following + exhaustion check)
  REV   - Reversal (wick rejection + key-level context)
  RUN   - Running-candle microstructure (tick-weighted pressure + reaction + speed)
  TRAP  - Trap (fake move then reverse, tick-weighted)
  GAP   - Gap fill/rejection
  LAST  - Last-portion exhaustion/recovery (tick-weighted)
  RNG   - Round-number proximity bias
  MST   - Market-state classification
  MICRO - Closed-candle internal microstructure (NEW)
  SHIFT - Momentum shift detection (NEW)
"""
import math
from collections import Counter


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _round_level(price):
    """Classify how close a price is to a 'round' psychological level."""
    if price <= 0:
        return None, 0, "NONE"
    decimals = max(0, -int(math.floor(math.log10(abs(price)))) + 2)
    big = round(price, max(0, decimals - 3))
    mid = round(price, max(0, decimals - 2))
    d_big = abs(price - big) if big else float("inf")
    d_mid = abs(price - mid) if mid else float("inf")
    if d_big < d_mid and d_big / price < 0.0003:
        return big, d_big, "BIG"
    if d_mid / price < 0.0003:
        return mid, d_mid, "MID"
    return None, 0, "NONE"


def _key_levels(candles):
    """Extract recent swing highs/lows as key levels."""
    if len(candles) < 5:
        return []
    levels = []
    for i in range(2, len(candles) - 2):
        c = candles[i]
        if (c["high"] >= candles[i-1]["high"] and c["high"] >= candles[i-2]["high"] and
            c["high"] >= candles[i+1]["high"] and c["high"] >= candles[i+2]["high"]):
            levels.append({"type": "swing_high", "price": c["high"], "idx": i})
        if (c["low"] <= candles[i-1]["low"] and c["low"] <= candles[i-2]["low"] and
            c["low"] <= candles[i+1]["low"] and c["low"] <= candles[i+2]["low"]):
            levels.append({"type": "swing_low", "price": c["low"], "idx": i})
    return levels[-10:]


def _parse_votes(reasons):
    """Parse reason strings like 'CON:+3 CALL' into (code, direction, magnitude)."""
    votes = []
    for r in reasons:
        if ":" not in str(r):
            continue
        code_part, rest = str(r).split(":", 1)
        code = code_part.strip()
        # Extract the numeric part (first token before the direction word)
        tokens = rest.split()
        if not tokens:
            continue
        first = tokens[0].lstrip("-+")
        mag = 1
        try:
            mag = abs(float(first))
        except ValueError:
            pass
        if "CALL" in rest.upper():
            votes.append((code, 1, mag))
        elif "PUT" in rest.upper():
            votes.append((code, -1, mag))
    return votes


def _atr(candles, n=20):
    if not candles:
        return 0.0001
    recent = candles[-n:] if len(candles) >= n else candles
    return sum(c["high"] - c["low"] for c in recent) / len(recent) or 0.0001


def _ema(prices, period):
    if not prices:
        return 0
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(d if d > 0 else 0)
        losses.append(-d if d < 0 else 0)
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def _classify_regime(candles):
    """Classify market regime using EMA crossover + RSI + ADX-like trending."""
    if len(candles) < 10:
        return {"trend": "SIDEWAYS", "zone": "UNKNOWN"}
    closes = [c["close"] for c in candles[-30:]]  # More data points
    ema9  = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    rsi_val = _rsi(closes)

    # EMA separation as a percentage of price
    sep = abs(ema9 - ema21) / ema21 if ema21 > 0 else 0
    # Strong separation = strong trend
    strong_trend = sep > 0.0003  # ~3 pips for EUR/USD

    if ema9 > ema21 and rsi_val > 48:
        trend = "UPTREND" if strong_trend or rsi_val > 55 else "SIDEWAYS"
    elif ema9 < ema21 and rsi_val < 52:
        trend = "DOWNTREND" if strong_trend or rsi_val < 45 else "SIDEWAYS"
    else:
        trend = "SIDEWAYS"

    # Zone: where price sits relative to recent range
    recent = candles[-10:]
    hi = max(c["high"] for c in recent)
    lo = min(c["low"] for c in recent)
    rng = hi - lo
    if rng == 0:
        zone = "MID"
    else:
        pos = (closes[-1] - lo) / rng
        if pos > 0.80:
            zone = "HIGH"
        elif pos < 0.20:
            zone = "LOW"
        else:
            zone = "MID"

    return {"trend": trend, "zone": zone, "ema9": round(ema9, 6),
            "ema21": round(ema21, 6), "rsi": round(rsi_val, 1)}


# ═══════════════════════════════════════════════════════════════════════════════
#  TICK-WEIGHTED MICROSTRUCTURE BUILDER
#  This is the CORE improvement — every tick's SIZE matters, not just direction.
# ═══════════════════════════════════════════════════════════════════════════════

def _build_micro(ticks, open_price):
    """
    Build rich microstructure from a tick list.
    Used by BOTH the closed-candle MICRO theory AND the running-ticks RUN theory.
    """
    ticks = list(ticks)
    if len(ticks) < 10:
        return None
    op  = open_price
    hi  = max(ticks)
    lo  = min(ticks)
    cur = ticks[-1]
    rng = hi - lo
    n   = len(ticks)

    # ── 1. Tick-weighted buyer/seller pressure ─────────────────────────────
    # Instead of just counting up/down ticks, weight by the SIZE of each move.
    # A 5-pip up-tick matters more than a 0.1-pip up-tick.
    raw_buy_vol = 0.0
    raw_sell_vol = 0.0
    up_count  = 0
    dn_count  = 0
    for i in range(1, n):
        delta = ticks[i] - ticks[i-1]
        if delta > 0:
            raw_buy_vol += delta
            up_count += 1
        elif delta < 0:
            raw_sell_vol += abs(delta)
            dn_count += 1
    total_vol = raw_buy_vol + raw_sell_vol
    buy_pct  = round(raw_buy_vol / total_vol * 100) if total_vol > 0 else 50
    sell_pct = 100 - buy_pct
    # Also keep simple count-based for cross-reference
    count_buy_pct = round(up_count / (up_count + dn_count) * 100) if (up_count + dn_count) > 0 else 50

    # Divergence: if volume-weighted and count-weighted DISAGREE, that's a signal
    vol_count_diverge = abs(buy_pct - count_buy_pct) > 20

    if buy_pct >= 62:
        pressure = "BUYER"
    elif sell_pct >= 62:
        pressure = "SELLER"
    else:
        pressure = "FIGHT"

    # ── 2. Fight zone: midpoint crossings ─────────────────────────────────
    mid     = (hi + lo) / 2
    crosses = sum(1 for i in range(1, n)
                  if (ticks[i-1] < mid) != (ticks[i] < mid))
    is_fight = crosses >= 4

    # ── 3. Volume profile: where did price spend the most time? ───────────
    hold_price = None
    hold_visits = 0
    if rng > 0:
        bin_size = rng / 10  # 10 bins for finer resolution
        bins = {}
        for t in ticks:
            b = int((t - lo) / bin_size)
            bins[b] = bins.get(b, 0) + 1
        top_bin = max(bins, key=bins.get)
        hold_price = round(lo + top_bin * bin_size + bin_size / 2, 6)
        hold_visits = bins[top_bin]
        # Volume-at-hold vs volume-at-edges
        edge_bins = {0, 9} if len(bins) >= 10 else {0, len(bins)-1}
        hold_pct_of_total = bins.get(top_bin, 0) / n * 100
    else:
        hold_price  = round(cur, 6)
        hold_visits = n
        hold_pct_of_total = 100

    # ── 4. Phase momentum (early / mid / late thirds) ─────────────────────
    t3 = max(n // 3, 1)
    early = ticks[t3] - ticks[0]
    mid_m = ticks[2 * t3] - ticks[t3]
    late  = ticks[-1] - ticks[2 * t3]

    def _dir(v):
        return "UP" if v > 0 else ("DOWN" if v < 0 else "FLAT")

    phases = [_dir(early), _dir(mid_m), _dir(late)]

    # Phase intensity (how strong each phase is relative to total range)
    def _intensity(v):
        return abs(v) / rng if rng > 0 else 0

    phase_intensity = [_intensity(early), _intensity(mid_m), _intensity(late)]

    # ── 5. Reaction (visited extreme then reversed) ───────────────────────
    reaction = None
    if rng > 0:
        from_hi = (hi - cur) / rng
        from_lo = (cur - lo) / rng
        net = cur - op
        late_q = max(n // 4, 2)
        late_move = ticks[-1] - ticks[-late_q]
        if from_hi > 0.45 and late_move <= 0 and net < 0:
            reaction = "SELLER"
        elif from_lo > 0.45 and late_move >= 0 and net > 0:
            reaction = "BUYER"

    # ── 6. Final-tick exhaustion / recovery ───────────────────────────────
    last_react = None
    if n >= 15:
        last_n2 = max(n // 6, 6)
        fin2 = ticks[-last_n2:]
        fi2_up = sum(1 for i in range(1, len(fin2)) if fin2[i] > fin2[i-1])
        fi2_dn = sum(1 for i in range(1, len(fin2)) if fin2[i] < fin2[i-1])
        fi2_tot = fi2_up + fi2_dn
        if fi2_tot >= 3:
            fbp2 = fi2_up / fi2_tot
            net_run = cur - op
            if net_run > 0:
                if fbp2 <= 0.30 or (fi2_tot >= 5 and fbp2 >= 0.90):
                    last_react = "EXHAUST"
                elif 0.55 <= fbp2 <= 0.85 and fi2_dn >= 2:
                    last_react = "RECOVERY"
            elif net_run < 0:
                if fbp2 >= 0.70 or (fi2_tot >= 5 and fbp2 <= 0.10):
                    last_react = "EXHAUST"
                elif 0.15 <= fbp2 <= 0.45 and fi2_up >= 2:
                    last_react = "RECOVERY"

    # ── 7. TICK SPEED: acceleration / deceleration ────────────────────────
    # Compare speed (price change per tick) in first half vs second half
    tick_speed = None
    if n >= 20:
        half = n // 2
        first_half_range = abs(ticks[half] - ticks[0])
        second_half_range = abs(ticks[-1] - ticks[half])
        # Speed = range / tick_count in that half
        spd_first  = first_half_range / half if half > 0 else 0
        spd_second = second_half_range / (n - half) if (n - half) > 0 else 0
        avg_speed  = (first_half_range + second_half_range) / n
        if avg_speed > 0:
            accel_ratio = spd_second / spd_first if spd_first > 0 else 1.0
        else:
            accel_ratio = 1.0
        tick_speed = {
            "first": round(spd_first, 8),
            "second": round(spd_second, 8),
            "accel": round(accel_ratio, 3),
            "avg": round(avg_speed, 8),
        }

    # ── 8. MOMENTUM SHIFT: direction change in last third ─────────────────
    momentum_shift = None
    if n >= 20:
        t2_3 = 2 * n // 3
        early_dir = "UP" if ticks[t2_3] > ticks[0] else ("DOWN" if ticks[t2_3] < ticks[0] else "FLAT")
        late_dir  = "UP" if ticks[-1] > ticks[t2_3] else ("DOWN" if ticks[-1] < ticks[t2_3] else "FLAT")
        if early_dir != "FLAT" and late_dir != "FLAT" and early_dir != late_dir:
            # Confirmed momentum shift in last third
            if late_dir == "UP":
                momentum_shift = "BULL_SHIFT"
            else:
                momentum_shift = "BEAR_SHIFT"

    return {
        "buy_pct": buy_pct, "sell_pct": sell_pct,
        "count_buy_pct": count_buy_pct,
        "pressure": pressure, "is_fight": is_fight, "crosses": crosses,
        "hold_price": hold_price, "hold_visits": hold_visits,
        "hold_pct_of_total": hold_pct_of_total,
        "phases": phases, "phase_intensity": phase_intensity,
        "reaction": reaction, "net": round(cur - op, 6),
        "tick_count": n, "last_react": last_react,
        "tick_speed": tick_speed,
        "momentum_shift": momentum_shift,
        "vol_count_diverge": vol_count_diverge,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  THEORIES
# ═══════════════════════════════════════════════════════════════════════════════

def _theory_con(candles, muted):
    """CON - Continuation: follow the trend, but CHECK for exhaustion."""
    if "CON" in muted:
        return None
    if len(candles) < 5:
        return None
    regime = _classify_regime(candles)
    score = 0
    reasons = []

    closes = [c["close"] for c in candles]
    # Last 3 candles same direction?
    dirs = []
    for c in candles[-3:]:
        if c["close"] > c["open"]:
            dirs.append(1)
        elif c["close"] < c["open"]:
            dirs.append(-1)
        else:
            dirs.append(0)

    if all(d >= 0 for d in dirs) and sum(dirs) >= 2:
        # Check for exhaustion: are candles getting SMALLER?
        sizes = [c["high"] - c["low"] for c in candles[-3:]]
        if sizes[0] > sizes[1] > sizes[2] and sizes[2] < sizes[0] * 0.5:
            # Shrinking bullish candles = momentum dying → DON'T follow
            score -= 1
            reasons.append("CON:-1 PUT bull-shrinking-exhaust")
        else:
            score += 3
            reasons.append("CON:+3 CALL 3-bull-continue")
    elif all(d <= 0 for d in dirs) and sum(dirs) <= -2:
        sizes = [c["high"] - c["low"] for c in candles[-3:]]
        if sizes[0] > sizes[1] > sizes[2] and sizes[2] < sizes[0] * 0.5:
            score += 1
            reasons.append("CON:+1 CALL bear-shrinking-exhaust")
        else:
            score += 3
            reasons.append("CON:-3 PUT 3-bear-continue")

    # EMA trend alignment — but check RSI for overbought/oversold
    rsi_val = regime.get("rsi", 50)
    if regime["trend"] == "UPTREND":
        if rsi_val >= 75:
            # Overbought in uptrend — continuation risky
            score += 1  # Still slightly bullish, but reduced
            reasons.append(f"CON:+1 CALL ema-bull-rsi={rsi_val:.0f}-overbought")
        else:
            score += 2
            reasons.append("CON:+2 CALL ema-bullish")
    elif regime["trend"] == "DOWNTREND":
        if rsi_val <= 25:
            score -= 1
            reasons.append(f"CON:-1 PUT ema-bear-rsi={rsi_val:.0f}-oversold")
        else:
            score -= 2
            reasons.append("CON:-2 PUT ema-bearish")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_rev(candles, muted):
    """REV - Reversal: wick rejection at extremes OR at key levels."""
    if "REV" in muted:
        return None
    if len(candles) < 3:
        return None
    last = candles[-1]
    body = abs(last["close"] - last["open"])
    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]

    atr = _atr(candles)
    if body < atr * 0.1:
        return None  # Doji — no clear rejection

    score = 0
    reasons = []

    # Get key levels for context
    levels = _key_levels(candles)

    # Strong lower wick = bullish rejection
    if lower_wick > body * 1.5 and lower_wick > atr * 0.2:
        boost = 3
        # Extra weight if the low is near a swing low (double bottom pattern)
        for lv in levels:
            if lv["type"] == "swing_low" and abs(last["low"] - lv["price"]) < atr * 0.3:
                boost = 4  # Stronger signal at key level
                reasons.append(f"REV:bonus+1 at-swing-low={lv['price']:.5f}")
                break
        score += boost
        reasons.append(f"REV:+{boost} CALL lower-wick={lower_wick:.6f}")

    # Strong upper wick = bearish rejection
    if upper_wick > body * 1.5 and upper_wick > atr * 0.2:
        boost = 3
        for lv in levels:
            if lv["type"] == "swing_high" and abs(last["high"] - lv["price"]) < atr * 0.3:
                boost = 4
                reasons.append(f"REV:bonus+1 at-swing-high={lv['price']:.5f}")
                break
        score -= boost
        reasons.append(f"REV:-{boost} PUT upper-wick={upper_wick:.6f}")

    # Also check: is the candle at a RANGE extreme? (HIGH/LOW zone)
    if len(candles) >= 10:
        recent = candles[-10:]
        hi = max(c["high"] for c in recent)
        lo = min(c["low"] for c in recent)
        rng = hi - lo
        if rng > 0:
            pos = (last["close"] - lo) / rng
            # At range low with bullish close = bounce
            if pos < 0.15 and last["close"] > last["open"]:
                score += 1
                reasons.append("REV:+1 CALL range-low-bounce")
            # At range high with bearish close = rejection
            elif pos > 0.85 and last["close"] < last["open"]:
                score -= 1
                reasons.append("REV:-1 PUT range-high-reject")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_run(candles, ticks, micro, muted):
    """RUN - Running candle microstructure (uses TICK-WEIGHTED analysis)."""
    if "RUN" in muted:
        return None
    if not micro or not ticks:
        return None

    score = 0
    reasons = []
    buy_pct    = micro.get("buy_pct", 50)
    pressure   = micro.get("pressure")
    reaction   = micro.get("reaction")
    phases     = micro.get("phases", [])
    last_react = micro.get("last_react")
    is_fight   = micro.get("is_fight", False)
    tick_speed = micro.get("tick_speed")
    mom_shift  = micro.get("momentum_shift")
    vol_div    = micro.get("vol_count_diverge", False)

    # ── Tick-weighted buyer/seller pressure ────────────────────────────────
    if buy_pct >= 70:
        score += 3
        reasons.append(f"RUN:+3 CALL buyer-pressure={buy_pct}%")
    elif buy_pct <= 30:
        score -= 3
        reasons.append(f"RUN:-3 PUT seller-pressure={100-buy_pct}%")
    elif buy_pct >= 60:
        score += 2  # Increased from +1 — tick-weighted is more reliable
        reasons.append(f"RUN:+2 CALL buyer-pressure={buy_pct}%")
    elif buy_pct <= 40:
        score -= 2
        reasons.append(f"RUN:-2 PUT seller-pressure={100-buy_pct}%")

    # ── Volume/count divergence: if they disagree, reduce confidence ───────
    if vol_div:
        score = int(score * 0.5)
        reasons.append("RUN:diverge vol-vs-count *0.5")

    # ── Reaction (visited extreme then reversed) ───────────────────────────
    if reaction == "BUYER":
        score += 2
        reasons.append("RUN:+2 CALL buyer-rejection-from-low")
    elif reaction == "SELLER":
        score -= 2
        reasons.append("RUN:-2 PUT seller-rejection-from-high")

    # ── Phase momentum consistency ────────────────────────────────────────
    if len(phases) == 3:
        intensity = micro.get("phase_intensity", [0, 0, 0])
        if phases == ["UP", "UP", "UP"]:
            # Stronger if LATE phase is also intense (not just direction)
            bonus = 3 if intensity[2] > 0.3 else 2
            score += bonus
            reasons.append(f"RUN:+{bonus} CALL all-phases-up late_i={intensity[2]:.2f}")
        elif phases == ["DOWN", "DOWN", "DOWN"]:
            bonus = 3 if intensity[2] > 0.3 else 2
            score -= bonus
            reasons.append(f"RUN:-{bonus} PUT all-phases-down late_i={intensity[2]:.2f}")
        # Late reversal: first two same, last different
        elif phases[0] == phases[1] and phases[2] != phases[0]:
            if phases[2] == "UP":
                score += 1
                reasons.append("RUN:+1 CALL late-phase-up")
            elif phases[2] == "DOWN":
                score -= 1
                reasons.append("RUN:-1 PUT late-phase-down")

    # ── Tick speed / acceleration ─────────────────────────────────────────
    if tick_speed:
        accel = tick_speed["accel"]
        if accel > 1.5:
            # Accelerating in the current direction = momentum building
            net = micro.get("net", 0)
            if net > 0:
                score += 1
                reasons.append(f"RUN:+1 CALL accel={accel:.1f}")
            elif net < 0:
                score -= 1
                reasons.append(f"RUN:-1 PUT accel={accel:.1f}")
        elif accel < 0.5:
            # Decelerating = momentum dying
            net = micro.get("net", 0)
            if net > 0:
                score -= 1
                reasons.append(f"RUN:-1 PUT decel={accel:.1f}")
            elif net < 0:
                score += 1
                reasons.append(f"RUN:+1 CALL decel={accel:.1f}")

    # ── Momentum shift (late direction change) ────────────────────────────
    if mom_shift:
        if mom_shift == "BULL_SHIFT":
            score += 2
            reasons.append("RUN:+2 CALL momentum-shift-bull")
        elif mom_shift == "BEAR_SHIFT":
            score -= 2
            reasons.append("RUN:-2 PUT momentum-shift-bear")

    # ── Exhaustion/recovery from last portion ─────────────────────────────
    if last_react == "EXHAUST":
        net = micro.get("net", 0)
        if net > 0:
            score -= 2
            reasons.append("RUN:-2 PUT bull-exhaust-end")
        elif net < 0:
            score += 2
            reasons.append("RUN:+2 CALL bear-exhaust-end")
    elif last_react == "RECOVERY":
        net = micro.get("net", 0)
        if net > 0:
            score += 1
            reasons.append("RUN:+1 CALL bull-recovery-end")
        elif net < 0:
            score -= 1
            reasons.append("RUN:-1 PUT bear-recovery-end")

    # ── Fight zone = uncertainty ──────────────────────────────────────────
    if is_fight:
        score = int(score * 0.3)

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_trap(candles, ticks, muted):
    """TRAP - Trap: big move in one direction then reversal within candle."""
    if "TRAP" in muted:
        return None
    if not ticks or len(ticks) < 15:
        return None
    op  = candles[-1]["open"] if candles else ticks[0]
    hi  = max(ticks)
    lo  = min(ticks)
    cur = ticks[-1]
    rng = hi - lo
    if rng == 0:
        return None

    from_hi = (hi - cur) / rng
    from_lo = (cur - lo) / rng
    net = cur - op

    score = 0
    reasons = []

    # Tick-weighted: check if the VOLUME of the reversal is significant
    n = len(ticks)
    mid_tick = n // 2

    # Find the extreme tick index
    hi_idx = ticks.index(hi)
    lo_idx = ticks.index(lo)

    if from_hi > 0.60 and net < 0:
        # Bull trap: went up high, came back down
        # Stronger if the reversal happened LATE (more ticks selling)
        sell_ticks_after_hi = sum(1 for i in range(hi_idx, n) if i > 0 and ticks[i] < ticks[i-1])
        buy_ticks_to_hi = sum(1 for i in range(1, hi_idx+1) if ticks[i] > ticks[i-1])
        if sell_ticks_after_hi > buy_ticks_to_hi * 0.6:
            score -= 4  # Strong conviction
            reasons.append(f"TRAP:-4 PUT bull-trap from-hi={from_hi:.0%} heavy-reversal")
        else:
            score -= 3
            reasons.append(f"TRAP:-3 PUT bull-trap from-hi={from_hi:.0%}")
    elif from_lo > 0.60 and net > 0:
        buy_ticks_after_lo = sum(1 for i in range(lo_idx, n) if i > 0 and ticks[i] > ticks[i-1])
        sell_ticks_to_lo = sum(1 for i in range(1, lo_idx+1) if ticks[i] < ticks[i-1])
        if buy_ticks_after_lo > sell_ticks_to_lo * 0.6:
            score += 4
            reasons.append(f"TRAP:+4 CALL bear-trap from-lo={from_lo:.0%} heavy-reversal")
        else:
            score += 3
            reasons.append(f"TRAP:+3 CALL bear-trap from-lo={from_lo:.0%}")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_gap(candles, muted):
    """GAP - Gap between candles."""
    if "GAP" in muted:
        return None
    if len(candles) < 2:
        return None
    prev = candles[-2]
    last = candles[-1]
    gap = last["open"] - prev["close"]
    if prev["close"] == 0:
        return None
    gap_pct = gap / prev["close"]

    if abs(gap_pct) < 0.00005:
        return None

    score = 0
    reasons = []
    if gap_pct > 0 and last["close"] < last["open"]:
        score -= 2
        reasons.append(f"GAP:-2 PUT gap-up-rejected {gap_pct:.5f}")
    elif gap_pct < 0 and last["close"] > last["open"]:
        score += 2
        reasons.append(f"GAP:+2 CALL gap-down-filled {gap_pct:.5f}")
    elif gap_pct > 0 and last["close"] > last["open"]:
        score += 2
        reasons.append(f"GAP:+2 CALL gap-up-continue {gap_pct:.5f}")
    elif gap_pct < 0 and last["close"] < last["open"]:
        score -= 2
        reasons.append(f"GAP:-2 PUT gap-down-continue {gap_pct:.5f}")

    return ("CALL" if score > 0 else "PUT", score, reasons)


def _theory_last(candles, ticks, muted):
    """LAST - Last-portion exhaustion/recovery (tick-weighted)."""
    if "LAST" in muted:
        return None
    if not ticks or len(ticks) < 10:
        return None

    n = len(ticks)
    last_n = max(n // 6, 3)
    fin = ticks[-last_n:]
    fi_up = sum(1 for i in range(1, len(fin)) if fin[i] > fin[i-1])
    fi_dn = sum(1 for i in range(1, len(fin)) if fin[i] < fin[i-1])
    fi_tot = fi_up + fi_dn
    if fi_tot < 2:
        return None

    op  = ticks[0]
    cur = ticks[-1]
    net = cur - op
    fbp = fi_up / fi_tot

    score = 0
    reasons = []

    if net > 0:  # Candle is bullish
        if fbp <= 0.25:
            score -= 3
            reasons.append("LAST:-3 PUT bull-exhaustion-final")
        elif fbp >= 0.85 and fi_tot >= 4:
            score -= 2
            reasons.append("LAST:-2 PUT overextended-bull")
    elif net < 0:  # Candle is bearish
        if fbp >= 0.75:
            score += 3
            reasons.append("LAST:+3 CALL bear-exhaustion-final")
        elif fbp <= 0.15 and fi_tot >= 4:
            score += 2
            reasons.append("LAST:+2 CALL overextended-bear")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_rng(candles, muted):
    """RNG - Round-number proximity bias."""
    if "RNG" in muted:
        return None
    if not candles:
        return None
    last = candles[-1]
    for price in [last["close"], last["high"], last["low"]]:
        lvl, _, strength = _round_level(price)
        if strength == "BIG":
            if last["close"] > last["open"] and price == last["high"]:
                return "PUT", -2, [f"RNG:-2 PUT rejected-at-big-round {lvl}"]
            elif last["close"] < last["open"] and price == last["low"]:
                return "CALL", 2, [f"RNG:+2 CALL rejected-at-big-round {lvl}"]
    return None


def _theory_mst(candles, muted):
    """MST - Market state: classify and apply directional bias."""
    if "MST" in muted:
        return None
    if len(candles) < 10:
        return None

    regime = _classify_regime(candles)
    score = 0
    reasons = []
    state = "NONE"
    bias = None

    recent_atr = _atr(candles[-5:], 5)
    older_atr = _atr(candles[-20:-5], 5) if len(candles) >= 20 else recent_atr
    vol_ratio = recent_atr / older_atr if older_atr > 0 else 1

    if regime["trend"] == "SIDEWAYS" and vol_ratio < 0.7:
        state = "QUIET"
        score = 0
    elif regime["trend"] != "SIDEWAYS" and vol_ratio > 1.3:
        state = "VOLATILE"
        if regime["trend"] == "UPTREND":
            score += 1
            bias = "CALL"
        else:
            score -= 1
            bias = "PUT"
        reasons.append(f"MST:{'+' if score > 0 else ''}{score} {bias} volatile-trend")
    elif regime["trend"] != "SIDEWAYS":
        state = "TRENDING"
        if regime["trend"] == "UPTREND":
            score += 1
            bias = "CALL"
        else:
            score -= 1
            bias = "PUT"
        reasons.append(f"MST:{'+' if score > 0 else ''}{score} {bias} steady-trend")

    result = {"state": state, "bias": bias}
    if score == 0:
        return None, result
    return ("CALL" if score > 0 else "PUT", score, reasons), result


def _theory_micro(candles, ticks, muted):
    """
    MICRO - NEW: Closed candle's internal microstructure vote.

    This is THE MOST IMPORTANT theory for the user's use case:
    it looks at the CLOSED candle's tick-level behavior to predict
    the NEXT candle's direction.

    Key insight: if the closed candle's internal pressure (buyer/seller)
    was strong AND the close was in the direction of pressure, the next
    candle is likely to CONTINUE. If pressure was strong but close
    went AGAINST pressure (reversal inside the candle), the next candle
    may continue the reversal.
    """
    if "MICRO" in muted:
        return None
    if not ticks or len(ticks) < 15:
        return None

    micro = _build_micro(ticks, candles[-1]["open"] if candles else ticks[0])
    if not micro:
        return None

    score = 0
    reasons = []

    buy_pct  = micro.get("buy_pct", 50)
    net      = micro.get("net", 0)
    reaction = micro.get("reaction")
    phases   = micro.get("phases", [])
    mom_shift = micro.get("momentum_shift")
    pressure  = micro.get("pressure")
    is_fight  = micro.get("is_fight", False)

    # ── Core: Pressure alignment with candle direction ────────────────────
    # If buyer pressure > 65% AND candle closed BULLISH → strong CALL signal
    # If buyer pressure > 65% BUT candle closed BEARISH → reversal → PUT
    if buy_pct >= 65:
        if net > 0:
            # Buyers dominated AND closed up → next candle continues up
            score += 3
            reasons.append(f"MICRO:+3 CALL buyers-won bp={buy_pct}% net=+{net:.5f}")
        elif net < 0:
            # Buyers dominated but SELLERS won the close → reversal incoming
            score -= 2
            reasons.append(f"MICRO:-2 PUT buyers-lost-close bp={buy_pct}% net={net:.5f}")
    elif buy_pct <= 35:
        if net < 0:
            score -= 3
            reasons.append(f"MICRO:-3 PUT sellers-won sp={100-buy_pct}% net={net:.5f}")
        elif net > 0:
            score += 2
            reasons.append(f"MICRO:+2 CALL sellers-lost-close sp={100-buy_pct}% net=+{net:.5f}")

    # ── Reaction at close ─────────────────────────────────────────────────
    if reaction == "BUYER" and net > 0:
        score += 2
        reasons.append("MICRO:+2 CALL closed-with-buyer-rejection")
    elif reaction == "SELLER" and net < 0:
        score -= 2
        reasons.append("MICRO:-2 PUT closed-with-seller-rejection")

    # ── Phase pattern at close ────────────────────────────────────────────
    if len(phases) == 3:
        if phases == ["DOWN", "DOWN", "UP"]:
            # Bearish start, bullish finish → momentum shift → CALL
            score += 2
            reasons.append("MICRO:+2 CALL phases=DOWN,DOWN,UP")
        elif phases == ["UP", "UP", "DOWN"]:
            score -= 2
            reasons.append("MICRO:-2 PUT phases=UP,UP,DOWN")
        elif phases == ["UP", "DOWN", "UP"]:
            # V-shape recovery → CALL
            score += 1
            reasons.append("MICRO:+1 CALL V-recovery")
        elif phases == ["DOWN", "UP", "DOWN"]:
            score -= 1
            reasons.append("MICRO:-1 PUT inverted-V")

    # ── Momentum shift from ticks ────────────────────────────────────────
    if mom_shift == "BULL_SHIFT":
        score += 2
        reasons.append("MICRO:+2 CALL late-momentum-shift-bull")
    elif mom_shift == "BEAR_SHIFT":
        score -= 2
        reasons.append("MICRO:-2 PUT late-momentum-shift-bear")

    # ── Fight zone dampening ─────────────────────────────────────────────
    if is_fight:
        score = int(score * 0.4)

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_shift(candles, ticks, muted):
    """
    SHIFT - NEW: Multi-candle momentum shift detection.

    Looks at the last 5 candles' tick-weighted pressure to detect if
    the overall momentum is shifting direction. This catches trend
    changes that single-candle analysis misses.
    """
    if "SHIFT" in muted:
        return None
    if len(candles) < 5:
        return None

    # We can't access past ticks, so use candle body analysis instead
    # Look at: body sizes shrinking + direction changes
    last5 = candles[-5:]
    bodies = []
    for c in last5:
        body = c["close"] - c["open"]
        bodies.append(body)

    score = 0
    reasons = []

    # Count consecutive same-direction candles
    dirs = [1 if b > 0 else (-1 if b < 0 else 0) for b in bodies]

    # Check for 3+ same direction then opposite
    if len(dirs) >= 4:
        if all(d >= 0 for d in dirs[:3]) and dirs[3] < 0 and dirs[4] < 0:
            # 3 bullish then 2 bearish → shift to PUT
            # But check body sizes: are the bearish bodies growing?
            bear_sizes = [abs(b) for b in bodies[3:]]
            bull_sizes = [abs(b) for b in bodies[:3]]
            avg_bear = sum(bear_sizes) / len(bear_sizes)
            avg_bull = sum(bull_sizes) / len(bull_sizes)
            if avg_bear > avg_bull * 0.5:
                score -= 3
                reasons.append("SHIFT:-3 PUT 3bull->2bear shift")
        elif all(d <= 0 for d in dirs[:3]) and dirs[3] > 0 and dirs[4] > 0:
            bear_sizes = [abs(b) for b in bodies[:3]]
            bull_sizes = [abs(b) for b in bodies[3:]]
            avg_bear = sum(bear_sizes) / len(bear_sizes)
            avg_bull = sum(bull_sizes) / len(bull_sizes)
            if avg_bull > avg_bear * 0.5:
                score += 3
                reasons.append("SHIFT:+3 CALL 3bear->2bull shift")

    # Also check: shrinking bodies in current direction = exhaustion
    if len(bodies) >= 3:
        recent_bodies = [abs(b) for b in bodies[-3:]]
        if recent_bodies[0] > recent_bodies[1] > recent_bodies[2]:
            if bodies[-1] > 0:
                # Shrinking bullish → reversal to PUT
                score -= 2
                reasons.append("SHIFT:-2 PUT shrinking-bull-bodies")
            elif bodies[-1] < 0:
                score += 2
                reasons.append("SHIFT:+2 CALL shrinking-bear-bodies")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_eoc(candles, ticks, micro_history=None, period=60,
                muted=None, asset="", running_ticks=None):
    """
    Main entry point: run all theories and blend into a signal.

    Returns dict with: signal, score, confidence, strength, agree, reasons,
                        regime, market_state, theories_detail
    """
    if muted is None:
        muted = {}

    if len(candles) < 5:
        return {"signal": "NEUTRAL", "score": 0, "confidence": 0,
                "strength": "WEAK", "agree": 0, "total": 0,
                "reasons": ["INSUFFICIENT_DATA"], "regime": {},
                "market_state": {}}

    regime = _classify_regime(candles)
    all_reasons = []
    call_score = 0
    put_score = 0
    agree = 0
    total_fired = 0
    theories_detail = []

    # ── Build micro from closed-candle ticks (the PRIMARY micro input) ────
    # This is the microstructure of the JUST-CLOSED candle — the core signal.
    closed_micro = None
    if ticks and len(ticks) >= 10:
        closed_micro = _build_micro(ticks, candles[-1]["open"])

    # ── Build micro from running_ticks if provided (for LIVE re-eval) ─────
    running_micro = None
    if running_ticks and len(running_ticks) >= 10:
        op = candles[-1]["close"] if candles else running_ticks[0]
        running_micro = _build_micro(running_ticks, op)

    # ── Run all theories ──────────────────────────────────────────────────
    # Use closed_micro for theories that analyze the closed candle
    # Use running_micro for the RUN theory when doing live re-eval
    run_micro = running_micro if running_micro else closed_micro

    theories = [
        ("CON",   lambda: _theory_con(candles, muted)),
        ("REV",   lambda: _theory_rev(candles, muted)),
        ("RUN",   lambda: _theory_run(candles, ticks, run_micro, muted)),
        ("TRAP",  lambda: _theory_trap(candles, ticks, muted)),
        ("GAP",   lambda: _theory_gap(candles, muted)),
        ("LAST",  lambda: _theory_last(candles, ticks, muted)),
        ("RNG",   lambda: _theory_rng(candles, muted)),
        ("MICRO", lambda: _theory_micro(candles, ticks, muted)),
        ("SHIFT", lambda: _theory_shift(candles, ticks, muted)),
    ]

    # MST is special — returns (result, market_state)
    mst_result = None
    market_state = {}
    if "MST" not in muted:
        mst = _theory_mst(candles, muted)
        if mst:
            if isinstance(mst, tuple) and len(mst) == 2:
                mst_result, market_state = mst
            if mst_result:
                total_fired += 1
                sig, sc, rs = mst_result
                all_reasons.extend(rs)
                if sig == "CALL":
                    call_score += abs(sc)
                else:
                    put_score += abs(sc)
                theories_detail.append({"code": "MST", "vote": sig, "score": sc})

    for code, fn in theories:
        try:
            result = fn()
            if result is None:
                continue
            sig, sc, rs = result
            total_fired += 1
            all_reasons.extend(rs)
            if sig == "CALL":
                call_score += abs(sc)
            else:
                put_score += abs(sc)
            theories_detail.append({"code": code, "vote": sig, "score": sc})
        except Exception as e:
            print(f"[analyze] theory {code} error: {e}")

    # ── Blend ─────────────────────────────────────────────────────────────
    net = call_score - put_score
    total = call_score + put_score

    if total == 0 or total_fired == 0:
        return {"signal": "NEUTRAL", "score": 0, "confidence": 0,
                "strength": "WEAK", "agree": 0, "total": 0,
                "reasons": all_reasons or ["NO_THEORY_FIRED"],
                "regime": regime, "market_state": market_state,
                "theories_detail": theories_detail}

    agree = max(call_score, put_score)
    confidence = round(agree / total * 100)
    majority = "CALL" if net > 0 else "PUT"

    # Strength thresholds — TIGHTENED
    if confidence >= 65 and abs(net) >= 5:
        strength = "STRONG"
    elif confidence >= 52:
        strength = "MEDIUM"
    else:
        strength = "WEAK"

    # Dead band: TIGHTENED from |net|<2/confidence<45 to |net|<1/confidence<40
    # This lets more signals through while still killing noise
    if abs(net) < 1 or confidence < 40:
        return {"signal": "NEUTRAL", "score": net, "confidence": confidence,
                "strength": "WEAK", "agree": agree, "total": total_fired,
                "reasons": all_reasons, "regime": regime,
                "market_state": market_state,
                "theories_detail": theories_detail}

    return {
        "signal": majority,
        "score": net,
        "confidence": confidence,
        "strength": strength,
        "agree": agree,
        "total": total_fired,
        "reasons": all_reasons,
        "regime": regime,
        "market_state": market_state,
        "theories_detail": theories_detail,
    }


# Keep the old function name for compatibility with feed.py's import
def _build_micro_from_ticks(ticks, open_price):
    """Compatibility wrapper — delegates to _build_micro."""
    return _build_micro(ticks, open_price)