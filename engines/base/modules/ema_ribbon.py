"""
Module: EMA RIBBON (USER-AUG-2026)

Web research finding (Task 3): EMA(5/8/13) ribbon has 55-62% expected WR
on Quotex OTC 1-min binary options for trend-following pairs (USDJPY-OTC,
AUDCAD-OTC, AUDJPY-OTC).

Strategy logic:
  - Compute EMA(5), EMA(8), EMA(13) from candle closes
  - Bullish stack: EMA5 > EMA8 > EMA13 (short-term trend up)
  - Bearish stack: EMA5 < EMA8 < EMA13 (short-term trend down)
  - Price confirmation: close above EMA5 (CALL) or below EMA5 (PUT)

CALL rule:
  - EMA5 > EMA8 > EMA13 (bullish stack) AND
  - close > EMA5 (price above shortest EMA) AND
  - stack just formed (prev candle was NOT a bullish stack) → strong
  - Or: stack exists and price > EMA5 → continuation

PUT rule:
  - EMA5 < EMA8 < EMA13 (bearish stack) AND
  - close < EMA5 (price below shortest EMA) AND
  - stack just formed → strong
  - Or: stack exists and price < EMA5 → continuation

The "stack just formed" detection gives stronger signals (early trend entry)
while persistent stacks give continuation signals (trend following).
"""
from engines.base.types import ModuleResult, MarketContext


def _ema(values, period):
    """Compute EMA seeded with SMA of first `period` values."""
    if not values or len(values) < period:
        return 0.0
    k = 2.0 / (period + 1)
    seed_n = min(period, len(values))
    ema = sum(values[:seed_n]) / seed_n
    for v in values[seed_n:]:
        ema = v * k + ema * (1 - k)
    return ema


def _ema_series(values, period):
    """Compute EMA series (full history)."""
    if not values or len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def analyze(candles, ctx: MarketContext) -> list:
    """Compute EMA ribbon signals."""
    if len(candles) < 15:
        return []

    closes = ctx.closes if ctx.closes else [c["close"] for c in candles]
    if len(closes) < 15:
        return []

    results = []
    last_close = closes[-1]

    # Current EMAs
    ema5_now = _ema(closes, 5)
    ema8_now = _ema(closes, 8)
    ema13_now = _ema(closes, 13)

    # Previous EMAs (for "stack just formed" detection)
    # Use EMA series up to second-to-last close
    if len(closes) >= 14:
        ema5_series = _ema_series(closes[:-1], 5)
        ema8_series = _ema_series(closes[:-1], 8)
        ema13_series = _ema_series(closes[:-1], 13)
        if ema5_series and ema8_series and ema13_series:
            ema5_prev = ema5_series[-1]
            ema8_prev = ema8_series[-1]
            ema13_prev = ema13_series[-1]
        else:
            ema5_prev = ema8_prev = ema13_prev = None
    else:
        ema5_prev = ema8_prev = ema13_prev = None

    # Bullish stack: EMA5 > EMA8 > EMA13
    bull_stack_now = ema5_now > ema8_now > ema13_now
    bear_stack_now = ema5_now < ema8_now < ema13_now

    # Stack just formed? (prev was NOT a stack)
    bull_stack_prev = (ema5_prev is not None
                       and ema8_prev is not None
                       and ema13_prev is not None
                       and ema5_prev > ema8_prev > ema13_prev)
    bear_stack_prev = (ema5_prev is not None
                       and ema8_prev is not None
                       and ema13_prev is not None
                       and ema5_prev < ema8_prev < ema13_prev)

    bull_just_formed = bull_stack_now and not bull_stack_prev
    bear_just_formed = bear_stack_now and not bear_stack_prev

    # ── CALL signals ────────────────────────────────────────────────────
    if bull_stack_now and last_close > ema5_now:
        if bull_just_formed:
            # Fresh bullish stack — strong entry
            results.append(ModuleResult(
                module_name="ema_ribbon",
                direction="CALL",
                score=3,
                confidence=62,
                signal_type="CONTINUATION",
                reliability="CANDLE",
                group="EMA_RIBBON",
                reasons=[
                    f"EMA ribbon CALL (fresh stack): EMA5 {ema5_now:.5f} > EMA8 {ema8_now:.5f} "
                    f"> EMA13 {ema13_now:.5f}, close {last_close:.5f} > EMA5 → trend entry"
                ],
            ))
        else:
            # Persistent stack — continuation
            results.append(ModuleResult(
                module_name="ema_ribbon",
                direction="CALL",
                score=2,
                confidence=57,
                signal_type="CONTINUATION",
                reliability="CANDLE",
                group="EMA_RIBBON",
                reasons=[
                    f"EMA ribbon CALL (continuation): bullish stack persists, "
                    f"close {last_close:.5f} > EMA5 {ema5_now:.5f}"
                ],
            ))

    # ── PUT signals ─────────────────────────────────────────────────────
    elif bear_stack_now and last_close < ema5_now:
        if bear_just_formed:
            results.append(ModuleResult(
                module_name="ema_ribbon",
                direction="PUT",
                score=3,
                confidence=62,
                signal_type="CONTINUATION",
                reliability="CANDLE",
                group="EMA_RIBBON",
                reasons=[
                    f"EMA ribbon PUT (fresh stack): EMA5 {ema5_now:.5f} < EMA8 {ema8_now:.5f} "
                    f"< EMA13 {ema13_now:.5f}, close {last_close:.5f} < EMA5 → trend entry"
                ],
            ))
        else:
            results.append(ModuleResult(
                module_name="ema_ribbon",
                direction="PUT",
                score=2,
                confidence=57,
                signal_type="CONTINUATION",
                reliability="CANDLE",
                group="EMA_RIBBON",
                reasons=[
                    f"EMA ribbon PUT (continuation): bearish stack persists, "
                    f"close {last_close:.5f} < EMA5 {ema5_now:.5f}"
                ],
            ))

    # ── Weak signal: stack exists but price hasn't confirmed ────────────
    elif bull_stack_now:
        results.append(ModuleResult(
            module_name="ema_ribbon",
            direction="CALL",
            score=1,
            confidence=52,
            signal_type="CONTINUATION",
            reliability="CANDLE",
            group="EMA_RIBBON",
            reasons=[
                f"EMA ribbon CALL (weak): bullish stack but close {last_close:.5f} "
                f"≤ EMA5 {ema5_now:.5f} — awaiting price confirmation"
            ],
        ))
    elif bear_stack_now:
        results.append(ModuleResult(
            module_name="ema_ribbon",
            direction="PUT",
            score=1,
            confidence=52,
            signal_type="CONTINUATION",
            reliability="CANDLE",
            group="EMA_RIBBON",
            reasons=[
                f"EMA ribbon PUT (weak): bearish stack but close {last_close:.5f} "
                f"≥ EMA5 {ema5_now:.5f} — awaiting price confirmation"
            ],
        ))

    return results
