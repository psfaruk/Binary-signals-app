"""
Module 4: Technical Indicator Engine (NEW)

Classical technical indicators computed from candle close prices.
Each indicator produces an independent vote.

Indicators:
  1. RSI (14) — overbought >70 → PUT, oversold <30 → CALL, divergence
  2. MACD (12,26,9) — crossover + histogram momentum
  3. EMA Crossover (9 vs 21) — short-term trend direction
  4. Bollinger Bands (20, 2σ) — squeeze + band touch
  5. Stochastic (14, 3) — %K/%D crossover + overbought/oversold

Reliability: INDICATOR ×1.0 (baseline — indicators are mathematically
derived, not price-action patterns, but in OTC they can be noisy)
"""
import math
from engines.base.types import ModuleResult, MarketContext


# ═══════════════════════════════════════════════════════════════════════════════
#  INDICATOR CALCULATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _ema(values, period):
    """Exponential Moving Average."""
    if not values or len(values) < period:
        return 0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(closes, period=14):
    """Relative Strength Index (Wilder's)."""
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd(closes, fast=12, slow=26, signal=9):
    """MACD line, signal line, histogram."""
    if len(closes) < slow + signal:
        return 0, 0, 0
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = ema_fast - ema_slow
    # Simplified signal line (EMA of MACD would need full history)
    # Use recent MACD values for signal
    macd_values = []
    for i in range(slow, len(closes)):
        ef = _ema(closes[:i + 1], fast)
        es = _ema(closes[:i + 1], slow)
        macd_values.append(ef - es)
    if len(macd_values) >= signal:
        signal_line = _ema(macd_values, signal)
    else:
        signal_line = macd_values[-1] if macd_values else 0
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bollinger(closes, period=20, num_std=2):
    """Bollinger Bands: middle (SMA), upper, lower."""
    if len(closes) < period:
        return 0, 0, 0
    recent = closes[-period:]
    sma = sum(recent) / period
    variance = sum((x - sma) ** 2 for x in recent) / period
    std = math.sqrt(variance) if variance > 0 else 0
    return sma, sma + num_std * std, sma - num_std * std


