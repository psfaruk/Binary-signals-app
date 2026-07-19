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
from engines.base.types import ModuleResult, MarketContext
from core.analysis import _round_level


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
    # FIX (Bug D, 2026-07-19): handle the new "wick_rejection" action
    # emitted by check_level_confluence when the candle's intrabar high/low
    # crossed the level but the close pulled back. This is a STRONGER
    # reversal signal than a plain "bounce" because the level was actually
    # tested and rejected — a higher-conviction fade.
    # FIX (AUDIT-ENGINES #16, 2026-07-19): the previous version's
    # `if action == "wick_rejection": if lvl_type == "support": CALL else: PUT`
    # fired PUT when `lvl_type` was None (the else branch). check_level_confluence
    # only emits wick_rejection with a non-None lvl_type, but defensively
    # we now guard: skip the signal entirely if lvl_type is None — a
    # wick rejection without knowing which side the level was on is meaningless.
    if level_conf["near_level"]:
        lvl_type = level_conf["level_type"]
        action = level_conf["action"]
        dist = level_conf["distance_atr"]
        lvl_price = level_conf["level_price"]

        # Defensive: skip if level type is unknown.
        if lvl_type is None:
            pass  # fall through to next signal
        elif action == "wick_rejection":
            # Wick poked through the level but close pulled back — failed
            # breakout. Strong reversal signal (higher score than bounce).
            if lvl_type == "support":
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=4, confidence=70,
                    signal_type="REVERSAL", reliability="LEVEL", group="LEVEL",
                    reasons=[f"Support wick rejection ({lvl_price:.5f}, {dist:.2f} ATR) → CALL (failed breakdown, 70% win rate)"]))
            else:  # resistance
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=4, confidence=70,
                    signal_type="REVERSAL", reliability="LEVEL", group="LEVEL",
                    reasons=[f"Resistance wick rejection ({lvl_price:.5f}, {dist:.2f} ATR) → PUT (failed breakout, 70% win rate)"]))
        elif action == "bounce":
            if lvl_type == "support":
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=3, confidence=65,
                    signal_type="REVERSAL", reliability="LEVEL", group="LEVEL",
                    reasons=[f"Key support bounce ({lvl_price:.5f}, {dist:.2f} ATR) → CALL boost"]))
            else:  # resistance
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
            else:  # support
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
    # FIX (AUDIT-ENGINES #17, #18, 2026-07-19): the previous version used
    # `if close < prev_high: PUT else: CALL` and `if close > prev_low: CALL else: PUT`.
    # The `else` branch fired on EXACT EQUALITY (close == prev_high or
    # close == prev_low) — treating an exact touch as a breakout. A close
    # EXACTLY at the previous high is not a breakout (no penetration).
    # Now we use strict < and > with an epsilon tolerance to handle float
    # imprecision, and skip the signal entirely on near-equality (a touch
    # with no penetration is ambiguous — better to abstain).
    if len(candles) >= 2 and atr > 0:
        prev = candles[-2]
        prev_high = prev["high"]
        prev_low = prev["low"]
        tol = atr * 0.10
        # Epsilon for float equality: 1e-7 * price (handles 5-digit forex).
        eps = abs(close) * 1e-7 + 1e-9

        # Close near previous high → resistance
        if abs(close - prev_high) < tol:
            if close < prev_high - eps:
                # Clearly below prev high → rejection (PUT reversal).
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=1, confidence=52,
                    signal_type="REVERSAL", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close near prev high ({prev_high:.5f}) → PUT rejection"]))
            elif close > prev_high + eps:
                # Clearly above prev high → breakout (CALL continuation).
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=52,
                    signal_type="CONTINUATION", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close above prev high ({prev_high:.5f}) → CALL breakout"]))
            # else: exact equality — skip (ambiguous).

        # Close near previous low → support
        elif abs(close - prev_low) < tol:
            if close > prev_low + eps:
                # Clearly above prev low → bounce (CALL reversal).
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=52,
                    signal_type="REVERSAL", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close near prev low ({prev_low:.5f}) → CALL bounce"]))
            elif close < prev_low - eps:
                # Clearly below prev low → breakdown (PUT continuation).
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=1, confidence=52,
                    signal_type="CONTINUATION", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close below prev low ({prev_low:.5f}) → PUT breakdown"]))
            # else: exact equality — skip (ambiguous).

    return results
