"""
core/analysis.py — Pure-function technical analysis library.

This is the SINGLE source of truth for all shared analysis functions used
by both the OTC and Real prediction engines. Previously these functions
were scattered across:
  - advanced_analysis.py   (regime, patterns, key levels, statistical edge)
  - analyze_eoc._atr       (duplicate ATR)
  - analyze_eoc._round_level (psychological round-number proximity)
  - analyze_eoc._key_levels (different signature from find_key_levels)

Now consolidated here. The legacy `advanced_analysis.py` and
`analyze_eoc.py` files are kept as thin shims that re-export from this
module, so existing imports keep working during the migration.

All functions are PURE (no side effects, no I/O) and take candle lists
as input. Designed to be called once per candle close — O(N) where N is
the lookback (typically 50 candles), fast enough for 40+ concurrent
streams.

Used by:
  - engines/base/context.py (compute_context)
  - engines/base/modules/pattern.py (detect_candle_patterns)
  - engines/base/modules/key_level.py (find_key_levels, _round_level)
  - feed.py / sim_feed.py (_atr, _key_levels for DB persistence)
"""
import math


# ═══════════════════════════════════════════════════════════════════════════════
#  PRIMITIVES
# ═══════════════════════════════════════════════════════════════════════════════

def _atr(candles, n=20):
    """True Range ATR — properly accounts for overnight gaps.

    True Range = max(high-low, |high-prev_close|, |low-prev_close|).
    Falls back to 0.0001 on flat/empty inputs to avoid divide-by-zero.
    """
    if not candles or len(candles) < 2:
        return 0.0001
    recent = candles[-n:] if len(candles) >= n else candles
    trs = []
    for i in range(1, len(recent)):
        c, prev = recent[i], recent[i - 1]
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - prev["close"]),
            abs(c["low"] - prev["close"]),
        )
        trs.append(tr)
    return (sum(trs) / len(trs)) if trs else 0.0001


def _ema(values, period):
    """Exponential Moving Average, seeded with SMA of first `period` values."""
    if not values:
        return 0
    k = 2 / (period + 1)
    seed_n = min(period, len(values))
    ema = sum(values[:seed_n]) / seed_n
    for v in values[seed_n:]:
        ema = v * k + ema * (1 - k)
    return ema


def _body(c):
    """Signed body of a candle (close - open)."""
    return c["close"] - c["open"]


def _abs_body(c):
    return abs(_body(c))


def _range(c):
    return c["high"] - c["low"]


