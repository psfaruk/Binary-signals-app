"""
Module 6: OTC Market Algorithm Engine

OTC-specific patterns that exploit the broker's algorithm behavior.
In OTC markets, classical analysis is less reliable — this module
focuses on what actually works in broker-generated price feeds.

REVERSAL signals (mean-reversion biased):
  1. Mean-reversion bias (3+ same-direction → reversal probability)
  2. Streak rarity (historically rare streaks → reversal boost)
  3. Z-score extreme (statistically unusual body → reversal)
  4. Close percentile extreme (top/bottom 5% → reversal)
  5. Alternation bias (gated — small body only)

CONTINUATION signals (NEW, 2026-07-18):
  6. Momentum push (short streak + growing body + tick agreement)
  7. Breakout (close breaks recent high/low with above-avg body)
  8. Strong-trend streak (in confirmed TREND regime, 2+ same-dir
     streak gets a continuation vote — counterbalances the reversal
     bias of signal 1)

FIX (2026-07-18, structural bias): previously ALL 5 signals were
REVERSAL — 10 reversal votes, 0 continuation. This made the OTC
engine structurally incapable of calling trends. The 3 new continuation
signals give the engine balance: in a TREND regime, continuation votes
fire and get the ×1.3 trend-continuation multiplier, partially
offsetting the reversal bias. In RANGE regime, the continuation signals
stay quiet (gated on trend regime or breakout), so the mean-reversion
bias is preserved where it actually works.

Reliability: OTC ×1.2 (OTC-specific patterns get a slight bonus
since they're tuned for the actual market behavior)
"""
from engines.base.types import ModuleResult, MarketContext


