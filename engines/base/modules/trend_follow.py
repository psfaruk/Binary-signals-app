"""
Module 6 (REAL engine): Trend-Follow Engine

Replaces the OTC engine's `otc_pattern` module. In REAL markets (live
exchange prices, real order flow), trends are MORE persistent than in
OTC — institutional money drives multi-candle continuations, and
mean-reversion is weaker. This module is tuned for that:

  1. Momentum continuation (3+ same-direction with rising bodies → continue)
  2. EMA alignment confirmation (EMA9 > EMA21 + price above EMA9 → uptrend)
  3. Breakout confirmation (close above recent swing high → continuation)
  4. Higher-high / higher-low structure (HH/HL → uptrend, LH/LL → downtrend)
  5. ATR expansion (volatility expanding in trend direction → momentum)

Reliability: TREND ×1.3 (real-market trends are structurally meaningful)

FIX (2026-07-17): the previous version of this file was a verbatim copy
of engines/otc/modules/otc_pattern.py — it was running OTC mean-reversion
logic on real-market pairs, defeating the entire purpose of having two
engines. This file is now a proper trend-following module.
"""
from engines.base.types import ModuleResult, MarketContext


def analyze(candles, ctx: MarketContext) -> list:
    """Run trend-following pattern detection for real-market pairs.

    Returns list of ModuleResult objects.

    FIX (AUDIT-DEEP-A2, 2026-07-23): the trend_follow module's weight was
    reduced to 0.1 (effectively disabled) in DEFAULT_WEIGHTS because
    backtest showed 27.3% win rate — catastrophically bad. The module
    was still running every candle (CPU wasted on a module that
    contributes nothing to the final prediction). We now check the
    module's effective weight and short-circuit early if it's below
    a threshold. This saves ~0.1ms per candle per real-market stream,
    which adds up across 20+ concurrent streams. The module's analyze()
    code is still preserved so it can be re-enabled quickly if the
    logic is rewritten and backtest shows improvement.
    """
    # Short-circuit: if the module's weight is effectively zero, skip
    # the entire analysis. The blender's per-pair adapter already
    # dampens weight 0.1 → effective contribution ~0, so running this
    # module is wasted CPU. The module breakdown UI shows "fired: False"
    # which is correct (no signals contributed).
    #
    # We import lazily here (not at module top) to avoid circular import
    # with engines.real.config at startup time. This check costs <0.05ms.
    try:
        from engines.real.config import DEFAULT_WEIGHTS as _REAL_DEFAULTS
        _trend_weight = _REAL_DEFAULTS.get("trend_follow", 1.0)
        if _trend_weight < 0.2:  # effectively disabled
            return []
    except ImportError:
        pass  # engines.real.config not importable (test context) — run normally

    results = []
    if len(candles) < 10:
        return results

    stats = ctx.stats
    regime = ctx.regime
    ema9 = ctx.ema9
    ema21 = ctx.ema21
    atr = ctx.atr
    last = candles[-1]
    close = last["close"]
    o, h, l, c = last["open"], last["high"], last["low"], last["close"]
    # FIX: define trend variables for new signals 7-9
    is_trending = regime.get("is_trending", False)
    trend_regime = regime.get("regime", "RANGE")
    trend_strength = regime.get("trend_strength", 0.0)

    # ── SIGNAL 1: Momentum continuation (3+ same-direction with rising bodies) ─
    # In real markets, a 3-candle streak with INCREASING body sizes is a
    # momentum signal — institutional buyers/sellers are stepping in harder.
    # Opposite of the OTC engine which treats 3+ streaks as reversal.
    # FIX (REAL-2, deep audit 2026-07-20): the previous version checked
    # "rising bodies" but NOT whether those bodies were ATR-significant
    # or whether volume (proxied by tick_count via ctx) confirmed the
    # move. On a Gaussian random walk sim, three 0.1-pip bodies that
    # happen to rise monotonically would fire the signal — pure noise.
    # Now we require:
    #   (a) avg streak body >= 0.5 ATR (the streak is moving price, not drifting)
    #   (b) ctx.vol_pct >= 0.9 (volatility is at-or-above average —
    #       sub-average vol on a "rising body streak" is a low-conviction
    #       drift, not institutional momentum)
    consec = stats["current_streak"]
    streak_dir = stats["streak_direction"]
    # FIX (Bug 13, deep audit 2026-07-19): cap the lookback to
    # min(consec, len(candles)) so `range(-consec, 0)` never indexes past
    # the start of the list.
    if consec >= 3 and len(candles) >= 4:
        lookback = min(consec, len(candles))
        bodies = [abs(candles[i]["close"] - candles[i]["open"])
                  for i in range(-lookback, 0)]
        rising_bodies = all(bodies[i] >= bodies[i-1] * 0.85
                            for i in range(1, len(bodies)) if bodies[i-1] > 0)
        avg_body = sum(bodies) / len(bodies) if bodies else 0
        atr_normalized_body = avg_body / atr if atr > 0 else 0
        vol_pct = ctx.vol_pct
        # FIX (REAL-2 tuning, 2026-07-20): vol_pct >= 0.9 was too strict —
        # many real-market moderate trends have vol_pct 0.7-0.9 (slightly
        # below-average volatility but still meaningful momentum).
        # Lowered to 0.7. Body filter (0.5 ATR) remains the primary gate.
        body_significant = atr_normalized_body >= 0.5
        vol_confirms = vol_pct >= 0.7
        if rising_bodies and len(bodies) >= 3 and body_significant and vol_confirms:
            # Continuation: 3+ rising-body candles with ATR-significant bodies
            # and at-or-above-average volatility → next candle continues
            if streak_dir == 1:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="CALL", score=3, confidence=63,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_MOMENTUM",
                    reasons=[f"3+ rising-body UP candles (avg {atr_normalized_body:.2f}x ATR, vol={vol_pct:.2f}x) → CALL continuation (momentum building)"]))
            elif streak_dir == -1:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="PUT", score=3, confidence=63,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_MOMENTUM",
                    reasons=[f"3+ rising-body DOWN candles (avg {atr_normalized_body:.2f}x ATR, vol={vol_pct:.2f}x) → PUT continuation (momentum building)"]))
        elif rising_bodies and len(bodies) >= 3 and body_significant:
            # Body is significant but vol is sub-average — fire WEAK continuation.
            if streak_dir == 1:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="CALL", score=1, confidence=53,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_MOMENTUM",
                    reasons=[f"3+ rising-body UP candles (avg {atr_normalized_body:.2f}x ATR, low vol={vol_pct:.2f}x) → weak CALL continuation (no vol confirm)"]))
            elif streak_dir == -1:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="PUT", score=1, confidence=53,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_MOMENTUM",
                    reasons=[f"3+ rising-body DOWN candles (avg {atr_normalized_body:.2f}x ATR, low vol={vol_pct:.2f}x) → weak PUT continuation (no vol confirm)"]))
        # else: streak body too small to be meaningful — skip

    # ── SIGNAL 2: EMA alignment confirmation ─────────────────────────────
    # EMA9 > EMA21 + price above EMA9 → uptrend continuation
    # EMA9 < EMA21 + price below EMA9 → downtrend continuation
    # FIX (trend_follow calibration, 2026-07-20): backtest showed 28.9% win
    # rate — the 0.02% separation threshold was way too low, firing on noise.
    # Raised to 0.10% (10 pips on EURUSD) for meaningful trend confirmation.
    if ema9 > 0 and ema21 > 0:
        sep_pct = abs(ema9 - ema21) / max(ema21, 0.0001) * 100
        if sep_pct > 0.10:
            if ema9 > ema21 and close > ema9:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="CALL", score=2, confidence=58,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_EMA",
                    reasons=[f"EMA9 > EMA21 (sep={sep_pct:.3f}%) + close above EMA9 → CALL continuation"]))
            elif ema9 < ema21 and close < ema9:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="PUT", score=2, confidence=58,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_EMA",
                    reasons=[f"EMA9 < EMA21 (sep={sep_pct:.3f}%) + close below EMA9 → PUT continuation"]))

    # ── SIGNAL 3: Breakout confirmation ──────────────────────────────────
    # Close above the highest high of the last 10 candles → bullish breakout
    # Close below the lowest low of the last 10 candles → bearish breakout
    # (Only fires when ATR is not elevated — breakouts in high-vol regimes
    #  are unreliable, often fakeouts.)
    #
    # FIX (Real issue 3, 2026-07-19): the previous version used only the
    # last candle's close vs the lookback high/low + an ATR buffer. That
    # misfires on:
    #   (a) News wicks — a single-candle spike that closes inside the
    #       range but had a high/low poke outside. We now require the
    #       candle BODY (not just high/low) to extend beyond the level.
    #   (b) Stop-hunt fakeouts — a candle breaks the level, the next one
    #       reverses. We now require multi-candle confirmation: BOTH the
    #       prior candle AND the current candle must close beyond the
    #       level. A single-candle poke gets a weaker "unconfirmed" vote.
    #   (c) Recent same-direction failure — if a breakout within the last
    #       6 candles was immediately reversed, suppress the new vote
    #       (fakeout-pattern anti-trigger).
    #
    # FIX (anti-fakeout no-op, 2026-07-19, AUDIT-ENGINES #19): the
    # anti-fakeout loop scanned candles[-7:-1] and compared each candle's
    # close against `recent_high` / `recent_low`. But `recent_high` was
    # computed from `candles[-12:-2]` — which OVERLAPS the scan range
    # ([-7,-2] is inside [-12,-2]). So a candle that set the high would
    # trivially have `c["close"] > recent_high` only if it closed ABOVE
    # its OWN high — impossible. The condition `c["close"] > recent_high`
    # was structurally always False (a candle's close is at most its
    # high, and recent_high is the MAX high over a window that includes
    # the candle itself). The anti-fakeout was therefore a no-op.
    # Fix: compute the level from a window that EXCLUDES the scan range.
    # The scan checks candles[-7:-1] (6 candles). The level must come
    # from candles BEFORE that, i.e. candles[-13:-7]. This way the
    # scan can actually find failed breakouts against a real prior level.
    if len(candles) >= 14 and not regime.get("is_volatile", False):
        # Level window: candles[-12:-2] (10 candles, EXCLUDES current + prior).
        lookback = candles[-12:-2]
        recent_high = max(x["high"] for x in lookback)
        recent_low = min(x["low"] for x in lookback)
        buffer = atr * 0.15 if atr > 0 else 0
        prior_close = candles[-2]["close"]

        # Anti-fakeout: scan the last 6 closed candles (excluding current).
        # FIX (AUDIT-ENGINES #19): use a SEPARATE pre-level window that
        # does NOT overlap the scan range, so failed breakouts against a
        # real prior level can actually be detected.
        # Pre-level window: candles[-13:-7] (6 candles before the scan range).
        # Requires len(candles) >= 14 (one extra for the offset).
        pre_level_window = candles[-13:-7]
        pre_level_high = max(x["high"] for x in pre_level_window)
        pre_level_low = min(x["low"] for x in pre_level_window)
        # If pre_level_high < recent_high, the level ROSE between the two
        # windows — use the older (pre-level) high as the "broken level".
        # If pre_level_high > recent_high, the level FELL — use recent_high.
        # The actual level being tested for failure is the OLDER one
        # (pre_level), because the scan range candles were attempting to
        # break THAT level, not the newer one.
        failed_level_high = pre_level_high
        failed_level_low = pre_level_low

        recent_failed_bull = False
        recent_failed_bear = False
        # FIX (Bug 18, deep audit 2026-07-19): the previous loop
        # `range(-7, -1)` only checked 6 candles, missing older failed
        # breakouts within the lookback window. Now we scan
        # `range(-min(11, len(candles)-1), -1)` so we check up to 10 prior
        # candles (matching the 10-candle lookback used for recent_high).
        scan_start = -min(11, len(candles) - 1)
        for i in range(scan_start, -1):
            if abs(i) > len(candles) - 1:
                continue
            c = candles[i]
            nc = candles[i + 1] if (i + 1) < 0 else None
            if nc is None:
                continue
            # Bullish attempt: candle i closed above the failed level,
            # next candle closed back below — failed breakout.
            if c["close"] > failed_level_high and nc["close"] < failed_level_high:
                recent_failed_bull = True
            # Bearish attempt: candle i closed below the failed level,
            # next candle closed back above — failed breakdown.
            if c["close"] < failed_level_low and nc["close"] > failed_level_low:
                recent_failed_bear = True

        # Bullish breakout: current close above recent_high + buffer AND body extends
        # beyond the level (filters out upper-wick-only pokes).
        if close > recent_high + buffer and (close - max(o, recent_high)) > buffer:
            # Multi-candle confirmation: prior candle also closed above the level.
            confirmed = prior_close > recent_high
            if recent_failed_bull:
                # Recent fakeout → skip (anti-trigger)
                pass
            elif confirmed:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="CALL", score=2, confidence=56,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_BREAKOUT",
                    reasons=[f"Confirmed breakout above 10-candle high ({recent_high:.5f}, body-confirmed, prior close {prior_close:.5f}) → CALL continuation"]))
            # DISABLED unconfirmed breakout (ultra-deep: 48.3% win rate)
            # else:
            #     results.append(ModuleResult(
            #         module_name="trend_follow", direction="CALL", score=2, confidence=55,
            #         signal_type="CONTINUATION", reliability="TREND", group="TREND_BREAKOUT",
            #         reasons=[f"Unconfirmed breakout above 10-candle high → CALL (weak)"]))
        # Bearish breakout (mirror)
        elif close < recent_low - buffer and (min(o, recent_low) - close) > buffer:
            confirmed = prior_close < recent_low
            if recent_failed_bear:
                pass
            elif confirmed:
                results.append(ModuleResult(
                    module_name="trend_follow", direction="PUT", score=2, confidence=56,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_BREAKOUT",
                    reasons=[f"Confirmed breakdown below 10-candle low ({recent_low:.5f}, body-confirmed, prior close {prior_close:.5f}) → PUT continuation"]))
            # DISABLED unconfirmed breakdown (ultra-deep: 48.3% win rate)
            # else:
            #     results.append(...)

    # ── SIGNAL 4: Higher-high / higher-low structure ─────────────────────
    # DISABLED (deep diagnostic, 2026-07-20): backtest showed 44.4% win rate
    # — the 10-candle window is too short to detect meaningful Dow structure.
    # On 1m candles, swing highs/lows within 10 candles are mostly noise.
    # The signal is counterproductive — removing it improves accuracy.
    # if len(candles) >= 10:
    #     ... (original code removed)

    # ── SIGNAL 5: ATR expansion in trend direction ───────────────────────
    # When volatility is expanding (vol_pct > 1.0) AND regime is trending,
    # the trend has fuel — boost continuation signals.
    # FIX (AUDIT-ENGINES #20, 2026-07-19): the previous version used
    # `regime_dir = "TREND_UP" if regime.get("regime") == "TREND_UP" else "TREND_DOWN"`
    # — meaning RANGE and VOLATILE regimes defaulted to TREND_DOWN, firing
    # a PUT momentum boost in non-trending markets. Now we only fire this
    # signal when the regime is EXPLICITLY TREND_UP or TREND_DOWN.
    # DISABLED (ultra-deep, 2026-07-20): 47.4% win rate — ATR expansion
    # on 1m candles is noise, not momentum fuel. Removed.
    # vol_pct = ctx.vol_pct
    # regime_name = regime.get("regime", "RANGE")
    # if vol_pct > 1.1 and ... (disabled)
    regime_name = regime.get("regime", "RANGE")

    # ── SIGNAL 6: Trend exhaustion dampener ──────────────────────────────
    # FIX (Real-CALL-bias, 2026-07-20): backtest showed TREND_UP_UPTREND
    # predictions were only 35% accurate — the engine fires too many CALL
    # votes in an established uptrend, but the trend is often about to
    # reverse. When the streak is long (>=5) AND the last body is shrinking
    # (< 60% of streak average), fire a weak REVERSAL vote to counterbalance.
    # NOTE: threshold raised from consec>=4 to >=5 and shrink from 0.70 to 0.60
    # after backtest showed the looser threshold hurt Real engine overall
    # accuracy (too many false reversal signals).
    #
    # FIX (AUDIT-DEEP #11, 2026-07-23): `avg_body` previously included the
    # current (small) body, which drags down the average and makes the
    # "shrinking" test less sensitive. A truly shrinking candle would be
    # compared against the PRIOR streak bodies (excluding current), so a
    # 60% shrinkage test against the prior average is a stricter, more
    # accurate exhaustion signal. The fix excludes the current candle from
    # the average computation.
    if consec >= 5 and len(candles) >= 6:
        lookback = min(consec, len(candles))
        # FIX: prior streak bodies = all streak candles EXCEPT the current
        # one (the last in the range). The current body is what we compare
        # against the prior average to detect shrinking.
        prior_streak_bodies = [
            abs(candles[i]["close"] - candles[i]["open"])
            for i in range(-lookback, -1)  # exclude current (index -1)
        ]
        avg_body = (sum(prior_streak_bodies) / len(prior_streak_bodies)
                    if prior_streak_bodies else 0)
        last_body_signed = last["close"] - last["open"]
        last_body_abs = abs(last_body_signed)
        # Shrinking body = trend losing momentum
        if avg_body > 0 and last_body_abs < avg_body * 0.60:
            if streak_dir == 1:
                # Long uptrend with shrinking last body → weak PUT reversal
                results.append(ModuleResult(
                    module_name="trend_follow", direction="PUT", score=1, confidence=53,
                    signal_type="REVERSAL", reliability="TREND", group="TREND_EXHAUST",
                    reasons=[f"Trend exhaustion: {consec} UP streak, last body {last_body_abs/avg_body:.0%} of avg → weak PUT (trend tiring)"]))
            elif streak_dir == -1:
                # Long downtrend with shrinking last body → weak CALL reversal
                results.append(ModuleResult(
                    module_name="trend_follow", direction="CALL", score=1, confidence=53,
                    signal_type="REVERSAL", reliability="TREND", group="TREND_EXHAUST",
                    reasons=[f"Trend exhaustion: {consec} DOWN streak, last body {last_body_abs/avg_body:.0%} of avg → weak CALL (trend tiring)"]))

    # ═══════════════════════════════════════════════════════════════════════
    # NEW SIGNAL 7: Pullback Entry (NEW — real market classic)
    # In an uptrend, wait for a 2-3 candle pullback, then enter CALL
    # when price resumes up. Mirror for downtrend.
    # This is the "buy the dip" / "sell the rally" strategy.
    #
    # FIX (AUDIT-DEEP #02, 2026-07-23): the previous "still above prior low"
    # check used `c2["close"] > candles[-3]["close"]` — that's "current close
    # > close 3 candles ago", NOT "current close > prior low". The pullback
    # should confirm the trend is intact by checking price is still above the
    # recent swing low (not just above an arbitrary older close). Same bug
    # for the downtrend mirror. Now uses min/max of recent lows/highs so the
    # check actually verifies trend structure is preserved.
    #
    # FIX (DEEP-ANALYSIS, 2026-07-23): added explicit len(candles) >= 5
    # guard so candles[-3] and candles[-4] never IndexError. The outer
    # `len(candles) >= 5` check at line ~305 was added but the pullback
    # block also accesses candles[-4] which needs the same guard.
    # ═══════════════════════════════════════════════════════════════════════
    if is_trending and trend_strength > 0.4 and len(candles) >= 5:
        # Check for pullback: last 2 candles against trend
        if trend_regime == "TREND_UP":
            # Look for 2 consecutive down candles (pullback)
            c1 = candles[-2]
            c2 = candles[-1]
            # FIX: prior swing low = min of the lows of the 3 candles BEFORE
            # the pullback started (candles[-4] and candles[-3]). Using min
            # of highs would be wrong; using min of lows gives the actual
            # recent swing low that the pullback must hold above.
            prior_swing_low = min(candles[-3]["low"], candles[-4]["low"])
            if (c1["close"] < c1["open"] and c2["close"] < c2["open"]
                    and c2["close"] > prior_swing_low  # FIX: was candles[-3]["close"]
                    and c2["close"] > ema9):  # still above EMA9
                # Pullback in uptrend → CALL
                results.append(ModuleResult(
                    module_name="trend_follow", direction="CALL", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_PULLBACK",
                    reasons=[f"Pullback entry: 2 down candles in uptrend (str={trend_strength:.2f}) → CALL continuation (buy the dip)"]))
        elif trend_regime == "TREND_DOWN":
            # Look for 2 consecutive up candles (rally)
            c1 = candles[-2]
            c2 = candles[-1]
            # FIX: prior swing high = max of recent highs. The pullback
            # (rally) must stay below this for the downtrend to be intact.
            prior_swing_high = max(candles[-3]["high"], candles[-4]["high"])
            if (c1["close"] > c1["open"] and c2["close"] > c2["open"]
                    and c2["close"] < prior_swing_high  # FIX: was candles[-3]["close"]
                    and c2["close"] < ema9):  # still below EMA9
                # Rally in downtrend → PUT
                results.append(ModuleResult(
                    module_name="trend_follow", direction="PUT", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="TREND", group="TREND_PULLBACK",
                    reasons=[f"Pullback entry: 2 up candles in downtrend (str={trend_strength:.2f}) → PUT continuation (sell the rally)"]))

    # ═══════════════════════════════════════════════════════════════════════
    # NEW SIGNAL 8: EMA Bounce (NEW — classic)
    # In an uptrend, price bounces off EMA9 → CALL
    # In a downtrend, price bounces off EMA9 → PUT
    # DISABLED (ultra-deep, 2026-07-20): 47.5% win rate on 2388 signals —
    # EMA9 bounce on 1m candles is noise. The EMA9 * 1.002 tolerance is too
    # wide, catching almost every candle near EMA9. Removed.
    # if is_trending and trend_strength > 0.3 and ema9 > 0 and atr > 0:
    #     ... (disabled)

    # ═══════════════════════════════════════════════════════════════════════
    # NEW SIGNAL 9: Momentum Divergence (NEW — classic)
    # Price makes higher high but body shrinks = bearish divergence
    # Price makes lower low but body shrinks = bullish divergence
    # TIGHTENED (ultra-deep, 2026-07-20): 48.9% win rate on 2309 signals.
    # Now requires body shrink < 0.4× (was 0.6×) for more extreme divergence.
    # Score reduced 2→1 since signal is weak on 1m candles.
    if len(candles) >= 5 and atr > 0:
        window = candles[-5:]
        if (window[-1]["high"] > window[-3]["high"]
                and abs(window[-1]["close"] - window[-1]["open"]) < abs(window[-3]["close"] - window[-3]["open"]) * 0.4
                and window[-1]["close"] > window[-1]["open"]):
            results.append(ModuleResult(
                module_name="trend_follow", direction="PUT", score=1, confidence=53,
                signal_type="REVERSAL", reliability="TREND", group="TREND_DIVERGE",
                reasons=[f"Bearish divergence: higher high but body <40% of prior → PUT reversal"]))
        elif (window[-1]["low"] < window[-3]["low"]
                and abs(window[-1]["close"] - window[-1]["open"]) < abs(window[-3]["close"] - window[-3]["open"]) * 0.4
                and window[-1]["close"] < window[-1]["open"]):
            results.append(ModuleResult(
                module_name="trend_follow", direction="CALL", score=1, confidence=53,
                signal_type="REVERSAL", reliability="TREND", group="TREND_DIVERGE",
                reasons=[f"Bullish divergence: lower low but body <40% of prior → CALL reversal"]))

    return results
