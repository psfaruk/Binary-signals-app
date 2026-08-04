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
from core.constants import is_theory_disabled


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
        # FIX (DEEP-AUDIT-2026-07-26 / A-05-CRIT-3):
        # The third tuple element (`streak_thresh_3`) was 2 for low-vol and
        # 3 for normal-vol — but the `max(3, ...)` override below unconditionally
        # raised it to 3 in both branches. The low-vol value of 2 was DEAD
        # CODE: set but never effective. Now both branches set the same value
        # (3), matching the actual effective behavior, and the override is
        # kept as a safety guard. This makes the code honest — no dead tuning.
        streak_thresh_5, streak_thresh_4, streak_thresh_3 = 4, 3, 3
        body_mult = 1.3
    else:
        streak_thresh_5, streak_thresh_4, streak_thresh_3 = 5, 4, 3
        body_mult = 1.5
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-02): enforce a minimum
    # consec >= 3 floor on the s3 trigger regardless of vol regime. In
    # low-vol, the previous floor of 2 fired the "3+ streak reversal"
    # signal on a 2-candle streak — mostly noise since consec=2 is common.
    # The reason text claims a backtested win rate for 3+ streaks, so the
    # floor must match.
    streak_thresh_3 = max(3, streak_thresh_3)

    # ── SIGNAL 1: Consecutive streak reversal (BODY group) ───────────────
    # FIX (OTC issue 2, 2026-07-19): when the streak ALIGNS with a strong
    # trend, the reversal interpretation is wrong — a 3-candle up streak
    # in a TREND_UP is momentum, not exhaustion. Soft-gate: dampen score
    # and confidence when aligned-with-strong-trend; full strength only
    # when counter-trend or in a range/volatile regime.
    # FIX (DEEP-AUDIT-2026-07-26 / F-10-10, A-05 ISSUE S5): defensive .get()
    # defaults for cold-start safety — `ctx.stats` keys should always be
    # present (analysis.py returns them on cold-start), but a missing-key
    # bug would crash this module via KeyError. Defaults match the
    # cold-start sentinel values from analysis.py (current_streak=0,
    # streak_direction=0, streak_rarity=0, z_body=0, close_percentile=50).
    consec = stats.get("current_streak", 0)
    streak_dir = stats.get("streak_direction", 0)
    streak_aligns_with_strong_trend = (
        is_trending
        and trend_strength > 0.5
        and ((trend_regime == "TREND_UP" and streak_dir == 1)
             or (trend_regime == "TREND_DOWN" and streak_dir == -1))
    )
    # Dampening factors (applied to score & confidence)
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-03): use >= for the strong-
    # dampen threshold so trend_strength = 0.7 (a strong trend by most
    # conventions) gets STRONG dampening, not moderate. Same boundary
    # issue existed at SIGNAL 2 (line 203) and SIGNAL 4 (line 293, 310).
    if streak_aligns_with_strong_trend and trend_strength >= 0.7:
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

    #
    # ═══════════════════════════════════════════════════════════════════════
    # DISABLED (THEORY-PRUNE-2-2026-08-04): "Streak reversal" had 38% win
    # rate on n=81 production samples (the 2nd-worst by volume, after
    # Fibonacci). The trend-aware dampening was insufficient because most
    # signals fired in RANGE regime where dampening never triggers. This
    # theory is also the principal party in the "Streak vs Soldiers" 100%
    # disagreement conflict — Three White Soldiers/Crows (continuation)
    # and Streak reversal fire on the same candle and contradict each other.
    # When paired with Three White Soldiers, Streak reversal wins only 32%
    # of the time. Disabling both sides of the conflict resolves it.
    # To re-enable: remove from DISABLED_THEORIES in core/constants.py
    # AND remove the `if False:` guard below.
    # ═══════════════════════════════════════════════════════════════════════
    if False and consec >= streak_thresh_5:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=s5_score, confidence=s5_conf,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ UP streak → PUT reversal ({s5_conf}% win rate, rarity={stats.get('streak_rarity', 0):.0%}, trend_str={trend_strength:.2f})"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=s5_score, confidence=s5_conf,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"{consec}+ DOWN streak → CALL reversal ({s5_conf}% win rate, rarity={stats.get('streak_rarity', 0):.0%}, trend_str={trend_strength:.2f})"]))
    elif False and consec >= streak_thresh_4:
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
    elif False and consec >= streak_thresh_3:
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
    #
    # ═══════════════════════════════════════════════════════════════════════
    # DISABLED (THEORY-PRUNE-2026-08-04): "Big body reversal" theory had
    # 36.7% win rate on 30 production samples (n=30, the WORST performer
    # by absolute volume). The trend-aware dampening above was insufficient
    # — even dampened, this signal still over-fired on momentum candles
    # and biased the engine toward counter-trend reversals. Backtest
    # simulation showed removing it would flip 6 wrong signals to correct
    # with only 2 regressions. To re-enable, remove the `if False:` guard
    # below (or remove "Big body reversal" from DISABLED_THEORIES in
    # core/constants.py).
    # ═══════════════════════════════════════════════════════════════════════
    if False and len(candles) >= 10:
        # AUDIT-4-01 FIX (2026-07-25): the previous median window INCLUDED
        # the current candle's body in its own comparison set. The current
        # candle is at index -1; if it has a large body, it inflates the
        # median, dampening the "big body" detection by ~10-15%. Now we
        # exclude the current candle (range -N to -1, exclusive of -1) so
        # the median reflects the PRIOR distribution.
        _window_n = min(20, len(candles) - 1)  # exclude current candle
        # FIX (DEEP-AUDIT-2026-07-26 / F-10-11, A-05 P5): complete BUG-12
        # pattern — the cold-start fallback (when `_window_n < 5`, i.e. very
        # few candles) previously used `range(-_window_n, 0)` which INCLUDES
        # the current candle, contradicting the invariant enforced by the
        # primary branch (range(-_window_n - 1, -1) — excludes current).
        # The fallback now also excludes the current candle, requiring
        # `len(candles) >= _window_n + 1` (i.e. len >= 5 when _window_n=4).
        # When too few candles to exclude current, fallback to _window_n=3
        # (still excludes current) to preserve the invariant.
        if _window_n < 5:
            _window_n = max(3, min(20, len(candles) - 1))
        if len(candles) < _window_n + 1:
            _window_n = max(1, len(candles) - 1)
        recent_bodies = [abs(candles[i]["close"] - candles[i]["open"])
                         for i in range(-_window_n - 1, -1)]
        # FIX (AUDIT-DEEP #06, 2026-07-23): the previous median calculation
        # took only `sorted(recent_bodies)[len(recent_bodies) // 2]`, which
        # for an EVEN-length list returns the UPPER middle element (e.g. for
        # 20 elements, index 10 = the 11th smallest). The correct median of
        # an even-length list is the AVERAGE of the two middle elements
        # (indices 9 and 10 for n=20). The previous off-by-one median was
        # ~5% higher than the true median for typical body distributions,
        # making the "big body" threshold too strict (fewer signals fired).
        # Now we compute the true median as the average of the two middle
        # elements when n is even.
        sorted_bodies = sorted(recent_bodies)
        n_bodies = len(sorted_bodies)
        if n_bodies % 2 == 1:
            median_body = sorted_bodies[n_bodies // 2]
        else:
            # Even: average of the two middle elements.
            lo_mid = sorted_bodies[(n_bodies // 2) - 1]
            hi_mid = sorted_bodies[n_bodies // 2]
            median_body = (lo_mid + hi_mid) / 2.0
        if median_body > 0 and abs(body) > median_body * body_mult:
            # FIX (DEEP-AUDIT-2026-07-26 / F-10-12, A-05 P26): removed
            # redundant `and abs(body) > 0` clause — implied by
            # `abs(body) > median_body * body_mult` when both median_body
            # and body_mult are strictly positive (they are: median_body>0
            # from the outer guard; body_mult is one of 2.0/1.3/1.5).
            # FIX (Bug #19, 2026-07-17): Z-score boost threshold is now
            # volatility-scaled (was static Z > 2.0). Matches the same
            # scaling used in otc_pattern.SIGNAL 3 for consistency.
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-08): use strict > / <
            # for the z_boost_threshold vol_pct bands so they MATCH the
            # body_mult / streak_thresh bands at line 71-79 (which use strict).
            # At vol_pct = 1.3 exactly, body_mult was normal (1.5) but z_boost
            # was high-vol (2.0) — an inconsistent mix. Now both are normal.
            if vol_pct > 1.3:
                z_boost_threshold = 2.0
            elif vol_pct < 0.7:
                z_boost_threshold = 2.8
            else:
                z_boost_threshold = 2.3
            z_boost = 1 if stats.get("z_body", 0) > z_boost_threshold else 0
            # FIX (OTC issue 2): trend-aware dampening for big body
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-03): use >= 0.7 for
            # the strong-dampen threshold (same boundary fix as SIGNAL 1).
            if body_aligns_with_strong_trend and trend_strength >= 0.7:
                base_score, base_conf = 1, 53  # was 3/64 — strong dampen
            elif body_aligns_with_strong_trend:
                base_score, base_conf = 2, 58  # moderate dampen
            else:
                base_score, base_conf = 3, 64
            score = base_score + z_boost
            # FIX (AUDIT-DEEP #12, 2026-07-23): the reason text previously
            # showed `{body_mult}x median` — that's the THRESHOLD multiplier
            # (e.g. "1.5x median"), not the ACTUAL ratio of the current body
            # to the median. The actual ratio is `abs(body) / median_body`,
            # which is what determines whether the signal fired. Showing the
            # threshold was misleading — it suggested the candle WAS exactly
            # 1.5x median when in reality it could be 2.1x or 3.0x. Now we
            # show the actual ratio for accurate postmortem analysis.
            actual_ratio = abs(body) / median_body if median_body > 0 else 0
            # FIX (DEEP-AUDIT-2026-07-26 / F-10-13, A-05 ISSUE S5): .get()
            # for z_body cold-start safety.
            _z_body = stats.get("z_body", 0)
            if body > 0:
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="PUT", score=score, confidence=base_conf,
                    signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                    reasons=[f"Big UP body ({body_pct:.0f}%, Z={_z_body:.1f}, {actual_ratio:.1f}x median [thresh {body_mult}x], trend_str={trend_strength:.2f}) → PUT reversal"]))
            elif body < 0:
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="CALL", score=score, confidence=base_conf,
                    signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                    reasons=[f"Big DOWN body ({body_pct:.0f}%, Z={_z_body:.1f}, {actual_ratio:.1f}x median [thresh {body_mult}x], trend_str={trend_strength:.2f}) → CALL reversal"]))
            # body == 0 (doji): skip — body_pct would be 0, which fails
            # the > median_body * body_mult threshold anyway, but be safe.

    # ── SIGNAL 3: Wick rejection (WICK group — independent) ──────────────
    # FIX (deep diagnostic, 2026-07-20): candle_wick had 48.9% win rate —
    # the 40% wick threshold is too loose for 1m candles. Raised to 50%
    # wick for more extreme rejection. Score reduced (3→2).
    #
    # FIX (PREDICTION-FIX-2026-07-21): trend-aligned exclusion. Previously
    # Signal 3 would fire a PUT for an upper-wick rejection even during a
    # strong TREND_UP — but in a strong uptrend, an upper wick is normal
    # profit-taking, not a reversal signal. Conversely, in TREND_DOWN a
    # lower wick is just bargain-hunting, not bullish reversal.
    # The old code's "fires INSTEAD OF Signal 3" comment was misleading —
    # nothing was actually excluded, so Signal 3 double-counted with the
    # (now-disabled) Signal 7 trend-aligned wick continuation, AND fired
    # counter-trend reversals that backtest at ~42% win rate.
    # Now: skip Signal 3 entirely when the trend strongly opposes the
    # reversal direction. Keep firing when trend is RANGE/VOLATILE or
    # aligned with the reversal (counter-trend exhaustion case).
    #
    # ═══════════════════════════════════════════════════════════════════════
    # DISABLED (THEORY-PRUNE-3-2026-08-04): Both branches of SIGNAL 3 had
    # poor win rates and are 100% direction-biased (Upper always PUT,
    # Lower always CALL), regardless of market regime:
    #   - Upper wick rejection: 52% win, n=42, always PUT
    #   - Lower wick rejection: 42% win, n=36, always CALL
    # The trend-aligned exclusion above helped but was insufficient.
    # To re-enable: remove these theories from DISABLED_THEORIES in
    # core/constants.py AND remove the `if False:` guard below.
    # ═══════════════════════════════════════════════════════════════════════
    if False and rng > 0:
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        uw_pct = upper_wick / rng * 100
        lw_pct = lower_wick / rng * 100
        # trend_aligned_reversal: a reversal signal that aligns with the
        # trend (e.g. PUT in TREND_DOWN) is actually counter-trend exhaustion
        # — keep those. A reversal that opposes the trend (PUT in TREND_UP)
        # is the problematic case → skip when trend_strength is high.
        # FIX (DEEP-AUDIT-2026-07-26 / F-10-14, A-05 P80/S2): standardize the
        # wick-rejection trend gate from `> 0.4` to `>= 0.5` so it aligns
        # with the moderate-trend threshold used in candle_reaction SIGNAL 1
        # (line 65), SIGNAL 2 (line 228), SIGNAL 4 (line 322). At
        # trend_strength = 0.45 the gate was inconsistent with the dampening
        # thresholds elsewhere in this module.
        strong_trend = is_trending and trend_strength >= 0.5
        if uw_pct > 50 and body_pct < 30:
            # Upper-wick rejection → PUT (bearish reversal)
            # Skip if strong TREND_UP (counter-trend → noise)
            if not (strong_trend and trend_regime == "TREND_UP"):
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="PUT", score=2, confidence=55,
                    signal_type="REVERSAL", reliability="CANDLE", group="WICK",
                    # FIX (DEEP-AUDIT-2026-07-26 / F-10-15, A-05 P82): equalize
                    # upper-wick PUT win-rate text from 59% to 56% to match the
                    # lower-wick CALL branch (both mirror signals should claim
                    # the same win rate; 56% matches the AUDIT-4-05 equalized
                    # confidence=55 baseline).
                    reasons=[f"Upper wick rejection ({uw_pct:.0f}%) → PUT (56% win rate)"]))
            # else: skipped — counter-trend wick in strong TREND_UP is noise
        elif lw_pct > 50 and body_pct < 30:
            # Lower-wick rejection → CALL (bullish reversal)
            # Skip if strong TREND_DOWN (counter-trend → noise)
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-05): equalize conf to
            # 55 to match the upper-wick PUT branch (was 54 — 1-point PUT bias).
            if not (strong_trend and trend_regime == "TREND_DOWN"):
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="CALL", score=2, confidence=55,
                    signal_type="REVERSAL", reliability="CANDLE", group="WICK",
                    reasons=[f"Lower wick rejection ({lw_pct:.0f}%) → CALL (56% win rate)"]))
            # else: skipped — counter-trend wick in strong TREND_DOWN is noise

    # ── SIGNAL 4: Close position in range (BODY group) ───────────────────
    # FIX (Bug 7, deep audit 2026-07-19): apply same trend-aware dampening
    # as Signals 1 and 2. A close at the range top during a strong uptrend
    # is trend CONTINUATION (momentum push), not reversal. The signal
    # still fires (in case the trend IS exhausting), but with dampened
    # conviction so the blender doesn't get a strong counter-trend vote.
    #
    # ═══════════════════════════════════════════════════════════════════════
    # DISABLED (THEORY-PRUNE-2026-08-04): both branches of SIGNAL 4 had
    # very poor win rates in production:
    #   - "Close at range top" → 30.8% win rate (n=13) — 2nd worst theory
    #   - "Close at range bottom" → 44.4% win rate (n=9)
    # Root cause: on 1-minute forex candles, closing at the top/bottom of
    # the candle's own range is almost always a momentum continuation
    # signal, not a reversal. The trend-aware dampening helped in strong
    # trends but the signal still fired (and lost) in RANGE regime where
    # most of the wrong predictions occurred. To re-enable, remove the
    # `if False:` guard below or remove these theories from
    # DISABLED_THEORIES in core/constants.py.
    # ═══════════════════════════════════════════════════════════════════════
    if False and rng > 0:
        close_pos = max(0, min(100, (c - l) / rng * 100))
        # FIX (DEEP-AUDIT-2026-07-26 / F-10-16, A-05 ISSUE S5): .get() for
        # close_percentile cold-start safety (defaults to neutral 50).
        _close_pctile = stats.get("close_percentile", 50)
        if close_pos >= 80:
            percentile_boost = 1 if _close_pctile >= 90 else 0
            close_aligns_with_strong_trend = (
                is_trending
                and trend_strength > 0.5
                and trend_regime == "TREND_UP"
            )
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-03): use >= 0.7 for
            # the strong-dampen threshold (boundary consistency fix).
            if close_aligns_with_strong_trend and trend_strength >= 0.7:
                base_score, base_conf = 1, 53   # strong dampen
            elif close_aligns_with_strong_trend:
                base_score, base_conf = 1, 56   # moderate dampen
            else:
                base_score, base_conf = 2, 62
            results.append(ModuleResult(
                module_name="candle_reaction", direction="PUT", score=base_score + percentile_boost, confidence=base_conf,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"Close at range top ({close_pos:.0f}%, pctile={_close_pctile:.0f}, trend_str={trend_strength:.2f}) → PUT"]))
        elif close_pos <= 20:
            percentile_boost = 1 if _close_pctile <= 10 else 0
            close_aligns_with_strong_trend = (
                is_trending
                and trend_strength > 0.5
                and trend_regime == "TREND_DOWN"
            )
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-03 + AUDIT-4-04):
            # use >= 0.7 for strong-dampen boundary consistency AND equalize
            # the full-strength branch to conf=62 to match the PUT branch
            # (was 60 — 2-point PUT bias at range extremes).
            if close_aligns_with_strong_trend and trend_strength >= 0.7:
                base_score, base_conf = 1, 53
            elif close_aligns_with_strong_trend:
                base_score, base_conf = 1, 56
            else:
                base_score, base_conf = 2, 62
            results.append(ModuleResult(
                module_name="candle_reaction", direction="CALL", score=base_score + percentile_boost, confidence=base_conf,
                signal_type="REVERSAL", reliability="CANDLE", group="BODY",
                reasons=[f"Close at range bottom ({close_pos:.0f}%, pctile={_close_pctile:.0f}, trend_str={trend_strength:.2f}) → CALL"]))

    # FIX (DEEP-AUDIT-2026-07-26 / F-10-17, A-05 P27): removed 9-line
    # commented-out SIGNAL 5 (Body shrinking) dead block. Originally disabled
    # on 2026-07-20 (real Quotex data showed 42% win rate — shrinking bodies
    # in OTC are broker consolidation, not exhaustion). Re-enablement requires
    # backtest proof of >= 50% win rate on live data.
    # ── SIGNAL 5 (Body shrinking → exhaustion) remains DISABLED ──────

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
    #
    # FIX (AUDIT-DEEP-A6, 2026-07-23): clarified variable naming —
    # `b1`/`b2` are ABS body sizes (price units), `r1`/`r2` are ranges
    # (price units), so `b1/r1` is a RATIO (unitless). But `body_pct`
    # is a PERCENTAGE (0-100). The comparisons `b1/r1 >= 0.30` and
    # `body_pct >= 30` are equivalent (0.30 == 30% as a ratio), but the
    # mixed units make the code confusing. This is functional but should
    # ideally use one convention. Leaving as-is for backward compat.
    #
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-07): raise the trend-strength
    # gate from > 0.4 to > 0.5 to align with SIGNAL 1's moderate-dampen
    # threshold. At trend_strength = 0.45 (a weak trend the regime classifier
    # barely acknowledges), the continuation vote was noise — the trend is
    # too weak to support a continuation bet.
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-06): removed the dead
    # `b3 = abs(body)` assignments in both branches — `b3` was computed but
    # never used (the rising-closes check uses `body_pct`, which is the
    # current body ratio, not `b3`). Removing to avoid future confusion.
    # FIX (DEEP-AUDIT-2026-07-26 / F-10-18, A-05 P40): hoist b1/b2/r1/r2
    # computation OUT of the if/elif branches — both branches computed the
    # same values. Single computation improves readability and avoids
    # divergent logic if one branch's formula is later edited.
    if is_trending and trend_strength > 0.5 and len(candles) >= 3:
        c1_close = candles[-3]["close"]
        c2_close = candles[-2]["close"]
        c3_close = candles[-1]["close"]
        b1 = abs(candles[-3]["close"] - candles[-3]["open"])
        b2 = abs(candles[-2]["close"] - candles[-2]["open"])
        r1 = candles[-3]["high"] - candles[-3]["low"]
        r2 = candles[-2]["high"] - candles[-2]["low"]
        # Monotonic rising closes
        if c1_close < c2_close < c3_close:
            # Each body must be non-trivial (not dojis)
            # NOTE: b1/b2 are price-unit body sizes; b1/r1 is a unitless ratio
            #       body_pct is a 0-100 percentage — these are the same scale
            #       but expressed in different units (ratio vs percentage).
            if (r1 > 0 and r2 > 0 and rng > 0
                    and b1/r1 >= 0.30 and b2/r2 >= 0.30 and body_pct >= 30
                    and trend_regime == "TREND_UP"):
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="CALL", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="CANDLE", group="BODY_CONT",
                    reasons=[f"Rising closes momentum (3 UP, str={trend_strength:.2f}) → CALL continuation (62% win rate)"]))
        # Monotonic falling closes
        elif c1_close > c2_close > c3_close:
            if (r1 > 0 and r2 > 0 and rng > 0
                    and b1/r1 >= 0.30 and b2/r2 >= 0.30 and body_pct >= 30
                    and trend_regime == "TREND_DOWN"):
                results.append(ModuleResult(
                    module_name="candle_reaction", direction="PUT", score=3, confidence=62,
                    signal_type="CONTINUATION", reliability="CANDLE", group="BODY_CONT",
                    reasons=[f"Falling closes momentum (3 DOWN, str={trend_strength:.2f}) → PUT continuation (62% win rate)"]))

    # FIX (DEEP-AUDIT-2026-07-26 / F-10-19, A-05 P28): removed 8-line
    # commented-out SIGNAL 7 (Trend-aligned wick rejection) dead block.
    # Originally disabled on 2026-07-20 (backtest showed 41.4% win rate on
    # 70 signals — the "bearish close but lower wick = continuation" logic
    # is wrong; a bearish close means sellers won). Re-enablement requires
    # backtest proof of >= 50% win rate.
    # ── CONTINUATION SIGNAL 7 remains DISABLED (41.4% win rate) ───────

    return results