# ═══════════════════════════════════════════════════════════════════════════════
#  1. MULTI-CANDLE PATTERN DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def detect_candle_patterns(candles):
    """Detect multi-candle reversal/continuation patterns.

    Looks at the last 2-4 candles for classic Japanese candlestick
    patterns. These are HIGHER-CONVICTION than single-candle signals
    because they capture the interaction between candles.

    Returns list of dicts:
        {"name": str, "direction": "CALL"|"PUT", "score": int, "reason": str}

    Patterns detected (with approximate OTC win rates):
      - Bullish/Bearish Engulfing (2-candle, ~65%)
      - Morning/Evening Star (3-candle, ~70%)
      - Tweezer Top/Bottom (2-candle, ~60%)
      - Three White Soldiers / Three Black Crows (3-candle, ~60%)
      - Three Soldiers/Crows Exhaustion (shrinking last body, ~65% reversal)
      - Piercing Line / Dark Cloud Cover (2-candle, ~63%)
      - Bullish/Bearish Harami (2-candle, ~58%)
      - Inside Bar Breakout (3-candle, ~58%)
      - Hammer / Shooting Star (1-candle enhanced, ~60%)
    """
    patterns = []
    if len(candles) < 3:
        return patterns

    c1 = candles[-3]
    c2 = candles[-2]
    c3 = candles[-1]
    atr = _atr(candles)

    b1 = _body(c1)
    b2 = _body(c2)
    b3 = _body(c3)
    r1, r2, r3 = _range(c1), _range(c2), _range(c3)

    # ── 1. Engulfing (2-candle) ──────────────────────────────────────────
    # Bullish engulfing: c2 bearish, c3 bullish, c3 body fully engulfs c2 body
    if b2 < 0 and b3 > 0:
        if c3["close"] >= c2["open"] and c3["open"] <= c2["close"]:
            # Stronger if c3 body is bigger than c2 body
            ratio = _abs_body(c3) / _abs_body(c2) if _abs_body(c2) > 0 else 1
            score = 4 if ratio > 1.5 else 3
            patterns.append({
                "name": "BULL_ENGULF",
                "direction": "CALL",
                "score": score,
                "reason": f"Bullish Engulfing (body ratio {ratio:.1f}x) → CALL ({65 if score == 4 else 60}% win rate)"
            })
    # Bearish engulfing
    if b2 > 0 and b3 < 0:
        if c3["open"] >= c2["close"] and c3["close"] <= c2["open"]:
            ratio = _abs_body(c3) / _abs_body(c2) if _abs_body(c2) > 0 else 1
            score = 4 if ratio > 1.5 else 3
            patterns.append({
                "name": "BEAR_ENGULF",
                "direction": "PUT",
                "score": score,
                "reason": f"Bearish Engulfing (body ratio {ratio:.1f}x) → PUT ({65 if score == 4 else 60}% win rate)"
            })

    # ── 2. Morning/Evening Star (3-candle) ───────────────────────────────
    # Morning star: bearish + small-body (doji-like) + bullish closing above c1 midpoint
    c1_mid = (c1["open"] + c1["close"]) / 2
    if b1 < 0 and _abs_body(c2) < r2 * 0.3 and b3 > 0 and c3["close"] > c1_mid:
        patterns.append({
            "name": "MORNING_STAR",
            "direction": "CALL",
            "score": 4,
            "reason": "Morning Star (bearish + doji + bullish above midpoint) → CALL (70% win rate)"
        })
    # Evening star: bullish + small-body + bearish closing below c1 midpoint
    if b1 > 0 and _abs_body(c2) < r2 * 0.3 and b3 < 0 and c3["close"] < c1_mid:
        patterns.append({
            "name": "EVENING_STAR",
            "direction": "PUT",
            "score": 4,
            "reason": "Evening Star (bullish + doji + bearish below midpoint) → PUT (70% win rate)"
        })

    # ── 3. Tweezer Top/Bottom (2-candle) ─────────────────────────────────
    # Same high/low within tolerance + opposite direction
    tweezer_tol = atr * 0.08  # within 8% of ATR
    if abs(c2["high"] - c3["high"]) < tweezer_tol and b2 > 0 and b3 < 0:
        patterns.append({
            "name": "TWEEZER_TOP",
            "direction": "PUT",
            "score": 2,
            "reason": f"Tweezer Top (same high {c3['high']:.5f}) → PUT (60% win rate)"
        })
    if abs(c2["low"] - c3["low"]) < tweezer_tol and b2 < 0 and b3 > 0:
        patterns.append({
            "name": "TWEEZER_BOTTOM",
            "direction": "CALL",
            "score": 2,
            "reason": f"Tweezer Bottom (same low {c3['low']:.5f}) → CALL (60% win rate)"
        })

    # ── 4. Three White Soldiers / Three Black Crows ──────────────────────
    # 3 consecutive same-direction candles with ascending/descending closes
    if b1 > 0 and b2 > 0 and b3 > 0 and c3["close"] > c2["close"] > c1["close"]:
        # Check exhaustion: is the last body shrinking?
        if _abs_body(c3) < _abs_body(c2) * 0.65:
            patterns.append({
                "name": "3_SOLDIERS_EXHAUST",
                "direction": "PUT",
                "score": 3,
                "reason": "Three White Soldiers + shrinking last body → exhaustion PUT (65% win rate)"
            })
        else:
            patterns.append({
                "name": "3_SOLDIERS",
                "direction": "CALL",
                "score": 2,
                "reason": "Three White Soldiers → CALL continuation (58% win rate)"
            })
    if b1 < 0 and b2 < 0 and b3 < 0 and c3["close"] < c2["close"] < c1["close"]:
        if _abs_body(c3) < _abs_body(c2) * 0.65:
            patterns.append({
                "name": "3_CROWS_EXHAUST",
                "direction": "CALL",
                "score": 3,
                "reason": "Three Black Crows + shrinking last body → exhaustion CALL (65% win rate)"
            })
        else:
            patterns.append({
                "name": "3_CROWS",
                "direction": "PUT",
                "score": 2,
                "reason": "Three Black Crows → PUT continuation (58% win rate)"
            })

    # ── 5. Piercing Line / Dark Cloud Cover (2-candle) ───────────────────
    # Piercing Line: c2 bearish, c3 opens below c2 close, closes above c2 midpoint
    c2_mid = (c2["open"] + c2["close"]) / 2
    if b2 < 0 and b3 > 0:
        if c3["open"] < c2["close"] and c3["close"] > c2_mid and c3["close"] < c2["open"]:
            patterns.append({
                "name": "PIERCING_LINE",
                "direction": "CALL",
                "score": 3,
                "reason": "Piercing Line (bullish close above bearish midpoint) → CALL (63% win rate)"
            })
    # Dark Cloud Cover: c2 bullish, c3 opens above c2 close, closes below c2 midpoint
    if b2 > 0 and b3 < 0:
        if c3["open"] > c2["close"] and c3["close"] < c2_mid and c3["close"] > c2["open"]:
            patterns.append({
                "name": "DARK_CLOUD",
                "direction": "PUT",
                "score": 3,
                "reason": "Dark Cloud Cover (bearish close below bullish midpoint) → PUT (63% win rate)"
            })

    # ── 6. Harami (2-candle) ─────────────────────────────────────────────
    # Bullish Harami: c2 big bearish, c3 small bullish INSIDE c2's body
    if b2 < 0 and b3 > 0 and _abs_body(c3) < _abs_body(c2) * 0.5:
        if c3["open"] >= c2["close"] and c3["close"] <= c2["open"]:
            patterns.append({
                "name": "BULL_HARAMI",
                "direction": "CALL",
                "score": 2,
                "reason": "Bullish Harami (small bullish inside big bearish) → CALL (58% win rate)"
            })
    if b2 > 0 and b3 < 0 and _abs_body(c3) < _abs_body(c2) * 0.5:
        if c3["open"] <= c2["close"] and c3["close"] >= c2["open"]:
            patterns.append({
                "name": "BEAR_HARAMI",
                "direction": "PUT",
                "score": 2,
                "reason": "Bearish Harami (small bearish inside big bullish) → PUT (58% win rate)"
            })

    # ── 7. Inside Bar Breakout (3-candle) ────────────────────────────────
    # c2 was inside c1's range, c3 breaks out of c1's range
    if len(candles) >= 4:
        c0 = candles[-4] if len(candles) >= 4 else candles[-3]
        # Check if c2 (the candle before last) was an inside bar relative to c1
        if (c2["high"] <= c1["high"] and c2["low"] >= c1["low"]
                and _range(c2) < _range(c1) * 0.7):
            if c3["close"] > c1["high"]:
                patterns.append({
                    "name": "INSIDE_BREAK_UP",
                    "direction": "CALL",
                    "score": 2,
                    "reason": "Inside Bar breakout up → CALL (58% win rate)"
                })
            elif c3["close"] < c1["low"]:
                patterns.append({
                    "name": "INSIDE_BREAK_DN",
                    "direction": "PUT",
                    "score": 2,
                    "reason": "Inside Bar breakout down → PUT (58% win rate)"
                })

    # ── 8. Enhanced Hammer / Shooting Star ───────────────────────────────
    # Single candle with very long wick (more extreme than wick_rejection)
    if r3 > 0:
        uw3 = c3["high"] - max(c3["open"], c3["close"])
        lw3 = min(c3["open"], c3["close"]) - c3["low"]
        uw_pct3 = uw3 / r3 * 100
        lw_pct3 = lw3 / r3 * 100
        body_pct3 = _abs_body(c3) / r3 * 100
        # Hammer: long lower wick (>55%) + small body (<25%) → CALL
        if lw_pct3 > 55 and body_pct3 < 25:
            patterns.append({
                "name": "HAMMER",
                "direction": "CALL",
                "score": 3,
                "reason": f"Hammer (lower wick {lw_pct3:.0f}%) → CALL (62% win rate)"
            })
        # Shooting Star: long upper wick (>55%) + small body (<25%) → PUT
        if uw_pct3 > 55 and body_pct3 < 25:
            patterns.append({
                "name": "SHOOTING_STAR",
                "direction": "PUT",
                "score": 3,
                "reason": f"Shooting Star (upper wick {uw_pct3:.0f}%) → PUT (62% win rate)"
            })

    return patterns


