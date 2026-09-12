"""core/otc_predict/features_ext.py — extended feature set (PART 6+7).

USER SPEC (PART 6 — Feature Engineering):

    শুধু Open/High/Low/Close যথেষ্ট নয়।
      Candle Features : body, range, upper/lower wick, body_ratio, direction
      Momentum        : 1/3/5/10-candle return
      Volatility      : rolling range, ATR-like, rolling std
      Market Structure: recent high/low, higher-high/low, lower-high/low,
                        breakout, false breakout
      Price Action    : rejection, engulfing, strong/small body, Doji-like,
                        Hammer-like, consecutive candles

    PART 7 (Context Window): শুধু current candle নয় — 20-50 আগের candle।

This module is a SUPERSET of core/otc_features.py (the 24 PIPELINE-PHASE-2
features stay first, unchanged, so older datasets remain compatible) and
adds the PART 6 structure / price-action block.

LEAK-SAFETY (PART 19) — same API design as core/otc_features.py:
`build_extended_row(window)` receives ONLY closed candles up to and
including the prediction-time candle. There is no index parameter and no
way to touch the future. scripts/test_otc_predict.py proves it with the
same perturbation protocol used for the Phase-2 features.
"""

import math

from core.otc_features import (build_feature_row, FEATURE_NAMES as BASE_NAMES,
                               MIN_WINDOW, atr)  # noqa: F401  (MIN_WINDOW re-export)
from core.otc_predict.strategy_bridge import (
    strategy_votes, STRATEGY_FEATURE_NAMES, MIN_WINDOW_STRATEGY)

__all__ = ["build_extended_row", "EXTENDED_FEATURE_NAMES", "MIN_WINDOW_EXT",
           "build_unified_row", "UNIFIED_FEATURE_NAMES", "MIN_WINDOW_UNIFIED"]

# EMA20 needs a slightly longer warmup than the base MIN_WINDOW=20.
MIN_WINDOW_EXT = 24

# UNIFIED-SIGNAL (2026-09-13): classic-strategy features need the blender's
# own indicator warmup floor (30) — higher than EXT's 24.
MIN_WINDOW_UNIFIED = max(MIN_WINDOW_EXT, MIN_WINDOW_STRATEGY)

_EXTRA_NAMES = (
    # -- momentum extensions (PART 6) --
    "ret_3",              # close[i]/close[i-3] - 1
    "ema_gap_atr",        # (ema10 - ema20) / ATR14   — trend pressure
    "slope10_atr",        # (close[i] - close[i-10]) / ATR14 — slope in ATRs
    # -- market structure (PART 6) --
    "hh_count",           # higher-highs in the last 10 candles
    "hl_count",           # higher-lows  in the last 10 candles
    "lh_count",           # lower-highs  in the last 10 candles
    "ll_count",           # lower-lows   in the last 10 candles
    "breakout_up",        # close > max(prior 19 highs)
    "breakout_down",      # close < min(prior 19 lows)
    "false_break_up",     # prior candle spiked above prior range, closed back in
    "false_break_down",   # prior candle dove below prior range, closed back in
    # -- price action patterns (PART 6) --
    "is_doji",            # body/range < 0.10
    "is_hammer",          # long lower wick, small upper wick
    "is_star",            # long upper wick, small lower wick (shooting-star-like)
    "engulf_bull",        # bullish engulfing
    "engulf_bear",        # bearish engulfing
    "strong_body",        # body/range >= 0.70
    "small_body",         # body/range <= 0.30
    "rejection_score",    # (lower_wick - upper_wick)/range  in [-1, 1]
    "consec_up",          # consecutive UP candles ending at i
    "consec_down",        # consecutive DOWN candles ending at i
    # -- volatility structure --
    "vol_expansion",      # vol_10 / (vol_20 + eps) — vol speeding up?
    "range_compression",  # mean(range last10) / mean(range last30)
)

EXTENDED_FEATURE_NAMES = tuple(BASE_NAMES) + _EXTRA_NAMES

