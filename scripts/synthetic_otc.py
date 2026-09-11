"""scripts/synthetic_otc.py — synthetic OTC candle generator (shared).

Generates 1-minute OHLC candles from a tick-level random walk so the
prediction engine can be exercised END-TO-END without real data.

EDGES (injected, verifiable):
  persistence  — P(next candle UP) = 0.5 + φ * dir(current candle)
                 (matches the ONLY walk-forward-verified edge found in the
                 production ledger — AUDIT_2026-09-11: OTC feeds mean-revert,
                 real feeds persist)
  none         — fair random walk; an honest pipeline MUST show ≈50% here
                 and emit (almost) nothing above the GOOD tier. If it shows
                 an edge on a fair walk, the pipeline leaks.

Tick construction: each candle gets `ticks_per_candle` sub-ticks from a
GBM-like walk; open/high/low/close follow the same definitions the real
candle builder uses (feed.py), so features see realistic anatomy.
"""

import math
import random


def gen_candles(asset, n, seed=1, start_price=1.10000, edge="none",
                phi=0.12, ticks_per_candle=8, start_ts=None, vol=0.00012,
                period=60):
    """Return list of candle dicts (oldest→newest, contiguous `period` grid)."""
    rng = random.Random(f"{asset}:{seed}")
    px = start_price
    ts0 = start_ts if start_ts is not None else 1_700_000_000
    # align to a clean minute grid
    ts0 -= ts0 % period
    prev_dir = rng.choice((-1, 1))
    candles = []
    t = ts0
    for i in range(n):
        if edge == "persistence":
            up_prob = 0.5 + phi * prev_dir
        else:
            up_prob = 0.5
        # drift for this candle's sub-ticks
        drift = (1 if rng.random() < up_prob else -1) * vol * 0.55
        o = px
        hi, lo = o, o
        for _ in range(ticks_per_candle):
            px += drift + rng.gauss(0, vol)
            hi = max(hi, px)
            lo = min(lo, px)
        c = px
        # nudge exact-doji closes away (dataset drops dojis; the real feed
        # virtually never produces them on FX pairs)
        if c == o:
            c = o + (vol if rng.random() < 0.5 else -vol)
        candles.append({"time": t, "open": o, "high": hi, "low": lo,
                        "close": c,
                        "buy_pct": 50.0 + rng.gauss(0, 12),
                        "sell_pct": None, "tick_count": ticks_per_candle,
                        "is_fight": 0})
        if candles[-1]["buy_pct"] is not None:
            candles[-1]["sell_pct"] = 100.0 - candles[-1]["buy_pct"]
        prev_dir = 1 if c > o else -1
        t += period
    return candles


def gen_multi(pairs, n_per_pair, seed=1, edge="persistence", **kw):
    """{asset: candles} with distinct per-pair start offsets (no ctime
    collisions across pairs — mirrors backtest_deep.py's learned fix)."""
    out = {}
    for k, asset in enumerate(pairs):
        out[asset] = gen_candles(asset, n_per_pair, seed=seed + k,
                                 edge=edge, start_ts=1_700_000_000 + k * 86_400,
                                 **kw)
    return out
