"""Module: Key Level Engine (PRUNED 2026-08-04)."""
from engines.base.types import ModuleResult, MarketContext

JPY_PRICE_THRESHOLD = 50
EPS_PRICE_SCALE = 1e-7
EPS_GRANULARITY_SCALE = 0.1
MICRO_SR_PROXIMITY_ATR = 0.10
MAX_SR_FLIP_LEVELS = 4
SR_FLIP_PROXIMITY_ATR = 0.20
TRENDLINE_WINDOW = 6


def analyze(candles, ctx: MarketContext) -> list:
    """Analyze price action at key S/R levels."""
    results = []
    if len(candles) < 5:
        return results

    last = candles[-1]
    close = last["close"]
    atr = ctx.atr
    level_conf = ctx.level_confluence

    regime = ctx.regime
    is_trending = regime.get("is_trending", False)
    trend_regime = regime.get("regime", "RANGE")
    trend_strength = regime.get("trend_strength", 0.0)

    # SIGNAL 1: Support / Resistance wick rejection
    if level_conf.get("near_level", False):
        lvl_type = level_conf.get("level_type")
        action = level_conf.get("action")
        dist = level_conf.get("distance_atr", 0.0)
        lvl_price = level_conf.get("level_price", 0.0)

        if lvl_type is not None:
            if action == "wick_rejection":
                if lvl_type == "support":
                    results.append(ModuleResult(
                        module_name="key_level", direction="CALL", score=4, confidence=70,
                        signal_type="REVERSAL", reliability="LEVEL", group="LEVEL",
                        reasons=[f"Support wick rejection ({lvl_price:.5f}, {dist:.2f} ATR) -> CALL (failed breakdown)"]))
                # FIX (DEEP-FIX-2026-08-07 / A-17 B09): Resistance wick rejection → PUT
                # was MISSING — only support → CALL existed, creating a structural
                # CALL bias. Resistance rejection (price spikes above resistance but
                # closes back below) is a classic bearish reversal signal.
                elif lvl_type == "resistance":
                    results.append(ModuleResult(
                        module_name="key_level", direction="PUT", score=4, confidence=70,
                        signal_type="REVERSAL", reliability="LEVEL", group="LEVEL",
                        reasons=[f"Resistance wick rejection ({lvl_price:.5f}, {dist:.2f} ATR) -> PUT (failed breakout)"]))

    # SIGNAL 3: Previous candle high/low as micro-S/R (prev_low only)
    if len(candles) >= 2 and atr > 0:
        prev = candles[-2]
        prev_low = prev["low"]
        tol = atr * MICRO_SR_PROXIMITY_ATR
        _granularity = 0.01 if abs(close) > JPY_PRICE_THRESHOLD else 0.0001
        eps = max(abs(close) * EPS_PRICE_SCALE, _granularity * EPS_GRANULARITY_SCALE)

        if abs(close - prev_low) < tol:
            if close > prev_low + eps:
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=52,
                    signal_type="REVERSAL", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close near prev low ({prev_low:.5f}) -> CALL bounce"]))
            pass

    # SIGNAL 6: Support/Resistance Flip
    if len(candles) >= 10 and atr > 0:
        levels = ctx.key_levels
        recent_levels = sorted(levels, key=lambda lv: lv.get("idx", 0),
                               reverse=True)[:MAX_SR_FLIP_LEVELS]
        prev = candles[-2]
        for level in recent_levels:
            lvl_price = level["price"]
            lvl_type = level["type"]
            if lvl_type == "resistance" and prev["close"] > lvl_price and close > lvl_price:
                if abs(close - lvl_price) < atr * SR_FLIP_PROXIMITY_ATR:
                    results.append(ModuleResult(
                        module_name="key_level", direction="CALL", score=2, confidence=57,
                        signal_type="CONTINUATION", reliability="LEVEL", group="SR_FLIP",
                        reasons=[f"Broken resistance now support ({lvl_price:.5f}) -> CALL (continuation of original breakout)"]))
                    break
            elif lvl_type == "support" and prev["close"] < lvl_price and close < lvl_price:
                if abs(close - lvl_price) < atr * SR_FLIP_PROXIMITY_ATR:
                    results.append(ModuleResult(
                        module_name="key_level", direction="PUT", score=2, confidence=57,
                        signal_type="CONTINUATION", reliability="LEVEL", group="SR_FLIP",
                        reasons=[f"Broken support now resistance ({lvl_price:.5f}) -> PUT (continuation of original breakdown)"]))
                    break

    # SIGNAL 7: Trendline Breakout
    if len(candles) >= 12 and atr > 0:
        window = candles[-12:]
        highs = [c["high"] for c in window[-TRENDLINE_WINDOW:]]
        lows = [c["low"] for c in window[-TRENDLINE_WINDOW:]]

        _tol = atr * 0.05
        if highs[0] > highs[-1] and all(highs[i] >= highs[i+1] - _tol
                                        for i in range(len(highs)-1)):
            if close > max(highs[-2], highs[-1]):
                _sig_type = "REVERSAL"
                _score, _conf = 2, 56
                if is_trending and trend_strength > 0.5:
                    if trend_regime == "TREND_DOWN":
                        _score, _conf = 1, 50  # likely false breakout
                    elif trend_regime == "TREND_UP":
                        _sig_type = "CONTINUATION"
                        _score, _conf = 3, 60
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=_score, confidence=_conf,
                    signal_type=_sig_type, reliability="LEVEL", group="TRENDLINE",
                    reasons=[f"Trendline breakout above descending highs -> CALL ({_sig_type})"]))
        elif lows[0] < lows[-1] and all(lows[i] <= lows[i+1] + _tol
                                        for i in range(len(lows)-1)):
            if close < min(lows[-2], lows[-1]):
                _sig_type = "REVERSAL"
                _score, _conf = 2, 56
                if is_trending and trend_strength > 0.5:
                    if trend_regime == "TREND_UP":
                        _score, _conf = 1, 50  # likely false breakdown
                    elif trend_regime == "TREND_DOWN":
                        _sig_type = "CONTINUATION"
                        _score, _conf = 3, 60
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=_score, confidence=_conf,
                    signal_type=_sig_type, reliability="LEVEL", group="TRENDLINE",
                    reasons=[f"Trendline breakdown below ascending lows -> PUT ({_sig_type})"]))

    return results
