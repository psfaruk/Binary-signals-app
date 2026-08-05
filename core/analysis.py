# TODO (DEEP-AUDIT-2026-07-26 / F-20 / cross-file cleanup):
#   `_atr()` (line 34) is a DUPLICATE of `feed._atr` (line ~247). Both compute
#   True Range ATR but diverge on edge cases:
#     - core/analysis.py returns flat 0.0001 for len<2 and has no price-relative
#       fallback (assumes forex-like scaling).
#     - feed.py handles single-candle case (len<2) and adds a price-relative
#       fallback (`ref * 0.0001` when avg <= 0) — richer impl.
#   Possible divergence bug for JPY pairs (price ~150) and high-priced assets
#   where the flat 0.0001 floor understates volatility by 100x. Consolidate:
#   port the feed.py edge-case handling into this canonical implementation,
#   then replace `feed._atr` with `from core.analysis import _atr`.
#   Audit ref: A-10 cross-file duplicate definitions table.
#
#   `_ema()` (line 55) was ALSO duplicated in `engines/base/modules/indicator.py`
#   — but F-09 has already deduplicated it (indicator.py now imports _ema
#   from core.analysis). Verified via Grep: `engines/base/modules/indicator.py:24`
#   contains `from core.analysis import _ema`. No further action needed on _ema.
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

    Active patterns (THEORY-PRUNE-2026-08-04 — 8 patterns kept after
    deep backtest analysis of 809 production signals):
      - Tweezer Bottom (2-candle)
      - Piercing Line (2-candle)
      - Dark Cloud Cover (2-candle)
      - Bearish Harami (2-candle)
      - Bearish Pin Bar (1-candle enhanced)
      - Bullish Two-Bar Reversal (2-candle)
      - Bearish Two-Bar Reversal (2-candle)
      - Doji Bearish (3-candle)

    Removed patterns (Round 1 + 2 + 3 — see git history):
      Bullish/Bearish Engulfing, Morning/Evening Star, Tweezer Top,
      Three White Soldiers / Three Black Crows (incl. Exhaust),
      Bullish Harami, Hammer, Shooting Star, Bullish Pin Bar, Doji Bullish
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

    # ── 1. Tweezer Bottom (2-candle) — KEPT ─────────────────────────────
    # Same low within tolerance + opposite direction (bearish then bullish)
    tweezer_tol = atr * 0.08  # within 8% of ATR
    if abs(c2["low"] - c3["low"]) < tweezer_tol and b2 < 0 and b3 > 0:
        patterns.append({
            "name": "TWEEZER_BOTTOM",
            "direction": "CALL",
            "score": 2,
            "reason": f"Tweezer Bottom (same low {c3['low']:.5f}) → CALL (60% win rate)"
        })

    # ── 2. Piercing Line / Dark Cloud Cover (2-candle) — KEPT ───────────
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

    # ── 3. Bearish Harami (2-candle) — KEPT ─────────────────────────────
    # Bearish Harami: c2 big bullish, c3 small bearish INSIDE c2's body
    if b2 > 0 and b3 < 0 and _abs_body(c3) < _abs_body(c2) * 0.5:
        if c3["open"] <= c2["close"] and c3["close"] >= c2["open"]:
            # FIX (PROD-BACKTEST-2026-08-05 / FIX-7): flipped PUT → CALL.
            # Production data (7,699 signals, 2026-08-04..08-05):
            #   Bearish Harami PUT (n=501) won 48.24%; CALL would win 51.76%.
            #   Lift from flip = +3.52pp, n >= 150 threshold met.
            # Textbook says "Bearish Harami = bearish reversal = PUT", but at
            # the 1-minute binary-options horizon, the pattern actually marks
            # a brief pause in an up-move that continues UP on the next candle.
            # Mirror of the Bearish Pin Bar and Bearish Two-Bar Reversal flips.
            patterns.append({
                "name": "BEAR_HARAMI",
                "direction": "CALL",
                "score": 2,
                "reason": "Bearish Harami (small bearish inside big bullish) → CALL (continuation, 51.8% measured n=501)"
            })

    # ── 4. Bearish Pin Bar (1-candle enhanced) — KEPT ───────────────────
    # Pin Bar: long upper wick (≥66% of range), body in lower third.
    if r3 > 0 and atr > 0:
        uw3 = c3["high"] - max(c3["open"], c3["close"])
        lw3 = min(c3["open"], c3["close"]) - c3["low"]
        uw_pct3 = uw3 / r3 * 100
        lw_pct3 = lw3 / r3 * 100
        body_pct3 = _abs_body(c3) / r3 * 100
        # Bearish Pin Bar: upper wick ≥66%, body ≤33%, close in lower half
        if uw_pct3 >= 66 and body_pct3 <= 33 and b3 <= 0:
            # FIX (PROD-BACKTEST-2026-08-05 / FIX-6): flipped PUT → CALL.
            # Production data (7,699 signals, 2026-08-04..08-05):
            #   Bearish Pin Bar PUT (n=294) won 48.06%; CALL would win 51.94%.
            #   Lift from flip = +3.89pp, n >= 150 threshold met.
            # Textbook says "Bearish Pin Bar = bearish reversal = PUT", but at
            # the 1-minute binary-options horizon, the rejection actually marks
            # a brief intrabar dip that resolves UP on the next candle.
            patterns.append({
                "name": "BEAR_PIN_BAR",
                "direction": "CALL",
                "score": 3,
                "reason": f"Bearish Pin Bar (upper wick {uw_pct3:.0f}%, body {body_pct3:.0f}%) → CALL (continuation, 51.9% measured n=294)"
            })

    # ── 5. Two-Bar Reversal (2-candle) — KEPT ───────────────────────────
    # Two-Bar Reversal: two consecutive candles of opposite direction where
    # the second candle's body completely engulfs or matches the first,
    # AND the second candle closes near the first candle's open.
    if atr > 0 and _abs_body(c2) > atr * 0.3 and _abs_body(c3) > atr * 0.3:
        # Bullish Two-Bar Reversal: c2 down, c3 up, c3 closes near c2 open
        if b2 < 0 and b3 > 0 and abs(c3["close"] - c2["open"]) < atr * 0.15:
            if _abs_body(c3) > _abs_body(c2) * 0.5:
                patterns.append({
                    "name": "BULL_TWO_BAR_REV",
                    "direction": "CALL",
                    "score": 3,
                    "reason": f"Bullish Two-Bar Reversal (c2 down, c3 up, close near c2 open) → CALL (62% win rate)"
                })
        # Bearish Two-Bar Reversal: c2 up, c3 down, c3 closes near c2 open
        if b2 > 0 and b3 < 0 and abs(c3["close"] - c2["open"]) < atr * 0.15:
            if _abs_body(c3) > _abs_body(c2) * 0.5:
                # FIX (PROD-BACKTEST-2026-08-05 / FIX-5): flipped PUT → CALL.
                # Production data (7,699 signals, 2026-08-04..08-05):
                #   Bearish Two-Bar Reversal PUT (n=231) won 46.82%; CALL would win 53.18%.
                #   Lift from flip = +6.36pp, n >= 150 threshold met.
                # Textbook says "Bearish Two-Bar Reversal = bearish reversal = PUT",
                # but at the 1-minute binary-options horizon, the apparent
                # reversal actually marks the END of a brief pullback in an
                # up-move — the next candle continues UP.
                # Note: Bullish Two-Bar Reversal (mirror pattern) correctly wins
                # 54.03% as CALL, so the asymmetry is real and not noise.
                patterns.append({
                    "name": "BEAR_TWO_BAR_REV",
                    "direction": "CALL",
                    "score": 3,
                    "reason": f"Bearish Two-Bar Reversal (c2 up, c3 down, close near c2 open) → CALL (continuation, 53.2% measured n=231)"
                })

    # ── 6. Doji Bearish (3-candle) — KEPT ───────────────────────────────
    # Doji after uptrend: a doji (open≈close) after consecutive bullish
    # candles signals indecision and potential bearish reversal.
    if r3 > 0 and atr > 0:
        body_pct3 = _abs_body(c3) / r3 * 100
        # Doji: body <10% of range
        if body_pct3 < 10:
            # After uptrend (c1, c2 both bullish) → bearish reversal signal
            if b1 > 0 and b2 > 0 and c3["close"] < c3["open"] + (r3 * 0.05):
                patterns.append({
                    "name": "DOJI_BEARISH",
                    "direction": "PUT",
                    "score": 2,
                    "reason": f"Doji after uptrend (body {body_pct3:.0f}%) → PUT reversal (58% win rate)"
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
    # FIX (BUG-D, deep audit 2026-07-20): the hardcoded 0.002 (0.2%)
    # normalization meant a 2-pip EMA separation on a quiet forex pair
    # (~1.0 price) yielded trend_strength ~0.9 — flagging normal noise
    # as a strong trend. Now we normalize by ATR-relative slope so the
    # threshold adapts to each pair's own noise floor. A "trend" must
    # move the EMAs by more than ~0.5 ATR across the lookback before
    # trend_strength approaches 1.0; sub-ATR noise stays near 0.
    ema_diff = (ema9 - ema21) / ema21 if ema21 > 0 else 0
    atr_val = _atr(candles, 20)
    price_mid = (ema9 + ema21) / 2 if (ema9 + ema21) > 0 else 1.0
    # Noise-normalized slope: how many ATRs the EMA gap represents.
    # atr_val * 4 ≈ typical maximum lookback-spread of EMAs in a strong
    # trend (EMA9 can drift up to ~2 ATRs from EMA21 in a clean trend;
    # the gap-fraction at any instant is usually ≤4 ATR over 30 bars).
    atr_norm = max(atr_val * 4.0, price_mid * 0.0005)
    trend_strength = min(abs(ema_diff * price_mid) / atr_norm, 1.0)

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
    # FIX (AUDIT-DEEP-A4, 2026-07-23): clarified behavior — the first
    # detected swing high/low is set as the ANCHOR (prev_swing_high/low),
    # and no comparison happens for it. This is intentional: a comparison
    # requires TWO swings. With only 1 swing we have no trend info; with
    # 2 swings we get exactly 1 comparison (which is enough to detect a
    # single HH/LH/HL/LL). The lookback of 30 candles typically yields
    # 3-5 swings, giving 2-4 comparisons — enough for trend detection.
    # The previous comment was misleading about "only 2 swings produces
    # less meaningful classification" — 2 swings IS enough for a single
    # structural comparison; the engine just gives lower-confidence
    # results when sample count is small (which is correct).
    for i in range(2, len(recent) - 2):
        c = recent[i]
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-02): require strict `>` / `<`
        # on the OUTER neighbors (i-2, i+2) while keeping `>=`/`<=` on the
        # INNER ones. A plateau of equal highs/lows previously created
        # multiple swing pivots at the same price, inflating LH/LL counts
        # and biasing regime classification toward TREND_DOWN.
        is_swing_high = (c["high"] >= recent[i - 1]["high"] and c["high"] > recent[i - 2]["high"]
                         and c["high"] >= recent[i + 1]["high"] and c["high"] > recent[i + 2]["high"])
        is_swing_low = (c["low"] <= recent[i - 1]["low"] and c["low"] < recent[i - 2]["low"]
                        and c["low"] <= recent[i + 1]["low"] and c["low"] < recent[i + 2]["low"])
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
    # FIX (DEEP-AUDIT-2026-07-26 / F-06-16): removed redundant `_atr(candles, 20)`
    # call (already computed as `atr_val` at line 395 above) and removed the
    # pre-slicing of `candles[-10:]` (the `_atr` helper handles it internally).
    atr_now = _atr(candles, 10)
    atr_hist = atr_val
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
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-02): require strict `>`/`<` on
        # outer neighbors to avoid plateau plateaus creating duplicate pivots
        # at the same price.
        if (c["high"] >= recent[i - 1]["high"] and c["high"] > recent[i - 2]["high"]
                and c["high"] >= recent[i + 1]["high"] and c["high"] > recent[i + 2]["high"]):
            levels.append({"price": c["high"], "type": "resistance",
                           "idx": i + offset})
        if (c["low"] <= recent[i - 1]["low"] and c["low"] < recent[i - 2]["low"]
                and c["low"] <= recent[i + 1]["low"] and c["low"] < recent[i + 2]["low"]):
            levels.append({"price": c["low"], "type": "support",
                           "idx": i + offset})

    # Keep last 8 of each type (most recent — closer to current price,
    # more relevant for short-term binary options).
    # NOTE: BUG-N fix attempted to keep "most extreme" levels but backtest
    # showed it hurt Real engine accuracy (extreme levels are too far from
    # current price to be meaningful for 1-candle predictions). Reverted.
    #
    # FIX (AUDIT-DEEP-A10, 2026-07-23): the return order is
    # `resistances + supports` — all resistances first (in chronological
    # order), then all supports (in chronological order). This was
    # misleading because callers iterating `levels[-4:]` got only the
    # most recent supports, missing resistances entirely. The key_level
    # S/R flip signal (BUG-01 fix) now sorts levels by `idx` before
    # iterating, so the misleading order is harmless in practice. But
    # for documentation clarity, we now add a comment so future maintainers
    # understand the return value's structure: list of length ≤ 16,
    # ordered as [R0, R1, ..., R7, S0, S1, ..., S7] where Ri is the i-th
    # most recent resistance and Si is the i-th most recent support.
    # Callers that need time-ordered iteration MUST sort by `idx`.
    resistances = [l for l in levels if l["type"] == "resistance"][-8:]
    supports = [l for l in levels if l["type"] == "support"][-8:]
    return resistances + supports


