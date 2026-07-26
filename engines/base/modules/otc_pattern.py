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
from core.analysis import _atr as _compute_atr


# ─── Algorithm-aware gating helper ──────────────────────────────────────
# FIX (OTC-DEEP-REPORT Phase 1, 2026-07-23): integrate algorithm_monitor
# state directly into the module's signal-firing logic. The previous
# design relied on the blender's downstream strategy multipliers, which
# added a 5+ candle lag between algo change and strategy adjustment.
# Now we query the algorithm_monitor's CURRENT guess directly and use
# it to gate which signals fire. This is PROACTIVE — by the time the
# blender's strategy cooldown kicks in, the module has already adapted.
#
# Returns one of: "trending" | "reversing" | "neutral" | "unknown"
# "unknown" when there's not enough data — caller should default to
# conservative behavior (don't enable risky signals).
def _get_algo_gate(asset: str) -> str:
    """Query algorithm_monitor for the current algorithm guess.

    Looks up the in-memory rolling window for this asset and returns
    the algorithm_guess: 'trending', 'reversing', 'random_walk', or
    'unknown' (insufficient samples).
    """
    try:
        from core.algorithm_monitor import _WINDOWS, _LAST_ALGO_GUESS
        window = _WINDOWS.get(asset)
        if not window or len(window) < 15:
            return "unknown"
        return _LAST_ALGO_GUESS.get(asset, "unknown")
    except Exception:
        return "unknown"


