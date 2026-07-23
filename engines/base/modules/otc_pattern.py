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
    # DISABLED (live data, 2026-07-20): real Quotex data showed 0% win rate!
    # Z-score extremes in OTC are broker momentum spikes, not exhaustion.
    # The broker pushes price further, not reverses.
    #
    # FIX (AUDIT-DEEP #10, 2026-07-23): the previous code kept the entire
    # 35-line signal block in place but set an impossibly-high threshold
    # (z > 999) to make it unreachable. This is dead code — it can never
    # fire, but it adds ~35 lines of confusing logic that future
    # maintainers might "fix" by lowering the threshold back to a
    # meaningful value, re-introducing the 0% win rate signal. Now the
    # entire block is REMOVED, with a clear comment explaining why. If
    # live data ever shows z-score extremes have predictive value again,
    # re-add this signal with proper backtesting.
    #
    # (Original signal logic removed — see git history for the
    # implementation if needed for future backtesting.)

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
    # DISABLED (live data, 2026-07-20): real Quotex data showed 43% win rate
    # — OTC breakouts fail because the broker reverses them. The broker's
    # algorithm creates false breakouts to trap traders. Removing this
    # signal entirely improves accuracy.
    # if len(candles) >= 21:
    #     ... (original code removed)

    # ── CONTINUATION SIGNAL 8: Strong-trend streak ───────────────────────
    # DISABLED (live data, 2026-07-20): real Quotex data showed 43.2% win
    # rate — OTC trend streaks reverse (broker algorithm). Combined with
    # the regime multiplier inversion (reversal boosted in trends), this
    # continuation signal is counterproductive. Removing it.
    # if is_trending and trend_strength > 0.35 and consec >= 2:
    #     ... (original code removed)

    return results
