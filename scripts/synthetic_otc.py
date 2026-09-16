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


# ── TICK-EYE (2026-09-16): tick-level generators for the human-eye module ────

def gen_candles_with_ticks(asset, n, seed=1, start_price=1.10000,
                           edge="none", phi=0.10, ticks_per_candle=90,
                           start_ts=None, vol=0.000012, period=60,
                           flip_prob=0.30, flip_ticks=8, flip_step=3.0):
    """Generate 1-min candles WITH their intra-candle tick sequences.

    Modes:
      none             — fair random-walk ticks; the eye MUST show ~50%.
      flip_persistence — with probability flip_prob, the candle's final
                         flip_ticks ticks push hard in direction D (a REAL
                         late flip: multi-tick, no single giant step). When
                         a flip fires, the NEXT candle's direction is D with
                         probability 0.5+phi (closing-momentum continuation).
      spike_noise      — with probability flip_prob, the flip is a 1-2 tick
                         giant print spike (max_step dominates travel). The
                         NEXT candle mean-reverts with 0.5+phi.

    Returns list of (candle_dict, ticks_list) — ticks are prices only,
    oldest→newest, exactly what feed.py's base_ticks looks like at EOC.
    """
    rng = random.Random(f"{asset}:tickseye:{seed}")
    px = start_price
    ts0 = start_ts if start_ts is not None else 1_700_000_000
    ts0 -= ts0 % period
    candles = []
    t = ts0

    next_dir_bias = 0  # -1 / 0 / +1 — continuation force on the NEXT candle
    for i in range(n):
        o = px
        ticks = [o]
        hi, lo = o, o

        # Candle-level direction: bias carried from the previous candle's
        # late flip (continuation for real pushes, reversion for spikes).
        if edge in ("flip_persistence", "spike_noise") and next_dir_bias != 0:
            up_prob = 0.5 + phi * next_dir_bias
        else:
            up_prob = 0.5
        candle_dir = 1 if rng.random() < up_prob else -1

        # Should this candle END with a late push?
        fire_late = (edge in ("flip_persistence", "spike_noise")
                     and rng.random() < flip_prob)
        if fire_late:
            if edge == "spike_noise":
                # The user's exact scenario: body drifts one way (RED at
                # 57-58s), the giant print spikes the OTHER way (GREEN in
                # the last 2s) — a genuine color flip made of noise.
                push_dir = -candle_dir
            else:
                # Real controlled push in the candle's own direction —
                # strong closing momentum (continuation case).
                push_dir = candle_dir
        else:
            push_dir = 0

        # ── Body ticks (all but the final segment) ──────────────────────────
        # The body ALWAYS carries the candle's directional drift — this is
        # the "candle was RED at 57-58s" part the user described. The late
        # segment then either CONFIRMS (real push) or FAKES (spike) it.
        body_ticks = max(1, ticks_per_candle - flip_ticks)
        for _ in range(body_ticks):
            drift = candle_dir * vol * 0.10
            if edge in ("flip_persistence", "spike_noise") and next_dir_bias != 0:
                drift += next_dir_bias * vol * 0.12
            px += drift + rng.gauss(0, vol)
            ticks.append(round(px, 6))
            hi, lo = max(hi, px), min(lo, px)

        # ── Late segment: the human-eye zone ────────────────────────────────
        for k in range(flip_ticks):
            if fire_late and k >= flip_ticks // 3:
                if edge == "spike_noise":
                    # ONE giant print spike, then normal jitter — real feeds
                    # never freeze flat after a spike.
                    if k == flip_ticks // 3:
                        px += push_dir * vol * flip_step * 6
                    else:
                        px += rng.gauss(0, vol * 0.5)
                else:
                    # REAL controlled push: many small same-direction steps.
                    px += push_dir * vol * 0.55 + rng.gauss(0, vol * 0.35)
            else:
                px += rng.gauss(0, vol)
            ticks.append(round(px, 6))
            hi, lo = max(hi, px), min(lo, px)

        c = px
        if c == o:
            c = o + (vol if rng.random() < 0.5 else -vol)
            ticks[-1] = round(c, 6)

        candles.append((
            {"time": t, "open": o, "high": hi, "low": lo, "close": c,
             "buy_pct": 50.0 + rng.gauss(0, 12), "sell_pct": None,
             "tick_count": len(ticks), "is_fight": 0},
            ticks,
        ))
        if candles[-1][0]["buy_pct"] is not None:
            candles[-1][0]["sell_pct"] = 100.0 - candles[-1][0]["buy_pct"]

        # Carry-over: what does the eye see at this candle's close, and does
        # it bias the NEXT candle?
        next_dir_bias = 0
        if fire_late:
            final_dir = 1 if c > o else (-1 if c < o else 0)
            if edge == "spike_noise":
                # spike flips mean-revert: NEXT candle opposes the spike
                next_dir_bias = -final_dir if final_dir else 0
            else:
                next_dir_bias = final_dir if final_dir else 0

        t += period
    return candles
