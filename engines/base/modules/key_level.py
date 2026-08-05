"""
Module: Key Level Engine (PRUNED 2026-08-04)

After THEORY-PRUNE-2026-08-04 (3 rounds), this module keeps only the
key-level signals that performed well in production backtesting.

Active signals:
  1. Support wick rejection (LEVEL group) — 49.6% measured, n=9581
  3. Close NEAR prev low → CALL bounce (MICRO_SR group) — 49.4%, n=2709
  6. S/R flip (SR_FLIP group) — 49.2-49.8%, n=1886-2020
     resistance→support side: coin flip, 51.6% UP over n=215 live,
     95% CI [45.0, 58.2]. Its 2026-08-05 CALL→PUT inversion was
     falsified out-of-sample and reverted 2026-08-06 — see the comment
     at SIGNAL 6 before proposing another direction change.
  7. Trendline breakout (TRENDLINE group) — kept, fires too rarely to measure

Measured win rates above are from a walk-forward run over 5 days of real
Quotex history, all 19 live pairs (WALK-FORWARD-2026-08-05). They replace
the previous in-sample figures, which were counted off a few dozen live
signals and overstated every theory by 5-10pp. None of these signals has a
demonstrated edge — they are kept because none has a demonstrated ANTI-edge
either, and they feed the group-consensus count.

Removed signals (see git history):
  - Close BELOW prev low / breakdown (47.3% win, n=2536 walk-forward — the
    only theory with a confirmed anti-edge; removed WALK-FORWARD-2026-08-05)
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
                        # FIX (2026-08-06): the reason text claimed "70% win
                        # rate" — that number was just `confidence=70` restated
                        # as if it were a measured result, and it rendered in
                        # the UI as one. This module's own docstring measures
                        # this signal at 49.6% (n=9581 walk-forward); live
                        # theory_votes put it at 50.8% (n=1280). Claim removed
                        # rather than corrected: no win rate belongs in a reason
                        # string, because nothing keeps it in sync with the data.
                        reasons=[f"Support wick rejection ({lvl_price:.5f}, {dist:.2f} ATR) → CALL (failed breakdown)"]))
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
            # REMOVED (WALK-FORWARD-2026-08-05): "Close below prev low → PUT
            # breakdown" measured 47.3% over n=2536 on 5 days of real Quotex
            # history across all 19 pairs (95% CI [45.4, 49.2] — the whole
            # interval sits below 50%, so this is a confirmed anti-edge, not
            # noise). It was the ONLY theory of the 15 active ones whose CI
            # excluded 50%. The docstring above claimed "55-57% win"; that
            # figure came from an in-sample count on a few dozen live signals.
            #
            # The bounce side of this branch (close > prev_low) measured 49.4%
            # (n=2709) — indistinguishable from a coin flip, so it stays: it
            # contributes to group consensus without a measured negative edge.
            #
            # Reproduce with: scripts/live_backtest/backtest_theories.py
            pass

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
                    # REVERTED (2026-08-06): PROD-BACKTEST-2026-08-05 / FIX-1
                    # flipped this branch CALL → PUT, predicting 56.19% from an
                    # in-sample count of n=105 ("the highest-confidence single
                    # fix in the commit"). It then ran in production for ~7
                    # hours and was falsified out-of-sample:
                    #
                    #   era        voted   n     won
                    #   pre-flip   CALL    114   43.9%
                    #   post-flip  PUT     101   39.6%   <- predicted 56.2%
                    #
                    # PUT's 95% CI is [30.6, 49.4] — it excludes the 56.2% the
                    # flip was justified by, missing it by 16.6pp. Flipping the
                    # direction on a stationary pattern MUST turn 43.9% into
                    # 56.1%; it produced 39.6% instead, which means the trigger
                    # population is not stationary at all. What actually
                    # happened is that the market's tendency on this setup
                    # changed sign across the deploy boundary: 56.1% DOWN
                    # before, 60.4% UP after. The flip was fitted to one side
                    # of that swing and landed on the wrong side of the next.
                    #
                    # Pooled over both eras the trigger is a coin flip:
                    # 111/215 = 51.6% UP, 95% CI [45.0, 58.2]. Neither CALL nor
                    # PUT has any measured edge, so this reverts to the textbook
                    # label rather than keeping a direction chosen by noise.
                    # The "56.2% measured n=105" text was also being rendered in
                    # the UI as if it were a validated win rate — it never was.
                    #
                    # Kept (not deleted) on the same grounds as the other
                    # signals in this module: no demonstrated anti-edge either,
                    # and it feeds the SR_FLIP group-consensus count. It fires
                    # on only 1.47% of signals.
                    #
                    # Template for this revert: the `agree=0` fallback inversion
                    # in blender.py — hypothesis from train, verdict from
                    # holdout, revert when it does not replicate.
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