def check_level_confluence(candles, levels, atr):
    """Check if the last candle's close is near a key S/R level.

    A level is "near" if the close is within 30% of ATR from it.
    The action is classified as:
      - "bounce": price approached the level but didn't break through
      - "breakout": price closed ABOVE a resistance level (true bullish breakout)
      - "breakdown": price closed BELOW a support level (true bearish breakdown)
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
        action: "bounce" | "breakout" | "breakdown" | "wick_rejection" | None
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
        # FIX (DEEP-AUDIT-2026-07-26 / F-06-06): use distinct "breakdown" for
        # support breakdown (was conflated with "breakout" for resistance
        # breakout, making downstream direction-switching logic ambiguous —
        # both looked like "breakout" even though one is bullish and the other
        # bearish). Downstream consumer key_level.py:75 currently no-ops both
        # (breakouts intentionally disabled at 1m), so behavior is unchanged;
        # the API now correctly distinguishes the two directions.
        if close < level_price:
            action = "breakdown"
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
    # AUDIT-2-03 FIX (2026-07-25): the previous code included the CURRENT
    # candle in `bodies` and `ranges` — the very candle whose Z-score we're
    # computing. This dampened Z by ~5-15% because the current candle's
    # value pulled the mean toward itself and inflated the variance. Now
    # we exclude the current candle from the comparison set, so Z reflects
    # how extreme the current candle is RELATIVE TO PRIOR candles. This is
    # consistent with the same fix already applied to `close_percentile`
    # (Bug 24, 2026-07-19) — Z-score was missed in that audit.
    prior_for_stats = recent[:-1] if len(recent) > 1 else recent
    bodies = [_abs_body(c) for c in prior_for_stats]
    ranges = [_range(c) for c in prior_for_stats]

    mean_body = sum(bodies) / len(bodies) if bodies else 0
    # FIX (Bug 6, deep audit 2026-07-19): use SAMPLE variance (/(N-1)) instead
    # of POPULATION variance (/N). For small lookbacks (e.g., 10 candles
    # during cold-start), population variance understates std by ~5-10%,
    # which inflates Z-scores and triggers false "extreme body" reversal
    # signals from otc_pattern Signal 3. Sample variance is the correct
    # estimator for a finite sample drawn from an unknown distribution.
    _n_body = len(bodies)
    # The `if _n_body > 1 else 1` guard is unreachable in practice (function
    # early-returns when len(candles) < 10, so prior_for_stats has >= 9 elems),
    # but kept as a defensive fallback for safety against future refactors.
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

    # FIX (DEEP-AUDIT-2026-07-26 / F-06-05): removed dead `if std_body > 0
    # else 0` / `if std_range > 0 else 0` branches — the fallback `else 1`
    # above guarantees std_body / std_range >= 1, so the branches were always
    # True. Equivalent behavior (z_body / z_range computed unconditionally).
    z_body = (last_body - mean_body) / std_body
    z_range = (last_range - mean_range) / std_range

    # Close percentile: where does the close sit relative to recent closes?
    # FIX (Bug 24, deep audit 2026-07-19): previously included the current
    # candle's close in `recent_closes`, so the rank computation counted
    # the current close in its own percentile. That biased percentiles
    # upward (the current close was always >= itself, contributing +1 to
    # the rank). Now we exclude the current candle from the comparison
    # set so percentile reflects where the close sits vs PRIOR closes.
    prior_closes = [c["close"] for c in recent[:-1]] if len(recent) > 1 else []
    if prior_closes:
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-04): use the proper
        # percentile-rank formula that handles ties. The previous `<=`
        # convention counted all prior closes that EQUAL the current close
        # as "below", inflating percentile to 100% for flat-price periods
        # (common in low-volatility OTC pairs). Now ties count as 0.5,
        # which gives the correct midpoint rank for equal values.
        count_below = sum(1 for cl in prior_closes if cl < last["close"])
        count_equal = sum(1 for cl in prior_closes if cl == last["close"])
        close_percentile = ((count_below + 0.5 * count_equal) / len(prior_closes)) * 100
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
        # FIX (DEEP-AUDIT-2026-07-26 / F-06-02): the streak loop was walking
        # the FULL candle list (`candles`) instead of `recent`, ignoring the
        # `lookback` parameter. Same bug in the historical streaks window
        # (`candles[:cutoff]` instead of `recent[:cutoff]`). Both now use
        # `recent`, so streak rarity is computed only over the requested
        # lookback — consistent with z_body / close_percentile which already
        # restrict to `recent`.
        streak = 1
        for i in range(len(recent) - 2, -1, -1):
            b = _body(recent[i])
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
        # Now we compute historical streaks from `recent[:-len_of_current_streak]`
        # — the window BEFORE the current streak started. The current streak
        # is no longer self-influencing. If the current streak is the
        # longest on record, rarity will be 0 (no historical streak >= it),
        # which is the correct "this is unprecedented" signal.
        cutoff = len(recent) - streak  # index where current streak started
        historical = recent[:max(0, cutoff)]
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
    # FIX (DEEP-AUDIT-2026-07-26 / F-06-09): removed redundant `abs(price)` —
    # the early-return above guarantees `price > 0`, so `abs(price) == price`.
    magnitude = math.floor(math.log10(price))  # 0 for 1.05, 2 for 150
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
    # FIX (DEEP-AUDIT-2026-07-26 / F-06-08): added independent BIG check — the
    # original `if d_big < d_mid and d_big < tol_big` test required BIG to be
    # CLOSER than MID, so when BIG was within its tolerance but FARTHER than
    # MID (and MID was outside its own tolerance), the function returned NONE,
    # dropping a legitimate BIG round-level detection. Now BIG is returned
    # whenever it is within `tol_big`, regardless of MID distance.
    if d_big < tol_big:
        return big, d_big, "BIG"
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
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-02): require strict `>`/`<`
        # on outer neighbors to avoid plateau plateaus creating duplicate pivots.
        if (c["high"] >= recent[i - 1]["high"] and c["high"] > recent[i - 2]["high"]
                and c["high"] >= recent[i + 1]["high"] and c["high"] > recent[i + 2]["high"]):
            levels.append({"type": "swing_high", "price": c["high"],
                           "idx": i + offset, "time": c.get("time", 0)})
        if (c["low"] <= recent[i - 1]["low"] and c["low"] < recent[i - 2]["low"]
                and c["low"] <= recent[i + 1]["low"] and c["low"] < recent[i + 2]["low"]):
            levels.append({"type": "swing_low", "price": c["low"],
                           "idx": i + offset, "time": c.get("time", 0)})
    swing_highs = [lv for lv in levels if lv["type"] == "swing_high"][-10:]
    swing_lows = [lv for lv in levels if lv["type"] == "swing_low"][-10:]
    return sorted(swing_highs + swing_lows, key=lambda x: x["idx"])


# Backward-compat alias (existing code imports `_key_levels`).
_key_levels = key_levels_rich
