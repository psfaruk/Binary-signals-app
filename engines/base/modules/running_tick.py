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

# FIX (DEEP-AUDIT-2026-07-26 / F-10-31, A-05 P99): module-level constants
# for the magic numbers in this module. Kept inline-equivalent for backward
# compat — the values are unchanged, just named for readability.
ATR_FALLBACK = 0.0001
# Cap the backward scan for prior-direction detection — the last non-doji
# candle in a realistic market is within 30 candles of the current one.
# Scanning the full candle list was O(N) per call (PROBLEM 34).
PRIOR_DIR_SCAN_CAP = 30


def analyze(candles, ticks, micro, ctx: MarketContext) -> list:
    """Analyze running candle tick microstructure.

    Returns list with 0 or 1 ModuleResult (composite vote).

    FIX (DEEP-AUDIT-2026-07-26 / F-10-32, A-05 P10/S6): the `ticks`
    parameter is unused but kept in the signature for API stability —
    `engines/base/blender.py:134` calls `mod_tick.analyze(candles, ticks,
    micro, ctx)` with 4 positional args. Removing `ticks` would require a
    coordinated change to the blender. All other modules use 2-arg
    `(candles, ctx)` or 3-arg `(candles, ctx, asset)` signatures; this is
    the only 4-arg module. The parameter is acknowledged as dead via the
    `_ = ticks` statement below so linters don't flag it.
    """
    # Acknowledge the unused `ticks` parameter (kept for API stability).
    _ = ticks
    if not micro:
        return []

    sub_votes = []  # (direction, score, reason)
    atr = ctx.atr if ctx.atr > 0 else ATR_FALLBACK

    # FIX (THEORY-LOGIC-FIX-2026-08-03): read regime for composite type classification.
    # Previously the composite type (CONTINUATION vs REVERSAL) was based solely on
    # the prior candle's body direction. In a strong TREND_UP, if the prior candle
    # was a small DOWN pullback and the running candle resumes upward, the composite
    # voted CALL (correct) but classified it as REVERSAL (wrong — should be CONTINUATION).
    # This caused the blender to penalize trend-continuation votes by ~46% (×0.7 vs ×1.3).
    # Now we use regime when available, falling back to prior-candle comparison in RANGE.
    regime = ctx.regime
    is_trending = regime.get("is_trending", False)
    trend_regime = regime.get("regime", "RANGE")
    trend_strength = regime.get("trend_strength", 0.0)

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
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-36): align threshold with
        # SUB-SIGNAL 1 (line 55) which uses >= 65. The previous >= 70 here
        # meant a microstructure with ed_buy=68 and buy_pct=68 got score 3
        # from SUB-SIGNAL 1 but only score 2 from SUB-SIGNAL 2 — an
        # inconsistent score for the same pressure level.
        score = 3 if buy_pct >= 65 else 2
        sub_votes.append(("CALL", score, f"buyer pressure ({buy_pct}%)"))
    elif pressure == "SELLER":
        sell_pct = 100 - buy_pct
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-36): same threshold fix
        # as the BUYER branch above (>= 65 instead of >= 70).
        score = 3 if sell_pct >= 65 else 2
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
            # FIX (PROD-BACKTEST-2026-08-05 / FIX-8): flipped PUT → CALL.
            # Production data (7,699 signals, 2026-08-04..08-05):
            #   VAP migration PUT (n=954) won 48.43%; CALL would win 51.57%.
            #   Lift from flip = +3.14pp, n >= 150 threshold met.
            # Textbook says "VAP migrating down = sellers in control = PUT",
            # but 1-minute binary-option data shows this is a mean-reversion
            # signal: after VAP migrates down, next candle tends to revert UP.
            score = 2 if vap_pct < -0.40 else 1
            sub_votes.append(("CALL", score,
                f"VAP migrating DOWN ({vap_pct:.0%}) → mean-reversion CALL"))

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
            # FIX (PROD-BACKTEST-2026-08-05 / FIX-2): flipped PUT → CALL.
            # Production data (7,699 signals, 2026-08-04..08-05):
            #   V-shape reversal PUT (V_TOP, n=273) won 45.05%; CALL would win 54.95%.
            #   Lift from flip = +9.89pp, n >= 150 threshold met.
            # Textbook says "V-top = bearish reversal = PUT", but 1-minute
            # binary-option data shows the next candle mean-reverts UP after
            # a V-top, not down. This is one of the strongest edges in the
            # data — only Tweezer Bottom (n=487) has a similar effect size.
            sub_votes.append(("CALL", 3,
                "V-top: sharp up then sharp down → mean-reversion CALL"))

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
                # FIX (PROD-BACKTEST-2026-08-05 / FIX-3): flipped PUT → CALL.
                # Production data (7,699 signals, 2026-08-04..08-05):
                #   Tick acceleration PUT (n=536) won 46.46%; CALL would win 53.54%.
                #   Lift from flip = +7.09pp, n >= 150 threshold met.
                # Tick acceleration DOWN in the late candle tends to mark
                # exhaustion of the down-move, not continuation — the next
                # candle mean-reverts UP. Mirror of the V-shape V_TOP edge.
                sub_votes.append(("CALL", 2,
                    f"tick acceleration DOWN (accel={accel:.1f}x) → mean-reversion CALL"))
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
            # FIX (PROD-BACKTEST-2026-08-05 / FIX-4): flipped CALL → PUT.
            # Production data (7,699 signals, 2026-08-04..08-05):
            #   Last-N exhaustion CALL (net<0, n=116) won 46.55%; PUT would win 53.45%.
            #   Lift from flip = +6.90pp. n < 150 but lift is large.
            # microstructure.py:240-242 conflates two opposite sub-patterns
            # under EXHAUST+net<0: true exhaustion (fbp2>=0.70, reversal UP)
            # and capitulation (fbp2<=0.10, continuation DOWN). The aggregate
            # loses as CALL because the capitulation sub-pattern dominates.
            # Cleaner fix would be to split EXHAUST into two labels in
            # microstructure.py; pragmatic fix is to flip the whole branch.
            sub_votes.append(("PUT", 2,
                "last-N exhaustion after down move → continuation PUT"))
    elif last_react == "RECOVERY":
        # Production data (7,699 signals, 2026-08-04..08-05) split by sub-case:
        #
        #   RECOVERY + net<0 ("after down move"): CALL wins 52.83% (n=1378)
        #     → KEEP CALL. Task 2-b's semantic analysis predicted this branch
        #       should be PUT (continuation of down-move), but the data shows
        #       CALL wins. The dev's AUDIT-4-37 fix on this branch was correct.
        #
        #   RECOVERY + net>0 ("continuing up move"): CALL wins 48.99% (n=1439)
        #     → FLIP to PUT (PUT would win 51.01%, lift = +2.02pp).
        #     The dev's AUDIT-4-37 fix on this branch was WRONG — the previous
        #     PUT vote was correct. Production data shows that when an up-move
        #     candle's recent ticks are 55-85% up (with 2+ down ticks for
        #     two-way action), the next candle tends to mean-revert DOWN.
        if net < 0:
            sub_votes.append(("CALL", 1,
                "last-N recovery after down move → weak CALL"))
        elif net > 0:
            # FIX (PROD-BACKTEST-2026-08-05 / FIX-9): revert AUDIT-4-37's
            # CALL back to PUT. n=1439 gives the highest statistical
            # confidence of any fix in this commit.
            sub_votes.append(("PUT", 1,
                "last-N recovery after up move → mean-reversion PUT"))

    # ═══════════════════════════════════════════════════════════════════════
    # SUB-SIGNAL 12: Phase momentum alignment (NEW)
    # All 3 phases (early/mid/late) same direction = strong trend
    # ═══════════════════════════════════════════════════════════════════════
    phases = micro.get("phases", [])
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-38): use `>= 3` instead of
    # strict `== 3` and check the LAST 3 phases via `phases[-3:]`. The
    # microstructure.phases function should always return 3, but defensively
    # this prevents the signal from being silently dropped if it ever returns
    # a different length (cold-start, unusual cases, future bugs).
    if len(phases) >= 3:
        p = phases[-3:]
        if p[0] == "UP" and p[1] == "UP" and p[2] == "UP":
            sub_votes.append(("CALL", 2,
                "all 3 phases UP → strong bullish momentum"))
        elif p[0] == "DOWN" and p[1] == "DOWN" and p[2] == "DOWN":
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

    # FIX (THEORY-TRACKING-2026-08-05): tag each sub-signal with the direction
    # it voted. Previously this joined only the reason text, so once the 12
    # sub-signals were collapsed into one composite vote there was no way to
    # recover which sub-signal wanted CALL and which wanted PUT — and
    # db._extract_theory_votes had no `running_tick` entry at all, so the
    # module that fires on 96% of live signals contributed ZERO rows to
    # theory_votes. Every per-theory report was therefore built from the three
    # modules that drive ~25% of signals, while the dominant one was invisible.
    # It also cannot be backtested (Quotex history is OHLC only, no ticks), so
    # live logging is the only way to ever measure these sub-signals.
    #
    # The `[CALL]`/`[PUT]` tag is parsed back out in db._extract_theory_votes.
    # It sits AFTER the `[running_tick]` module prefix that blender.py adds, so
    # the existing "module name = text between the first [ ]" parse is
    # unaffected.
    reasons_str = " | ".join(f"[{d}] {r}" for d, _, r in sub_votes)

    if call_sum == put_sum:
        return []  # exact tie — no vote

    # Determine prior direction for CONTINUATION vs REVERSAL classification
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-34): look back further to find
    # the last non-doji candle. The previous check only looked at candles[-2];
    # if the prior candle was a doji (prev_body == 0), prior_dir stayed 0 and
    # the composite was classified as "REVERSAL fresh-direction" with a score
    # penalty. But a doji after a long UP streak is just a pause — the prior
    # TREND was UP. Now we scan backward to find the last non-doji candle.
    #
    # FIX (DEEP-AUDIT-2026-07-26 / F-10-33, A-05 P34/HIGH): cap the backward
    # scan at PRIOR_DIR_SCAN_CAP candles (30) — the original `range(len-2,
    # -1, -1)` was O(N) per call, scanning the entire candle list. For 1000+
    # candles, that's a hot-path performance issue. The prior non-doji in a
    # realistic market is within 30 candles of the current one (doji streaks
    # longer than 30 are statistically negligible on 1m forex candles).
    # Early-exit guard preserves the original semantics for the common case
    # while bounding the worst-case scan to O(30).
    prior_dir = 0  # 1=up, -1=down, 0=doji/unknown (only if all are dojis)
    _scan_start = max(0, len(candles) - 2 - PRIOR_DIR_SCAN_CAP)
    for _i in range(len(candles) - 2, _scan_start - 1, -1):
        _prev = candles[_i]
        _prev_body = _prev["close"] - _prev["open"]
        if _prev_body > 0:
            prior_dir = 1
            break
        elif _prev_body < 0:
            prior_dir = -1
            break
        # else: doji, keep looking back (up to PRIOR_DIR_SCAN_CAP)

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
        # FIX (THEORY-LOGIC-FIX-2026-08-03): use regime for composite type when trending.
        # Previously used prior candle only — a single counter-trend pullback candle
        # caused the composite to be mislabeled as REVERSAL, triggering the blender's
        # trend-reversal penalty on what was actually trend-continuation.
        if is_trending and trend_strength > 0.5:
            # In a strong trend, classify based on whether the vote aligns with the trend
            if trend_regime == "TREND_UP":
                composite_type = "CONTINUATION"  # CALL in TREND_UP = continuation
                type_reason = f"continues TREND_UP (str={trend_strength:.2f})"
            elif trend_regime == "TREND_DOWN":
                composite_type = "REVERSAL"  # CALL in TREND_DOWN = counter-trend reversal
                type_reason = f"reverses TREND_DOWN (str={trend_strength:.2f})"
                composite_score = max(1, composite_score - 1)  # dampen counter-trend
            else:
                composite_type = "REVERSAL"
                type_reason = "fresh-direction (volatile regime)"
        else:
            # RANGE/VOLATILE — fall back to prior-candle comparison (original logic)
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
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-35): weight confidence by
        # the MAX sub-signal score (depth) instead of the COUNT of agreeing
        # sub-signals (breadth). The previous formula `composite_score * 12 +
        # call_n * 2` rewarded many weak sub-signals over few strong ones —
        # 5 sub-signals of score 1 (composite_score=6, call_n=5) got conf 70
        # while 1 sub-signal of score 5 (composite_score=5, call_n=1) got conf
        # 62. The strong-signal case is more reliable but got LOWER
        # confidence. Now uses max_sub_signal_score so depth is rewarded over
        # breadth. composite_score multiplier raised 12→14 to compensate.
        max_sub_signal_score = max((s for d, s, _ in sub_votes if d == "CALL"), default=0)
        confidence = min(70, composite_score * 14 + max_sub_signal_score * 2)
        return [ModuleResult(
            module_name="running_tick", direction="CALL", score=composite_score,
            confidence=confidence,
            signal_type=composite_type, reliability="MICRO", group="MICRO",
            reasons=[f"Micro composite CALL ({type_reason}, {call_n} signals): {reasons_str}"])]

    # put_sum > call_sum
    net_diff = put_sum - call_sum
    breadth_bonus = min(2, put_n // 3)
    composite_score = min(6, net_diff + breadth_bonus)
    # FIX (THEORY-LOGIC-FIX-2026-08-03): use regime for composite type when trending (mirror of CALL branch)
    if is_trending and trend_strength > 0.5:
        if trend_regime == "TREND_DOWN":
            composite_type = "CONTINUATION"  # PUT in TREND_DOWN = continuation
            type_reason = f"continues TREND_DOWN (str={trend_strength:.2f})"
        elif trend_regime == "TREND_UP":
            composite_type = "REVERSAL"  # PUT in TREND_UP = counter-trend reversal
            type_reason = f"reverses TREND_UP (str={trend_strength:.2f})"
            composite_score = max(1, composite_score - 1)  # dampen counter-trend
        else:
            composite_type = "REVERSAL"
            type_reason = "fresh-direction (volatile regime)"
    else:
        # RANGE/VOLATILE — fall back to prior-candle comparison (original logic)
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
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-35): same depth-over-breadth
    # fix as the CALL branch above (use max_sub_signal_score instead of put_n).
    max_sub_signal_score = max((s for d, s, _ in sub_votes if d == "PUT"), default=0)
    confidence = min(70, composite_score * 14 + max_sub_signal_score * 2)
    return [ModuleResult(
        module_name="running_tick", direction="PUT", score=composite_score,
        confidence=confidence,
        signal_type=composite_type, reliability="MICRO", group="MICRO",
        reasons=[f"Micro composite PUT ({type_reason}, {put_n} signals): {reasons_str}"])]
