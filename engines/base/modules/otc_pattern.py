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
    consec = stats["current_streak"]
    streak_dir = stats["streak_direction"]
    mean_rev_gated = (is_trending and trend_strength > 0.6)
    if consec >= 3 and not mean_rev_gated:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=62,
                signal_type="REVERSAL", reliability="OTC", group="OTC_MEANREV",
                reasons=[f"OTC mean-rev: {consec}+ UP → PUT (62% reversal in OTC, regime={trend_regime})"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=62,
                signal_type="REVERSAL", reliability="OTC", group="OTC_MEANREV",
                reasons=[f"OTC mean-rev: {consec}+ DOWN → CALL (62% reversal in OTC, regime={trend_regime})"]))

    # ── REVERSAL SIGNAL 2: Streak rarity boost ───────────────────────────
    # Rare streaks (<10% occurrence) get a reversal boost. Keep this one
    # un-gated because a truly rare streak IS more likely to reverse even
    # in a trend (statistical edge overrides regime).
    if consec >= 3 and stats["streak_rarity"] < 0.10:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=65,
                signal_type="REVERSAL", reliability="OTC", group="OTC_RARITY",
                reasons=[f"Rare streak (n={consec}, rarity={stats['streak_rarity']:.0%}) → PUT reversal boost"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=65,
                signal_type="REVERSAL", reliability="OTC", group="OTC_RARITY",
                reasons=[f"Rare streak (n={consec}, rarity={stats['streak_rarity']:.0%}) → CALL reversal boost"]))

    # ── REVERSAL SIGNAL 3: Z-score extreme reversal ──────────────────────
    vol_pct = ctx.vol_pct
    if vol_pct >= 1.3:
        z_threshold = 2.0
    elif vol_pct <= 0.7:
        z_threshold = 2.8
    else:
        z_threshold = 2.3

    if stats["z_body"] > z_threshold:
        last = candles[-1]
        body = last["close"] - last["open"]
        if body > 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=63,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ZSCORE",
                reasons=[f"Z-score extreme body (Z={stats['z_body']:.1f} > {z_threshold}, vol={vol_pct:.1f}x) → PUT reversal (statistical edge)"]))
        elif body < 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=63,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ZSCORE",
                reasons=[f"Z-score extreme body (Z={stats['z_body']:.1f} > {z_threshold}, vol={vol_pct:.1f}x) → CALL reversal (statistical edge)"]))

    # ── REVERSAL SIGNAL 4: Close percentile extreme ──────────────────────
    pctile = stats["close_percentile"]
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
    if consec == 1 and stats["streak_rarity"] > 0.30 and stats["z_body"] < 0.5:
        last = candles[-1]
        body = last["close"] - last["open"]
        if body > 0 and not results:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=1, confidence=53,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ALTERNATE",
                reasons=["OTC alternation bias (small body, 53% opposite) → PUT"]))
        elif body < 0 and not results:
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
    if len(candles) >= 20:
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
    # In a confirmed TREND regime (trend_strength > 0.5), a 2+ same-dir
    # streak gets a continuation vote. This directly counterbalances
    # Signal 1 (mean-rev) which is now gated OFF in strong trends.
    # Without this, the engine would have NO opinion during trends.
    if is_trending and trend_strength > 0.5 and consec >= 2:
        if trend_regime == "TREND_UP" and streak_dir == 1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=60,
                signal_type="CONTINUATION", reliability="OTC", group="OTC_TRENDSTREAK",
                reasons=[f"OTC trend streak: {consec} UP in TREND_UP (str={trend_strength:.2f}) → CALL continuation"]))
        elif trend_regime == "TREND_DOWN" and streak_dir == -1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=60,
                signal_type="CONTINUATION", reliability="OTC", group="OTC_TRENDSTREAK",
                reasons=[f"OTC trend streak: {consec} DOWN in TREND_DOWN (str={trend_strength:.2f}) → PUT continuation"]))

    return results