# UNIFIED-SIGNAL (2026-09-13): extended block + the 13 classic strategy
# modules' net votes + cluster votes + agreement scalars. Bundles trained
# with this superset carry UNIFIED_FEATURE_NAMES in bundle.feature_names,
# so live prediction feeds the models EXACTLY what they were trained on
# (predict_up() keys off bundle.feature_names — old bundles unaffected).
UNIFIED_FEATURE_NAMES = tuple(EXTENDED_FEATURE_NAMES) + tuple(
    STRATEGY_FEATURE_NAMES)


def build_unified_row(window, micro=None, ticks=None):
    """UNIFIED-SIGNAL feature row: extended features + strategy votes.

    Same leak-safety contract as build_extended_row — the strategy bridge
    receives the SAME closed-candle window and nothing else. Needs >=
    MIN_WINDOW_UNIFIED candles. Returns the extended row dict with the
    sv_* / svc_* block merged in.

    `ticks` — optional tick buffer for the tickrun module (live path may
    pass it; training history has none → tickrun abstains honestly).
    """
    feats = dict(build_extended_row(window, micro=micro))
    sv, _summary = strategy_votes(window, ticks=ticks)
    feats.update(sv)
    return feats


def _ema(values, n):
    """Simple EMA over a list; None if not enough data."""
    if len(values) < n:
        return None
    k = 2.0 / (n + 1.0)
    e = sum(values[:n]) / n  # seed = SMA of first n
    for v in values[n:]:
        e = v * k + e * (1.0 - k)
    return e


def build_extended_row(window, micro=None):
    """Compute the extended (PART 6) feature vector from the past window.

    `window` — CLOSED candles oldest→newest, ending with the prediction-time
    candle. Needs >= MIN_WINDOW_EXT candles (50 recommended, PART 7).
    `micro`  — optional microstructure dict of the LAST candle (same
    semantics as build_feature_row).
    """
    if not window or len(window) < MIN_WINDOW_EXT:
        raise ValueError(
            f"build_extended_row: need >= {MIN_WINDOW_EXT} closed candles, "
            f"got {len(window) if window else 0}")

    # Block 1-4: the verified PIPELINE-PHASE-2 features, unchanged.
    feats = dict(build_feature_row(window, micro=micro))

    cur = window[-1]
    o, h, l, c = cur["open"], cur["high"], cur["low"], cur["close"]
    body = abs(c - o)
    rng = max(0.0, h - l)
    a = atr(window, 14)
    eps = 1e-12

    closes = [x["close"] for x in window]

    # ── momentum extensions ────────────────────────────────────────────
    feats["ret_3"] = (c / closes[-4] - 1.0) if len(closes) >= 4 and closes[-4] else 0.0
    e10, e20 = _ema(closes, 10), _ema(closes, 20)
    if e10 is not None and e20 is not None and a > 0:
        feats["ema_gap_atr"] = (e10 - e20) / a
    else:
        feats["ema_gap_atr"] = 0.0
    feats["slope10_atr"] = ((c - closes[-11]) / a) if len(closes) >= 11 and a > 0 else 0.0

    # ── market structure: HH/HL/LH/LL over the last 10 candles ─────────
    hh = hl = lh = ll = 0
    w = window[-11:]
    for j in range(1, len(w)):
        if w[j]["high"] > w[j - 1]["high"]:
            hh += 1
        else:
            lh += 1
        if w[j]["low"] > w[j - 1]["low"]:
            hl += 1
        else:
            ll += 1
    feats["hh_count"], feats["hl_count"] = float(hh), float(hl)
    feats["lh_count"], feats["ll_count"] = float(lh), float(ll)

    # breakout vs the PRIOR 19 candles (current candle excluded — it is the
    # candidate breaker; all inputs are closed candles ≤ i)
    prior = window[-20:-1]
    ph = max(x["high"] for x in prior)
    pl = min(x["low"] for x in prior)
    feats["breakout_up"] = 1.0 if c > ph else 0.0
    feats["breakout_down"] = 1.0 if c < pl else 0.0

    # false breakout: the PREVIOUS candle broke the range of the 19 before
    # it but closed back inside (a failed push)
    if len(window) >= 21:
        prev = window[-2]
        prior2 = window[-21:-2]
        ph2 = max(x["high"] for x in prior2)
        pl2 = min(x["low"] for x in prior2)
        feats["false_break_up"] = (
            1.0 if prev["high"] > ph2 and prev["close"] < ph2 else 0.0)
        feats["false_break_down"] = (
            1.0 if prev["low"] < pl2 and prev["close"] > pl2 else 0.0)
    else:
        feats["false_break_up"] = feats["false_break_down"] = 0.0

    # ── price action patterns (current candle i, all closed) ───────────
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - l)
    body_ratio = (body / rng) if rng > 0 else 0.0

    feats["is_doji"] = 1.0 if (rng > 0 and body_ratio < 0.10) else 0.0
    feats["is_hammer"] = 1.0 if (rng > 0 and lower >= 2.0 * body
                                 and upper <= body) else 0.0
    feats["is_star"] = 1.0 if (rng > 0 and upper >= 2.0 * body
                               and lower <= body) else 0.0

    prev = window[-2]
    po, pc_ = prev["open"], prev["close"]
    feats["engulf_bull"] = 1.0 if (c > o and pc_ < po
                                   and o <= pc_ and c >= po) else 0.0
    feats["engulf_bear"] = 1.0 if (c < o and pc_ > po
                                   and o >= pc_ and c <= po) else 0.0

    feats["strong_body"] = 1.0 if body_ratio >= 0.70 else 0.0
    feats["small_body"] = 1.0 if (rng > 0 and body_ratio <= 0.30) else 0.0
    feats["rejection_score"] = ((lower - upper) / rng) if rng > 0 else 0.0

    streak = feats.get("streak", 0.0)
    feats["consec_up"] = max(0.0, streak)
    feats["consec_down"] = max(0.0, -streak)

    # ── volatility structure ───────────────────────────────────────────
    feats["vol_expansion"] = feats["vol_10"] / (feats["vol_20"] + eps)
    r10 = [x["high"] - x["low"] for x in window[-10:]]
    r30 = [x["high"] - x["low"] for x in window[-30:]]
    m30 = sum(r30) / len(r30) if r30 else 0.0
    feats["range_compression"] = ((sum(r10) / len(r10)) / m30) if m30 > eps else 1.0

    return feats


