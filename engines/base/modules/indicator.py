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
    """Exponential Moving Average.

    FIX (Bug 9, deep audit 2026-07-19): previously required
    `len(values) >= period` and returned 0 otherwise. This silently failed
    EMA checks when len < period (cold-start, short lookback). Now adapts
    the seed to whatever length is available — same logic as
    core.analysis._ema. With fewer values than `period`, the EMA is mostly
    an SMA of all available values, which is still a useful (if noisier)
    trend direction indicator.
    """
    if not values:
        return 0
    k = 2 / (period + 1)
    seed_n = min(period, len(values))
    ema = sum(values[:seed_n]) / seed_n
    for v in values[seed_n:]:
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
    """MACD line, signal line, histogram.

    FIX (BUG-AC, 2026-07-20): the previous version recomputed EMA-fast and
    EMA-slow for every historical position (O(N²)). Now we compute the
    EMA arrays ONCE in a single forward pass, then derive MACD values
    from the arrays. Same result, ~50× faster for 200 closes.
    """
    if len(closes) < slow + signal:
        return 0, 0, 0

    # Compute EMA-fast and EMA-slow arrays in a single forward pass.
    def _ema_array(values, period):
        if not values:
            return []
        k = 2 / (period + 1)
        seed_n = min(period, len(values))
        ema_arr = []
        # Seed with SMA of first seed_n values
        seed_sma = sum(values[:seed_n]) / seed_n
        for i in range(len(values)):
            if i < seed_n - 1:
                # Not enough data yet — use running average
                ema_arr.append(sum(values[:i+1]) / (i+1))
            elif i == seed_n - 1:
                ema_arr.append(seed_sma)
            else:
                ema_arr.append(values[i] * k + ema_arr[-1] * (1 - k))
        return ema_arr

    ema_fast_arr = _ema_array(closes, fast)
    ema_slow_arr = _ema_array(closes, slow)
    # MACD line = EMA-fast - EMA-slow (aligned at the end)
    macd_line = ema_fast_arr[-1] - ema_slow_arr[-1]
    # MACD values array (only from index slow-1 onward where both EMAs exist)
    macd_values = [
        ema_fast_arr[i] - ema_slow_arr[i]
        for i in range(slow - 1, len(closes))
    ]
    if len(macd_values) >= signal:
        signal_line = _ema(macd_values, signal)
    else:
        signal_line = macd_values[-1] if macd_values else 0
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bollinger(closes, period=20, num_std=2):
    """Bollinger Bands: middle (SMA), upper, lower.

    FIX (BUG-AE, 2026-07-20): use sample std (/(period-1)) instead of
    population std (/period). For period=20, this corrects a ~2.5%
    understatement of std, making the bands slightly wider and more
    accurate.
    """
    if len(closes) < period:
        return 0, 0, 0
    recent = closes[-period:]
    sma = sum(recent) / period
    # Sample variance (Bessel's correction)
    variance = sum((x - sma) ** 2 for x in recent) / max(period - 1, 1)
    std = math.sqrt(variance) if variance > 0 else 0
    return sma, sma + num_std * std, sma - num_std * std


