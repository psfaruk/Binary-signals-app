"""
Candle Reaction Engine — pure price action, NO theories.

Based on Quotex OTC algorithm findings from 200-candle analysis:
  1. Mean reversion: 3+ same-direction candles → reversal (52-75%)
  2. Big body → reversal (63-64%)
  3. Wick rejection → reversal (56-59%)
  4. Close at range extreme → reversal (60-62%)
  5. Alternation: 53% of time next candle is opposite direction

Signal = weighted sum of reaction signals from the LAST closed candle.
One prediction per candle. No re-evaluation. No theories.
"""
import math


def predict_from_candle(candles, ticks=None, micro=None):
    """
    Predict next candle direction from the last closed candle's reaction.

    Args:
        candles: list of closed candle dicts (time, open, high, low, close)
        ticks: tick list for the closed candle (optional, for micro)
        micro: microstructure dict (optional, for ending direction)

    Returns dict with:
        signal: "CALL" | "PUT" | "NEUTRAL"
        confidence: 0-100
        strength: "STRONG" | "MEDIUM" | "NEUTRAL"
        score: net score (positive=CALL, negative=PUT)
        reasons: list of reason strings
    """
    if not candles or len(candles) < 3:
        return {"signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
                "score": 0, "reasons": ["INSUFFICIENT_DATA"]}

    last = candles[-1]
    o, h, l, c = last["open"], last["high"], last["low"], last["close"]
    body = c - o
    rng = h - l
    body_pct = abs(body) / rng * 100 if rng > 0 else 0

    call_score = 0
    put_score = 0
    reasons = []

    # ═══════════════════════════════════════════════════════════════════
    # SIGNAL 1: Consecutive streak reversal (62-75% accuracy)
    # 3+ same-direction candles → reversal
    # ═══════════════════════════════════════════════════════════════════
    consec = 1
    for i in range(len(candles) - 2, -1, -1):
        prev_body = candles[i]["close"] - candles[i]["open"]
        if (body > 0 and prev_body > 0) or (body < 0 and prev_body < 0):
            consec += 1
        else:
            break

    if consec >= 5:
        # 5+ same direction → 75% reversal
        if body > 0:
            put_score += 4
            reasons.append(f"5+ UP streak → PUT reversal (75% win rate)")
        else:
            call_score += 4
            reasons.append(f"5+ DOWN streak → CALL reversal (75% win rate)")
    elif consec >= 4:
        # 4+ same direction → 60% reversal
        if body > 0:
            put_score += 3
            reasons.append(f"4+ UP streak → PUT reversal (60% win rate)")
        else:
            call_score += 3
            reasons.append(f"4+ DOWN streak → CALL reversal (60% win rate)")
    elif consec >= 3:
        # 3+ same direction → 52-62% reversal
        if body > 0:
            put_score += 2
            reasons.append(f"3+ UP streak → PUT reversal (62% win rate)")
        else:
            call_score += 2
            reasons.append(f"3+ DOWN streak → CALL reversal (62% win rate)")

    # ═══════════════════════════════════════════════════════════════════
    # SIGNAL 2: Big body → reversal (63-64% accuracy)
    # Body > 1.5× median body = big → opposite direction next
    # ═══════════════════════════════════════════════════════════════════
    if len(candles) >= 10:
        recent_bodies = [abs(candles[i]["close"] - candles[i]["open"])
                        for i in range(-min(20, len(candles)), 0)]
        median_body = sorted(recent_bodies)[len(recent_bodies) // 2]

        if median_body > 0 and abs(body) > median_body * 1.5:
            if body > 0:
                put_score += 3
                reasons.append(f"Big UP body ({body_pct:.0f}%) → PUT reversal (64% win rate)")
            else:
                call_score += 3
                reasons.append(f"Big DOWN body ({body_pct:.0f}%) → CALL reversal (63% win rate)")

    # ═══════════════════════════════════════════════════════════════════
    # SIGNAL 3: Wick rejection (56-59% accuracy)
    # Upper wick > 40% of range + small body → bearish rejection → PUT
    # Lower wick > 40% of range + small body → bullish rejection → CALL
    # ═══════════════════════════════════════════════════════════════════
    if rng > 0:
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        uw_pct = upper_wick / rng * 100
        lw_pct = lower_wick / rng * 100

        if uw_pct > 40 and body_pct < 35:
            put_score += 3
            reasons.append(f"Upper wick rejection ({uw_pct:.0f}%) → PUT (59% win rate)")
        elif lw_pct > 40 and body_pct < 35:
            call_score += 3
            reasons.append(f"Lower wick rejection ({lw_pct:.0f}%) → CALL (56% win rate)")

    # ═══════════════════════════════════════════════════════════════════
    # SIGNAL 4: Close position in range (60-62% accuracy)
    # Close in top 20% → PUT (62%)
    # Close in bottom 20% → CALL (60%)
    # ═══════════════════════════════════════════════════════════════════
    if rng > 0:
        close_pos = (c - l) / rng * 100
        if close_pos >= 80:
            put_score += 2
            reasons.append(f"Close at range top ({close_pos:.0f}%) → PUT (62% win rate)")
        elif close_pos <= 20:
            call_score += 2
            reasons.append(f"Close at range bottom ({close_pos:.0f}%) → CALL (60% win rate)")

    # ═══════════════════════════════════════════════════════════════════
    # SIGNAL 5: Body shrinking → exhaustion (54% accuracy)
    # Body < 50% of previous body → trend dying → reversal
    # ═══════════════════════════════════════════════════════════════════
    if len(candles) >= 2:
        prev_body = abs(candles[-2]["close"] - candles[-2]["open"])
        if prev_body > 0 and abs(body) < prev_body * 0.5 and abs(body) > 0:
            if body > 0:
                put_score += 1
                reasons.append(f"Shrinking bull body → PUT exhaustion (54% win rate)")
            else:
                call_score += 1
                reasons.append(f"Shrinking bear body → CALL exhaustion (54% win rate)")

    # ═══════════════════════════════════════════════════════════════════
    # SIGNAL 6: Ending direction (5-second analysis, from micro)
    # Last 10 ticks strongly one direction → continuation bias
    # ═══════════════════════════════════════════════════════════════════
    if micro:
        ed = micro.get("ending_direction", {})
        ed_dir = ed.get("direction", "FLAT")
        ed_dom = ed.get("dominance", "FIGHT")
        ed_buy = ed.get("buy_pct", 50)

        if ed_dir == "UP" and ed_dom == "BUYER":
            call_score += 2
            reasons.append(f"5-sec ending UP/BUYER ({ed_buy}%) → CALL continuation")
        elif ed_dir == "DOWN" and ed_dom == "SELLER":
            put_score += 2
            reasons.append(f"5-sec ending DOWN/SELLER ({ed_buy}%) → PUT continuation")

        # Buyer/seller pressure from micro
        buy_pct = micro.get("buy_pct", 50)
        pressure = micro.get("pressure", "FIGHT")
        if pressure == "BUYER" and buy_pct >= 65:
            call_score += 2
            reasons.append(f"Strong buyer pressure ({buy_pct}%) → CALL")
        elif pressure == "SELLER" and buy_pct <= 35:
            put_score += 2
            reasons.append(f"Strong seller pressure ({buy_pct}%) → PUT")

        # Reaction (visited extreme then reversed)
        reaction = micro.get("reaction")
        if reaction == "BUYER":
            call_score += 2
            reasons.append(f"Buyer reaction from low → CALL")
        elif reaction == "SELLER":
            put_score += 2
            reasons.append(f"Seller reaction from high → PUT")

    # ═══════════════════════════════════════════════════════════════════
    # BLEND
    # ═══════════════════════════════════════════════════════════════════
    net = call_score - put_score
    total = call_score + put_score

    if total == 0 or net == 0:
        return {"signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
                "score": 0, "reasons": reasons or ["NO_SIGNAL"]}

    signal = "CALL" if net > 0 else "PUT"
    confidence = round(abs(net) / total * 100) if total > 0 else 0

    # Strength: based on how many signals agree
    agree = max(call_score, put_score)
    if confidence >= 70 and agree >= 6:
        strength = "STRONG"
    elif confidence >= 60 and agree >= 4:
        strength = "MEDIUM"
    else:
        # No WEAK signals — return NEUTRAL instead
        return {"signal": "NEUTRAL", "confidence": confidence, "strength": "NEUTRAL",
                "score": net, "reasons": reasons + [f"Confidence too low ({confidence}%) → NEUTRAL"]}

    return {
        "signal": signal,
        "confidence": confidence,
        "strength": strength,
        "score": net,
        "reasons": reasons,
        "agree": agree,
        "total": total,
    }
