"""
End-of-Candle analysis engine.
Multiple theories vote CALL/PUT. The blend produces the final signal.

Theories:
  CON  - Continuation (trend following)
  REV  - Reversal (wick rejection)
  RUN  - Running-candle microstructure (buyer/seller pressure + reaction)
  TRAP - Trap (fake move then reverse)
  GAP  - Gap fill/rejection
  LAST - Last-portion exhaustion/recovery
  RNG  - Round-number proximity bias
  MST  - Market-state classification
"""
import math
from collections import Counter


def _round_level(price):
    """Classify how close a price is to a 'round' psychological level.
    Returns (level, distance, strength) where strength is BIG/MID/NONE."""
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
        if c["high"] >= candles[i - 1]["high"] and c["high"] >= candles[i - 2]["high"] and \
           c["high"] >= candles[i + 1]["high"] and c["high"] >= candles[i + 2]["high"]:
            levels.append({"type": "swing_high", "price": c["high"], "idx": i})
        if c["low"] <= candles[i - 1]["low"] and c["low"] <= candles[i - 2]["low"] and \
           c["low"] <= candles[i + 1]["low"] and c["low"] <= candles[i + 2]["low"]:
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
        if "CALL" in rest.upper():
            votes.append((code, 1, abs(float(rest.split()[0])) if rest.split()[0].lstrip("-").replace(".", "").isdigit() else 1))
        elif "PUT" in rest.upper():
            votes.append((code, -1, abs(float(rest.split()[0])) if rest.split()[0].lstrip("-").replace(".", "").isdigit() else 1))
    return votes


def _atr(candles, n=20):
    if len(candles) < 2:
        return candles[-1]["high"] - candles[-1]["low"] if candles else 0.0001
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
        d = closes[i] - closes[i - 1]
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
    """Classify market regime: UPTREND/DOWNTREND/SIDEWAYS and zone."""
    if len(candles) < 10:
        return {"trend": "SIDEWAYS", "zone": "UNKNOWN"}
    closes = [c["close"] for c in candles[-20:]]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    rsi_val = _rsi(closes)

    if ema9 > ema21 and rsi_val > 50:
        trend = "UPTREND"
    elif ema9 < ema21 and rsi_val < 50:
        trend = "DOWNTREND"
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
        if pos > 0.75:
            zone = "HIGH"
        elif pos < 0.25:
            zone = "LOW"
        else:
            zone = "MID"

    return {"trend": trend, "zone": zone, "ema9": round(ema9, 6),
            "ema21": round(ema21, 6), "rsi": round(rsi_val, 1)}