def verify_extended_lock(candles, n_checks=25, window=50, seed=11):
    """PART 19 perturbation proof for the EXTENDED features.

    Mutating every candle strictly after i must not change any feature;
    mutating candle i must change features (non-vacuity).
    Returns (n_checks, future_hits, self_hits).
    """
    import random
    rng = random.Random(seed)
    n = len(candles)
    if n < max(window, MIN_WINDOW_EXT) + 3:
        raise ValueError("verify_extended_lock: not enough candles")

    future_hits = self_hits = 0
    for _ in range(n_checks):
        i = rng.randrange(MIN_WINDOW_EXT, n - 2)
        base = build_extended_row(candles[i - MIN_WINDOW_EXT + 1: i + 1])

        mutated = [dict(x) for x in candles]
        for j in range(i + 1, n):
            mutated[j]["open"] = mutated[j]["open"] * 1.9 + 0.31
            mutated[j]["close"] = mutated[j]["close"] * 0.5 + 7.77
            mutated[j]["high"] = mutated[j]["high"] * 1.4 + 3.3
            mutated[j]["low"] = mutated[j]["low"] * 0.7 + 1.1
        after = build_extended_row(mutated[i - MIN_WINDOW_EXT + 1: i + 1])
        if repr(base) != repr(after):
            future_hits += 1

        mutated2 = [dict(x) for x in candles]
        mutated2[i]["close"] = mutated2[i]["close"] * 1.07 + 0.002
        after2 = build_extended_row(mutated2[i - MIN_WINDOW_EXT + 1: i + 1])
        if repr(base) != repr(after2):
            self_hits += 1

    return n_checks, future_hits, self_hits