def analyze(candles, ctx: MarketContext, asset: str = "") -> list:
    """Run OTC-specific pattern detection.

    Returns list of ModuleResult objects.

    FIX (OTC-DEEP-REPORT Phase 1, 2026-07-23): now accepts an `asset`
    parameter so we can query the algorithm_monitor's state directly
    and gate signals based on the CURRENT algorithm (trending/reversing/
    random_walk/unknown). The blender passes `asset` via the standard
    module call signature `(candles, ctx)` — we accept it as a keyword
    arg with default empty string for backward compat. When asset is
    empty (test context), we skip algorithm gating and use the old
    trend_strength-based gating only.
    """
    results = []
    if len(candles) < 10:
        return results

    stats = ctx.stats
    regime = ctx.regime
    is_trending = regime.get("is_trending", False)
    trend_regime = regime.get("regime", "RANGE")
    trend_strength = regime.get("trend_strength", 0.0)

    # ── Algorithm-aware gating ──────────────────────────────────────────
    # Phase 1: query the algorithm_monitor's current guess for this asset.
    # When the algorithm is 'trending', we DAMPEN mean-reversion signals
    # (the broker's trending algo continues moves, doesn't reverse them).
    # When 'reversing', we can RE-ENABLE the previously disabled Signal 3
    # (Z-score extreme) because in a reversing algo, Z-score extremes DO
    # indicate exhaustion. When 'unknown' (insufficient data), we use
    # the old trend_strength-based gating as fallback.
    algo_gate = _get_algo_gate(asset) if asset else "unknown"
    algo_is_trending = (algo_gate == "trending")
    algo_is_reversing = (algo_gate == "reversing")
    # If algo unknown, fall back to regime-based trend detection.
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-30): use >= 0.5 instead of
    # strict > 0.5 so trend_strength = 0.5 (the candle_reaction module's
    # strong-trend threshold) is consistently treated as trending across
    # both modules. Previously, otc_pattern gated at > 0.5 but
    # candle_reaction dampened at > 0.5, leaving the boundary case
    # inconsistent between the two modules.
    effective_trending = algo_is_trending or (algo_gate == "unknown" and is_trending and trend_strength >= 0.5)

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
    # FIX (DEEP-AUDIT-2026-07-26 / F-10-20, A-05 ISSUE S5): defensive .get()
    # defaults for cold-start safety. Defaults match the cold-start sentinel
    # values from analysis.py (current_streak=0, streak_direction=0,
    # streak_rarity=0, z_body=0, close_percentile=50).
    consec = stats.get("current_streak", 0)
    streak_dir = stats.get("streak_direction", 0)
    # FIX (OTC-DEEP Phase 1): use effective_trending which considers BOTH
    # the algorithm_monitor's guess AND the regime-based trend_strength.
    # When algo is 'trending', we gate mean-reversion even if regime is
    # RANGE (the algo is the higher-authority signal).
    mean_rev_gated = effective_trending
    # ATR-normalized streak filter: average body of the streak must be
    # at least 0.3 ATR (lowered from 0.4 — backtest showed 0.4 was too
    # strict and filtered out genuine OTC mean-reversion setups).
    atr_val = ctx.atr if ctx.atr > 0 else 0.0001
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-1-15): compute a SEPARATE
    # streak-filter ATR from `candles[:-consec]` (excluding the streak
    # candles themselves). The previous `atr_val = ctx.atr` was computed over
    # the last 20 candles INCLUDING the streak — a strong streak with large
    # bodies inflated the ATR, raising the 0.3×ATR threshold and filtering
    # OUT the very streaks the signal is designed to catch (feedback loop).
    # The streak_atr is used only for the threshold check; the reason text
    # still uses the original `atr_val` for consistency with other modules.
    if consec >= 1 and len(candles) > consec + 5:
        _streak_atr = _compute_atr(candles[:-consec], 20)
        streak_atr = _streak_atr if _streak_atr > 0 else atr_val
    else:
        streak_atr = atr_val
    streak_bodies = [
        abs(candles[i]["close"] - candles[i]["open"])
        for i in range(-min(consec, len(candles)), 0)
    ]
    avg_streak_body = (sum(streak_bodies) / len(streak_bodies)) if streak_bodies else 0
    streak_is_meaningful = avg_streak_body >= streak_atr * 0.3
    if consec >= 3 and not mean_rev_gated and streak_is_meaningful:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=62,
                signal_type="REVERSAL", reliability="OTC", group="OTC_MEANREV",
                # FIX (DEEP-AUDIT-2026-07-26 / F-10-21, A-05 P6/CRIT): display
                # the actual ratio against `streak_atr` (the ATR used by the
                # threshold check at line 157), not `atr_val` (the full-window
                # ATR which inflates with the streak). Operators reading the
                # reason now see the same ratio the threshold gate applied.
                reasons=[f"OTC mean-rev: {consec}+ UP (avg body {avg_streak_body/streak_atr:.2f}x ATR) → PUT (62% reversal in OTC, regime={trend_regime}, str={trend_strength:.2f})"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=62,
                signal_type="REVERSAL", reliability="OTC", group="OTC_MEANREV",
                reasons=[f"OTC mean-rev: {consec}+ DOWN (avg body {avg_streak_body/streak_atr:.2f}x ATR) → CALL (62% reversal in OTC, regime={trend_regime}, str={trend_strength:.2f})"]))

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
    if consec >= 3 and stats.get("streak_rarity", 0) < 0.10:
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-24): use `effective_trending`
        # (algo-aware) instead of `is_trending` (regime-only) so a rare streak
        # in a "trending" algo with regime RANGE is correctly dampened. In a
        # trending algo, a rare same-direction streak is trend continuation,
        # not exhaustion. Previously, SIGNAL 2 fired at full strength in this
        # case while SIGNAL 1 (which uses effective_trending) was gated —
        # inconsistent gating between the two reversal signals.
        aligned_with_trend = (
            effective_trending
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
                    reasons=[f"Rare streak (n={consec}, rarity={stats.get('streak_rarity', 0):.0%}, trend_str={trend_strength:.2f}) → PUT reversal boost"]))
            elif streak_dir == -1:
                results.append(ModuleResult(
                    module_name="otc_pattern", direction="CALL", score=score, confidence=conf,
                    signal_type="REVERSAL", reliability="OTC", group="OTC_RARITY",
                    reasons=[f"Rare streak (n={consec}, rarity={stats.get('streak_rarity', 0):.0%}, trend_str={trend_strength:.2f}) → CALL reversal boost"]))

    # ── REVERSAL SIGNAL 3: Z-score extreme reversal ──────────────────────
    # Originally disabled (live data, 2026-07-20) because real Quotex data
    # showed 0% win rate. Z-score extremes in OTC are broker momentum
    # spikes, not exhaustion — the broker pushes price further.
    #
    # FIX (OTC-DEEP-REPORT Phase 2, 2026-07-23): RE-ENABLED with algorithm
    # gating. The 0% win rate was because the signal fired on BOTH trending
    # and reversing regimes without discrimination. During a TRENDING
    # algorithm, Z-score spikes = momentum continuation (NOT reversal).
    # During a REVERSING algorithm, Z-score spikes DO indicate exhaustion.
    # Now we fire ONLY when algo_is_reversing is True. This recovers a
    # valuable signal without re-introducing the 0% win rate problem.
    #
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-25): use >= 2.5 (inclusive)
    # so a Z-score of exactly 2.5 (still a top-0.6% body) fires. Combined
    # with the AUDIT-2-03 z_body self-inclusion dampening in analysis.py
    # (which lowers a true z=2.7 to ~2.4-2.5), the strict > was suppressing
    # borderline-extreme signals that the Phase 2 re-enable intended to
    # recover.
    if algo_is_reversing and stats.get("z_body", 0) >= 2.5:
        last = candles[-1]
        body = last["close"] - last["open"]
        _z_body = stats.get("z_body", 0)
        # In a reversing algorithm, an extreme Z-score = exhaustion spike.
        # Bet the OPPOSITE direction of the body (mean reversion).
        if body > 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=3, confidence=65,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ZSCORE",
                reasons=[f"Z-score extreme (Z={_z_body:.1f}) in REVERSING algo → PUT reversal (exhaustion spike)"]))
        elif body < 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=3, confidence=65,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ZSCORE",
                reasons=[f"Z-score extreme (Z={_z_body:.1f}) in REVERSING algo → CALL reversal (exhaustion spike)"]))
    # ── REVERSAL SIGNAL 4: Close percentile extreme ──────────────────────
    # FIX (OTC issue 1, 2026-07-19): same gating as Signal 3 — a close at
    # the 95th percentile during a strong uptrend is trend continuation,
    # not reversal. Soft-gate aligned-with-trend extremes.
    # FIX (OTC-6 backtest, 2026-07-20): also check streak alignment — if
    # the close is at the 95th percentile but the streak is DOWN (a
    # counter-trend bounce to the upside), firing PUT reversal is wrong
    # (the bounce is already exhausting). Skip when streak opposes the
    # percentile direction.
    pctile = stats.get("close_percentile", 50)
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-29): use `effective_trending`
    # (algo-aware) instead of `is_trending` (regime-only) so a close at the
    # 95th percentile in a "trending" algo with regime RANGE is correctly
    # dampened. Same inconsistency as AUDIT-4-24 — in trending-algo +
    # RANGE-regime, the close-at-95th-percentile fired reversal at full
    # strength when it should be dampened (the close is likely trend
    # continuation, not reversal).
    pctile_aligns_with_trend = (
        effective_trending
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
    #
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-26): include the doji case
    # (consec == 0). A doji after a strong move IS an alternation signal
    # (indecision → reversal) — the most predictive alternation setup.
    # The previous strict `consec == 1` missed the doji-after-streak pattern.
    #
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-27): check DIRECTION AGREEMENT
    # for prior_reversal_fired. The previous check blocked alternation if
    # ANY prior reversal fired regardless of direction — so a prior PUT
    # reversal followed by an UP body (alternation predicts PUT, agreeing
    # with the prior) was blocked even though it would REINFORCE the prior.
    # Now we only block alternation when a prior reversal AGREES with the
    # alternation direction (e.g. prior PUT blocks alternation PUT).
    if consec in (0, 1) and stats.get("streak_rarity", 0) > 0.30 and stats.get("z_body", 0) < 0.5:
        last = candles[-1]
        body = last["close"] - last["open"]
        prior_put_reversal = any(
            r.signal_type == "REVERSAL" and r.direction == "PUT" for r in results
        )
        prior_call_reversal = any(
            r.signal_type == "REVERSAL" and r.direction == "CALL" for r in results
        )
        if body > 0 and not prior_put_reversal:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=1, confidence=53,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ALTERNATE",
                reasons=["OTC alternation bias (small body, 53% opposite) → PUT"]))
        elif body < 0 and not prior_call_reversal:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=1, confidence=53,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ALTERNATE",
                reasons=["OTC alternation bias (small body, 53% opposite) → CALL"]))

    # ═══════════════════════════════════════════════════════════════════════
    #  CONTINUATION SIGNALS (NEW, 2026-07-18)
    #  These counterbalance the 5 reversal signals above so the OTC engine
    #  can recognize trends instead of always betting on reversal.
    # ═══════════════════════════════════════════════════════════════════════

    # FIX (DEEP-AUDIT-2026-07-26 / F-10-22, A-05 P31/P44): hoist `last` and
    # `last_body` to a single computation at the top of the continuation
    # block — previously recomputed 3x (lines 233, 317, 342) within
    # analyze(). One dict lookup + one subtraction saved per call. The
    # signals above (1-5) used their own inline `last = candles[-1]` for
    # cold-start isolation; we keep that for the reversal branch but
    # consolidate the continuation branch here.
    last = candles[-1]
    last_body = last["close"] - last["open"]
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-23): removed the dead
    # `last_body_abs = abs(last_body)` assignment — the variable was computed
    # but never used (SIGNAL 6 uses `last_body` (signed) at lines ~386/391
    # after the F-10-22 hoist).
    # FIX (DEEP-AUDIT-2026-07-26 / F-10-23, A-05 P32): the stale comment
    # above previously referenced line numbers 294/299 (SIGNAL 6 in an old
    # layout) — updated to reflect the current line numbers.

    # ── CONTINUATION SIGNAL 6: Momentum push ─────────────────────────────
    # A short streak (1-2) with a GROWING body (z_body in 0.5-2.0 range —
    # above average but not yet extreme) is a momentum push, not exhaustion.
    # This is the early-trend signal that the old engine completely missed.
    #
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-1-06): gate on
    # `effective_trending` so Signal 6 doesn't fire continuation votes in
    # reversing or random_walk algorithms (where Signals 1-5 correctly bet
    # reversal). Previously Signal 6 fired in any algorithm, casting a
    # continuation vote that offset the reversal majority and diluted
    # confidence. Now Signal 6 only fires in confirmed-trending algos.
    #
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-28): extend the z_body range
    # from < 2.0 to < 2.5 so bodies in [2.0, 2.5] (the 2-5% largest) get a
    # continuation vote instead of falling through the gap between Signal 6
    # (was < 2.0) and Signal 3 (was > 2.5, now >= 2.5).
    # FIX (DEEP-AUDIT-2026-07-26 / F-10-26, A-05 ISSUE S5): .get() for z_body
    # cold-start safety. Cache in local var to avoid repeated dict lookups
    # in the reason text below.
    _z_body_s6 = stats.get("z_body", 0)
    if effective_trending and 1 <= consec <= 2 and 0.5 <= _z_body_s6 < 2.5:
        if streak_dir == 1 and last_body > 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="OTC", group="OTC_MOMENTUM",
                reasons=[f"OTC momentum push: {consec} UP + growing body (Z={_z_body_s6:.1f}) → CALL continuation"]))
        elif streak_dir == -1 and last_body < 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="OTC", group="OTC_MOMENTUM",
                reasons=[f"OTC momentum push: {consec} DOWN + growing body (Z={_z_body_s6:.1f}) → PUT continuation"]))

    # ── CONTINUATION SIGNAL 7 (Breakout) — REMAINS DISABLED (43% win rate) ──
    # FIX (DEEP-AUDIT-2026-07-26 / F-10-24, A-05 P33): removed 5-line
    # commented-out SIGNAL 7 dead block. Originally disabled on 2026-07-20
    # (real Quotex data showed 43% win rate — OTC breakouts fail because the
    # broker reverses them; the broker's algorithm creates false breakouts).
    # Re-enablement requires backtest proof of >= 50% win rate.

    # ── CONTINUATION SIGNAL 8: Strong-trend streak (RE-ENABLED with algo gating) ──
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-1-04): RE-ENABLED with
    # algorithm_monitor gating. The previous version fired in fake trends
    # (regime-only check `is_trending and trend_strength > 0.35`) and got
    # 43.2% win rate, which disabled it. With proper algorithm gating —
    # ONLY firing when `algo_is_trending` (the algorithm_monitor's actual
    # trending detection, the higher-authority signal) — this signal
    # provides the continuation balance the OTC engine needs to follow
    # strong trends instead of betting against every one. Without Signal 8,
    # a 3+ candle streak in a confirmed trending algo gets ZERO continuation
    # votes (Signal 6 only fires for 1-2 candle streaks), leaving the engine
    # structurally reversal-biased.
    if algo_is_trending and consec >= 2:
        # FIX (DEEP-AUDIT-2026-07-26 / F-10-25, A-05 P7/CRIT): add the missing
        # `last_body > 0` / `last_body < 0` guard so SIGNAL 8 only fires when
        # the LAST candle's body matches `streak_dir`. The previous version
        # fired CALL on a 2-candle up streak even if the last candle was a
        # doji or a small red body — misleading continuation vote.
        if streak_dir == 1 and last_body > 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="OTC", group="OTC_TRENDSTREAK",
                reasons=[f"OTC trend-streak: {consec}+ UP in confirmed trending algo → CALL continuation (broker trend continues)"]))
        elif streak_dir == -1 and last_body < 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="OTC", group="OTC_TRENDSTREAK",
                reasons=[f"OTC trend-streak: {consec}+ DOWN in confirmed trending algo → PUT continuation (broker trend continues)"]))

    return results