def _theory_con(candles, muted):
    """CON - Continuation: follow the trend."""
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
        score += 3
        reasons.append("CON:+3 CALL 3-bull-continue")
    elif all(d <= 0 for d in dirs) and sum(dirs) <= -2:
        score += 3
        reasons.append("CON:-3 PUT 3-bear-continue")

    # EMA trend alignment
    if regime["trend"] == "UPTREND":
        score += 2
        reasons.append("CON:+2 CALL ema-bullish")
    elif regime["trend"] == "DOWNTREND":
        score -= 2
        reasons.append("CON:-2 PUT ema-bearish")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_rev(candles, muted):
    """REV - Reversal: wick rejection at extremes."""
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
    # Strong lower wick = bullish rejection
    if lower_wick > body * 1.5 and lower_wick > atr * 0.2:
        score += 3
        reasons.append(f"REV:+3 CALL lower-wick={lower_wick:.6f}")
    # Strong upper wick = bearish rejection
    if upper_wick > body * 1.5 and upper_wick > atr * 0.2:
        score -= 3
        reasons.append(f"REV:-3 PUT upper-wick={upper_wick:.6f}")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_run(candles, ticks, micro, muted):
    """RUN - Running candle microstructure: buyer/seller pressure + reaction."""
    if "RUN" in muted:
        return None
    if not micro or not ticks:
        return None

    score = 0
    reasons = []
    buy_pct = micro.get("buy_pct", 50)
    pressure = micro.get("pressure")
    reaction = micro.get("reaction")
    phases = micro.get("phases", [])
    last_react = micro.get("last_react")
    is_fight = micro.get("is_fight", False)

    # Buyer/Seller pressure
    if buy_pct >= 70:
        score += 3
        reasons.append(f"RUN:+3 CALL buyer-pressure={buy_pct}%")
    elif buy_pct <= 30:
        score -= 3
        reasons.append(f"RUN:-{3} PUT seller-pressure={100 - buy_pct}%")
    elif buy_pct >= 60:
        score += 1
        reasons.append(f"RUN:+1 CALL mild-buyer={buy_pct}%")
    elif buy_pct <= 40:
        score -= 1
        reasons.append(f"RUN:-1 PUT mild-seller={100 - buy_pct}%")

    # Reaction (visited extreme then reversed)
    if reaction == "BUYER":
        score += 2
        reasons.append("RUN:+2 CALL buyer-rejection-from-low")
    elif reaction == "SELLER":
        score -= 2
        reasons.append("RUN:-2 PUT seller-rejection-from-high")

    # Phase momentum consistency
    if len(phases) == 3:
        if phases == ["UP", "UP", "UP"]:
            score += 2
            reasons.append("RUN:+2 CALL all-phases-up")
        elif phases == ["DOWN", "DOWN", "DOWN"]:
            score -= 2
            reasons.append("RUN:-2 PUT all-phases-down")
        # Late reversal: first two same, last different
        elif phases[0] == phases[1] and phases[2] != phases[0]:
            if phases[2] == "UP":
                score += 1
                reasons.append("RUN:+1 CALL late-phase-up")
            elif phases[2] == "DOWN":
                score -= 1
                reasons.append("RUN:-1 PUT late-phase-down")

    # Exhaustion/recovery from last portion
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

    # Fight zone = uncertainty
    if is_fight:
        score = int(score * 0.3)  # Heavy dampening

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_trap(candles, ticks, muted):
    """TRAP - Trap: big move in one direction then reversal within candle."""
    if "TRAP" in muted:
        return None
    if not ticks or len(ticks) < 15:
        return None
    op = candles[-1]["open"] if candles else ticks[0]
    hi = max(ticks)
    lo = min(ticks)
    cur = ticks[-1]
    rng = hi - lo
    if rng == 0:
        return None

    # Price went far one way but ended near the other extreme
    from_hi = (hi - cur) / rng
    from_lo = (cur - lo) / rng
    net = cur - op

    score = 0
    reasons = []

    # Bull trap: price spiked up but closed near low
    if from_hi > 0.65 and net < 0:
        score -= 3
        reasons.append(f"TRAP:-3 PUT bull-trap from-hi={from_hi:.0%}")
    # Bear trap: price dropped but closed near high
    elif from_lo > 0.65 and net > 0:
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
    # Gap up but candle closed down = gap rejection → PUT
    if gap_pct > 0 and last["close"] < last["open"]:
        score -= 2
        reasons.append(f"GAP:-2 PUT gap-up-rejected {gap_pct:.5f}")
    # Gap down but candle closed up = gap fill → CALL
    elif gap_pct < 0 and last["close"] > last["open"]:
        score += 2
        reasons.append(f"GAP:+2 CALL gap-down-filled {gap_pct:.5f}")
    # Gap up + bullish candle = continuation → CALL
    elif gap_pct > 0 and last["close"] > last["open"]:
        score += 2
        reasons.append(f"GAP:+2 CALL gap-up-continue {gap_pct:.5f}")
    # Gap down + bearish candle = continuation → PUT
    elif gap_pct < 0 and last["close"] < last["open"]:
        score -= 2
        reasons.append(f"GAP:-2 PUT gap-down-continue {gap_pct:.5f}")

    return ("CALL" if score > 0 else "PUT", score, reasons)