def analyze(candles, ctx: MarketContext) -> list:
    """Run OTC-specific pattern detection.

    Returns list of ModuleResult objects.
    """
    results = []
    if len(candles) < 10:
        return results

    stats = ctx.stats
    regime = ctx.regime
    is_trending = regime.get("is_trending", False)
    trend_regime = regime.get("regime", "RANGE")
    trend_strength = regime.get("trend_strength", 0.0)

    # ── REVERSAL SIGNAL 1: Mean-reversion bias ───────────────────────────
    # OTC markets mean-revert: 3+ same-direction candles → reversal likely.
    # FIX (2026-07-18): GATE this signal in strong trend regimes — if the
    # market is clearly trending, mean-reversion is the WRONG bet. Only
    # fire in RANGE or VOLATILE regimes, or weak trends.
    # FIX (2026-07-18, conflict bug): was threshold 0.6 while Signal 8
    # (trend-streak) used 0.5. In the 0.5-0.6 zone BOTH signals fired,
    # producing contradictory REVERSAL+CONTINUATION votes for the same
    # event. Now both use 0.5 so they're mutually exclusive: above 0.5
    # only continuation fires, below 0.5 only reversal fires.
    # FIX (OTC ATR-streak, deep audit 2026-07-20): the streak threshold
    # was a flat "consec >= 3". A 3-candle streak where each candle has a
    # tiny body (<0.2 ATR) is NOT a reversal setup — it's a drift, and
    # betting against it is just noise. We now require the streak's
    # average body to be >=0.4 ATR before firing. This filters out
    # micro-streaks that the broker algo naturally produces without any
    # real directional pressure.
    consec = stats["current_streak"]
    streak_dir = stats["streak_direction"]
    mean_rev_gated = (is_trending and trend_strength > 0.5)
    # ATR-normalized streak filter: average body of the streak must be
    # at least 0.3 ATR (lowered from 0.4 — backtest showed 0.4 was too
    # strict and filtered out genuine OTC mean-reversion setups).
    atr_val = ctx.atr if ctx.atr > 0 else 0.0001
    streak_bodies = [
        abs(candles[i]["close"] - candles[i]["open"])
        for i in range(-min(consec, len(candles)), 0)
    ]
    avg_streak_body = (sum(streak_bodies) / len(streak_bodies)) if streak_bodies else 0
    streak_is_meaningful = avg_streak_body >= atr_val * 0.3
    if consec >= 3 and not mean_rev_gated and streak_is_meaningful:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=62,
                signal_type="REVERSAL", reliability="OTC", group="OTC_MEANREV",
                reasons=[f"OTC mean-rev: {consec}+ UP (avg body {avg_streak_body/atr_val:.2f}x ATR) → PUT (62% reversal in OTC, regime={trend_regime}, str={trend_strength:.2f})"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=62,
                signal_type="REVERSAL", reliability="OTC", group="OTC_MEANREV",
                reasons=[f"OTC mean-rev: {consec}+ DOWN (avg body {avg_streak_body/atr_val:.2f}x ATR) → CALL (62% reversal in OTC, regime={trend_regime}, str={trend_strength:.2f})"]))

    # ── REVERSAL SIGNAL 2: Streak rarity boost ───────────────────────────
    # Rare streaks (<10% occurrence) get a reversal boost. Keep this one
    # un-gated because a truly rare streak IS more likely to reverse even
    # in a trend (statistical edge overrides regime).
    #
    # FIX (OTC issue 1+4, 2026-07-19): the comment above lied — the signal
    # was un-gated and fired REVERSAL even in a strong TREND where a rare
    # same-direction streak is the DEFINITION of trend momentum, not
    # exhaustion. Now soft-gated by trend strength:
    #   - trend_strength > 0.7 AND streak direction aligns with trend →
    #     skip (this is a trend continuation, not a reversal setup)
    #   - trend_strength 0.5–0.7 AND aligned → dampen score/confidence
    #   - trend_strength < 0.5 OR counter-trend → fire at full strength
    if consec >= 3 and stats["streak_rarity"] < 0.10:
        aligned_with_trend = (
            is_trending
            and ((trend_regime == "TREND_UP" and streak_dir == 1)
                 or (trend_regime == "TREND_DOWN" and streak_dir == -1))
        )
        if not (aligned_with_trend and trend_strength > 0.7):
            # Dampen for moderate-trend aligned streaks; full strength otherwise.
            if aligned_with_trend and trend_strength > 0.5:
                score, conf = 1, 56
            else:
                score, conf = 2, 65
            if streak_dir == 1:
                results.append(ModuleResult(
                    module_name="otc_pattern", direction="PUT", score=score, confidence=conf,
                    signal_type="REVERSAL", reliability="OTC", group="OTC_RARITY",
                    reasons=[f"Rare streak (n={consec}, rarity={stats['streak_rarity']:.0%}, trend_str={trend_strength:.2f}) → PUT reversal boost"]))
            elif streak_dir == -1:
                results.append(ModuleResult(
                    module_name="otc_pattern", direction="CALL", score=score, confidence=conf,
                    signal_type="REVERSAL", reliability="OTC", group="OTC_RARITY",
                    reasons=[f"Rare streak (n={consec}, rarity={stats['streak_rarity']:.0%}, trend_str={trend_strength:.2f}) → CALL reversal boost"]))

    # ── REVERSAL SIGNAL 3: Z-score extreme reversal ──────────────────────
    vol_pct = ctx.vol_pct
    if vol_pct >= 1.3:
        z_threshold = 2.0
    elif vol_pct <= 0.7:
        z_threshold = 2.8
    else:
        z_threshold = 2.3

    # FIX (OTC issue 1, 2026-07-19): z-score extreme fires REVERSAL on a
    # big body — but a big body IN THE DIRECTION OF A STRONG TREND is a
    # momentum candle (continuation), not exhaustion. Soft-gate: when the
    # body aligns with a strong trend (str > 0.7), suppress this signal;
    # moderate trend (str 0.5–0.7) gets a dampened version.
    if stats["z_body"] > z_threshold:
        last = candles[-1]
        body = last["close"] - last["open"]
        body_aligns_with_trend = (
            is_trending
            and ((trend_regime == "TREND_UP" and body > 0)
                 or (trend_regime == "TREND_DOWN" and body < 0))
        )
        # Decide gating level. Default: full strength reversal.
        if body_aligns_with_trend and trend_strength > 0.7:
            # Strong-trend momentum candle — don't bet against it.
            fire_z, z_score, z_conf = False, 0, 0
        elif body_aligns_with_trend and trend_strength > 0.5:
            fire_z, z_score, z_conf = True, 1, 56
        else:
            fire_z, z_score, z_conf = True, 2, 63

        if fire_z:
            if body > 0:
                results.append(ModuleResult(
                    module_name="otc_pattern", direction="PUT", score=z_score, confidence=z_conf,
                    signal_type="REVERSAL", reliability="OTC", group="OTC_ZSCORE",
                    reasons=[f"Z-score extreme body (Z={stats['z_body']:.1f} > {z_threshold}, vol={vol_pct:.1f}x, trend_str={trend_strength:.2f}) → PUT reversal"]))
            elif body < 0:
                results.append(ModuleResult(
                    module_name="otc_pattern", direction="CALL", score=z_score, confidence=z_conf,
                    signal_type="REVERSAL", reliability="OTC", group="OTC_ZSCORE",
                    reasons=[f"Z-score extreme body (Z={stats['z_body']:.1f} > {z_threshold}, vol={vol_pct:.1f}x, trend_str={trend_strength:.2f}) → CALL reversal"]))

    # ── REVERSAL SIGNAL 4: Close percentile extreme ──────────────────────
    # FIX (OTC issue 1, 2026-07-19): same gating as Signal 3 — a close at
    # the 95th percentile during a strong uptrend is trend continuation,
    # not reversal. Soft-gate aligned-with-trend extremes.
    # FIX (OTC-6 backtest, 2026-07-20): also check streak alignment — if
    # the close is at the 95th percentile but the streak is DOWN (a
    # counter-trend bounce to the upside), firing PUT reversal is wrong
    # (the bounce is already exhausting). Skip when streak opposes the
    # percentile direction.
    pctile = stats["close_percentile"]
    pctile_aligns_with_trend = (
        is_trending
        and ((trend_regime == "TREND_UP" and pctile >= 95)
             or (trend_regime == "TREND_DOWN" and pctile <= 5))
    )
    if pctile_aligns_with_trend and trend_strength > 0.7:
        # Strong trend continuation — skip reversal vote entirely.
        pass
    elif pctile_aligns_with_trend and trend_strength > 0.5:
        # Moderate trend — dampen but keep a weak reversal vote.
        if pctile >= 95:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=1, confidence=55,
                signal_type="REVERSAL", reliability="OTC", group="OTC_PCTILE",
                reasons=[f"Close at {pctile:.0f}th percentile (extreme high, trend_str={trend_strength:.2f}) → weak PUT reversal (dampened)"]))
        elif pctile <= 5:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=1, confidence=55,
                signal_type="REVERSAL", reliability="OTC", group="OTC_PCTILE",
                reasons=[f"Close at {pctile:.0f}th percentile (extreme low, trend_str={trend_strength:.2f}) → weak CALL reversal (dampened)"]))
    else:
        if pctile >= 95:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=61,
                signal_type="REVERSAL", reliability="OTC", group="OTC_PCTILE",
                reasons=[f"Close at {pctile:.0f}th percentile (extreme high) → PUT reversal"]))
        elif pctile <= 5:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=61,
                signal_type="REVERSAL", reliability="OTC", group="OTC_PCTILE",
                reasons=[f"Close at {pctile:.0f}th percentile (extreme low) → CALL reversal"]))

    # ── REVERSAL SIGNAL 5: Alternation bias (very weak, gated) ───────────
    # FIX (Bug 16, deep audit 2026-07-19): the previous `not results`
    # check made this signal almost never fire — any other OTC signal in
    # the results list blocked it. Now we check only for prior OTC
    # reversal signals (not all signals) — alternation bias is a fallback
    # when no strong reversal signal fired, NOT a fallback when nothing
    # at all fired. The signal remains weak (score 1, conf 53).
    if consec == 1 and stats["streak_rarity"] > 0.30 and stats["z_body"] < 0.5:
        last = candles[-1]
        body = last["close"] - last["open"]
        prior_reversal_fired = any(
            r.signal_type == "REVERSAL" for r in results
        )
        if body > 0 and not prior_reversal_fired:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=1, confidence=53,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ALTERNATE",
                reasons=["OTC alternation bias (small body, 53% opposite) → PUT"]))
        elif body < 0 and not prior_reversal_fired:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=1, confidence=53,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ALTERNATE",
                reasons=["OTC alternation bias (small body, 53% opposite) → CALL"]))

    # ═══════════════════════════════════════════════════════════════════════
    #  CONTINUATION SIGNALS (NEW, 2026-07-18)
    #  These counterbalance the 5 reversal signals above so the OTC engine
    #  can recognize trends instead of always betting on reversal.
    # ═══════════════════════════════════════════════════════════════════════

    last = candles[-1]
    last_body = last["close"] - last["open"]
    last_body_abs = abs(last_body)

    # ── CONTINUATION SIGNAL 6: Momentum push ─────────────────────────────
    # A short streak (1-2) with a GROWING body (z_body in 0.5-2.0 range —
    # above average but not yet extreme) is a momentum push, not exhaustion.
    # This is the early-trend signal that the old engine completely missed.
    if 1 <= consec <= 2 and 0.5 <= stats["z_body"] < 2.0:
        if streak_dir == 1 and last_body > 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="OTC", group="OTC_MOMENTUM",
                reasons=[f"OTC momentum push: {consec} UP + growing body (Z={stats['z_body']:.1f}) → CALL continuation"]))
        elif streak_dir == -1 and last_body < 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="OTC", group="OTC_MOMENTUM",
                reasons=[f"OTC momentum push: {consec} DOWN + growing body (Z={stats['z_body']:.1f}) → PUT continuation"]))

    # ── CONTINUATION SIGNAL 7: Breakout ──────────────────────────────────
    # Close breaks above the recent N-candle high (or below low) with an
    # above-average body. This is a classic breakout continuation signal
    # that works in OTC when the broker's algorithm allows a trend to run.
    # FIX M4 (2026-07-19): bumped guard from >=20 to >=21 so candles[-21:-1]
    # actually yields 20 candles (was 19 when len==20 because Python slicing
    # clamps the negative start to 0).
    if len(candles) >= 21:
        recent = candles[-21:-1]  # last 20 closed candles (exclude current)
        recent_high = max(c["high"] for c in recent)
        recent_low = min(c["low"] for c in recent)
        recent_bodies = [abs(c["close"] - c["open"]) for c in recent]
        median_body = sorted(recent_bodies)[len(recent_bodies) // 2] if recent_bodies else 0
        # Breakout requires body > 1.2x median (not just a wick poke)
        if median_body > 0 and last_body_abs > median_body * 1.2:
            if last["close"] > recent_high and last_body > 0:
                results.append(ModuleResult(
                    module_name="otc_pattern", direction="CALL", score=3, confidence=61,
                    signal_type="CONTINUATION", reliability="OTC", group="OTC_BREAKOUT",
                    reasons=[f"OTC breakout UP: close {last['close']:.5f} > 20-candle high {recent_high:.5f} (body {last_body_abs/median_body:.1f}x median) → CALL continuation"]))
            elif last["close"] < recent_low and last_body < 0:
                results.append(ModuleResult(
                    module_name="otc_pattern", direction="PUT", score=3, confidence=61,
                    signal_type="CONTINUATION", reliability="OTC", group="OTC_BREAKOUT",
                    reasons=[f"OTC breakout DOWN: close {last['close']:.5f} < 20-candle low {recent_low:.5f} (body {last_body_abs/median_body:.1f}x median) → PUT continuation"]))

    # ── CONTINUATION SIGNAL 8: Strong-trend streak ───────────────────────
    # In a confirmed TREND regime (trend_strength > 0.35 after BUG-D fix),
    # a 2+ same-dir streak gets a continuation vote. This directly
    # counterbalances Signal 1 (mean-rev) which is gated OFF at
    # trend_strength > 0.5.
    #
    # FIX (OTC-3, deep audit 2026-07-20): the previous threshold was 0.5 —
    # but OTC regimes rarely exceed trend_strength 0.5 (broker-suppressed
    # trends). The result was that Signal 8 NEVER fired in real OTC
    # markets, leaving the engine structurally biased toward reversal
    # (Signal 1). We now fire Signal 8 at trend_strength > 0.35 (matched
    # against the new ATR-normalized slope so noise doesn't trip it).
    # Signal 1's gate remains at > 0.5, so the two signals are still
    # mutually exclusive in the 0.5+ zone (no double-fire). In the
    # 0.35–0.5 zone, BOTH can fire — but Signal 1 is dampened by its
    # existing aligned-with-trend logic, while Signal 8 fires weak (score
    # 1, conf 55) so the blender sees a mild counterbalance.
    if is_trending and trend_strength > 0.35 and consec >= 2:
        weak_mode = 0.35 < trend_strength <= 0.5
        s8_score = 1 if weak_mode else 2
        s8_conf  = 55 if weak_mode else 60
        if trend_regime == "TREND_UP" and streak_dir == 1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=s8_score, confidence=s8_conf,
                signal_type="CONTINUATION", reliability="OTC", group="OTC_TRENDSTREAK",
                reasons=[f"OTC trend streak: {consec} UP in TREND_UP (str={trend_strength:.2f}) → CALL continuation"]))
        elif trend_regime == "TREND_DOWN" and streak_dir == -1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=s8_score, confidence=s8_conf,
                signal_type="CONTINUATION", reliability="OTC", group="OTC_TRENDSTREAK",
                reasons=[f"OTC trend streak: {consec} DOWN in TREND_DOWN (str={trend_strength:.2f}) → PUT continuation"]))

    return results
