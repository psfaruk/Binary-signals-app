"""
Module: BOLLINGER + RSI + ENGULFING (USER-AUG-2026)

Web research finding (Task 3): The BB+RSI+Engulfing combo has the highest
expected win rate (60-70%) on Quotex OTC 1-min binary options.

Strategy logic:
  - Bollinger Bands (20, 2): price extending outside band = mean-reversion setup
  - RSI (14): extreme reading confirms overbought/oversold
  - Engulfing candle: confirms reversal entry

CALL rule:
  - price closes BELOW lower BB AND
  - RSI < 30 (oversold) AND
  - bullish engulfing pattern on the just-closed candle

PUT rule:
  - price closes ABOVE upper BB AND
  - RSI > 70 (overbought) AND
  - bearish engulfing pattern on the just-closed candle

If price is outside BB but RSI/engulfing don't confirm, emit a WEAK signal
(score=1) — useful as confluence for other modules but not strong enough
to trade alone.
"""
from engines.base.types import ModuleResult, MarketContext


def _sma(values, period):
    """Simple Moving Average over the last `period` values."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _stddev(values, period):
    """Population standard deviation over the last `period` values."""
    if len(values) < period:
        return 0.0
    window = values[-period:]
    mean = sum(window) / period
    var = sum((v - mean) ** 2 for v in window) / period
    return var ** 0.5


def _bollinger_bands(closes, period=20, num_std=2.0):
    """Compute Bollinger Bands. Returns (lower, middle, upper) or None."""
    if len(closes) < period:
        return None
    middle = _sma(closes, period)
    sd = _stddev(closes, period)
    if middle is None or sd <= 0:
        return None
    return (middle - num_std * sd, middle, middle + num_std * sd)


def _rsi(closes, period=14):
    """Compute RSI (Wilder's smoothing)."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if len(gains) <= period:
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def _detect_engulfing(candles):
    """Detect bullish/bearish engulfing on the last 2 candles.

    Returns: 'BULL_ENGULFING', 'BEAR_ENGULFING', or None.
    """
    if len(candles) < 2:
        return None
    c_prev = candles[-2]
    c_curr = candles[-1]
    prev_body = c_prev["close"] - c_prev["open"]
    curr_body = c_curr["close"] - c_curr["open"]
    prev_abs = abs(prev_body)
    curr_abs = abs(curr_body)
    if prev_abs <= 0 or curr_abs <= 0:
        return None
    # Bullish engulfing: prev bearish, curr bullish, curr body engulfs prev body
    if (prev_body < 0 and curr_body > 0
            and c_curr["open"] <= c_prev["close"]
            and c_curr["close"] >= c_prev["open"]
            and curr_abs > prev_abs):
        return "BULL_ENGULFING"
    # Bearish engulfing: prev bullish, curr bearish, curr body engulfs prev body
    if (prev_body > 0 and curr_body < 0
            and c_curr["open"] >= c_prev["close"]
            and c_curr["close"] <= c_prev["open"]
            and curr_abs > prev_abs):
        return "BEAR_ENGULFING"
    return None


def analyze(candles, ctx: MarketContext) -> list:
    """Compute BB+RSI+Engulfing signals."""
    if len(candles) < 30:
        return []

    closes = ctx.closes if ctx.closes else [c["close"] for c in candles]
    if len(closes) < 30:
        return []

    results = []
    last_close = closes[-1]

    bb = _bollinger_bands(closes, period=20, num_std=2.0)
    rsi_val = _rsi(closes, period=14)
    engulfing = _detect_engulfing(candles)

    if bb is None or rsi_val is None:
        return []

    lower_bb, middle_bb, upper_bb = bb

    # ── Strong signal: all 3 confirm ────────────────────────────────────
    # CALL: price below lower BB + RSI oversold + bullish engulfing
    if last_close < lower_bb and rsi_val < 30 and engulfing == "BULL_ENGULFING":
        results.append(ModuleResult(
            module_name="bollinger_rsi",
            direction="CALL",
            score=4,
            confidence=68,
            signal_type="REVERSAL",
            reliability="PATTERN",
            group="BB_RSI_ENGULF",
            reasons=[
                f"BB+RSI+Engulf CALL: close {last_close:.5f} < BB_lower {lower_bb:.5f}, "
                f"RSI {rsi_val:.0f} < 30, bullish engulfing → strong reversal"
            ],
        ))
        return results

    # PUT: price above upper BB + RSI overbought + bearish engulfing
    if last_close > upper_bb and rsi_val > 70 and engulfing == "BEAR_ENGULFING":
        results.append(ModuleResult(
            module_name="bollinger_rsi",
            direction="PUT",
            score=4,
            confidence=68,
            signal_type="REVERSAL",
            reliability="PATTERN",
            group="BB_RSI_ENGULF",
            reasons=[
                f"BB+RSI+Engulf PUT: close {last_close:.5f} > BB_upper {upper_bb:.5f}, "
                f"RSI {rsi_val:.0f} > 70, bearish engulfing → strong reversal"
            ],
        ))
        return results

    # ── Medium signal: 2 of 3 confirm ───────────────────────────────────
    # CALL confluence: below BB + RSI oversold (no engulfing)
    if last_close < lower_bb and rsi_val < 30:
        results.append(ModuleResult(
            module_name="bollinger_rsi",
            direction="CALL",
            score=2,
            confidence=60,
            signal_type="REVERSAL",
            reliability="CANDLE",
            group="BB_RSI_ENGULF",
            reasons=[
                f"BB+RSI CALL: close < BB_lower ({lower_bb:.5f}), RSI {rsi_val:.0f} < 30 "
                f"(no engulfing confirm) → medium reversal"
            ],
        ))
        return results

    # PUT confluence: above BB + RSI overbought (no engulfing)
    if last_close > upper_bb and rsi_val > 70:
        results.append(ModuleResult(
            module_name="bollinger_rsi",
            direction="PUT",
            score=2,
            confidence=60,
            signal_type="REVERSAL",
            reliability="CANDLE",
            group="BB_RSI_ENGULF",
            reasons=[
                f"BB+RSI PUT: close > BB_upper ({upper_bb:.5f}), RSI {rsi_val:.0f} > 70 "
                f"(no engulfing confirm) → medium reversal"
            ],
        ))
        return results

    # ── Weak signal: BB extreme only (confluence for other modules) ─────
    if last_close < lower_bb:
        results.append(ModuleResult(
            module_name="bollinger_rsi",
            direction="CALL",
            score=1,
            confidence=52,
            signal_type="REVERSAL",
            reliability="CANDLE",
            group="BB_RSI_ENGULF",
            reasons=[
                f"BB extreme CALL: close < BB_lower ({lower_bb:.5f}), RSI {rsi_val:.0f} "
                f"(no RSI/engulfing confirm) → weak confluence"
            ],
        ))
    elif last_close > upper_bb:
        results.append(ModuleResult(
            module_name="bollinger_rsi",
            direction="PUT",
            score=1,
            confidence=52,
            signal_type="REVERSAL",
            reliability="CANDLE",
            group="BB_RSI_ENGULF",
            reasons=[
                f"BB extreme PUT: close > BB_upper ({upper_bb:.5f}), RSI {rsi_val:.0f} "
                f"(no RSI/engulfing confirm) → weak confluence"
            ],
        ))

    return results
