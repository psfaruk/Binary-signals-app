"""
Module 1: Candle Reaction Engine

Single-candle price action signals from the last closed candle.
Signals are grouped into BODY (correlated) and WICK (independent) groups.

Signals:
  1. Consecutive streak reversal (3+/4+/5+ same-direction)
  2. Big body → reversal (>1.5× median body, Z-score boosted)
  3. Wick rejection (upper/lower wick >40%)
  4. Close position in range (top/bottom 20%, percentile boosted)
  5. Body shrinking → exhaustion (<50% of previous body)

Volatility-scaled thresholds adapt to market noise level.
"""
from engines.base.types import ModuleResult, MarketContext


def analyze(candles, ctx: MarketContext) -> list:
    """Run all single-candle signals.

    Returns list of ModuleResult objects. Body-derived signals share
    group="BODY" (will be collapsed by blender). Wick signals use
    group="WICK" (independent).

    FIX (OTC issue 2, 2026-07-19): the BODY-group reversal signals
    (streak, big body, close-in-range, shrinking body) used to fire
    REVERSAL regardless of trend context. In a strong TREND, a same-
    direction streak + big body + close at the range top is the
    DEFINITION of trend momentum — calling it REVERSAL makes the
    engine structurally incapable of following a trend.

    We now pull `regime` AT THE TOP and compute a `body_aligns_with_trend`
    flag. When the BODY reversal signal's direction OPPOSES the trend
    (i.e. the signal is predicting a reversal back INTO the trend's
    starting direction... no wait, BODY reversal predicts OPPOSITE-of-
    streak-direction = OPPOSITE-of-trend-direction when aligned), we
    dampen the score/confidence. The signal still fires (in case the
    trend IS exhausting), but with weaker conviction so the blender
    doesn't get a strong counter-trend vote.
    """
    results = []
    if not candles or len(candles) < 3:
        return results

    last = candles[-1]
    o, h, l, c = last["open"], last["high"], last["low"], last["close"]
    body = c - o
    rng = h - l
    body_pct = abs(body) / rng * 100 if rng > 0 else 0

    stats = ctx.stats
    vol_pct = ctx.vol_pct

    # FIX (OTC issue 2): pull regime at the TOP so all BODY-group signals
    # can be trend-aware, not just the continuation signals at the bottom.
    regime = ctx.regime
    is_trending = regime.get("is_trending", False)
    trend_regime = regime.get("regime", "RANGE")
    trend_strength = regime.get("trend_strength", 0.0)
    # Does the current candle's body align with the confirmed trend?
    # body > 0 in TREND_UP, or body < 0 in TREND_DOWN.
    body_aligns_with_strong_trend = (
        is_trending
        and trend_strength > 0.5
        and ((trend_regime == "TREND_UP" and body > 0)
             or (trend_regime == "TREND_DOWN" and body < 0))
    )

    # ── Volatility-scaled thresholds ─────────────────────────────────────
    if vol_pct > 1.3:
        streak_thresh_5, streak_thresh_4, streak_thresh_3 = 6, 5, 4
        body_mult = 2.0
    elif vol_pct < 0.7:
        streak_thresh_5, streak_thresh_4, streak_thresh_3 = 4, 3, 2
        body_mult = 1.3
    else:
        streak_thresh_5, streak_thresh_4, streak_thresh_3 = 5, 4, 3
        body_mult = 1.5

    # ── SIGNAL 1: Consecutive streak reversal (BODY group) ───────────────
    # FIX (OTC issue 2, 2026-07-19): when the streak ALIGNS with a strong
    # trend, the reversal interpretation is wrong — a 3-candle up streak
    # in a TREND_UP is momentum, not exhaustion. Soft-gate: dampen score
    # and confidence when aligned-with-strong-trend; full strength only
    # when counter-trend or in a range/volatile regime.
    consec = stats["current_streak"]
    streak_dir = stats["streak_direction"]
    streak_aligns_with_strong_trend = (
        is_trending
        and trend_strength > 0.5
        and ((trend_regime == "TREND_UP" and streak_dir == 1)
             or (trend_regime == "TREND_DOWN" and streak_dir == -1))
    )
    # Dampening factors (applied to score & confidence)
    if streak_aligns_with_strong_trend and trend_strength > 0.7:
        s5_score, s5_conf = 1, 56   # was 4/75 — strong dampen
        s4_score, s4_conf = 1, 53   # was 3/60
        s3_score, s3_conf = 1, 51   # was 2/55
    elif streak_aligns_with_strong_trend:
        s5_score, s5_conf = 2, 62   # moderate dampen
        s4_score, s4_conf = 2, 56
        s3_score, s3_conf = 1, 52
    else:
        s5_score, s5_conf = 4, 75
        s4_score, s4_conf = 3, 60
        s3_score, s3_conf = 2, 55

    if consec >= streak_thresh_5:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=s5_score, confidence=s5_conf,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ UP streak → PUT reversal ({s5_conf}% win rate, rarity={stats['streak_rarity']:.0%}, trend_str={trend_strength:.2f})"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=s5_score, confidence=s5_conf,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ DOWN streak → CALL reversal ({s5_conf}% win rate, rarity={stats['streak_rarity']:.0%}, trend_str={trend_strength:.2f})"]))
    elif consec >= streak_thresh_4:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=s4_score, confidence=s4_conf,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ UP streak → PUT reversal ({s4_conf}% win rate, trend_str={trend_strength:.2f})"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=s4_score, confidence=s4_conf,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ DOWN streak → CALL reversal ({s4_conf}% win rate, trend_str={trend_strength:.2f})"]))
    elif consec >= streak_thresh_3:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=s3_score, confidence=s3_conf,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ UP streak → PUT reversal ({s3_conf}% win rate, trend_str={trend_strength:.2f})"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=s3_score, confidence=s3_conf,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ DOWN streak → CALL reversal ({s3_conf}% win rate, trend_str={trend_strength:.2f})"]))

    # ── SIGNAL 2: Big body → reversal (BODY group, vol-scaled) ───────────
    # FIX (OTC issue 2, 2026-07-19): same trend-aware dampening — a big
    # body IN THE TREND DIRECTION is momentum, not exhaustion.
    # FIX (doji bug, 2026-07-19, AUDIT-ENGINES #14): the previous
    # version fired `if body > 0: PUT else: CALL` — meaning a DOJI
    # (body == 0, which is not > 0) fell into the else branch and fired
    # CALL. A doji is a NEUTRAL candle by definition; calling it a big-
    # body CALL reversal is wrong. Now we use `if body > 0: PUT elif
    # body < 0: CALL` — doji (body == 0) skips this signal entirely.
    # Also: the threshold `abs(body) > median_body * body_mult` is
    # never satisfied by body == 0 anyway (0 > anything_positive is
    # False), so the doji case was already unreachable in practice —
    # but the structural elif fix is still important for correctness
    # and to prevent regressions if the threshold changes.
    if len(candles) >= 10:
        recent_bodies = [abs(candles[i]["close"] - candles[i]["open"])
                         for i in range(-min(20, len(candles)), 0)]
        median_body = sorted(recent_bodies)[len(recent_bodies) // 2]
        if median_body > 0 and abs(body) > median_body * body_mult and abs(body) > 0:
            # FIX (Bug #19, 2026-07-17): Z-score boost threshold is now
            # volatility-scaled (was static Z > 2.0). Matches the same
            # scaling used in otc_pattern.SIGNAL 3 for consistency.
            if vol_pct >= 1.3:
                z_boost_threshold = 2.0
            elif vol_pct <= 0.7:
                z_boost_threshold = 2.8
            else:
                z_boost_threshold = 2.3
            z_boost = 1 if stats["z_body"] > z_boost_threshold else 0
            # FIX (OTC issue 2): trend-aware dampening for big body
            if body_aligns_with_strong_trend and trend_strength > 0.7:
                base_score, base_conf = 1, 53  # was 3/64 — strong dampen
            elif body_aligns_with_strong_trend:
                base_score, base_conf = 2, 58  # moderate dampen
            else:
                base_score, base_conf = 3, 64
            score = base_score + z_boost
            if body > 0:
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="PUT", score=score, confidence=base_conf,
                    signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                    reasons=[f"Big UP body ({body_pct:.0f}%, Z={stats['z_body']:.1f}, {body_mult}x median, trend_str={trend_strength:.2f}) → PUT reversal"]))
            elif body < 0:
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="CALL", score=score, confidence=base_conf,
                    signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                    reasons=[f"Big DOWN body ({body_pct:.0f}%, Z={stats['z_body']:.1f}, {body_mult}x median, trend_str={trend_strength:.2f}) → CALL reversal"]))
            # body == 0 (doji): skip — body_pct would be 0, which fails
            # the > median_body * body_mult threshold anyway, but be safe.

    # ── SIGNAL 3: Wick rejection (WICK group — independent) ──────────────
    if rng > 0:
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        uw_pct = upper_wick / rng * 100
        lw_pct = lower_wick / rng * 100
        if uw_pct > 40 and body_pct < 35:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=3, confidence=59,
                signal_type="REVERSAL", reliability="CANDLE", group="WICK",
                reasons=[f"Upper wick rejection ({uw_pct:.0f}%) → PUT (59% win rate)"]))
        elif lw_pct > 40 and body_pct < 35:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=3, confidence=56,
                signal_type="REVERSAL", reliability="CANDLE", group="WICK",
                reasons=[f"Lower wick rejection ({lw_pct:.0f}%) → CALL (56% win rate)"]))

    # ── SIGNAL 4: Close position in range (BODY group) ───────────────────
    if rng > 0:
        close_pos = max(0, min(100, (c - l) / rng * 100))
        if close_pos >= 80:
            percentile_boost = 1 if stats["close_percentile"] >= 90 else 0
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=2 + percentile_boost, confidence=62,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"Close at range top ({close_pos:.0f}%, pctile={stats['close_percentile']:.0f}) → PUT"]))
        elif close_pos <= 20:
            percentile_boost = 1 if stats["close_percentile"] <= 10 else 0
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=2 + percentile_boost, confidence=60,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"Close at range bottom ({close_pos:.0f}%, pctile={stats['close_percentile']:.0f}) → CALL"]))

    # ── SIGNAL 5: Body shrinking → exhaustion (BODY group) ───────────────
    # FIX (doji bug, 2026-07-19, AUDIT-ENGINES #15): same fix as Signal 2 —
    # `if body > 0: PUT else: CALL` fired CALL on doji. Now uses elif.
    if len(candles) >= 2:
        prev_body = abs(candles[-2]["close"] - candles[-2]["open"])
        if prev_body > 0 and abs(body) < prev_body * 0.5 and abs(body) > 0:
            if body > 0:
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="PUT", score=1, confidence=54,
                    signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                    reasons=["Shrinking bull body → PUT exhaustion (54% win rate)"]))
            elif body < 0:
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="CALL", score=1, confidence=54,
                    signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                    reasons=["Shrinking bear body → CALL exhaustion (54% win rate)"]))
            # body == 0: doji — skip (a doji is ALREADY an exhaustion
            # signal but is handled by Signal 3 wick analysis).

    # ═══════════════════════════════════════════════════════════════════════
    #  CONTINUATION SIGNALS (NEW, 2026-07-18)
    #  Previously this module was 100% REVERSAL (5/5 signals) — making it
    #  structurally incapable of voting for a trend. Since candle_reaction
    #  fires on EVERY candle close (it's the most active module), this
    #  was the single biggest source of structural reversal bias.
    #
    #  Two CONTINUATION signals added:
    #    6. Rising/falling closes momentum — 3+ candles with monotonically
    #       rising (or falling) closes, each with a non-trivial body.
    #       This is the definition of trend momentum.
    #    7. Trend-aligned wick rejection — a wick rejection in the direction
    #       of a confirmed trend is a continuation signal (e.g. lower wick
    #       during an uptrend = buyers stepped in = continuation).
    #  These are gated on trend regime so they don't fire in RANGE markets
    #  where reversal interpretation is correct.
    # ═══════════════════════════════════════════════════════════════════════

    # FIX (OTC issue 2, 2026-07-19): regime / is_trending / trend_regime /
    # trend_strength are now pulled at the TOP of analyze() so the
    # BODY-group reversal signals above can also be trend-aware. The
    # continuation signals below reuse the same variables.

    # ── CONTINUATION SIGNAL 6: Rising/falling closes momentum ────────────
    # 3+ consecutive candles with monotonically rising closes + each body
    # is non-trivial (>= 30% of range) = clean trend momentum.
    # This is the structural opposite of Signal 1 (streak reversal):
    # Signal 1 bets the streak ENDS, Signal 6 bets the streak CONTINUES.
    # The difference: Signal 6 requires monotonic CLOSES (not just bodies)
    # AND requires a confirmed trend regime — so it only fires when the
    # trend is real, not a 3-candle blip in a range.
    if is_trending and trend_strength > 0.4 and len(candles) >= 3:
        c1_close = candles[-3]["close"]
        c2_close = candles[-2]["close"]
        c3_close = candles[-1]["close"]
        # Monotonic rising closes
        if c1_close < c2_close < c3_close:
            # Each body must be non-trivial (not dojis)
            b1 = abs(candles[-3]["close"] - candles[-3]["open"])
            b2 = abs(candles[-2]["close"] - candles[-2]["open"])
            b3 = abs(body)
            r1 = candles[-3]["high"] - candles[-3]["low"]
            r2 = candles[-2]["high"] - candles[-2]["low"]
            if (r1 > 0 and r2 > 0 and rng > 0
                    and b1/r1 >= 0.30 and b2/r2 >= 0.30 and body_pct >= 30
                    and trend_regime == "TREND_UP"):
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="CALL", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="CANDLE", group="BODY_CONT",
                    reasons=[f"Rising closes momentum (3 UP, str={trend_strength:.2f}) → CALL continuation (62% win rate)"]))
        # Monotonic falling closes
        elif c1_close > c2_close > c3_close:
            b1 = abs(candles[-3]["close"] - candles[-3]["open"])
            b2 = abs(candles[-2]["close"] - candles[-2]["open"])
            b3 = abs(body)
            r1 = candles[-3]["high"] - candles[-3]["low"]
            r2 = candles[-2]["high"] - candles[-2]["low"]
            if (r1 > 0 and r2 > 0 and rng > 0
                    and b1/r1 >= 0.30 and b2/r2 >= 0.30 and body_pct >= 30
                    and trend_regime == "TREND_DOWN"):
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="PUT", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="CANDLE", group="BODY_CONT",
                    reasons=[f"Falling closes momentum (3 DOWN, str={trend_strength:.2f}) → PUT continuation (62% win rate)"]))

    # ── CONTINUATION SIGNAL 7: Trend-aligned wick rejection ──────────────
    # Signal 3 (WICK) always treats wick rejection as REVERSAL. But a
    # lower-wick rejection during an UPTREND is actually a CONTINUATION
    # signal — it means buyers stepped in at the low and pushed price
    # back up, confirming the trend. Similarly for upper-wick in downtrend.
    # This signal fires INSTEAD OF Signal 3 when the wick aligns with
    # a confirmed trend. We use group="WICK_CONT" (not "WICK") so the
    # blender doesn't double-count with Signal 3.
    if is_trending and trend_strength > 0.4 and rng > 0:
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        uw_pct = upper_wick / rng * 100
        lw_pct = lower_wick / rng * 100
        # Lower wick rejection in uptrend = buyers defended the low → CALL continuation
        if lw_pct > 40 and body_pct < 35 and trend_regime == "TREND_UP" and body > 0:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="CANDLE", group="WICK_CONT",
                reasons=[f"Trend-aligned lower wick ({lw_pct:.0f}%, uptrend str={trend_strength:.2f}) → CALL continuation (58% win rate)"]))
        # Upper wick rejection in downtrend = sellers defended the high → PUT continuation
        elif uw_pct > 40 and body_pct < 35 and trend_regime == "TREND_DOWN" and body < 0:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="CANDLE", group="WICK_CONT",
                reasons=[f"Trend-aligned upper wick ({uw_pct:.0f}%, downtrend str={trend_strength:.2f}) → PUT continuation (58% win rate)"]))

    return results