def _theory_last(candles, ticks, muted):
    """LAST - Last-portion exhaustion/recovery (simplified)."""
    if "LAST" in muted:
        return None
    if not ticks or len(ticks) < 10:
        return None

    n = len(ticks)
    last_n = max(n // 6, 3)
    fin = ticks[-last_n:]
    fi_up = sum(1 for i in range(1, len(fin)) if fin[i] > fin[i - 1])
    fi_dn = sum(1 for i in range(1, len(fin)) if fin[i] < fin[i - 1])
    fi_tot = fi_up + fi_dn
    if fi_tot < 2:
        return None

    op = ticks[0]
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
            # Price near big round number — expect rejection
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

    # Volatility: compare recent ATR to older ATR
    recent_atr = _atr(candles[-5:], 5)
    older_atr = _atr(candles[-20:-5], 5) if len(candles) >= 20 else recent_atr
    vol_ratio = recent_atr / older_atr if older_atr > 0 else 1

    if regime["trend"] == "SIDEWAYS" and vol_ratio < 0.7:
        state = "QUIET"
        score = 0  # No edge in quiet sideways
    elif regime["trend"] != "SIDEWAYS" and vol_ratio > 1.3:
        state = "VOLATILE"
        # In volatile trend, momentum tends to continue
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

    # Build micro from ticks if running_ticks provided
    micro = None
    if running_ticks and len(running_ticks) >= 10:
        op = candles[-1]["close"] if candles else running_ticks[0]
        micro = _build_micro_from_ticks(running_ticks, op)

    # Run each theory
    theories = [
        ("CON", lambda: _theory_con(candles, muted)),
        ("REV", lambda: _theory_rev(candles, muted)),
        ("RUN", lambda: _theory_run(candles, ticks, micro, muted)),
        ("TRAP", lambda: _theory_trap(candles, ticks, muted)),
        ("GAP", lambda: _theory_gap(candles, muted)),
        ("LAST", lambda: _theory_last(candles, ticks, muted)),
        ("RNG", lambda: _theory_rng(candles, muted)),
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

    # Blend
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

    # Strength
    if confidence >= 70 and abs(net) >= 5:
        strength = "STRONG"
    elif confidence >= 55:
        strength = "MEDIUM"
    else:
        strength = "WEAK"

    # Dead band: if net is very small relative to total, NEUTRAL
    if abs(net) < 2 or confidence < 45:
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


def _build_micro_from_ticks(ticks, open_price):
    """Build microstructure dict from tick list (for running_ticks param)."""
    ticks = list(ticks)
    if len(ticks) < 10:
        return None
    op = open_price
    hi = max(ticks)
    lo = min(ticks)
    cur = ticks[-1]
    rng = hi - lo

    up_t = sum(1 for i in range(1, len(ticks)) if ticks[i] > ticks[i - 1])
    dn_t = sum(1 for i in range(1, len(ticks)) if ticks[i] < ticks[i - 1])
    moves = up_t + dn_t
    buy_pct = round(up_t / moves * 100) if moves else 50

    if buy_pct >= 62:
        pressure = "BUYER"
    elif 100 - buy_pct >= 62:
        pressure = "SELLER"
    else:
        pressure = "FIGHT"

    mid = (hi + lo) / 2
    crosses = sum(1 for i in range(1, len(ticks))
                  if (ticks[i - 1] < mid) != (ticks[i] < mid))
    is_fight = crosses >= 4

    n = len(ticks)
    t3 = max(n // 3, 1)
    early = ticks[t3] - ticks[0]
    mid_m = ticks[2 * t3] - ticks[t3]
    late = ticks[-1] - ticks[2 * t3]

    def _dir(v):
        return "UP" if v > 0 else ("DOWN" if v < 0 else "FLAT")

    phases = [_dir(early), _dir(mid_m), _dir(late)]

    reaction = None
    if rng > 0:
        from_hi = (hi - cur) / rng
        from_lo = (cur - lo) / rng
        net = cur - op
        late_q = max(n // 4, 2)
        late_move = ticks[-1] - ticks[-late_q]
        if from_hi > 0.50 and late_move <= 0 and net < 0:
            reaction = "SELLER"
        elif from_lo > 0.50 and late_move >= 0 and net > 0:
            reaction = "BUYER"

    last_react = None
    if n >= 15:
        last_n2 = max(n // 6, 6)
        fin2 = ticks[-last_n2:]
        fi2_up = sum(1 for i in range(1, len(fin2)) if fin2[i] > fin2[i - 1])
        fi2_dn = sum(1 for i in range(1, len(fin2)) if fin2[i] < fin2[i - 1])
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

    return {
        "buy_pct": buy_pct, "sell_pct": 100 - buy_pct,
        "pressure": pressure, "is_fight": is_fight, "crosses": crosses,
        "phases": phases, "reaction": reaction, "net": round(cur - op, 6),
        "tick_count": n, "last_react": last_react,
    }