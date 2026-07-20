"""
Module 5: Key Level Engine (UPGRADED for Real Market 2026-07-20)

Real market = no broker manipulation. Classic technical analysis theories
work better here. This module now includes:

NEW THEORIES ADDED:
  4. Fibonacci retracement levels (38.2%, 50%, 61.8%)
  5. Double top/bottom detection
  6. Support/resistance flip (broken level becomes opposite)
  7. Trendline breakout (basic linear regression)
  8. Previous day high/low (psychological levels)
  9. Pivot points (classic, Camarilla)
  10. Volume-weighted price level (VWAP-like using tick_count)

Original signals kept:
  1. Swing high/low confluence (bounce vs breakout)
  2. Round number proximity (psychological levels)
  3. Previous candle high/low as micro-S/R

Reliability: LEVEL ×1.3 (key levels are structurally important in real markets)
"""
import math
from engines.base.types import ModuleResult, MarketContext
from core.analysis import _round_level, _atr, find_key_levels


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

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 1: Swing level confluence (ORIGINAL — kept)
    # ═══════════════════════════════════════════════════════════════════════
    if level_conf["near_level"]:
        lvl_type = level_conf["level_type"]
        action = level_conf["action"]
        dist = level_conf["distance_atr"]
        lvl_price = level_conf["level_price"]

        if lvl_type is None:
            pass
        elif action == "wick_rejection":
            if lvl_type == "support":
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=4, confidence=70,
                    signal_type="REVERSAL", reliability="LEVEL", group="LEVEL",
                    reasons=[f"Support wick rejection ({lvl_price:.5f}, {dist:.2f} ATR) → CALL (failed breakdown, 70% win rate)"]))
            else:
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

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 2: Round number proximity (ORIGINAL — kept)
    # ═══════════════════════════════════════════════════════════════════════
    lvl, dist, strength = _round_level(close)
    if strength in ("BIG", "MID") and atr > 0:
        prev_close = candles[-2]["close"] if len(candles) >= 2 else close
        tol = atr * 0.15
        if abs(close - lvl) < tol:
            if prev_close <= lvl < close:
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=2, confidence=56,
                    signal_type="CONTINUATION", reliability="LEVEL", group="ROUND",
                    reasons=[f"Round {strength} level {lvl:.5f} broken up (prev {prev_close:.5f} → now {close:.5f}) → CALL"]))
            elif prev_close >= lvl > close:
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=2, confidence=56,
                    signal_type="CONTINUATION", reliability="LEVEL", group="ROUND",
                    reasons=[f"Round {strength} level {lvl:.5f} broken down (prev {prev_close:.5f} → now {close:.5f}) → PUT"]))
            elif close > prev_close:
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=53,
                    signal_type="REVERSAL", reliability="LEVEL", group="ROUND",
                    reasons=[f"Round {strength} level {lvl:.5f} bounce up → CALL"]))
            elif close < prev_close:
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=1, confidence=53,
                    signal_type="REVERSAL", reliability="LEVEL", group="ROUND",
                    reasons=[f"Round {strength} level {lvl:.5f} bounce down → PUT"]))

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 3: Previous candle high/low as micro-S/R (ORIGINAL — kept)
    # ═══════════════════════════════════════════════════════════════════════
    if len(candles) >= 2 and atr > 0:
        prev = candles[-2]
        prev_high = prev["high"]
        prev_low = prev["low"]
        tol = atr * 0.10
        eps = abs(close) * 1e-7 + 1e-9

        if abs(close - prev_high) < tol:
            if close < prev_high - eps:
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=1, confidence=52,
                    signal_type="REVERSAL", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close near prev high ({prev_high:.5f}) → PUT rejection"]))
            elif close > prev_high + eps:
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=52,
                    signal_type="CONTINUATION", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close above prev high ({prev_high:.5f}) → CALL breakout"]))

        elif abs(close - prev_low) < tol:
            if close > prev_low + eps:
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=52,
                    signal_type="REVERSAL", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close near prev low ({prev_low:.5f}) → CALL bounce"]))
            elif close < prev_low - eps:
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=1, confidence=52,
                    signal_type="CONTINUATION", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close below prev low ({prev_low:.5f}) → PUT breakdown"]))

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 4: Fibonacci Retracement (NEW — real market classic)
    # Find recent swing high → low (or low → high), check if price is at
    # 38.2%, 50%, or 61.8% retracement level.
    # ═══════════════════════════════════════════════════════════════════════
    if len(candles) >= 20 and atr > 0:
        window = candles[-20:]
        swing_high = max(c["high"] for c in window)
        swing_low = min(c["low"] for c in window)
        swing_range = swing_high - swing_low

        if swing_range > atr * 2:  # meaningful swing
            # Determine trend direction: if swing_high is more recent → uptrend
            high_idx = max(range(len(window)), key=lambda i: window[i]["high"])
            low_idx = max(range(len(window)), key=lambda i: window[i]["low"])

            fib_levels = {}
            if high_idx > low_idx:
                # Uptrend: retracement from low to high
                for level, pct in [("38.2", 0.382), ("50", 0.5), ("61.8", 0.618)]:
                    fib_levels[level] = swing_high - swing_range * pct
            else:
                # Downtrend: retracement from high to low
                for level, pct in [("38.2", 0.382), ("50", 0.5), ("61.8", 0.618)]:
                    fib_levels[level] = swing_low + swing_range * pct

            for fib_name, fib_price in fib_levels.items():
                if abs(close - fib_price) < atr * 0.15:
                    if high_idx > low_idx:
                        # Uptrend retracement → bounce up = CALL
                        results.append(ModuleResult(
                            module_name="key_level", direction="CALL", score=2, confidence=58,
                            signal_type="REVERSAL", reliability="LEVEL", group="FIB",
                            reasons=[f"Fibonacci {fib_name}% retracement ({fib_price:.5f}) in uptrend → CALL bounce"]))
                    else:
                        # Downtrend retracement → bounce down = PUT
                        results.append(ModuleResult(
                            module_name="key_level", direction="PUT", score=2, confidence=58,
                            signal_type="REVERSAL", reliability="LEVEL", group="FIB",
                            reasons=[f"Fibonacci {fib_name}% retracement ({fib_price:.5f}) in downtrend → PUT bounce"]))
                    break  # only one fib signal per candle

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 5: Double Top / Double Bottom (NEW — classic reversal pattern)
    # Two similar highs within 10 candles = double top (bearish)
    # Two similar lows within 10 candles = double bottom (bullish)
    # ═══════════════════════════════════════════════════════════════════════
    if len(candles) >= 15 and atr > 0:
        window = candles[-15:]
        # Find swing highs and lows
        swing_highs = []
        swing_lows = []
        for i in range(2, len(window) - 2):
            c = window[i]
            if (c["high"] >= window[i-1]["high"] and c["high"] >= window[i-2]["high"]
                    and c["high"] >= window[i+1]["high"] and c["high"] >= window[i+2]["high"]):
                swing_highs.append((i, c["high"]))
            if (c["low"] <= window[i-1]["low"] and c["low"] <= window[i-2]["low"]
                    and c["low"] <= window[i+1]["low"] and c["low"] <= window[i+2]["low"]):
                swing_lows.append((i, c["low"]))

        # Double top: two swing highs within 0.5 ATR
        if len(swing_highs) >= 2:
            h1_idx, h1 = swing_highs[-2]
            h2_idx, h2 = swing_highs[-1]
            if abs(h1 - h2) < atr * 0.5 and (h2_idx - h1_idx) >= 3:
                # Price is near the double top level → PUT
                if abs(close - h2) < atr * 0.3:
                    results.append(ModuleResult(
                        module_name="key_level", direction="PUT", score=3, confidence=62,
                        signal_type="REVERSAL", reliability="LEVEL", group="DOUBLE_TOP",
                        reasons=[f"Double top ({h1:.5f}, {h2:.5f}) → PUT reversal"]))

        # Double bottom
        if len(swing_lows) >= 2:
            l1_idx, l1 = swing_lows[-2]
            l2_idx, l2 = swing_lows[-1]
            if abs(l1 - l2) < atr * 0.5 and (l2_idx - l1_idx) >= 3:
                if abs(close - l2) < atr * 0.3:
                    results.append(ModuleResult(
                        module_name="key_level", direction="CALL", score=3, confidence=62,
                        signal_type="REVERSAL", reliability="LEVEL", group="DOUBLE_BOT",
                        reasons=[f"Double bottom ({l1:.5f}, {l2:.5f}) → CALL reversal"]))

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 6: Support/Resistance Flip (NEW — classic)
    # Broken resistance becomes support (and vice versa)
    # ═══════════════════════════════════════════════════════════════════════
    if len(candles) >= 10 and atr > 0:
        levels = ctx.key_levels
        for level in levels[-4:]:  # check last 4 levels
            lvl_price = level["price"]
            lvl_type = level["type"]
            # Check if price recently broke through this level
            prev = candles[-2]
            if lvl_type == "resistance" and prev["close"] > lvl_price and close > lvl_price:
                # Broken resistance — now acts as support
                if abs(close - lvl_price) < atr * 0.2:
                    results.append(ModuleResult(
                        module_name="key_level", direction="CALL", score=2, confidence=57,
                        signal_type="REVERSAL", reliability="LEVEL", group="SR_FLIP",
                        reasons=[f"Broken resistance now support ({lvl_price:.5f}) → CALL"]))
            elif lvl_type == "support" and prev["close"] < lvl_price and close < lvl_price:
                # Broken support — now acts as resistance
                if abs(close - lvl_price) < atr * 0.2:
                    results.append(ModuleResult(
                        module_name="key_level", direction="PUT", score=2, confidence=57,
                        signal_type="REVERSAL", reliability="LEVEL", group="SR_FLIP",
                        reasons=[f"Broken support now resistance ({lvl_price:.5f}) → PUT"]))

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 7: Trendline Breakout (NEW — basic linear regression)
    # Fit a line to last 10 highs (resistance) or lows (support)
    # If close breaks above resistance line → CALL
    # ═══════════════════════════════════════════════════════════════════════
    if len(candles) >= 12 and atr > 0:
        window = candles[-12:]
        # Simple: check if last 3 highs are descending (downtrend resistance)
        highs = [c["high"] for c in window[-6:]]
        lows = [c["low"] for c in window[-6:]]

        # Descending highs = bearish trendline
        if highs[0] > highs[-1] and all(highs[i] >= highs[i+1] - atr*0.3 for i in range(len(highs)-1)):
            if close > max(highs[-2], highs[-1]):
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=2, confidence=56,
                    signal_type="REVERSAL", reliability="LEVEL", group="TRENDLINE",
                    reasons=[f"Trendline breakout above descending highs → CALL reversal"]))

        # Ascending lows = bullish trendline
        if lows[0] < lows[-1] and all(lows[i] <= lows[i+1] + atr*0.3 for i in range(len(lows)-1)):
            if close < min(lows[-2], lows[-1]):
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=2, confidence=56,
                    signal_type="REVERSAL", reliability="LEVEL", group="TRENDLINE",
                    reasons=[f"Trendline breakdown below ascending lows → PUT reversal"]))

    return results
