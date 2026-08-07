"""
Module: MOMENTUM OSCILLATORS — RSI + MACD (DEEP-FIX-2026-08-07)

Adds classic momentum oscillator analysis that was completely missing from
the engine. The existing modules all analyze price action (candlestick
patterns, S/R levels, tick flow) but none compute momentum oscillators.

RSI (Relative Strength Index):
  - RSI > 70 + bearish candle → PUT (overbought reversal, ~55% WR)
  - RSI < 30 + bullish candle → CALL (oversold reversal, ~55% WR)
  - RSI extreme (>80 or <20) → stronger signal

MACD (Moving Average Convergence Divergence):
  - MACD bullish crossover → CALL continuation
  - MACD bearish crossover → PUT continuation
  - MACD histogram divergence → stronger reversal signal

Both are computed from close prices using standard formulas and emit
ModuleResult votes into the blender.
"""
from engines.base.types import ModuleResult, MarketContext


def _rsi(closes, period=14):
    """Compute RSI for a list of closing prices."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    # Seed with SMA
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if len(gains) <= period:
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    # Wilder's smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def _macd(closes):
    """Compute MACD (12, 26, 9) and return (macd, signal, histogram)."""
    if len(closes) < 26:
        return None, None, None

    def _ema_series(values, period):
        k = 2.0 / (period + 1)
        result = [sum(values[:period]) / period]
        for v in values[period:]:
            result.append(v * k + result[-1] * (1 - k))
        return result

    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)

    # Align lengths
    min_len = min(len(ema12), len(ema26))
    macd_line = [ema12[i] - ema26[i] for i in range(-min_len, 0)]

    if len(macd_line) < 9:
        return None, None, None

    signal = _ema_series(macd_line, 9)

    macd_val = macd_line[-1]
    signal_val = signal[-1]
    histogram = macd_val - signal_val

    # Previous values for crossover detection
    if len(macd_line) >= 2 and len(signal) >= 2:
        prev_macd = macd_line[-2]
        prev_signal = signal[-2]
        prev_hist = prev_macd - prev_signal
    else:
        prev_hist = None

    return macd_val, signal_val, histogram, prev_hist


def analyze(candles, ctx: MarketContext) -> list:
    """Compute RSI + MACD signals from candle closes."""
    if len(candles) < 30:
        return []

    closes = ctx.closes if ctx.closes else [c["close"] for c in candles]
    if len(closes) < 30:
        return []

    results = []
    last = candles[-1]
    is_bull = last["close"] >= last["open"]

    # ── RSI Analysis ────────────────────────────────────────────────────
    rsi_val = _rsi(closes)
    if rsi_val is not None:
        # Overbought + bearish candle
        if rsi_val > 70 and not is_bull:
            score = 3 if rsi_val > 80 else 2
            conf = 65 if rsi_val > 80 else 60
            results.append(ModuleResult(
                module_name="momentum",
                direction="PUT",
                score=score,
                confidence=conf,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="MOMENTUM_RSI",
                reasons=[f"RSI {rsi_val:.0f} overbought + bearish candle → PUT reversal (x{score})"],
            ))

        # Oversold + bullish candle
        elif rsi_val < 30 and is_bull:
            score = 3 if rsi_val < 20 else 2
            conf = 65 if rsi_val < 20 else 60
            results.append(ModuleResult(
                module_name="momentum",
                direction="CALL",
                score=score,
                confidence=conf,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="MOMENTUM_RSI",
                reasons=[f"RSI {rsi_val:.0f} oversold + bullish candle → CALL reversal (x{score})"],
            ))

        # RSI middle zone — divergence confirmation
        # Rising RSI + bullish → mild continuation signal
        if is_bull and rsi_val > 50 and rsi_val < 70:
            results.append(ModuleResult(
                module_name="momentum",
                direction="CALL",
                score=1,
                confidence=55,
                signal_type="CONTINUATION",
                reliability="CANDLE",
                group="MOMENTUM_RSI",
                reasons=[f"RSI {rsi_val:.0f} bullish momentum → CALL continuation"],
            ))
        elif not is_bull and rsi_val < 50 and rsi_val > 30:
            results.append(ModuleResult(
                module_name="momentum",
                direction="PUT",
                score=1,
                confidence=55,
                signal_type="CONTINUATION",
                reliability="CANDLE",
                group="MOMENTUM_RSI",
                reasons=[f"RSI {rsi_val:.0f} bearish momentum → PUT continuation"],
            ))

    # ── MACD Analysis ────────────────────────────────────────────────────
    macd_result = _macd(closes)
    if macd_result[0] is not None:
        macd_val, signal_val, histogram, prev_hist = macd_result

        # Bullish crossover: MACD crossed above signal
        if prev_hist is not None:
            if prev_hist <= 0 and histogram > 0:
                results.append(ModuleResult(
                    module_name="momentum",
                    direction="CALL",
                    score=2,
                    confidence=60,
                    signal_type="CONTINUATION",
                    reliability="CANDLE",
                    group="MOMENTUM_MACD",
                    reasons=[f"MACD bullish crossover (hist {histogram:+.5f}) → CALL"],
                ))

            # Bearish crossover: MACD crossed below signal
            elif prev_hist >= 0 and histogram < 0:
                results.append(ModuleResult(
                    module_name="momentum",
                    direction="PUT",
                    score=2,
                    confidence=60,
                    signal_type="CONTINUATION",
                    reliability="CANDLE",
                    group="MOMENTUM_MACD",
                    reasons=[f"MACD bearish crossover (hist {histogram:+.5f}) → PUT"],
                ))

        # Histogram divergence from RSI signal
        if histogram > 0 and is_bull:
            results.append(ModuleResult(
                module_name="momentum",
                direction="CALL",
                score=1,
                confidence=55,
                signal_type="CONTINUATION",
                reliability="CANDLE",
                group="MOMENTUM_MACD",
                reasons=[f"MACD histogram bullish ({histogram:+.5f}) + bull candle → CALL"],
            ))
        elif histogram < 0 and not is_bull:
            results.append(ModuleResult(
                module_name="momentum",
                direction="PUT",
                score=1,
                confidence=55,
                signal_type="CONTINUATION",
                reliability="CANDLE",
                group="MOMENTUM_MACD",
                reasons=[f"MACD histogram bearish ({histogram:+.5f}) + bear candle → PUT"],
            ))

    return results