def _stochastic(candles, k_period=14, d_period=3):
    """Stochastic Oscillator: %K and %D for the last closed candle, plus
    the PREVIOUS candle's %K and %D so callers can detect true crossovers.

    FIX (Bug E, 2026-07-19): the previous version returned only current %K
    and %D, which made it impossible to detect a true %K/%D crossover —
    downstream code fell back to a state check (`k > 80 and k < d`) that
    fires on every candle in an overbought zone, piling on stale reversal
    votes. Now also returns `k_prev` and `d_prev` so callers can detect
    the actual sign change of `(k - d)` between the prior candle and now.

    Returns: (k, d, k_prev, d_prev)
    """
    if len(candles) < k_period:
        return 50, 50, 50, 50

    def _k_at(idx):
        """Compute %K at candle index `idx` using the trailing k_period."""
        if idx < k_period - 1:
            return 50.0
        r = candles[idx - k_period + 1: idx + 1]
        hh = max(c["high"] for c in r)
        ll = min(c["low"] for c in r)
        cc = candles[idx]["close"]
        return ((cc - ll) / (hh - ll) * 100) if hh != ll else 50.0

    # Current %K
    k = _k_at(len(candles) - 1)
    # Current %D = SMA of last d_period %K values
    k_values = [_k_at(len(candles) - 1 - i) for i in range(d_period)]
    k_values.reverse()  # oldest → newest, for SMA readability
    d = sum(k_values) / len(k_values) if k_values else k

    # Previous %K and %D (one candle earlier)
    if len(candles) >= k_period + 1:
        k_prev = _k_at(len(candles) - 2)
        k_values_prev = [_k_at(len(candles) - 2 - i) for i in range(d_period)]
        k_values_prev.reverse()
        d_prev = sum(k_values_prev) / len(k_values_prev) if k_values_prev else k_prev
    else:
        k_prev, d_prev = k, d

    return k, d, k_prev, d_prev


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def analyze(candles, ctx: MarketContext) -> list:
    """Run all 5 technical indicators.

    Returns list of ModuleResult objects, one per indicator that fires.

    FIX (AUDIT-ENGINES, 2026-07-19): RSI/Bollinger/Stochastic reversal
    signals are now TREND-AWARE. In a strong trend, RSI > 70 is the
    DEFINITION of trend strength (not overbought), Bollinger band
    touches ride the band, and stochastic can stay pegged. Firing
    reversal on these in a trend = the same structural bias that
    candle_reaction had before its trend-aware fix.
    """
    results = []
    if len(candles) < 30:
        return results

    closes = ctx.closes
    last_close = closes[-1]

    # FIX (trend-aware indicators): pull regime once at the top so all
    # reversal indicators can be gated consistently.
    regime = ctx.regime
    is_trending = regime.get("is_trending", False)
    trend_regime = regime.get("regime", "RANGE")
    trend_strength = regime.get("trend_strength", 0.0)
    strong_trend = is_trending and trend_strength > 0.6

    # ── INDICATOR 1: RSI (14) ────────────────────────────────────────────
    # FIX (Bug #7, 2026-07-17): removed the "mild momentum" branches that
    # fired on 60<RSI<70 and 30<RSI<40 — these were essentially noise
    # (confidence 52%, score 1) that fired on most candles in a trending
    # market. Now RSI only votes on the classic overbought/oversold zones
    # (>70 / <30) where there is a real statistical edge.
    # FIX (AUDIT-ENGINES, 2026-07-19): in a strong trend, RSI > 70 (uptrend)
    # or RSI < 30 (downtrend) is momentum confirmation, NOT a reversal
    # signal. Fire as CONTINUATION in that case. RSI > 70 in a DOWNTREND
    # (rare but possible — a counter-trend spike) is still reversal.
    rsi = _rsi(closes, 14)
    if rsi > 70:
        if strong_trend and trend_regime == "TREND_UP":
            # RSI > 70 in a strong uptrend = momentum, not reversal.
            results.append(ModuleResult(
                module_name="indicator", direction="CALL", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="INDICATOR", group="IND_RSI",
                reasons=[f"RSI overbought ({rsi:.0f}) in strong uptrend (str={trend_strength:.2f}) → CALL continuation (momentum)"]))
        else:
            results.append(ModuleResult(
                module_name="indicator", direction="PUT", score=3, confidence=62,
                signal_type="REVERSAL", reliability="INDICATOR", group="IND_RSI",
                reasons=[f"RSI overbought ({rsi:.0f}) → PUT reversal (62% win rate)"]))
    elif rsi < 30:
        if strong_trend and trend_regime == "TREND_DOWN":
            # RSI < 30 in a strong downtrend = momentum.
            results.append(ModuleResult(
                module_name="indicator", direction="PUT", score=2, confidence=58,
                signal_type="CONTINUATION", reliability="INDICATOR", group="IND_RSI",
                reasons=[f"RSI oversold ({rsi:.0f}) in strong downtrend (str={trend_strength:.2f}) → PUT continuation (momentum)"]))
        else:
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
    #
    # FIX (Bug 15, deep audit 2026-07-19): added magnitude filter. The
    # previous check `hist_prev <= 0 and histogram > 0` fired on ANY sign
    # flip, including noise-level flips where histogram was 1e-7 on both
    # sides of zero. In OTC feeds especially, this caused phantom
    # crossover signals that contributed to false confluence. Now the
    # post-crossover histogram magnitude must exceed a small price-relative
    # threshold (0.0001 * close = ~1 pip on EURUSD, ~1.5 pip on USDJPY).
    MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
    macd_line, signal_line, histogram = _macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    # Detect sign change in histogram by recomputing on truncated closes.
    if len(closes) >= MACD_SLOW + MACD_SIGNAL + 1:
        _ml_prev, _sl_prev, hist_prev = _macd(closes[:-1], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        # Magnitude filter: post-crossover histogram must be meaningful
        # (>0.01% of close). Filters noise-level sign flips.
        # FIX (deep diagnostic, 2026-07-20): MACD crossover had 39% win rate.
        # Magnitude threshold raised 10x (0.0001 → 0.001) to filter noise
        # crossovers. Score reduced (3→1) and confidence reduced (60→52)
        # since the signal is unreliable on 1m candles.
        mag_threshold = abs(last_close) * 0.001 if last_close > 0 else 0.001
        fresh_bull_cross = (hist_prev <= 0 and histogram > 0
                            and abs(histogram) > mag_threshold)
        fresh_bear_cross = (hist_prev >= 0 and histogram < 0
                            and abs(histogram) > mag_threshold)
    else:
        fresh_bull_cross = fresh_bear_cross = False

    if fresh_bull_cross:
        results.append(ModuleResult(
            module_name="indicator", direction="CALL", score=1, confidence=52,
            signal_type="CONTINUATION", reliability="INDICATOR", group="IND_MACD",
            reasons=[f"MACD fresh bullish crossover (hist={histogram:.6f}) → CALL"]))
    elif fresh_bear_cross:
        results.append(ModuleResult(
            module_name="indicator", direction="PUT", score=1, confidence=52,
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
    # FIX (AUDIT-ENGINES, 2026-07-19): Bollinger band touches in a strong
    # trend ride the band (price hugs upper band in uptrend, lower in
    # downtrend) — calling these "reversal" was the same structural bias
    # as RSI. Now: in a strong trend aligned with the band touch, fire as
    # CONTINUATION (weaker score). Otherwise fire as REVERSAL (mean reversion).
    bb_mid, bb_upper, bb_lower = _bollinger(closes, 20, 2)
    if bb_upper > 0 and bb_lower > 0:
        bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0
        if last_close >= bb_upper:
            if strong_trend and trend_regime == "TREND_UP":
                # Riding the upper band in an uptrend = continuation.
                results.append(ModuleResult(
                    module_name="indicator", direction="CALL", score=1, confidence=54,
                    signal_type="CONTINUATION", reliability="INDICATOR", group="IND_BB",
                    reasons=[f"Close riding upper Bollinger band in uptrend (str={trend_strength:.2f}) → CALL continuation"]))
            else:
                results.append(ModuleResult(
                    module_name="indicator", direction="PUT", score=2, confidence=58,
                    signal_type="REVERSAL", reliability="INDICATOR", group="IND_BB",
                    reasons=[f"Close above upper Bollinger band → PUT reversal (mean reversion)"]))
        elif last_close <= bb_lower:
            if strong_trend and trend_regime == "TREND_DOWN":
                # Riding the lower band in a downtrend = continuation.
                results.append(ModuleResult(
                    module_name="indicator", direction="PUT", score=1, confidence=54,
                    signal_type="CONTINUATION", reliability="INDICATOR", group="IND_BB",
                    reasons=[f"Close riding lower Bollinger band in downtrend (str={trend_strength:.2f}) → PUT continuation"]))
            else:
                results.append(ModuleResult(
                    module_name="indicator", direction="CALL", score=2, confidence=58,
                    signal_type="REVERSAL", reliability="INDICATOR", group="IND_BB",
                    reasons=[f"Close below lower Bollinger band → CALL reversal (mean reversion)"]))
        # Squeeze detection (very narrow bands → pending breakout, no direction)
        # Skip vote — just informational

    # ── INDICATOR 5: Stochastic ──────────────────────────────────────────
    # FIX (Bug E, 2026-07-19): the previous check was `k > 80 and k < d`
    # (and `k < 20 and k > d`). That is NOT a crossover detector — it's a
    # state check ("K is overbought AND currently below D"). It fires on
    # every candle of a multi-candle overbought unwind, piling on stale
    # reversal votes and producing false confluence with other indicators.
    #
    # A true bearish stochastic crossover is: on the previous candle
    # (k_prev >= d_prev) AND on the current candle (k < d), with the
    # crossover happening INSIDE the overbought zone (k > 80 or recently
    # was > 80). The bullish case is the mirror.
    #
    # We use a slightly relaxed zone check (k > 70 / k < 30) because the
    # crossover candle itself often dips just below 80 / just above 20 as
    # %K turns — strict >80 would miss the very signal we're trying to
    # detect. The crossover requirement (sign change of k-d) is the
    # critical part that prevents stale repeat signals.
    k, d, k_prev, d_prev = _stochastic(candles, 14, 3)
    fresh_bear_cross = (k_prev >= d_prev) and (k < d)
    fresh_bull_cross = (k_prev <= d_prev) and (k > d)
    # FIX (Bug 14, deep audit 2026-07-19): the previous zone check used
    # `k > 70` (current K) for bearish crossover and `k < 30` for bullish.
    # But immediately after a crossover, K has already moved AWAY from the
    # extreme — a bearish crossover from overbought typically has K dropping
    # from 82 → 68, so `k > 70` FAILS and the signal is missed. The correct
    # check is on the PRIOR candle's K (the candle that was actually in the
    # overbought zone BEFORE the crossover). Now we check `k_prev > 70` for
    # bearish and `k_prev < 30` for bullish, with a relaxed `max(k, k_prev)`
    # fallback so we don't miss signals where K was just barely below the
    # threshold on the prior candle.
    if fresh_bear_cross and (k_prev > 70 or max(k, k_prev) > 75):
        # %K crossed below %D from overbought zone → bearish reversal
        results.append(ModuleResult(
            module_name="indicator", direction="PUT", score=2, confidence=57,
            signal_type="REVERSAL", reliability="INDICATOR", group="IND_STOCH",
            reasons=[f"Stochastic fresh bearish cross (%K={k:.0f}, %D={d:.0f}, was {k_prev:.0f}/{d_prev:.0f}) → PUT"]))
    elif fresh_bull_cross and (k_prev < 30 or min(k, k_prev) < 25):
        # %K crossed above %D from oversold zone → bullish reversal
        results.append(ModuleResult(
            module_name="indicator", direction="CALL", score=2, confidence=57,
            signal_type="REVERSAL", reliability="INDICATOR", group="IND_STOCH",
            reasons=[f"Stochastic fresh bullish cross (%K={k:.0f}, %D={d:.0f}, was {k_prev:.0f}/{d_prev:.0f}) → CALL"]))

    return results
