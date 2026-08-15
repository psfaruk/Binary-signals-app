"""
Module: S/R BOUNCE WITH CANDLE CONFIRMATION (USER-AUG-2026)

Web research finding (Task 3): S/R bounce with candle confirmation has
60-68% expected WR on Quotex OTC 1-min binary options — one of the most
reliable strategies.

Strategy logic:
  - Identify key support/resistance levels from prior 20-candle swings
  - When price touches a level (within 2 pips / 0.2 ATR), look for
    candle confirmation:
    - At support: hammer, dragonfly doji, bullish engulfing, bullish pin bar
    - At resistance: shooting star, gravestone doji, bearish engulfing, bearish pin bar
  - Strong signal: level touch + candle confirmation
  - Medium signal: level touch + close back inside the range
  - Weak signal: level touch only (confluence)

This module COMPLEMENTS key_level.py — key_level handles general S/R
rejection, while sr_bounce specifically requires a CONFIRMATION CANDLE.
The two together provide stronger confluence than either alone.
"""
from engines.base.types import ModuleResult, MarketContext


def _find_recent_swings(candles, lookback=20):
    """Find swing highs/lows in the last `lookback` candles (excluding the
    most recent candle, which we're analyzing)."""
    if len(candles) < 5:
        return [], []
    # Exclude the last candle from swing detection (it's the one we're trading)
    window = candles[-lookback - 1: -1] if len(candles) > lookback + 1 else candles[:-1]
    if len(window) < 4:
        return [], []

    highs = []
    lows = []
    for i in range(2, len(window) - 2):
        c = window[i]
        if (c["high"] >= window[i - 1]["high"]
                and c["high"] > window[i - 2]["high"]
                and c["high"] >= window[i + 1]["high"]
                and c["high"] > window[i + 2]["high"]):
            highs.append(c["high"])
        if (c["low"] <= window[i - 1]["low"]
                and c["low"] < window[i - 2]["low"]
                and c["low"] <= window[i + 1]["low"]
                and c["low"] < window[i + 2]["low"]):
            lows.append(c["low"])
    return highs, lows


def _detect_reversal_candle(candle, prev_candle):
    """Detect if the just-closed candle is a reversal candle.

    Returns: 'BULL_HAMMER', 'BULL_ENGULF', 'BULL_PIN', 'BULL_DRAGONFLY',
             'BEAR_STAR', 'BEAR_ENGULF', 'BEAR_PIN', 'BEAR_GRAVESTONE',
             or None.
    """
    if not candle or not prev_candle:
        return None

    o = candle["open"]
    c = candle["close"]
    h = candle["high"]
    l = candle["low"]
    rng = h - l
    if rng <= 0:
        return None

    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_pct = body / rng * 100
    upper_pct = upper_wick / rng * 100
    lower_pct = lower_wick / rng * 100

    prev_o = prev_candle["open"]
    prev_c = prev_candle["close"]
    prev_body = prev_c - prev_o

    # Bullish patterns
    if lower_pct >= 60 and body_pct <= 35 and (c - o) >= 0:
        return "BULL_HAMMER"
    if lower_pct >= 70 and body_pct <= 15:
        return "BULL_DRAGONFLY"
    if (prev_body < 0 and c - o > 0
            and c >= prev_o and o <= prev_c
            and body > abs(prev_body)):
        return "BULL_ENGULF"
    if lower_pct >= 66 and body_pct <= 33:
        return "BULL_PIN"

    # Bearish patterns
    if upper_pct >= 60 and body_pct <= 35 and (c - o) <= 0:
        return "BEAR_STAR"
    if upper_pct >= 70 and body_pct <= 15:
        return "BEAR_GRAVESTONE"
    if (prev_body > 0 and c - o < 0
            and c <= prev_o and o >= prev_c
            and body > abs(prev_body)):
        return "BEAR_ENGULF"
    if upper_pct >= 66 and body_pct <= 33:
        return "BEAR_PIN"

    return None


