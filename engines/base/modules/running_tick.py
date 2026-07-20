"""
Module 2: Running Candle Tick Engine (UPGRADED 2026-07-20)

Analyzes the running candle's tick-level microstructure. Collapses
multiple sub-signals into ONE composite vote to avoid confidence inflation.

UPGRADE: Now uses ALL microstructure features from core/microstructure.py:
  1. Ending direction (last 10 ticks — UP/BUYER or DOWN/SELLER)
  2. Buyer/seller pressure (tick-weighted volume, ≥65% = strong)
  3. Reaction (visited extreme then reversed)
  4. Order flow imbalance (big ticks vs small ticks divergence)
  5. VAP migration (volume profile moving up/down)
  6. V-shape detection (V-top / V-bottom reversal)
  7. Momentum shift (direction change in last third)
  8. Tick velocity acceleration/deceleration
  9. Live wick rejection (real-time wick formation)
  10. Time-decay pressure divergence (recent vs overall)
  11. Last-N tick exhaustion/recovery
  12. Phase momentum (early/mid/late thirds alignment)

All sub-signals come from the same tick data source → collapsed into 1 vote.
The composite score scales with how many sub-signals agree (breadth) and
how strong each is (depth).

FIX (2026-07-18): composite_type determined by comparing vote direction
against the PRIOR CLOSED candle's body direction.
FIX (BUG-A, 2026-07-20): prior-doji classified as REVERSAL fresh-direction.
FIX (UPGRADE, 2026-07-20): added 9 new sub-signals from microstructure.py
that were previously computed but never read. Expected to increase signal
count from ~78 to ~300+ and improve win rate from 56.4% to 58-62%.
"""
from engines.base.types import ModuleResult, MarketContext


