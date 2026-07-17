"""
Module 5: Key Level Engine

Support/resistance level analysis from historical price action.

Signals:
  1. Swing high/low confluence (bounce vs breakout)
  2. Round number proximity (psychological levels)
  3. Previous candle high/low as micro-S/R

Reliability: LEVEL ×1.3 (key levels are structurally important)
"""
import math
from engines.otc.types import ModuleResult, MarketContext
from analyze_eoc import _round_level


def analyze(candles, ctx: MarketContext) -> list:
    """Analyze price action at key S/R levels.

    Returns list of ModuleResult objects.
    """
    results = []
    if len(candles) < 5:
        return results

    last = candles[-1]
    close = last["close"]
    atr = ctx.atr
    level_conf = ctx.level_confluence

    # ── SIGNAL 1: Swing level confluence (bounce vs breakout) ────────────
    if level_conf["near_level"]:
        lvl_type = level_conf["level_type"]
        action = level_conf["action"]
        dist = level_conf["distance_atr"]
        lvl_price = level_conf["level_price"]

        if action == "bounce":
            if lvl_type == "support":
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=3, confidence=65,
                    signal_type="REVERSAL", reliability="LEVEL", group="LEVEL",
                    reasons=[f"Key support bounce ({lvl_price:.5f}, {dist:.2f} ATR) → CALL boost"]))
            else:
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=3, confidence=65,
                    signal_type="REVERSAL", reliability="LEVEL", group="LEVEL",
                    reasons=[f"Key resistance bounce ({lvl_price:.5f}, {dist:.2f} ATR) → PUT boost"]))
        elif action == "breakout":
            if lvl_type == "resistance":
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=2, confidence=58,
                    signal_type="CONTINUATION", reliability="LEVEL", group="LEVEL",
                    reasons=[f"Resistance breakout ({lvl_price:.5f}) → CALL"]))
            else:
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=2, confidence=58,
                    signal_type="CONTINUATION", reliability="LEVEL", group="LEVEL",
                    reasons=[f"Support breakdown ({lvl_price:.5f}) → PUT"]))

    # ── SIGNAL 2: Round number proximity ─────────────────────────────────
    lvl, dist, strength = _round_level(close)
    if strength in ("BIG", "MID") and atr > 0:
        # Check if price is bouncing off or breaking through the round level
        prev_close = candles[-2]["close"] if len(candles) >= 2 else close
        tol = atr * 0.15
        if abs(close - lvl) < tol:
            # Near round number — determine direction
            if close > prev_close and lvl < close:
                # Broke above round number → CALL continuation
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=2, confidence=56,
                    signal_type="CONTINUATION", reliability="LEVEL", group="ROUND",
                    reasons=[f"Round {strength} level {lvl:.5f} broken up → CALL"]))
            elif close < prev_close and lvl > close:
                # Broke below round number → PUT continuation
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=2, confidence=56,
                    signal_type="CONTINUATION", reliability="LEVEL", group="ROUND",
                    reasons=[f"Round {strength} level {lvl:.5f} broken down → PUT"]))
            elif close > prev_close:
                # Approaching from below, bouncing → CALL reversal
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=53,
                    signal_type="REVERSAL", reliability="LEVEL", group="ROUND",
                    reasons=[f"Round {strength} level {lvl:.5f} bounce up → CALL"]))
            elif close < prev_close:
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=1, confidence=53,
                    signal_type="REVERSAL", reliability="LEVEL", group="ROUND",
                    reasons=[f"Round {strength} level {lvl:.5f} bounce down → PUT"]))

    # ── SIGNAL 3: Previous candle high/low as micro-S/R ──────────────────
    if len(candles) >= 2 and atr > 0:
        prev = candles[-2]
        prev_high = prev["high"]
        prev_low = prev["low"]
        tol = atr * 0.10

        # Close near previous high → resistance
        if abs(close - prev_high) < tol:
            if close < prev_high:
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=1, confidence=52,
                    signal_type="REVERSAL", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close near prev high ({prev_high:.5f}) → PUT rejection"]))
            else:
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=52,
                    signal_type="CONTINUATION", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close above prev high ({prev_high:.5f}) → CALL breakout"]))

        # Close near previous low → support
        elif abs(close - prev_low) < tol:
            if close > prev_low:
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=52,
                    signal_type="REVERSAL", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close near prev low ({prev_low:.5f}) → CALL bounce"]))
            else:
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=1, confidence=52,
                    signal_type="CONTINUATION", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close below prev low ({prev_low:.5f}) → PUT breakdown"]))

    return results