def _stochastic(candles, k_period=14, d_period=3):
    """Stochastic Oscillator: %K and %D."""
    if len(candles) < k_period:
        return 50, 50
    recent = candles[-k_period:]
    highest = max(c["high"] for c in recent)
    lowest = min(c["low"] for c in recent)
    close = candles[-1]["close"]
    if highest == lowest:
        k = 50
    else:
        k = ((close - lowest) / (highest - lowest)) * 100
    # %D = SMA of last d_period %K values
    k_values = []
    for i in range(d_period, 0, -1):
        idx = len(candles) - i
        if idx >= k_period:
            r = candles[idx - k_period + 1:idx + 1]
            hh = max(c["high"] for c in r)
            ll = min(c["low"] for c in r)
            cc = candles[idx]["close"]
            k_values.append(((cc - ll) / (hh - ll) * 100) if hh != ll else 50)
    d = sum(k_values) / len(k_values) if k_values else k
    return k, d


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def analyze(candles, ctx: MarketContext) -> list:
    """Run all 5 technical indicators.

    Returns list of ModuleResult objects, one per indicator that fires.
    """
    results = []
    if len(candles) < 30:
        return results

    closes = ctx.closes
    last_close = closes[-1]

    # ── INDICATOR 1: RSI (14) ────────────────────────────────────────────
    # FIX (Bug #7, 2026-07-17): removed the "mild momentum" branches that
    # fired on 60<RSI<70 and 30<RSI<40 — these were essentially noise
    # (confidence 52%, score 1) that fired on most candles in a trending
    # market. Now RSI only votes on the classic overbought/oversold zones
    # (>70 / <30) where there is a real statistical edge.
    rsi = _rsi(closes, 14)
    if rsi > 70:
        results.append(ModuleResult(
            module_name="indicator", direction="PUT", score=3, confidence=62,
            signal_type="REVERSAL", reliability="INDICATOR", group="IND_RSI",
            reasons=[f"RSI overbought ({rsi:.0f}) → PUT reversal (62% win rate)"]))
    elif rsi < 30:
        results.append(ModuleResult(
            module_name="indicator", direction="CALL", score=3, confidence=60,
            signal_type="REVERSAL", reliability="INDICATOR", group="IND_RSI",
            reasons=[f"RSI oversold ({rsi:.0f}) → CALL reversal (60% win rate)"]))

    # ── INDICATOR 2: MACD (crossover detection) ─────────────────────────
    # FIX (Bug #8, 2026-07-17): the old version checked STATE (macd_line vs
    # signal_line position) which fires on every candle of a multi-candle
    # trend, piling on continuation votes for stale momentum. Now only
    # fires on a FRESH crossover within the last 2 candles: histogram
    # flips sign between candle N-2 and N-1 (or N-1 and N).
    MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
    macd_line, signal_line, histogram = _macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    # Detect sign change in histogram by recomputing on truncated closes.
    if len(closes) >= MACD_SLOW + MACD_SIGNAL + 1:
        _ml_prev, _sl_prev, hist_prev = _macd(closes[:-1], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        fresh_bull_cross = hist_prev <= 0 and histogram > 0
        fresh_bear_cross = hist_prev >= 0 and histogram < 0
    else:
        fresh_bull_cross = fresh_bear_cross = False

    if fresh_bull_cross:
        results.append(ModuleResult(
            module_name="indicator", direction="CALL", score=3, confidence=60,
            signal_type="CONTINUATION", reliability="INDICATOR", group="IND_MACD",
            reasons=[f"MACD fresh bullish crossover (hist={histogram:.6f}) → CALL"]))
    elif fresh_bear_cross:
        results.append(ModuleResult(
            module_name="indicator", direction="PUT", score=3, confidence=60,
            signal_type="CONTINUATION", reliability="INDICATOR", group="IND_MACD",
            reasons=[f"MACD fresh bearish crossover (hist={histogram:.6f}) → PUT"]))

    # ── INDICATOR 3: EMA Crossover (9 vs 21) ─────────────────────────────
    ema9 = ctx.ema9
    ema21 = ctx.ema21
    if ema9 > 0 and ema21 > 0:
        ema_diff_pct = (ema9 - ema21) / ema21 * 100 if ema21 > 0 else 0
        if ema9 > ema21 and ema_diff_pct > 0.05:
            results.append(ModuleResult(
                module_name="indicator", direction="CALL", score=2, confidence=56,
                signal_type="CONTINUATION", reliability="INDICATOR", group="IND_EMA",
                reasons=[f"EMA9 > EMA21 ({ema_diff_pct:.2f}%) → CALL uptrend"]))
        elif ema9 < ema21 and ema_diff_pct < -0.05:
            results.append(ModuleResult(
                module_name="indicator", direction="PUT", score=2, confidence=56,
                signal_type="CONTINUATION", reliability="INDICATOR", group="IND_EMA",
                reasons=[f"EMA9 < EMA21 ({ema_diff_pct:.2f}%) → PUT downtrend"]))

    # ── INDICATOR 4: Bollinger Bands ─────────────────────────────────────
    bb_mid, bb_upper, bb_lower = _bollinger(closes, 20, 2)
    if bb_upper > 0 and bb_lower > 0:
        bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0
        if last_close >= bb_upper:
            results.append(ModuleResult(
                module_name="indicator", direction="PUT", score=2, confidence=58,
                signal_type="REVERSAL", reliability="INDICATOR", group="IND_BB",
                reasons=[f"Close above upper Bollinger band → PUT reversal (mean reversion)"]))
        elif last_close <= bb_lower:
            results.append(ModuleResult(
                module_name="indicator", direction="CALL", score=2, confidence=58,
                signal_type="REVERSAL", reliability="INDICATOR", group="IND_BB",
                reasons=[f"Close below lower Bollinger band → CALL reversal (mean reversion)"]))
        # Squeeze detection (very narrow bands → pending breakout, no direction)
        # Skip vote — just informational

    # ── INDICATOR 5: Stochastic ──────────────────────────────────────────
    # FIX (Bug #10, 2026-07-17): removed the mid-zone momentum branches
    # (k>50, k<50 with confidence 51%) — pure noise that fired on nearly
    # every candle. Stochastic now only fires on classic overbought/oversold
    # reversals: %K crosses %D from above (>80) or from below (<20).
    k, d = _stochastic(candles, 14, 3)
    if k > 80 and k < d:
        # %K above 80 and crossing below %D → bearish signal
        results.append(ModuleResult(
            module_name="indicator", direction="PUT", score=2, confidence=57,
            signal_type="REVERSAL", reliability="INDICATOR", group="IND_STOCH",
            reasons=[f"Stochastic bearish cross (%K={k:.0f} > 80, crossing %D) → PUT"]))
    elif k < 20 and k > d:
        # %K below 20 and crossing above %D → bullish signal
        results.append(ModuleResult(
            module_name="indicator", direction="CALL", score=2, confidence=57,
            signal_type="REVERSAL", reliability="INDICATOR", group="IND_STOCH",
            reasons=[f"Stochastic bullish cross (%K={k:.0f} < 20, crossing %D) → CALL"]))

    return results