def analyze(candles, ctx: MarketContext) -> list:
    """Detect S/R bounce with confirmation candle."""
    if len(candles) < 25:
        return []

    results = []
    last = candles[-1]
    prev = candles[-2] if len(candles) >= 2 else None
    if not prev:
        return []

    close = last["close"]
    high = last["high"]
    low = last["low"]
    atr = ctx.atr
    if atr <= 0:
        return []

    # Use ctx.key_levels if available, else compute from recent swings
    levels = ctx.key_levels
    if not levels:
        highs, lows = _find_recent_swings(candles, lookback=20)
        levels = ([{"price": p, "type": "resistance"} for p in highs[-6:]]
                  + [{"price": p, "type": "support"} for p in lows[-6:]])

    if not levels:
        return []

    # Tolerance: 0.2 ATR (≈2 pips on most pairs)
    tolerance = atr * 0.20

    # Find nearest support and resistance within tolerance
    nearest_support = None
    nearest_resistance = None
    support_dist = float("inf")
    resistance_dist = float("inf")
    for lvl in levels:
        price = lvl.get("price", 0)
        if lvl.get("type") == "support":
            dist = abs(low - price)
            if dist < tolerance and dist < support_dist:
                support_dist = dist
                nearest_support = price
        elif lvl.get("type") == "resistance":
            dist = abs(high - price)
            if dist < tolerance and dist < resistance_dist:
                resistance_dist = dist
                nearest_resistance = price

    # Detect the reversal candle type
    reversal = _detect_reversal_candle(last, prev)

    bull_reversals = {"BULL_HAMMER", "BULL_ENGULF", "BULL_PIN", "BULL_DRAGONFLY"}
    bear_reversals = {"BEAR_STAR", "BEAR_ENGULF", "BEAR_PIN", "BEAR_GRAVESTONE"}

    # ── SUPPORT BOUNCE + BULLISH REVERSAL CANDLE ────────────────────────
    if nearest_support is not None and reversal in bull_reversals:
        # Close back above support (bounce confirmed)
        if close > nearest_support:
            results.append(ModuleResult(
                module_name="sr_bounce",
                direction="CALL",
                score=4,
                confidence=66,
                signal_type="REVERSAL",
                reliability="PATTERN",
                group="SR_BOUNCE",
                reasons=[
                    f"SR bounce CALL: support {nearest_support:.5f} touched "
                    f"(wick {support_dist / atr:.2f} ATR), {reversal} confirm, "
                    f"close {close:.5f} > support → strong reversal"
                ],
            ))
        else:
            # Touched support but closed below — breakdown risk, weaker signal
            results.append(ModuleResult(
                module_name="sr_bounce",
                direction="CALL",
                score=2,
                confidence=55,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="SR_BOUNCE",
                reasons=[
                    f"SR bounce CALL (weak): support {nearest_support:.5f} touched, "
                    f"{reversal} pattern but close {close:.5f} ≤ support — caution"
                ],
            ))

    # ── RESISTANCE BOUNCE + BEARISH REVERSAL CANDLE ─────────────────────
    elif nearest_resistance is not None and reversal in bear_reversals:
        if close < nearest_resistance:
            results.append(ModuleResult(
                module_name="sr_bounce",
                direction="PUT",
                score=4,
                confidence=66,
                signal_type="REVERSAL",
                reliability="PATTERN",
                group="SR_BOUNCE",
                reasons=[
                    f"SR bounce PUT: resistance {nearest_resistance:.5f} touched "
                    f"(wick {resistance_dist / atr:.2f} ATR), {reversal} confirm, "
                    f"close {close:.5f} < resistance → strong reversal"
                ],
            ))
        else:
            results.append(ModuleResult(
                module_name="sr_bounce",
                direction="PUT",
                score=2,
                confidence=55,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="SR_BOUNCE",
                reasons=[
                    f"SR bounce PUT (weak): resistance {nearest_resistance:.5f} touched, "
                    f"{reversal} pattern but close {close:.5f} ≥ resistance — caution"
                ],
            ))

    # ── Level touched but no reversal candle — weak confluence ──────────
    elif nearest_support is not None:
        results.append(ModuleResult(
            module_name="sr_bounce",
            direction="CALL",
            score=1,
            confidence=52,
            signal_type="REVERSAL",
            reliability="LEVEL",
            group="SR_BOUNCE",
            reasons=[
                f"SR touch CALL: support {nearest_support:.5f} touched "
                f"({support_dist / atr:.2f} ATR), no reversal candle confirm → weak"
            ],
        ))
    elif nearest_resistance is not None:
        results.append(ModuleResult(
            module_name="sr_bounce",
            direction="PUT",
            score=1,
            confidence=52,
            signal_type="REVERSAL",
            reliability="LEVEL",
            group="SR_BOUNCE",
            reasons=[
                f"SR touch PUT: resistance {nearest_resistance:.5f} touched "
                f"({resistance_dist / atr:.2f} ATR), no reversal candle confirm → weak"
            ],
        ))

    return results
