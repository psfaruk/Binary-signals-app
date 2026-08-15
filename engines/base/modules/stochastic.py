"""
Module: STOCHASTIC OSCILLATOR (USER-AUG-2026)

Web research finding (Task 3): Stochastic crossover has 55-60% expected WR
on Quotex OTC 1-min binary options, especially when combined with MACD.

Strategy logic:
  - Stochastic (14, 3, 3): %K and %D lines
  - %K < 20 = oversold, %K > 80 = overbought
  - %K crossing above %D from below 20 = bullish crossover → CALL
  - %K crossing below %D from above 80 = bearish crossover → PUT

Confirmation rules:
  - Strong signal (score=3): crossover happens in extreme zone (< 20 or > 80)
  - Medium signal (score=2): crossover happens in mid-zone
  - Weak signal (score=1): %K in extreme zone but no crossover yet (confluence)

The Stochastic is computed from high/low/close, giving it a different
sensitivity profile than RSI — it catches short-term exhaustion better.
"""
from engines.base.types import ModuleResult, MarketContext


def _stochastic(candles, k_period=14, k_smooth=3, d_smooth=3):
    """Compute Stochastic oscillator (%K, %D, prev_%K, prev_%D).

    Returns tuple (k_now, d_now, k_prev, d_prev) or None if insufficient data.
    """
    if len(candles) < k_period + k_smooth + d_smooth:
        return None

    # Raw %K = (close - lowest_low) / (highest_high - lowest_low) * 100
    raw_k_values = []
    for i in range(k_period - 1, len(candles)):
        window = candles[i - k_period + 1: i + 1]
        highest = max(c["high"] for c in window)
        lowest = min(c["low"] for c in window)
        close = candles[i]["close"]
        if highest - lowest <= 0:
            raw_k_values.append(50.0)
        else:
            raw_k_values.append((close - lowest) / (highest - lowest) * 100.0)

    # Smoothed %K = SMA of raw %K over k_smooth periods (often called "slow %K")
    if len(raw_k_values) < k_smooth + d_smooth:
        return None
    slow_k = []
    for i in range(k_smooth - 1, len(raw_k_values)):
        slow_k.append(sum(raw_k_values[i - k_smooth + 1: i + 1]) / k_smooth)

    # %D = SMA of slow %K over d_smooth periods
    if len(slow_k) < d_smooth:
        return None
    slow_d = []
    for i in range(d_smooth - 1, len(slow_k)):
        slow_d.append(sum(slow_k[i - d_smooth + 1: i + 1]) / d_smooth)

    if len(slow_k) < 2 or len(slow_d) < 2:
        return None

    # Align: slow_d[k] corresponds to slow_k[k] when len matches
    # slow_d has length len(slow_k) - d_smooth + 1
    # So slow_d[-1] aligns with slow_k[-1], slow_d[-2] aligns with slow_k[-2]
    return (slow_k[-1], slow_d[-1], slow_k[-2], slow_d[-2])


def analyze(candles, ctx: MarketContext) -> list:
    """Compute Stochastic crossover signals."""
    if len(candles) < 25:
        return []

    stoch = _stochastic(candles, k_period=14, k_smooth=3, d_smooth=3)
    if stoch is None:
        return []

    k_now, d_now, k_prev, d_prev = stoch
    results = []

    # ── Crossover detection ─────────────────────────────────────────────
    # Bullish crossover: %K was below %D, now above
    bull_cross = k_prev <= d_prev and k_now > d_now
    # Bearish crossover: %K was above %D, now below
    bear_cross = k_prev >= d_prev and k_now < d_now

    if bull_cross:
        # Strong: crossover from oversold zone (< 20)
        if k_prev < 20:
            results.append(ModuleResult(
                module_name="stochastic",
                direction="CALL",
                score=3,
                confidence=63,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="STOCHASTIC",
                reasons=[
                    f"Stoch CALL crossover in oversold: %K {k_prev:.1f}→{k_now:.1f}, "
                    f"%D {d_prev:.1f}→{d_now:.1f} (crossed from <20) → strong reversal"
                ],
            ))
        else:
            # Medium: crossover in mid-zone
            results.append(ModuleResult(
                module_name="stochastic",
                direction="CALL",
                score=2,
                confidence=57,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="STOCHASTIC",
                reasons=[
                    f"Stoch CALL crossover: %K {k_prev:.1f}→{k_now:.1f}, "
                    f"%D {d_prev:.1f}→{d_now:.1f} (mid-zone) → medium reversal"
                ],
            ))

    elif bear_cross:
        # Strong: crossover from overbought zone (> 80)
        if k_prev > 80:
            results.append(ModuleResult(
                module_name="stochastic",
                direction="PUT",
                score=3,
                confidence=63,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="STOCHASTIC",
                reasons=[
                    f"Stoch PUT crossover in overbought: %K {k_prev:.1f}→{k_now:.1f}, "
                    f"%D {d_prev:.1f}→{d_now:.1f} (crossed from >80) → strong reversal"
                ],
            ))
        else:
            results.append(ModuleResult(
                module_name="stochastic",
                direction="PUT",
                score=2,
                confidence=57,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="STOCHASTIC",
                reasons=[
                    f"Stoch PUT crossover: %K {k_prev:.1f}→{k_now:.1f}, "
                    f"%D {d_prev:.1f}→{d_now:.1f} (mid-zone) → medium reversal"
                ],
            ))
    else:
        # ── No crossover: emit extreme-zone confluence signal ────────────
        if k_now < 20:
            results.append(ModuleResult(
                module_name="stochastic",
                direction="CALL",
                score=1,
                confidence=52,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="STOCHASTIC",
                reasons=[
                    f"Stoch oversold confluence: %K {k_now:.1f} < 20, %D {d_now:.1f} "
                    f"(no crossover yet) → weak CALL"
                ],
            ))
        elif k_now > 80:
            results.append(ModuleResult(
                module_name="stochastic",
                direction="PUT",
                score=1,
                confidence=52,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="STOCHASTIC",
                reasons=[
                    f"Stoch overbought confluence: %K {k_now:.1f} > 80, %D {d_now:.1f} "
                    f"(no crossover yet) → weak PUT"
                ],
            ))

    return results
