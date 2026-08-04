"""
Module: Key Level Engine (PRUNED 2026-08-04)

After THEORY-PRUNE-2026-08-04 (3 rounds), this module keeps only the
key-level signals that performed well in production backtesting.

Active signals:
  1. Support wick rejection (LEVEL group) — 58% win, n=154
  3. Close near/below prev low (MICRO_SR group) — 55-57% win
  6. S/R flip (SR_FLIP group) — 50% win, small sample
  7. Trendline breakout (TRENDLINE group) — kept, not yet backtested

Removed signals (see git history):
  - Resistance wick rejection (33% win, n=27)
  - Key support bounce (36% win, n=11)
  - Key resistance bounce (38% win, n=32)
  - Fibonacci retracement (43% win, n=77 — toxic combo king)
  - Close near prev high (17% win, n=6)
  - Round number proximity (44% win — disabled 2026-07-26)
  - Breakout action (47% win — disabled 2026-07-26)
  - Double top/bottom (44% win — disabled 2026-07-26)

Reliability: LEVEL ×1.0
"""
from engines.base.types import ModuleResult, MarketContext


# Module-level constants (only those used by active signals)
JPY_PRICE_THRESHOLD = 50        # abs(close) > 50 ⇒ JPY-pair granularity
EPS_PRICE_SCALE = 1e-7          # relative eps floor for breakout checks
EPS_GRANULARITY_SCALE = 0.1      # fraction of pair granularity for eps floor
MICRO_SR_PROXIMITY_ATR = 0.10    # tol for "close near prev high/low"
MAX_SR_FLIP_LEVELS = 4           # recent levels to check for S/R flip
SR_FLIP_PROXIMITY_ATR = 0.20     # |close - lvl_price| < 0.20×ATR
TRENDLINE_WINDOW = 6             # candles for descending-highs / ascending-lows


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

    # FIX (THEORY-LOGIC-FIX-2026-08-03): read regime for context-aware signals.
    regime = ctx.regime
    is_trending = regime.get("is_trending", False)
    trend_regime = regime.get("regime", "RANGE")
    trend_strength = regime.get("trend_strength", 0.0)

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 1: Support wick rejection (KEPT — 58% win, n=154)
    # ═══════════════════════════════════════════════════════════════════════
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
                        reasons=[f"Support wick rejection ({lvl_price:.5f}, {dist:.2f} ATR) → CALL (failed breakdown, 70% win rate)"]))
                # Resistance wick rejection removed (Round 2 — 33% win, n=27)

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 3: Previous candle high/low as micro-S/R (KEPT — prev_low only)
    # ═══════════════════════════════════════════════════════════════════════
    if len(candles) >= 2 and atr > 0:
        prev = candles[-2]
        prev_low = prev["low"]
        tol = atr * MICRO_SR_PROXIMITY_ATR
        _granularity = 0.01 if abs(close) > JPY_PRICE_THRESHOLD else 0.0001
        eps = max(abs(close) * EPS_PRICE_SCALE, _granularity * EPS_GRANULARITY_SCALE)

        # "Close near prev high" branch removed (Round 2 — 17% win, n=6)
        if abs(close - prev_low) < tol:
            if close > prev_low + eps:
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=52,
                    signal_type="REVERSAL", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close near prev low ({prev_low:.5f}) → CALL bounce"]))
            elif close < prev_low - eps:
                # regime-conditional signal_type (mirror of removed prev_high branch)
                _sig_type = "CONTINUATION"
                _score, _conf = 1, 52
                if is_trending and trend_strength > 0.5:
                    if trend_regime == "TREND_UP":
                        _sig_type = "REVERSAL"  # counter-trend breakdown attempt
                        _score, _conf = 1, 48  # dampen
                    elif trend_regime == "TREND_DOWN":
                        _score, _conf = 2, 56  # trend-aligned — boost
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=_score, confidence=_conf,
                    signal_type=_sig_type, reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close below prev low ({prev_low:.5f}) → PUT breakdown ({_sig_type})"]))

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 6: Support/Resistance Flip (KEPT — 50% win, small sample)
    # ═══════════════════════════════════════════════════════════════════════
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
                        reasons=[f"Broken resistance now support ({lvl_price:.5f}) → CALL (continuation of original breakout)"]))
                    break
            elif lvl_type == "support" and prev["close"] < lvl_price and close < lvl_price:
                if abs(close - lvl_price) < atr * SR_FLIP_PROXIMITY_ATR:
                    results.append(ModuleResult(
                        module_name="key_level", direction="PUT", score=2, confidence=57,
                        signal_type="CONTINUATION", reliability="LEVEL", group="SR_FLIP",
                        reasons=[f"Broken support now resistance ({lvl_price:.5f}) → PUT (continuation of original breakdown)"]))
                    break

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 7: Trendline Breakout (KEPT — not yet backtested, kept for completeness)
    # ═══════════════════════════════════════════════════════════════════════
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
                        _score, _conf = 1, 50  # likely false breakout — dampen
                    elif trend_regime == "TREND_UP":
                        _sig_type = "CONTINUATION"  # trend-aligned breakout
                        _score, _conf = 3, 60  # boost
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=_score, confidence=_conf,
                    signal_type=_sig_type, reliability="LEVEL", group="TRENDLINE",
                    reasons=[f"Trendline breakout above descending highs → CALL ({_sig_type})"]))
        elif lows[0] < lows[-1] and all(lows[i] <= lows[i+1] + _tol
                                        for i in range(len(lows)-1)):
            if close < min(lows[-2], lows[-1]):
                _sig_type = "REVERSAL"
                _score, _conf = 2, 56
                if is_trending and trend_strength > 0.5:
                    if trend_regime == "TREND_UP":
                        _score, _conf = 1, 50  # likely false breakdown — dampen
                    elif trend_regime == "TREND_DOWN":
                        _sig_type = "CONTINUATION"  # trend-aligned breakdown
                        _score, _conf = 3, 60  # boost
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=_score, confidence=_conf,
                    signal_type=_sig_type, reliability="LEVEL", group="TRENDLINE",
                    reasons=[f"Trendline breakdown below ascending lows → PUT ({_sig_type})"]))

    return results