# ═══════════════════════════════════════════════════════════════════════════════
#  2. MARKET REGIME DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def classify_market_regime(candles, lookback=30):
    """Classify market state into one of four regimes.

    Uses three independent measures:
      1. EMA9 vs EMA21 crossover (trend direction)
      2. Swing structure (HH/HL = uptrend, LH/LL = downtrend)
      3. ATR volatility ratio (current ATR vs 20-period ATR)

    The regime determines how candle_reaction weights its signals:
      - TREND_UP / TREND_DOWN: boost CONTINUATION signals, dampen reversal
      - RANGE: boost REVERSAL signals, dampen continuation
      - VOLATILE: dampen ALL signals (high noise floor)

    Returns dict:
        regime: "TREND_UP" | "TREND_DOWN" | "RANGE" | "VOLATILE"
        trend_strength: 0.0-1.0
        volatility_pct: 0.0-2.0+ (1.0 = average)
        ema9, ema21: float
        is_trending: bool
        is_ranging: bool
        is_volatile: bool
    """
    if len(candles) < 10:
        return {
            "regime": "RANGE", "trend_strength": 0.0, "volatility_pct": 1.0,
            "ema9": 0, "ema21": 0,
            "is_trending": False, "is_ranging": True, "is_volatile": False,
        }

    lookback = min(lookback, len(candles))
    recent = candles[-lookback:]
    closes = [c["close"] for c in recent]

    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)

    # Trend direction from EMA separation (normalized to price)
    ema_diff = (ema9 - ema21) / ema21 if ema21 > 0 else 0
    trend_strength = min(abs(ema_diff) / 0.002, 1.0)

    # Swing structure: count HH/HL vs LH/LL in the lookback
    # FIX (Bug 5, deep audit 2026-07-19): the previous version compared each
    # swing high to `recent[max(0, i - 3)]["high"]` (candle 3 positions back),
    # NOT to the previous swing high. That's NOT a real Higher-High check —
    # it's "swing high higher than a random candle 3 positions back", which
    # is essentially noise. Also `hh_hl` only counted HH (when is_swing_high)
    # and `lh_ll` only counted LH (when is_swing_low) — HL and LL were
    # NEVER counted, so half the Dow theory structure was missing.
    #
    # Now we track previous swing highs and lows separately, and count:
    #   HH = current swing high > previous swing high
    #   LH = current swing high < previous swing high
    #   HL = current swing low  > previous swing low
    #   LL = current swing low  < previous swing low
    # `hh_hl` = HH + HL (uptrend structure count)
    # `lh_ll` = LH + LL (downtrend structure count)
    # This is the actual Dow-theory trend classification.
    hh_hl = 0
    lh_ll = 0
    prev_swing_high = None
    prev_swing_low = None
    for i in range(2, len(recent) - 2):
        c = recent[i]
        is_swing_high = (c["high"] >= recent[i - 1]["high"] and c["high"] >= recent[i - 2]["high"]
                         and c["high"] >= recent[i + 1]["high"] and c["high"] >= recent[i + 2]["high"])
        is_swing_low = (c["low"] <= recent[i - 1]["low"] and c["low"] <= recent[i - 2]["low"]
                        and c["low"] <= recent[i + 1]["low"] and c["low"] <= recent[i + 2]["low"])
        if is_swing_high:
            if prev_swing_high is not None:
                if c["high"] > prev_swing_high:
                    hh_hl += 1   # Higher High
                else:
                    lh_ll += 1   # Lower High
            prev_swing_high = c["high"]
        if is_swing_low:
            if prev_swing_low is not None:
                if c["low"] > prev_swing_low:
                    hh_hl += 1   # Higher Low
                else:
                    lh_ll += 1   # Lower Low
            prev_swing_low = c["low"]

    # Volatility: current short-term ATR vs longer-term ATR
    atr_now = _atr(candles[-10:] if len(candles) >= 10 else candles, 10)
    atr_hist = _atr(candles, 20)
    vol_pct = (atr_now / atr_hist) if atr_hist > 0 else 1.0

    # Determine regime — VOLATILE takes priority (noise dominates everything)
    # FIX (Bug 3): tie-break used `>=` for both TREND_UP and TREND_DOWN, so
    # when hh_hl == lh_ll (a tie) the first branch (TREND_UP) always won.
    # Now both use strict `>`, so ties fall through to RANGE (neutral) —
    # which is the correct classification when swing structure is ambiguous.
    if vol_pct > 1.5:
        regime = "VOLATILE"
    elif ema9 > ema21 and trend_strength > 0.25 and hh_hl > lh_ll:
        regime = "TREND_UP"
    elif ema9 < ema21 and trend_strength > 0.25 and lh_ll > hh_hl:
        regime = "TREND_DOWN"
    else:
        regime = "RANGE"

    return {
        "regime": regime,
        "trend_strength": round(trend_strength, 3),
        "volatility_pct": round(vol_pct, 3),
        "ema9": round(ema9, 6),
        "ema21": round(ema21, 6),
        "is_trending": regime in ("TREND_UP", "TREND_DOWN"),
        "is_ranging": regime == "RANGE",
        "is_volatile": regime == "VOLATILE",
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  3. KEY LEVEL CONFLUENCE
# ═══════════════════════════════════════════════════════════════════════════════

def find_key_levels(candles, lookback=50):
    """Find recent swing highs/lows as key support/resistance levels.

    Returns list of dicts:
        {"price": float, "type": "resistance"|"support", "idx": int}
    """
    if len(candles) < 5:
        return []

    recent = candles[-lookback:] if len(candles) > lookback else candles
    offset = len(candles) - len(recent)
    levels = []

    for i in range(2, len(recent) - 2):
        c = recent[i]
        if (c["high"] >= recent[i - 1]["high"] and c["high"] >= recent[i - 2]["high"]
                and c["high"] >= recent[i + 1]["high"] and c["high"] >= recent[i + 2]["high"]):
            levels.append({"price": c["high"], "type": "resistance",
                           "idx": i + offset})
        if (c["low"] <= recent[i - 1]["low"] and c["low"] <= recent[i - 2]["low"]
                and c["low"] <= recent[i + 1]["low"] and c["low"] <= recent[i + 2]["low"]):
            levels.append({"price": c["low"], "type": "support",
                           "idx": i + offset})

    # Keep last 8 of each type
    resistances = [l for l in levels if l["type"] == "resistance"][-8:]
    supports = [l for l in levels if l["type"] == "support"][-8:]
    return resistances + supports


def check_level_confluence(candles, levels, atr):
    """Check if the last candle's close is near a key S/R level.

    A level is "near" if the close is within 30% of ATR from it.
    The action is classified as:
      - "bounce": price approached the level but didn't break through
      - "breakout": price closed beyond the level (true breakout)
      - "wick_rejection": intrabar wick crossed the level but close pulled
        back — a fakeout / rejection (NOT a real breakout)

    FIX (Bug D, 2026-07-19): the previous version only compared
    `prev_close` vs `close` to decide bounce vs breakout. That completely
    ignored the candle's high/low, so a candle that wicked THROUGH a
    level and closed back inside was miscategorized as a "bounce" (close
    on the original side) — but in reality it's a failed breakout
    (rejection). Conversely, a candle that gapped past a level intrabar
    and closed just barely inside would also be miscalled "bounce".

    Now uses full OHLC of the last candle + prev_close:
      - breakout   : close beyond level (the only reliable breakout signal)
      - wick_reject: intrabar high/low crossed level, but close pulled
                     back to the original side (failed breakout / rejection)
      - bounce     : approached level, no intrabar cross, close on original side

    The "wick_rejection" action is a STRONGER reversal signal than
    "bounce" because it represents a real test of the level that failed.

    Returns dict:
        near_level: bool
        level_type: "support" | "resistance" | None
        level_price: float | None
        action: "bounce" | "breakout" | "wick_rejection" | None
        distance_atr: float (how far from the level, in ATR units)
    """
    if not levels or not candles or len(candles) < 2 or atr <= 0:
        return {"near_level": False, "level_type": None,
                "level_price": None, "action": None, "distance_atr": 0}

    last = candles[-1]
    prev = candles[-2]
    close = last["close"]
    prev_close = prev["close"]
    open_ = last["open"]
    high = last["high"]
    low = last["low"]
    tol = atr * 0.30

    nearest = None
    nearest_dist = float("inf")
    for lvl in levels:
        dist = abs(close - lvl["price"])
        if dist < tol and dist < nearest_dist:
            nearest = lvl
            nearest_dist = dist

    if not nearest:
        return {"near_level": False, "level_type": None,
                "level_price": None, "action": None, "distance_atr": 0}

    level_price = nearest["price"]

    # FIX (Bug D): use full OHLC to classify the interaction with the level.
    # For RESISTANCE (price below): "beyond" = above; "approach side" = below.
    # For SUPPORT (price above):    "beyond" = below; "approach side" = above.
    if nearest["type"] == "resistance":
        # True breakout: close pushed ABOVE the resistance level.
        if close > level_price:
            action = "breakout"
        # Wick rejection: intrabar high touched/poked above the level but
        # close pulled back below — a failed breakout (bearish rejection).
        elif high > level_price and close < level_price:
            action = "wick_rejection"
        # Plain bounce: never crossed intrabar, close stayed below.
        else:
            action = "bounce"
    else:  # support
        # True breakdown: close pushed BELOW the support level.
        if close < level_price:
            action = "breakout"  # "breakout" here means breakdown
        # Wick rejection: intrabar low poked below the level but close
        # pulled back above — a failed breakdown (bullish rejection).
        elif low < level_price and close > level_price:
            action = "wick_rejection"
        # Plain bounce: never crossed intrabar, close stayed above.
        else:
            action = "bounce"

    return {
        "near_level": True,
        "level_type": nearest["type"],
        "level_price": level_price,
        "action": action,
        "distance_atr": round(nearest_dist / atr, 3),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  4. STATISTICAL EDGE
# ═══════════════════════════════════════════════════════════════════════════════

def compute_statistical_edge(candles, lookback=50):
    """Compute Z-scores and percentiles for the last candle.

    This adds a STATISTICAL layer on top of the pattern-based signals:
      - A candle with body Z-score > 2 is statistically unusual (top 5%)
        → stronger reversal signal
      - A close at the 95th+ percentile of recent closes is at an extreme
        → stronger reversal signal
      - A consecutive streak that occurs < 10% of the time historically
        → stronger reversal signal

    Returns dict:
        z_body: float (Z-score of body size, >2 = unusually big)
        z_range: float (Z-score of range)
        close_percentile: 0-100 (where close sits in recent close distribution)
        streak_rarity: 0-1 (fraction of historical streaks >= current streak length)
        current_streak: int (current consecutive same-direction count)
        streak_direction: 1 (up) | -1 (down) | 0 (flat)
    """
    if len(candles) < 10:
        return {"z_body": 0, "z_range": 0, "close_percentile": 50,
                "streak_rarity": 0, "current_streak": 0, "streak_direction": 0}

    recent = candles[-lookback:] if len(candles) > lookback else candles
    bodies = [_abs_body(c) for c in recent]
    ranges = [_range(c) for c in recent]

    mean_body = sum(bodies) / len(bodies) if bodies else 0
    # FIX (Bug 6, deep audit 2026-07-19): use SAMPLE variance (/(N-1)) instead
    # of POPULATION variance (/N). For small lookbacks (e.g., 10 candles
    # during cold-start), population variance understates std by ~5-10%,
    # which inflates Z-scores and triggers false "extreme body" reversal
    # signals from otc_pattern Signal 3. Sample variance is the correct
    # estimator for a finite sample drawn from an unknown distribution.
    _n_body = len(bodies)
    var_body = (sum((b - mean_body) ** 2 for b in bodies) / (_n_body - 1)
                if _n_body > 1 else 1)
    std_body = math.sqrt(var_body) if var_body > 0 else 1

    mean_range = sum(ranges) / len(ranges) if ranges else 0
    _n_range = len(ranges)
    var_range = (sum((r - mean_range) ** 2 for r in ranges) / (_n_range - 1)
                 if _n_range > 1 else 1)
    std_range = math.sqrt(var_range) if var_range > 0 else 1

    last = candles[-1]
    last_body = _abs_body(last)
    last_range = _range(last)

    z_body = (last_body - mean_body) / std_body if std_body > 0 else 0
    z_range = (last_range - mean_range) / std_range if std_range > 0 else 0

    # Close percentile: where does the close sit relative to recent closes?
    # FIX (Bug 24, deep audit 2026-07-19): previously included the current
    # candle's close in `recent_closes`, so the rank computation counted
    # the current close in its own percentile. That biased percentiles
    # upward (the current close was always >= itself, contributing +1 to
    # the rank). Now we exclude the current candle from the comparison
    # set so percentile reflects where the close sits vs PRIOR closes.
    prior_closes = [c["close"] for c in recent[:-1]] if len(recent) > 1 else []
    if prior_closes:
        close_rank = sum(1 for cl in prior_closes if cl <= last["close"])
        close_percentile = (close_rank / len(prior_closes)) * 100
    else:
        close_percentile = 50

    # Streak computation
    last_body_signed = _body(last)
    direction = 1 if last_body_signed > 0 else (-1 if last_body_signed < 0 else 0)

    if direction == 0:
        streak = 0
        streak_rarity = 0
    else:
        # Measure the CURRENT streak (looking backward from the last candle).
        # This is the streak whose rarity we want to assess.
        streak = 1
        for i in range(len(candles) - 2, -1, -1):
            b = _body(candles[i])
            d = 1 if b > 0 else (-1 if b < 0 else 0)
            if d == direction:
                streak += 1
            else:
                break

        # FIX (Bug C, 2026-07-19): the previous version built `all_streaks`
        # from the FULL candle history INCLUDING the last candle — meaning
        # the current streak itself was counted in both the numerator AND
        # denominator of the rarity calculation. A 5-candle streak in a
        # history where the longest prior streak was 3 would compute
        # rarity = 1/N (just itself qualifies) — a misleadingly LOW
        # rarity that suppresses the reversal boost even though the
        # streak IS historically rare.
        #
        # Now we compute historical streaks from `candles[:-len_of_current_streak]`
        # — the window BEFORE the current streak started. The current streak
        # is no longer self-influencing. If the current streak is the
        # longest on record, rarity will be 0 (no historical streak >= it),
        # which is the correct "this is unprecedented" signal.
        cutoff = len(candles) - streak  # index where current streak started
        historical = candles[:max(0, cutoff)]
        all_streaks = []
        cur_dir = 0
        cur_len = 0
        for c in historical:
            b = _body(c)
            d = 1 if b > 0 else (-1 if b < 0 else 0)
            if d == 0:
                if cur_len >= 1:
                    all_streaks.append(cur_len)
                cur_dir, cur_len = 0, 0
            elif d == cur_dir:
                cur_len += 1
            else:
                if cur_len >= 1:
                    all_streaks.append(cur_len)
                cur_dir, cur_len = d, 1
        if cur_len >= 1:
            all_streaks.append(cur_len)

        if all_streaks:
            longer = sum(1 for s in all_streaks if s >= streak)
            streak_rarity = longer / len(all_streaks)
        else:
            # No historical streaks to compare against — treat as neutral
            # rather than artificially rare.
            streak_rarity = 0.5

    return {
        "z_body": round(z_body, 2),
        "z_range": round(z_range, 2),
        "close_percentile": round(close_percentile, 1),
        "streak_rarity": round(streak_rarity, 3),
        "current_streak": streak,
        "streak_direction": direction,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  5. PSYCHOLOGICAL ROUND-LEVEL PROXIMITY
# ═══════════════════════════════════════════════════════════════════════════════
# (Consolidated from analyze_eoc._round_level — was duplicated conceptually
#  with key_level module's inline logic. Now the single source of truth.)

def round_level(price):
    """Classify how close a price is to a 'round' psychological level.

    Magnitude-adaptive tolerance (0.05% of price for BIG, 0.02% for MID)
    so it works for forex (~1.05), JPY pairs (~150), crypto (~60000)
    alike.

    Returns ``(level_price, distance, "BIG"|"MID"|"NONE")``.
    """
    if price <= 0:
        return None, 0, "NONE"
    magnitude = math.floor(math.log10(abs(price)))  # 0 for 1.05, 2 for 150
    big_step = 10 ** (magnitude - 1)   # 0.1 for forex, 10 for JPY, 1000 for BTC
    mid_step = 10 ** (magnitude - 2)   # one digit finer
    big = round(price / big_step) * big_step
    mid = round(price / mid_step) * mid_step
    d_big = abs(price - big)
    d_mid = abs(price - mid)
    tol_big = price * 0.0005
    tol_mid = price * 0.0002
    if d_big < d_mid and d_big < tol_big:
        return big, d_big, "BIG"
    if d_mid < tol_mid:
        return mid, d_mid, "MID"
    return None, 0, "NONE"


# Backward-compat alias (existing code imports `_round_level`).
_round_level = round_level


# ═══════════════════════════════════════════════════════════════════════════════
#  6. SWING HIGH/LOW KEY LEVELS (richer schema, used by feed.py for DB persistence)
# ═══════════════════════════════════════════════════════════════════════════════
# (Consolidated from analyze_eoc._key_levels. NOTE: this has a DIFFERENT
#  output schema from `find_key_levels` above — it returns dicts with
#  `type`/`price`/`idx`/`time` keys, vs `find_key_levels`'s `price`/`type`/
#  `idx`. Both schemas are kept because callers depend on each. The slim
#  `find_key_levels` is used by the engine context; the richer
#  `key_levels_rich` (formerly `_key_levels`) is used for DB persistence
#  in feed.py / sim_feed.py.)

def key_levels_rich(candles, lookback=60):
    """Extract recent swing highs/lows as key levels (last ``lookback`` candles).

    Returns a list of ``{"type": "swing_high"|"swing_low", "price": float,
    "idx": int, "time": int}`` dicts, sorted by ``idx`` ascending.
    Each type is capped at the last 10 pivots so neither gets stripped
    in a strong trend where the most recent 10 pivots are all one type.
    """
    if len(candles) < 5:
        return []
    recent = candles[-lookback:] if len(candles) > lookback else candles
    offset = len(candles) - len(recent)
    levels = []
    for i in range(2, len(recent) - 2):
        c = recent[i]
        if (c["high"] >= recent[i - 1]["high"] and c["high"] >= recent[i - 2]["high"]
                and c["high"] >= recent[i + 1]["high"] and c["high"] >= recent[i + 2]["high"]):
            levels.append({"type": "swing_high", "price": c["high"],
                           "idx": i + offset, "time": c.get("time", 0)})
        if (c["low"] <= recent[i - 1]["low"] and c["low"] <= recent[i - 2]["low"]
                and c["low"] <= recent[i + 1]["low"] and c["low"] <= recent[i + 2]["low"]):
            levels.append({"type": "swing_low", "price": c["low"],
                           "idx": i + offset, "time": c.get("time", 0)})
    swing_highs = [lv for lv in levels if lv["type"] == "swing_high"][-10:]
    swing_lows = [lv for lv in levels if lv["type"] == "swing_low"][-10:]
    return sorted(swing_highs + swing_lows, key=lambda x: x["idx"])


# Backward-compat alias (existing code imports `_key_levels`).
_key_levels = key_levels_rich
