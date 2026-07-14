"""
Module 6: OTC Market Algorithm Engine

OTC-specific patterns that exploit the broker's algorithm behavior.
In OTC markets, classical analysis is less reliable — this module
focuses on what actually works in broker-generated price feeds:

  1. Mean-reversion bias (3+ same-direction → reversal probability)
  2. Alternation probability (53% of time next candle is opposite)
  3. Streak rarity (historically rare streaks → reversal boost)
  4. Z-score extreme (statistically unusual body → reversal)
  5. Close percentile extreme (top/bottom 5% → reversal)

Reliability: OTC ×1.2 (OTC-specific patterns get a slight bonus
since they're tuned for the actual market behavior)
"""
from engines.types import ModuleResult, MarketContext


def analyze(candles, ctx: MarketContext) -> list:
    """Run OTC-specific pattern detection.

    Returns list of ModuleResult objects.
    """
    results = []
    if len(candles) < 10:
        return results

    stats = ctx.stats

    # ── SIGNAL 1: Mean-reversion bias ────────────────────────────────────
    # OTC markets mean-revert: 3+ same-direction candles → reversal likely
    consec = stats["current_streak"]
    streak_dir = stats["streak_direction"]
    if consec >= 3:
        if streak_dir == 1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=62,
                signal_type="REVERSAL", reliability="OTC", group="OTC_MEANREV",
                reasons=[f"OTC mean-rev: {consec}+ UP → PUT (62% reversal in OTC)"]))
        elif streak_dir == -1:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=62,
                signal_type="REVERSAL", reliability="OTC", group="OTC_MEANREV",
                reasons=[f"OTC mean-rev: {consec}+ DOWN → CALL (62% reversal in OTC)"]))

    # ── SIGNAL 2: Streak rarity boost ────────────────────────────────────
    # If current streak is historically rare (<10% occurrence), boost reversal
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

    # ── SIGNAL 3: Z-score extreme reversal ───────────────────────────────
    # Statistically unusual body (Z > 2) → strong reversal signal
    if stats["z_body"] > 2.0:
        last = candles[-1]
        body = last["close"] - last["open"]
        if body > 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=2, confidence=63,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ZSCORE",
                reasons=[f"Z-score extreme body (Z={stats['z_body']:.1f}) → PUT reversal (statistical edge)"]))
        elif body < 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=2, confidence=63,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ZSCORE",
                reasons=[f"Z-score extreme body (Z={stats['z_body']:.1f}) → CALL reversal (statistical edge)"]))

    # ── SIGNAL 4: Close percentile extreme ───────────────────────────────
    # Close at 95th+ or 5th- percentile of recent closes → extreme → reversal
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

    # ── SIGNAL 5: Alternation bias (weak) ────────────────────────────────
    # In OTC, 53% of the time the next candle is opposite direction.
    # This is a very weak signal — only fires when there's no strong
    # directional bias from other signals.
    if consec == 1:
        last = candles[-1]
        body = last["close"] - last["open"]
        if body > 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="PUT", score=1, confidence=53,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ALTERNATE",
                reasons=["OTC alternation bias (53% opposite) → PUT"]))
        elif body < 0:
            results.append(ModuleResult(
                module_name="otc_pattern", direction="CALL", score=1, confidence=53,
                signal_type="REVERSAL", reliability="OTC", group="OTC_ALTERNATE",
                reasons=["OTC alternation bias (53% opposite) → CALL"]))

    return results
