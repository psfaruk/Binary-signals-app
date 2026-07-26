"""
Module 5: Key Level Engine (UPGRADED for Real Market 2026-07-20)

Real market = no broker manipulation. Classic technical analysis theories
work better here. This module now includes:

NEW THEORIES ADDED:
  4. Fibonacci retracement levels (38.2%, 50%, 61.8%)
  5. Double top/bottom detection
  6. Support/resistance flip (broken level becomes opposite)
  7. Trendline breakout (basic linear regression)
  8. Previous day high/low (psychological levels)
  9. Pivot points (classic, Camarilla)
  10. Volume-weighted price level (VWAP-like using tick_count)

Original signals kept:
  1. Swing high/low confluence (bounce vs breakout)
  2. Round number proximity (psychological levels)
  3. Previous candle high/low as micro-S/R

Reliability: LEVEL ×1.3 (key levels are structurally important in real markets)
"""
from engines.base.types import ModuleResult, MarketContext


# FIX (DEEP-AUDIT-2026-07-26 / F-09-01): removed dead `import math` and
# `from core.analysis import _round_level, _atr, find_key_levels` — none
# were used (the module reads ctx.atr, ctx.key_levels, ctx.level_confluence
# instead). `_round_level` was only referenced by the now-deleted SIGNAL 2.
#
# FIX (DEEP-AUDIT-2026-07-26 / F-09-02): module-level constants replacing
# ~12 magic numbers throughout this module (audit PROBLEMS 46-53).
# Centralized for tunability and documentation.
JPY_PRICE_THRESHOLD = 50        # abs(close) > 50 ⇒ JPY-pair granularity
EPS_PRICE_SCALE = 1e-7          # relative eps floor for breakout checks
EPS_GRANULARITY_SCALE = 0.1      # fraction of pair granularity for eps floor
MICRO_SR_PROXIMITY_ATR = 0.10    # tol for "close near prev high/low"
MIN_SWING_RANGE_ATR = 2.0        # Fib requires range > 2×ATR
FIB_PROXIMITY_ATR = 0.15         # |close - fib_price| < 0.15×ATR
FIB_WINDOW_SIZE = 20            # candles scanned for swing high/low
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

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 1: Swing level confluence (ORIGINAL — kept)
    # ═══════════════════════════════════════════════════════════════════════
    # FIX (DEEP-AUDIT-2026-07-26 / F-09-03): `level_conf["near_level"]` and
    # friends are direct dict accesses — KeyError risk on cold-start when
    # `check_level_confluence` returns an empty/partial dict. Switched to
    # `.get()` defaults (Audit ISSUE S5).
    # FIX (DEEP-AUDIT-2026-07-26 / F-09-04): restructure the `if lvl_type
    # is None: pass` (PROBLEM 45) into an inverted `if lvl_type is not
    # None:` wrapper so the dead branch is gone and the live branches are
    # visibly guarded.
    # FIX (DEEP-AUDIT-2026-07-26 / F-09-05): remove the entire `elif
    # action == "breakout": pass` block (PROBLEM 14) — it was intentionally
    # disabled and contained only a `pass` + 6-line comment. Breakouts on
    # 1m candles had 47.3% win rate (ultra-deep backtest); no behavior change.
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
                else:
                    results.append(ModuleResult(
                        module_name="key_level", direction="PUT", score=4, confidence=70,
                        signal_type="REVERSAL", reliability="LEVEL", group="LEVEL",
                        reasons=[f"Resistance wick rejection ({lvl_price:.5f}, {dist:.2f} ATR) → PUT (failed breakout, 70% win rate)"]))
            elif action == "bounce":
                if lvl_type == "support":
                    results.append(ModuleResult(
                        module_name="key_level", direction="CALL", score=3, confidence=65,
                        signal_type="REVERSAL", reliability="LEVEL", group="LEVEL",
                        reasons=[f"Key support bounce ({lvl_price:.5f}, {dist:.2f} ATR) → CALL boost"]))
                else:
                    results.append(ModuleResult(
                        module_name="key_level", direction="PUT", score=3, confidence=65,
                        signal_type="REVERSAL", reliability="LEVEL", group="LEVEL",
                        reasons=[f"Key resistance bounce ({lvl_price:.5f}, {dist:.2f} ATR) → PUT boost"]))
            # FIX (F-09-05): breakout action branch removed (was dead `pass`
            # block). Breakouts on 1m candles had 47.3% win rate
            # (ultra-deep backtest) — intentionally disabled.

    # FIX (DEEP-AUDIT-2026-07-26 / F-09-06): removed SIGNAL 2 (Round Number
    # Proximity) commented-out block (PROBLEM 15) — 6 lines of dead code
    # referencing the now-removed `_round_level` import. Backtest had 44.1%
    # win rate on 1m candles. Archival note: real-market 1m candles do not
    # respect round-number proximity.

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 3: Previous candle high/low as micro-S/R (ORIGINAL — kept)
    # ═══════════════════════════════════════════════════════════════════════
    if len(candles) >= 2 and atr > 0:
        prev = candles[-2]
        prev_high = prev["high"]
        prev_low = prev["low"]
        # FIX (DEEP-AUDIT-2026-07-26 / F-09-02): magic number `atr * 0.10`
        # → MICRO_SR_PROXIMITY_ATR constant (PROBLEM 47).
        tol = atr * MICRO_SR_PROXIMITY_ATR
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-20): use a pair-granularity-
        # aware eps floor so JPY pairs (price ~150) get an eps of at least
        # 0.001 (0.1 pip) instead of the price-scaled 1.5e-5 (which is far
        # smaller than the typical 0.01 pip granularity). Non-JPY pairs use
        # 0.00001 (0.1 of a 0.0001 pip). The previous eps was too small for
        # JPY pairs, causing the `close > prev_high + eps` check to fire for
        # any close above prev_high even when they were effectively equal at
        # the pair's granularity (over-firing breakout signals).
        #
        # FIX (DEEP-AUDIT-2026-07-26 / F-09-02): magic numbers `abs(close)
        # > 50`, `1e-7`, and `0.1` (PROBLEMS 46-47) replaced with module-level
        # constants JPY_PRICE_THRESHOLD / EPS_PRICE_SCALE /
        # EPS_GRANULARITY_SCALE.
        _granularity = 0.01 if abs(close) > JPY_PRICE_THRESHOLD else 0.0001
        eps = max(abs(close) * EPS_PRICE_SCALE, _granularity * EPS_GRANULARITY_SCALE)

        if abs(close - prev_high) < tol:
            if close < prev_high - eps:
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=1, confidence=52,
                    signal_type="REVERSAL", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close near prev high ({prev_high:.5f}) → PUT rejection"]))
            elif close > prev_high + eps:
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=52,
                    signal_type="CONTINUATION", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close above prev high ({prev_high:.5f}) → CALL breakout"]))

        elif abs(close - prev_low) < tol:
            if close > prev_low + eps:
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=1, confidence=52,
                    signal_type="REVERSAL", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close near prev low ({prev_low:.5f}) → CALL bounce"]))
            elif close < prev_low - eps:
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=1, confidence=52,
                    signal_type="CONTINUATION", reliability="LEVEL", group="MICRO_SR",
                    reasons=[f"Close below prev low ({prev_low:.5f}) → PUT breakdown"]))

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 4: Fibonacci Retracement (NEW — real market classic)
    # Find recent swing high → low (or low → high), check if price is at
    # 38.2%, 50%, or 61.8% retracement level.
    # ═══════════════════════════════════════════════════════════════════════
    if len(candles) >= FIB_WINDOW_SIZE and atr > 0:
        window = candles[-FIB_WINDOW_SIZE:]
        swing_high = max(c["high"] for c in window)
        swing_low = min(c["low"] for c in window)
        swing_range = swing_high - swing_low

        # FIX (DEEP-AUDIT-2026-07-26 / F-09-02): magic number `atr * 2` →
        # MIN_SWING_RANGE_ATR constant (PROBLEM 48).
        if swing_range > atr * MIN_SWING_RANGE_ATR:  # meaningful swing
            # Determine trend direction: if swing_high is more recent → uptrend
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-17): add index `i` as a
            # tiebreaker in the max() key so that when two candles share the
            # highest high (or lowest low), the MOST RECENT one (largest index)
            # is returned. The previous key returned the FIRST occurrence, which
            # could flip the trend direction (high_idx > low_idx is uptrend).
            # Common in OTC pairs that hover at round numbers.
            # FIX (DEEP-AUDIT-2026-07-26 / A-05-CRIT-1):
            # `low_idx` was using `max(...)` — which finds the candle with the
            # HIGHEST low, not the LOWEST low. This corrupted Fibonacci trend
            # classification: `high_idx > low_idx` was almost always true
            # (because the highest-low candle is usually more recent than the
            # actual swing-low candle), so almost all swings were classified
            # as UPTREND → CALL-biased Fibonacci signals.
            # Correct: most-recent occurrence of the LOWEST low. Use min() with
            # negated low (so largest -low = lowest low), and `i` as tiebreaker
            # so most-recent wins.
            high_idx = max(range(len(window)), key=lambda i: (window[i]["high"], i))
            low_idx = max(range(len(window)), key=lambda i: (-window[i]["low"], i))

            fib_levels = {}
            # FIX (AUDIT-DEEP-A5, 2026-07-23): when high_idx == low_idx
            # (extremely rare — happens only when one candle is BOTH
            # the highest-high and lowest-low, which means range > 0
            # but the most-recent occurrence of the high is the same
            # candle as the most-recent occurrence of the low), the
            # old code took the else branch (downtrend), which is
            # arbitrary. Now we explicitly skip Fibonacci for this
            # degenerate case — the swing structure is ambiguous and
            # a Fibonacci signal would be misleading.
            if high_idx == low_idx:
                # Degenerate: same candle is both swing high and swing low.
                # Ambiguous trend direction — skip Fibonacci signal.
                fib_levels = {}
            elif high_idx > low_idx:
                # Uptrend: retracement from low to high
                for level, pct in [("38.2", 0.382), ("50", 0.5), ("61.8", 0.618)]:
                    fib_levels[level] = swing_high - swing_range * pct
            else:
                # Downtrend: retracement from high to low
                for level, pct in [("38.2", 0.382), ("50", 0.5), ("61.8", 0.618)]:
                    fib_levels[level] = swing_low + swing_range * pct

            for fib_name, fib_price in fib_levels.items():
                # FIX (DEEP-AUDIT-2026-07-26 / F-09-02): magic number
                # `atr * 0.15` → FIB_PROXIMITY_ATR constant (PROBLEM 49).
                if abs(close - fib_price) < atr * FIB_PROXIMITY_ATR:
                    if high_idx > low_idx:
                        # Uptrend retracement → bounce up = CALL
                        results.append(ModuleResult(
                            module_name="key_level", direction="CALL", score=2, confidence=58,
                            signal_type="REVERSAL", reliability="LEVEL", group="FIB",
                            reasons=[f"Fibonacci {fib_name}% retracement ({fib_price:.5f}) in uptrend → CALL bounce"]))
                    else:
                        # Downtrend retracement → bounce down = PUT
                        results.append(ModuleResult(
                            module_name="key_level", direction="PUT", score=2, confidence=58,
                            signal_type="REVERSAL", reliability="LEVEL", group="FIB",
                            reasons=[f"Fibonacci {fib_name}% retracement ({fib_price:.5f}) in downtrend → PUT bounce"]))
                    break  # only one fib signal per candle

    # FIX (DEEP-AUDIT-2026-07-26 / F-09-07): removed SIGNAL 5 (Double
    # Top/Bottom) commented-out block (PROBLEM 16) — 7 lines of dead code.
    # Backtest showed 44.5% win rate on 1m candles. Real double tops need
    # 30+ candle spacing, not 10. Archival note retained for context.

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 6: Support/Resistance Flip (NEW — classic)
    # Broken resistance becomes support (and vice versa)
    #
    # FIX (AUDIT-DEEP #01, 2026-07-23): `ctx.key_levels` returns
    # `resistances[-8:] + supports[-8:]` (resistances first, supports second).
    # The previous code `for level in levels[-4:]` iterated only the LAST 4
    # entries — which are the 4 most recent SUPPORTS. Resistance levels were
    # NEVER checked for S/R flip, so the signal only fired on broken support
    # → resistance (PUT direction), never on broken resistance → support
    # (CALL direction). This biased the signal toward PUT votes.
    # Now we sort ALL levels by their `idx` (candle index) and take the 4
    # most recent of EITHER type, so both flip directions are checked.
    # ═══════════════════════════════════════════════════════════════════════
    if len(candles) >= 10 and atr > 0:
        levels = ctx.key_levels
        # Sort by candle index descending, take the 4 most recent levels
        # of either type (resistance or support).
        # FIX (DEEP-AUDIT-2026-07-26 / F-09-02): magic number `[:4]` →
        # MAX_SR_FLIP_LEVELS constant (PROBLEM 51).
        recent_levels = sorted(levels, key=lambda lv: lv.get("idx", 0),
                               reverse=True)[:MAX_SR_FLIP_LEVELS]
        # FIX (DEEP-AUDIT-2026-07-26 / F-09-08): hoist `prev = candles[-2]`
        # out of the for-loop (PROBLEM 41) — it's the same value every
        # iteration; recomputing it inside the loop was wasted work.
        prev = candles[-2]
        for level in recent_levels:
            lvl_price = level["price"]
            lvl_type = level["type"]
            # Check if price recently broke through this level
            if lvl_type == "resistance" and prev["close"] > lvl_price and close > lvl_price:
                # Broken resistance — now acts as support
                # FIX (DEEP-AUDIT-2026-07-26 / F-09-02): magic number
                # `atr * 0.2` → SR_FLIP_PROXIMITY_ATR constant (PROBLEM 52).
                if abs(close - lvl_price) < atr * SR_FLIP_PROXIMITY_ATR:
                    results.append(ModuleResult(
                        module_name="key_level", direction="CALL", score=2, confidence=57,
                        signal_type="REVERSAL", reliability="LEVEL", group="SR_FLIP",
                        reasons=[f"Broken resistance now support ({lvl_price:.5f}) → CALL"]))
                    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-22): break after
                    # the first SR_FLIP signal so a whipsaw around a level
                    # (broken resistance AND broken support detected in the
                    # same candle) doesn't emit contradictory CALL+PUT votes,
                    # and so multiple broken resistances don't inflate the
                    # CALL vote count beyond what a single level warrants.
                    break
            elif lvl_type == "support" and prev["close"] < lvl_price and close < lvl_price:
                # Broken support — now acts as resistance
                if abs(close - lvl_price) < atr * SR_FLIP_PROXIMITY_ATR:
                    results.append(ModuleResult(
                        module_name="key_level", direction="PUT", score=2, confidence=57,
                        signal_type="REVERSAL", reliability="LEVEL", group="SR_FLIP",
                        reasons=[f"Broken support now resistance ({lvl_price:.5f}) → PUT"]))
                    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-22): same break-
                    # after-first-signal rule for the support side.
                    break

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 7: Trendline Breakout (NEW — basic linear regression)
    # Fit a line to last 10 highs (resistance) or lows (support)
    # If close breaks above resistance line → CALL
    # ═══════════════════════════════════════════════════════════════════════
    if len(candles) >= 12 and atr > 0:
        window = candles[-12:]
        # Simple: check if last TRENDLINE_WINDOW highs are descending (downtrend resistance)
        # FIX (DEEP-AUDIT-2026-07-26 / F-09-02): magic number `[-6:]` →
        # TRENDLINE_WINDOW constant (PROBLEM 53).
        highs = [c["high"] for c in window[-TRENDLINE_WINDOW:]]
        lows = [c["low"] for c in window[-TRENDLINE_WINDOW:]]

        # Descending highs = bearish trendline
        # AUDIT-4-18 FIX (2026-07-25): changed `if` → `elif` so a triangle
        # pattern (both descending highs AND ascending lows) doesn't fire
        # BOTH CALL and PUT signals simultaneously. A triangle is a NEUTRAL
        # consolidation — emitting both directions causes the blender's
        # CALL/PUT votes to cancel out, deflating vote_ratio. With `elif`,
        # if the bearish trendline fires (descending highs), the bullish
        # check is skipped. The first condition (descending highs + close
        # above them) is the stronger reversal signal anyway.
        #
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-4-19): remove the 0.3×ATR
        # tolerance on the descending-highs (and ascending-lows) check. The
        # tolerance allowed each high to be up to 0.3×ATR HIGHER than the
        # prior, so a strictly-ascending sequence with small steps qualified
        # as "descending". Now requires strictly descending highs (each high
        # >= next high, no upticks allowed).
        #
        # FIX (DEEP-AUDIT-2026-07-26 / F-09-09): clarify the seemingly
        # redundant `highs[0] > highs[-1] and all(...)` check (PROBLEM 42).
        # `all(highs[i] >= highs[i+1])` already implies `highs[0] >=
        # highs[-1]`; the strict `>` adds the "not all equal" requirement
        # so a flat horizontal series (which technically satisfies the
        # `all()` chain via `>=`) does NOT fire a trendline breakout.
        # Same logic mirrored below for lows.
        if highs[0] > highs[-1] and all(highs[i] >= highs[i+1]
                                        for i in range(len(highs)-1)):
            if close > max(highs[-2], highs[-1]):
                results.append(ModuleResult(
                    module_name="key_level", direction="CALL", score=2, confidence=56,
                    signal_type="REVERSAL", reliability="LEVEL", group="TRENDLINE",
                    reasons=[f"Trendline breakout above descending highs → CALL reversal"]))
        # Ascending lows = bullish trendline
        elif lows[0] < lows[-1] and all(lows[i] <= lows[i+1]
                                        for i in range(len(lows)-1)):
            if close < min(lows[-2], lows[-1]):
                results.append(ModuleResult(
                    module_name="key_level", direction="PUT", score=2, confidence=56,
                    signal_type="REVERSAL", reliability="LEVEL", group="TRENDLINE",
                    reasons=[f"Trendline breakdown below ascending lows → PUT reversal"]))

    return results
