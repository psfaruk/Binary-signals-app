"""core/otc_features.py — OTC candle feature engineering (PIPELINE PHASE 2).

USER SPEC (2026-09-11, "OTC Future Candle Prediction System", Phase 2):

    প্রতিটি candle থেকে: body_size, upper_wick, lower_wick, range,
    body/range, direction, previous 3 candle direction, previous 5 candle
    momentum, previous 10 candle momentum, volatility, recent high/low,
    distance from support, distance from resistance.

LEAK-SAFETY BY API DESIGN (PIPELINE PHASE 8 — "Prediction Lock")
===============================================================
`build_feature_row(window)` receives ONLY the candles up to and including
the prediction-time candle. The function has no index parameter and no way
to reach the future — a caller physically cannot leak future candles into
features without bypassing this module. `core/otc_dataset.py` enforces the
slicing discipline and `scripts/test_otc_pipeline.py` proves it with a
perturbation test (mutating future candles must not change any feature).

Every feature is computed from CLOSED 1-minute candles built from the SAME
OTC price feed the binary platform uses (feed.py tick stream -> candle
builder -> candle_micro table). Microstructure fields (buy_pct, tick_count,
...) describe the prediction-time candle's own ticks and are known the
moment that candle closes — safe to use for the NEXT-candle prediction.
"""

import math

__all__ = ["build_feature_row", "FEATURE_NAMES", "MIN_WINDOW", "atr"]

# Shortest window that satisfies every lookback below (20-range, mom_10,
# ATR14, prev-3 dirs, 5/10 momentum) with margin.
MIN_WINDOW = 20

FEATURE_NAMES = (
    # -- current candle anatomy (Phase 2 list, items 1-6) --
    "body_size",            # |close - open|
    "body_size_atr",        # body_size / ATR14 (scale-free)
    "upper_wick",           # high - max(o, c)
    "lower_wick",           # min(o, c) - low
    "range",                # high - low
    "body_range_ratio",     # body / range in [0, 1]
    "close_range_pos",      # (c - low) / range in [0, 1]
    "direction",            # +1 UP / -1 DOWN / 0 doji (candle i itself)
    # -- previous candles (items 7-9) --
    "dir_1", "dir_2", "dir_3",   # previous 3 candle directions (+1/-1/0)
    "mom_5",                     # close[i]/close[i-5] - 1
    "mom_10",                    # close[i]/close[i-10] - 1
    "streak",                    # signed consecutive same-direction count
    # -- volatility (item 10) --
    "atr_norm",                  # ATR14 / close (relative volatility)
    "vol_10",                    # std of last 10 1-candle returns
    "vol_20",                    # std of last 20 1-candle returns
    # -- recent high/low + S/R distances (items 11-13) --
    "hi20_pos",                  # close position inside 20-candle [lo, hi]
    "dist_support_atr",          # (close - min_low20) / ATR14
    "dist_resistance_atr",       # (max_high20 - close) / ATR14
    "ret_1",                     # close[i]/close[i-1] - 1
    # -- same-feed microstructure (optional, candle i's own ticks) --
    "micro_buy_pct",
    "micro_sell_pct",
    "micro_tick_count",
    "micro_is_fight",
)


def atr(candles, n=14):
    """Simple average true range over the last `n` candles of the window."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for j in range(max(1, len(candles) - n), len(candles)):
        h = candles[j]["high"]
        l = candles[j]["low"]
        pc = candles[j - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def _dir(c):
    o, cl = c["open"], c["close"]
    if cl > o:
        return 1
    if cl < o:
        return -1
    return 0


def _std(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def build_feature_row(window, micro=None):
    """Compute the Phase-2 feature vector from the candle window.

    Parameters
    ----------
    window : list[dict]
        CLOSED candles {time, open, high, low, close}, oldest first, ending
        with the prediction-time candle (candle 100 in the spec's example).
        MUST contain at least MIN_WINDOW candles; 50 (the spec's window) is
        recommended.
    micro : dict | None
        Optional same-feed microstructure snapshot of the LAST candle
        (buy_pct / sell_pct / tick_count / is_fight from candle_micro).
        These describe candle i's own ticks — available at prediction time.

    Returns
    -------
    dict keyed by FEATURE_NAMES (all floats).
    """
    if not window or len(window) < MIN_WINDOW:
        raise ValueError(
            f"build_feature_row: need >= {MIN_WINDOW} closed candles, "
            f"got {len(window) if window else 0}")

    cur = window[-1]
    o, h, l, c = cur["open"], cur["high"], cur["low"], cur["close"]

    body = abs(c - o)
    rng = max(0.0, h - l)
    a = atr(window, 14)
    close_eps = abs(c) if c else 1.0

    feats = {}
    feats["body_size"] = body
    feats["body_size_atr"] = (body / a) if a > 0 else 0.0
    feats["upper_wick"] = max(0.0, h - max(o, c))
    feats["lower_wick"] = max(0.0, min(o, c) - l)
    feats["range"] = rng
    feats["body_range_ratio"] = (body / rng) if rng > 0 else 0.0
    feats["close_range_pos"] = ((c - l) / rng) if rng > 0 else 0.5
    feats["direction"] = float(_dir(cur))

    # previous 3 candle directions (most recent first)
    for k in (1, 2, 3):
        idx = -1 - k
        feats[f"dir_{k}"] = float(_dir(window[idx])) if len(window) > k else 0.0

    # momentum over 5 / 10 candles (close-to-close, uses candle i's close —
    # known at prediction time; no future data involved)
    feats["mom_5"] = (c / window[-6]["close"] - 1.0) if len(window) >= 6 else 0.0
    feats["mom_10"] = (c / window[-11]["close"] - 1.0) if len(window) >= 11 else 0.0

    # signed streak of same-direction candles ending at i
    streak = 0
    d0 = _dir(cur)
    if d0 != 0:
        streak = 1
        for j in range(len(window) - 2, -1, -1):
            if _dir(window[j]) == d0:
                streak += 1
            else:
                break
    feats["streak"] = float(d0 * streak if d0 != 0 else 0)

    # volatility
    rets = []
    for j in range(max(1, len(window) - 20), len(window)):
        pc = window[j - 1]["close"]
        if pc:
            rets.append(window[j]["close"] / pc - 1.0)
    feats["atr_norm"] = (a / close_eps) if close_eps else 0.0
    feats["vol_10"] = _std(rets[-10:])
    feats["vol_20"] = _std(rets[-20:])

    # recent 20-candle high/low position + S/R distances (ATR units)
    w20 = window[-20:]
    lo20 = min(x["low"] for x in w20)
    hi20 = max(x["high"] for x in w20)
    span20 = hi20 - lo20
    feats["hi20_pos"] = ((c - lo20) / span20) if span20 > 0 else 0.5
    feats["dist_support_atr"] = ((c - lo20) / a) if a > 0 else 0.0
    feats["dist_resistance_atr"] = ((hi20 - c) / a) if a > 0 else 0.0

    pc1 = window[-2]["close"]
    feats["ret_1"] = (c / pc1 - 1.0) if pc1 else 0.0

    # same-feed microstructure of candle i (None-safe → 0.0)
    m = micro or {}
    feats["micro_buy_pct"] = float(m.get("buy_pct") or 0.0)
    feats["micro_sell_pct"] = float(m.get("sell_pct") or 0.0)
    feats["micro_tick_count"] = float(m.get("tick_count") or 0.0)
    feats["micro_is_fight"] = float(1.0 if m.get("is_fight") else 0.0)

    return feats
