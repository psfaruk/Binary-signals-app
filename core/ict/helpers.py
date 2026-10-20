"""Shared test helpers for core/ict modules (Phase L).

Perturbation utilities prove the no-lookahead rule mechanically:
mutating candle j must not change any detection whose confirm_index < j.
"""
from __future__ import annotations

import copy
import json
from typing import Callable, Sequence

from .types import Candle, Detection, validate_candles, validate_detections


def perturb(candles: Sequence[Candle], index: int, variant: int = 0) -> Candle:
    """Return candles with candle[index] replaced by a perturbed copy.

    Four decisive variants so that threshold conditions on open/close/body
    direction are strongly stressed (variant chosen deterministically):
      v0 mirror+amplify the close move around the open (direction flip)
      v1 shift open/close decisively UP (gap-up style)
      v2 shift open/close decisively DOWN (gap-down style)
      v3 flatten the candle (open=close=mid, tight wicks)
    The result always satisfies the Candle invariants.
    """
    cs = list(candles)
    c = cs[index]
    rng = ((index * 2654435761 + variant * 40503) & 0xFFFFFFFF) / 0xFFFFFFFF
    body = max(c.body, c.range * 0.25, 1e-8)
    ref = c.range if c.range > 0 else max(abs(c.close), 1.0) * 0.001
    if variant == 0:
        o = c.open
        cl = round(o - (c.close - o) * 3.0 - ref * 0.05, 8)
    elif variant == 1:
        o = round(c.open + ref * 0.8 + body * 0.5, 8)
        cl = round(o + body + ref * 0.4, 8)
    elif variant == 2:
        o = round(c.open - ref * 0.8 - body * 0.5, 8)
        cl = round(o - body - ref * 0.4, 8)
    else:
        mid = round((c.high + c.low) / 2.0, 8)
        o = cl = mid
    hi = round(max(o, cl) + ref * (0.05 + 0.25 * rng), 8)
    lo = round(min(o, cl) - ref * (0.05 + 0.25 * (1.0 - rng)), 8)
    if lo <= 0:
        lo = round(min(o, cl) * 0.5, 8)
        hi = round(max(hi, min(o, cl) + abs(hi - lo) * 0.1 + 1e-8), 8)
        if hi <= min(o, cl):
            hi = round(min(o, cl) + 1e-4 + ref * 0.1, 8)
    cs[index] = Candle(ts=c.ts, open=o, high=hi, low=lo, close=cl, volume=c.volume)
    validate_candles(cs, strict_order=False)  # single-candle swap keeps order
    return cs


PERTURB_VARIANTS = (0, 1, 2, 3)


def assert_no_lookahead(
    detector: Callable[[Sequence[Candle], ...], list[Detection]],
    candles: Sequence[Candle],
    params: dict | None = None,
) -> None:
    """Mechanical no-lookahead proof for one candle series.

    For every j in [0, n) and every perturbation variant: perturb candle j,
    re-run, and require that the set of detections with confirm_index < j is
    IDENTICAL to the original. (Detections confirmed at/after j may change.)
    """
    params = params or {}
    base = detector(candles, **params)
    validate_detections(base, candles)
    n = len(candles)
    for j in range(n):
        base_key = _sig([d for d in base if d.confirm_index < j])
        for variant in PERTURB_VARIANTS:
            mutated = perturb(candles, j, variant)
            after = detector(mutated, **params)
            validate_detections(after, mutated)
            after_key = _sig([d for d in after if d.confirm_index < j])
            if base_key != after_key:
                raise AssertionError(
                    f"LOOKAHEAD (variant {variant}): mutating candle[{j}] changed "
                    f"detections confirmed before j\nbefore={base_key[:3]}\n"
                    f"after={after_key[:3]}"
                )


def _sig(detections: Sequence[Detection]) -> str:
    def clean(d: Detection) -> dict:
        return {
            "kind": d.kind,
            "start_index": d.start_index,
            "confirm_index": d.confirm_index,
            "side": d.side,
            "level_low": d.level_low,
            "level_high": d.level_high,
            "meta": _round_meta(d.meta),
        }

    return json.dumps([clean(d) for d in detections], sort_keys=True)


def _round_meta(meta: dict) -> dict:
    def _r(v):
        if isinstance(v, float):
            return round(v, 6)
        if isinstance(v, dict):
            return {k: _r(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_r(x) for x in v]
        return v

    return {k: _r(v) for k, v in meta.items()}


def assert_deterministic(
    detector: Callable[[Sequence[Candle], ...], list[Detection]],
    candles: Sequence[Candle],
    params: dict | None = None,
    runs: int = 3,
) -> None:
    """Same input -> byte-identical detection signature across runs."""
    params = params or {}
    sigs = {_sig(detector(list(candles), **params)) for _ in range(runs)}
    if len(sigs) != 1:
        raise AssertionError("detector is not deterministic across repeated runs")


def synthetic_series(n: int, seed: int = 42, start: float = 100.0,
                     drift: float = 0.0, vol: float = 0.5) -> list[Candle]:
    """Deterministic pseudo-random walk for fuzz-style property tests.

    Not market data — a synthetic control series only.
    """
    state = seed & 0xFFFFFFFF
    out: list[Candle] = []
    price = start
    for i in range(n):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        u = state / 0x7FFFFFFF  # [0,1)
        step = drift + (u - 0.5) * 2.0 * vol
        o = price
        cl = round(o + step, 8)
        if cl <= 0:
            cl = round(o + abs(step) + 1e-6, 8)
        wick = vol * (0.1 + 0.9 * ((state >> 7) & 0xFFFF) / 0xFFFF)
        hi = round(max(o, cl) + wick * u, 8)
        lo = round(min(o, cl) - wick * (1.0 - u), 8)
        if lo <= 0:
            lo = round(min(o, cl) * 0.5, 8)
        out.append(Candle(ts=1_700_000_000 + i * 60, open=round(o, 8),
                          high=hi, low=lo, close=cl,
                          volume=round(100.0 + 50.0 * u, 4)))
        price = cl
    validate_candles(out)
    return out


__all__ = [
    "perturb",
    "assert_no_lookahead",
    "assert_deterministic",
    "synthetic_series",
]