def analyze(candles, ticks, micro, ctx: MarketContext) -> list:
    """Analyze running candle tick microstructure.

    Returns list with 0 or 1 ModuleResult (composite vote).
    """
    if not micro:
        return []

    sub_votes = []  # (direction, score, reason)
    atr = ctx.atr if ctx.atr > 0 else 0.0001

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 1: Ending direction (last 10 ticks)
    # ═══════════════════════════════════════════════════════════════════════
    ed = micro.get("ending_direction", {})
    ed_dir = ed.get("direction", "FLAT")
    ed_dom = ed.get("dominance", "FIGHT")
    ed_buy = ed.get("buy_pct", 50)

    if ed_dir == "UP" and ed_dom == "BUYER":
        score = 3 if ed_buy >= 65 else 2
        sub_votes.append(("CALL", score, f"ending UP/BUYER ({ed_buy}%)"))
    elif ed_dir == "DOWN" and ed_dom == "SELLER":
        sell_pct = 100 - ed_buy
        score = 3 if sell_pct >= 65 else 2
        sub_votes.append(("PUT", score, f"ending DOWN/SELLER ({sell_pct}%)"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 2: Buyer/seller pressure (tick-weighted volume)
    # ═══════════════════════════════════════════════════════════════════════
    buy_pct = micro.get("buy_pct", 50)
    pressure = micro.get("pressure", "FIGHT")
    if pressure == "BUYER":
        score = 3 if buy_pct >= 70 else 2
        sub_votes.append(("CALL", score, f"buyer pressure ({buy_pct}%)"))
    elif pressure == "SELLER":
        sell_pct = 100 - buy_pct
        score = 3 if sell_pct >= 70 else 2
        sub_votes.append(("PUT", score, f"seller pressure ({sell_pct}%)"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 3: Reaction (visited extreme then reversed)
    # ═══════════════════════════════════════════════════════════════════════
    reaction = micro.get("reaction")
    if reaction == "BUYER":
        sub_votes.append(("CALL", 2, "buyer reaction from low"))
    elif reaction == "SELLER":
        sub_votes.append(("PUT", 2, "seller reaction from high"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 4: Order flow imbalance (NEW)
    # Big ticks one direction + small ticks other = institutional activity
    # ═══════════════════════════════════════════════════════════════════════
    orderflow = micro.get("orderflow")
    if orderflow and isinstance(orderflow, dict):
        imbalance = orderflow.get("imbalance", 0)
        big_dir = orderflow.get("big_dir", "FLAT")
        big_buy_pct = orderflow.get("big_buy_pct", 50)
        if imbalance == 1 and big_dir != "FLAT":
            # Big ticks pushing one way, small ticks other way = smart money
            if big_dir == "UP" and big_buy_pct >= 60:
                score = 3 if big_buy_pct >= 70 else 2
                sub_votes.append(("CALL", score,
                    f"orderflow: big ticks UP ({big_buy_pct}%), small ticks DOWN → smart money CALL"))
            elif big_dir == "DOWN" and big_buy_pct <= 40:
                sell_pct = 100 - big_buy_pct
                score = 3 if sell_pct >= 70 else 2
                sub_votes.append(("PUT", score,
                    f"orderflow: big ticks DOWN ({sell_pct}%), small ticks UP → smart money PUT"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 5: VAP migration (NEW)
    # Volume profile shifting up/down = where price will likely go
    # ═══════════════════════════════════════════════════════════════════════
    vap = micro.get("vap_migration")
    if vap and isinstance(vap, dict):
        vap_dir = vap.get("dir", "FLAT")
        vap_pct = vap.get("pct", 0)
        if vap_dir == "UP" and vap_pct > 0.30:
            score = 2 if vap_pct > 0.40 else 1
            sub_votes.append(("CALL", score,
                f"VAP migrating UP ({vap_pct:.0%}) → buyers in control"))
        elif vap_dir == "DOWN" and vap_pct < -0.30:
            score = 2 if vap_pct < -0.40 else 1
            sub_votes.append(("PUT", score,
                f"VAP migrating DOWN ({vap_pct:.0%}) → sellers in control"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 6: V-shape detection (NEW)
    # V-bottom = reversal up, V-top = reversal down
    # ═══════════════════════════════════════════════════════════════════════
    v_shape = micro.get("v_shape")
    if v_shape:
        if v_shape == "V_BOTTOM":
            sub_votes.append(("CALL", 3,
                "V-bottom: sharp down then sharp up → reversal CALL"))
        elif v_shape == "V_TOP":
            sub_votes.append(("PUT", 3,
                "V-top: sharp up then sharp down → reversal PUT"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 7: Momentum shift (NEW)
    # Direction change in last third of candle
    # ═══════════════════════════════════════════════════════════════════════
    momentum_shift = micro.get("momentum_shift")
    if momentum_shift == "BULL_SHIFT":
        sub_votes.append(("CALL", 2,
            "momentum shift: early DOWN → late UP → bullish reversal"))
    elif momentum_shift == "BEAR_SHIFT":
        sub_votes.append(("PUT", 2,
            "momentum shift: early UP → late DOWN → bearish reversal"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 8: Tick velocity acceleration (NEW)
    # Accelerating ticks = momentum building, decelerating = exhaustion
    # ═══════════════════════════════════════════════════════════════════════
    last_velocity = micro.get("last_velocity")
    if last_velocity and isinstance(last_velocity, dict):
        accel = last_velocity.get("accel", 1.0)
        dir5 = last_velocity.get("dir5", "FLAT")
        dir10 = last_velocity.get("dir10", "FLAT")
        spd5 = last_velocity.get("spd5", 0)
        # Accelerating in a direction = strong momentum
        if accel > 1.5 and dir5 == dir10 and dir5 != "FLAT":
            # Strong acceleration — momentum continuing
            if dir5 == "UP" and abs(spd5) > atr * 0.01:
                sub_votes.append(("CALL", 2,
                    f"tick acceleration UP (accel={accel:.1f}x) → momentum CALL"))
            elif dir5 == "DOWN" and abs(spd5) > atr * 0.01:
                sub_votes.append(("PUT", 2,
                    f"tick acceleration DOWN (accel={accel:.1f}x) → momentum PUT"))
        elif accel < 0.5 and dir10 != "FLAT":
            # Decelerating — exhaustion, reversal likely
            if dir10 == "UP":
                sub_votes.append(("PUT", 1,
                    f"tick deceleration (accel={accel:.1f}x) after UP → exhaustion PUT"))
            elif dir10 == "DOWN":
                sub_votes.append(("CALL", 1,
                    f"tick deceleration (accel={accel:.1f}x) after DOWN → exhaustion CALL"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 9: Live wick rejection (NEW)
    # Real-time wick forming = rejection happening NOW
    # ═══════════════════════════════════════════════════════════════════════
    live_wick = micro.get("live_wick")
    if live_wick and isinstance(live_wick, dict):
        wick_type = live_wick.get("type")
        lw_ratio = live_wick.get("lw_ratio", 0)
        uw_ratio = live_wick.get("uw_ratio", 0)
        if wick_type == "BULL_REJECT" and lw_ratio > 0.40:
            score = 3 if lw_ratio > 0.55 else 2
            sub_votes.append(("CALL", score,
                f"live bull wick (lower={lw_ratio:.0%}) → real-time CALL rejection"))
        elif wick_type == "BEAR_REJECT" and uw_ratio > 0.40:
            score = 3 if uw_ratio > 0.55 else 2
            sub_votes.append(("PUT", score,
                f"live bear wick (upper={uw_ratio:.0%}) → real-time PUT rejection"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 10: Time-decay pressure divergence (NEW)
    # Recent pressure differs from overall = shift happening
    # ═══════════════════════════════════════════════════════════════════════
    td_buy_pct = micro.get("td_buy_pct", 50)
    td_diverge = micro.get("td_diverge", False)
    if td_diverge:
        # Recent pressure differs from overall by >=20pp
        if td_buy_pct > buy_pct + 20:
            sub_votes.append(("CALL", 2,
                f"time-decay: recent buyer surge ({td_buy_pct}% vs {buy_pct}%) → shift CALL"))
        elif td_buy_pct < buy_pct - 20:
            sub_votes.append(("PUT", 2,
                f"time-decay: recent seller surge ({100-td_buy_pct}% vs {100-buy_pct}%) → shift PUT"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 11: Last-N tick exhaustion/recovery (NEW)
    # ═══════════════════════════════════════════════════════════════════════
    last_react = micro.get("last_react")
    net = micro.get("net", 0)
    if last_react == "EXHAUST":
        # Net move is up but recent ticks show exhaustion → reversal
        if net > 0:
            sub_votes.append(("PUT", 2,
                "last-N exhaustion after up move → reversal PUT"))
        elif net < 0:
            sub_votes.append(("CALL", 2,
                "last-N exhaustion after down move → reversal CALL"))
    elif last_react == "RECOVERY":
        # Net move is down but recent ticks show recovery → reversal
        if net < 0:
            sub_votes.append(("CALL", 1,
                "last-N recovery after down move → weak CALL"))
        elif net > 0:
            sub_votes.append(("PUT", 1,
                "last-N recovery after up move → weak PUT"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 12: Phase momentum alignment (NEW)
    # All 3 phases (early/mid/late) same direction = strong trend
    # ═══════════════════════════════════════════════════════════════════════
    phases = micro.get("phases", [])
    if len(phases) == 3:
        if phases[0] == "UP" and phases[1] == "UP" and phases[2] == "UP":
            sub_votes.append(("CALL", 2,
                "all 3 phases UP → strong bullish momentum"))
        elif phases[0] == "DOWN" and phases[1] == "DOWN" and phases[2] == "DOWN":
            sub_votes.append(("PUT", 2,
                "all 3 phases DOWN → strong bearish momentum"))

    # ═══════════════════════════════════════════════════════════════════════
    # COLLAPSE INTO ONE COMPOSITE VOTE
    # ═══════════════════════════════════════════════════════════════════════
    if not sub_votes:
        return []

    call_sum = sum(s for d, s, _ in sub_votes if d == "CALL")
    put_sum = sum(s for d, s, _ in sub_votes if d == "PUT")
    call_n = sum(1 for d, s, _ in sub_votes if d == "CALL")
    put_n = sum(1 for d, s, _ in sub_votes if d == "PUT")

    reasons_str = " | ".join(r for _, _, r in sub_votes)

    if call_sum == put_sum:
        return []  # exact tie — no vote

    # Determine prior direction for CONTINUATION vs REVERSAL classification
    prior_dir = 0  # 1=up, -1=down, 0=doji/unknown
    if len(candles) >= 2:
        prev = candles[-2]
        prev_body = prev["close"] - prev["open"]
        if prev_body > 0:
            prior_dir = 1
        elif prev_body < 0:
            prior_dir = -1

    # Composite score scales with:
    # 1. Net score difference (depth)
    # 2. Number of agreeing sub-signals (breadth)
    # Old: min(4, call_sum - put_sum)
    # New: min(6, net_diff + breadth_bonus)
    # This rewards predictions where many sub-signals agree
    if call_sum > put_sum:
        net_diff = call_sum - put_sum
        breadth_bonus = min(2, call_n // 3)  # +1 per 3 agreeing signals, max +2
        composite_score = min(6, net_diff + breadth_bonus)
        if prior_dir == 1:
            composite_type = "CONTINUATION"
            type_reason = "continues prior up"
        elif prior_dir == -1:
            composite_type = "REVERSAL"
            type_reason = "reverses prior down"
        else:
            composite_type = "REVERSAL"
            type_reason = "prior doji, fresh-direction"
            composite_score = max(1, composite_score - 1)
        confidence = min(70, composite_score * 12 + call_n * 2)
        return [ModuleResult(
            module_name="running_tick", direction="CALL", score=composite_score,
            confidence=confidence,
            signal_type=composite_type, reliability="MICRO", group="MICRO",
            reasons=[f"Micro composite CALL ({type_reason}, {call_n} signals): {reasons_str}"])]

    # put_sum > call_sum
    net_diff = put_sum - call_sum
    breadth_bonus = min(2, put_n // 3)
    composite_score = min(6, net_diff + breadth_bonus)
    if prior_dir == -1:
        composite_type = "CONTINUATION"
        type_reason = "continues prior down"
    elif prior_dir == 1:
        composite_type = "REVERSAL"
        type_reason = "reverses prior up"
    else:
        composite_type = "REVERSAL"
        type_reason = "prior doji, fresh-direction"
        composite_score = max(1, composite_score - 1)
    confidence = min(70, composite_score * 12 + put_n * 2)
    return [ModuleResult(
        module_name="running_tick", direction="PUT", score=composite_score,
        confidence=confidence,
        signal_type=composite_type, reliability="MICRO", group="MICRO",
        reasons=[f"Micro composite PUT ({type_reason}, {put_n} signals): {reasons_str}"])]
