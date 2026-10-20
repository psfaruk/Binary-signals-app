"""Point-of-interest (POI) zone detectors — ICT/SMC research features (Phase L2).

RESEARCH DISCLAIMER (read first):
    Everything in this module is a deterministic, hindsight-free DESCRIPTION
    of historical candle geometry. An order block, fair value gap, supply or
    demand base, equal highs/lows pair, or key level is a HYPOTHESIS REGION:
    a price area where ICT/SMC trading schools EXPECT the market to react.
    This module makes no claim that any zone, level, or freshness state
    predicts future price action, and it claims no edge of any kind. Zones
    produced here are research overlays only; any evaluation of predictive
    value happens exclusively through the pre-committed Phase-L protocol
    (docs/smc/PHASE_L_MASTER_PLAN.md), never inside these functions.

Contract (core/ict/types.py, frozen at CP-L0):
    * Pure functions: no I/O, no global state, fully deterministic.
    * A Detection with confirm_index == c depends ONLY on candles[:c+1]
      (verified mechanically by core/ict/helpers.assert_no_lookahead).
    * Inputs are validated (ContractError on bad candles/parameters) and
      every output list is re-checked with validate_detections before it
      is returned.
    * Zone bounds always satisfy level_low <= level_high.

Interpretation decisions (documented so results stay reproducible):
    * "Total base range" in detect_supply_demand = the SUM of the base
      candles' individual ranges (path length), compared against the net
      open-to-close move across the base.
    * Equal-level and key-level "window" distances are measured in candle
      indices (p2 - p1), not timestamps.
    * Key-level clusters are built greedily left-to-right and close as soon
      as they reach min_touches (later swings start a fresh cluster).
    * In detect_supply_demand the base length used is the LONGEST k that
      satisfies the band rule AND the consolidation rule AND the impulse
      break rule for that same k (a shorter base is used only when every
      longer candidate fails one of those rules).
"""
from __future__ import annotations

import math
from typing import Sequence

from .types import (
    Candle,
    ContractError,
    Detection,
    validate_candles,
    validate_detections,
)

__all__ = [
    "detect_fvg",
    "detect_order_blocks",
    "detect_supply_demand",
    "detect_equal_levels",
    "detect_key_levels",
    "classify_zone_status",
]


# ── parameter validation helpers ────────────────────────────────────────


def _check_real(value, name, minimum=None, strictly_positive=False):
    """Validate a numeric parameter; return it as float (ContractError otherwise)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} must be a real number, got {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise ContractError(f"{name} must be finite, got {value!r}")
    if strictly_positive and v <= 0.0:
        raise ContractError(f"{name} must be > 0, got {value!r}")
    if minimum is not None and v < minimum:
        raise ContractError(f"{name} must be >= {minimum}, got {value!r}")
    return v


def _check_int(value, name, minimum):
    """Validate an integer parameter (bool rejected); return it unchanged."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} must be an int, got {value!r}")
    if value < minimum:
        raise ContractError(f"{name} must be >= {minimum}, got {value!r}")
    return value


def _median(values):
    """Median of a non-empty sequence of numbers (mean of the middle two when even)."""
    if not values:
        raise ContractError("median of an empty sequence")
    ordered = sorted(values)
    n = len(ordered)
    if n % 2 == 1:
        return float(ordered[n // 2])
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def _swing_highs(candles):
    """3-point fractal swing highs: (index, level) for every i with
    high[i] strictly greater than BOTH immediate neighbors (1 <= i <= n-2).
    A swing at i is only knowable after candle i+1 closes."""
    out = []
    for i in range(1, len(candles) - 1):
        if candles[i].high > candles[i - 1].high and candles[i].high > candles[i + 1].high:
            out.append((i, float(candles[i].high)))
    return out


def _swing_lows(candles):
    """3-point fractal swing lows: (index, level) for every i with
    low[i] strictly less than BOTH immediate neighbors (1 <= i <= n-2)."""
    out = []
    for i in range(1, len(candles) - 1):
        if candles[i].low < candles[i - 1].low and candles[i].low < candles[i + 1].low:
            out.append((i, float(candles[i].low)))
    return out


# ── 1. Fair Value Gaps ──────────────────────────────────────────────────


def detect_fvg(candles: Sequence[Candle], min_gap: float = 0.0) -> list[Detection]:
    """Fair Value Gaps (3-candle imbalance) — descriptive research feature.

    For every index i >= 2 the triple (candles[i-2], candles[i-1], candles[i])
    is inspected (the middle candle plays no role in the gap test):

    * Bullish FVG: candles[i-2].high < candles[i].low. Gap size =
      candles[i].low - candles[i-2].high; zone = [candles[i-2].high,
      candles[i].low] as (level_low, level_high).
    * Bearish FVG: candles[i-2].low > candles[i].high. Gap size =
      candles[i-2].low - candles[i].high; zone = [candles[i].high,
      candles[i-2].low].

    A gap is emitted only when its size is STRICTLY greater than ``min_gap``;
    with the default 0.0 this means truly non-touching bounds (equal/touching
    or overlapping bounds are not a gap). Emitted fields: kind
    "fvg_bullish"/"fvg_bearish", side "bullish"/"bearish", start_index = i-2,
    confirm_index = i (the gap is knowable only once the third candle has
    closed), ts = candles[i].ts, meta = {"gap": float}.

    Parameters:
        candles: closed candles in strictly increasing ts order (validated
            by validate_candles; ContractError otherwise).
        min_gap: minimum gap size in price units; the gap must be STRICTLY
            greater than this value. Must be >= 0 and finite.
            Default 0.0 (any true gap qualifies).

    Returns:
        list[Detection] in ascending confirm_index order. Pure, deterministic,
        no I/O. A detected gap is a hypothesis region, not a prediction.
    """
    validate_candles(candles)
    min_gap = _check_real(min_gap, "min_gap", minimum=0.0)
    out: list[Detection] = []
    for i in range(2, len(candles)):
        left = candles[i - 2]
        right = candles[i]
        if left.high < right.low:
            gap = right.low - left.high
            if gap > min_gap:
                out.append(
                    Detection(
                        kind="fvg_bullish",
                        start_index=i - 2,
                        confirm_index=i,
                        ts=right.ts,
                        side="bullish",
                        level_low=float(left.high),
                        level_high=float(right.low),
                        meta={"gap": float(gap)},
                    )
                )
        elif left.low > right.high:
            gap = left.low - right.high
            if gap > min_gap:
                out.append(
                    Detection(
                        kind="fvg_bearish",
                        start_index=i - 2,
                        confirm_index=i,
                        ts=right.ts,
                        side="bearish",
                        level_low=float(right.high),
                        level_high=float(left.low),
                        meta={"gap": float(gap)},
                    )
                )
    validate_detections(out, candles)
    return out


# ── 2. Order Blocks ─────────────────────────────────────────────────────


def _last_colored(candles, i, direction):
    """Index of the LAST candle before i with the given close/open color.

    direction "bullish" -> close > open; "bearish" -> close < open.
    Returns None when no such candle exists before i. Pure backward scan;
    never reads candles at or after index i.
    """
    for j in range(i - 1, -1, -1):
        c = candles[j]
        if direction == "bullish" and c.close > c.open:
            return j
        if direction == "bearish" and c.close < c.open:
            return j
    return None


def detect_order_blocks(
    candles: Sequence[Candle], impulse_mult: float = 1.5, lookback: int = 10
) -> list[Detection]:
    """Order blocks: last opposite-color candle before a structure-breaking impulse.

    A candle i is a BEARISH impulse when all of the following hold:
      * body[i] > 0, and
      * body[i] >= impulse_mult * median(body of candles[max(0, i-lookback):i])
        (the median uses only candles STRICTLY before i — no self-inclusion),
        and
      * close[i] < min(low of candles[max(0, i-lookback):i]).

    The bearish order block is then the LAST BULLISH candle (close > open) at
    index j < i, found by scanning back from i-1 (not bounded by lookback);
    its FULL range [low_j, high_j] is the zone. kind "order_block_bearish",
    side "bearish", start_index = j, confirm_index = i, ts = candles[i].ts,
    meta = {"body_zone": [open_j, close_j] sorted ascending, "impulse_index": i,
    "impulse_body": float}. If no opposite-color candle exists before the
    impulse, the impulse is skipped (no detection).

    BULLISH impulses are the mirror image: close[i] > max(high of the
    lookback window) and the order block is the LAST BEARISH candle before i
    (kind "order_block_bullish", side "bullish"). A candle can never qualify
    as both impulse directions at once (the two close conditions are mutually
    exclusive), so each impulse emits at most one detection.

    Parameters:
        candles: closed candles in strictly increasing ts order (validated).
        impulse_mult: body multiple of the lookback median that an impulse
            must reach (inclusive comparison). Must be > 0 and finite.
            Default 1.5.
        lookback: number of candles BEFORE i (strictly) used for the body
            median and the break-extreme comparison. Must be >= 1.
            Default 10.

    Returns:
        list[Detection] in ascending confirm_index order. Pure, deterministic,
        no I/O. An order block is a hypothesis region, not a prediction.
    """
    validate_candles(candles)
    impulse_mult = _check_real(impulse_mult, "impulse_mult", strictly_positive=True)
    lookback = _check_int(lookback, "lookback", minimum=1)
    out: list[Detection] = []
    for i in range(1, len(candles)):
        impulse = candles[i]
        body = impulse.body
        if body <= 0.0:
            continue
        window = candles[max(0, i - lookback):i]
        med = _median([c.body for c in window])
        if body < impulse_mult * med:
            continue
        win_low = min(c.low for c in window)
        win_high = max(c.high for c in window)
        if impulse.close < win_low:
            j = _last_colored(candles, i, "bullish")
            if j is None:
                continue
            ob = candles[j]
            out.append(
                Detection(
                    kind="order_block_bearish",
                    start_index=j,
                    confirm_index=i,
                    ts=impulse.ts,
                    side="bearish",
                    level_low=float(ob.low),
                    level_high=float(ob.high),
                    meta={
                        "body_zone": sorted([float(ob.open), float(ob.close)]),
                        "impulse_index": i,
                        "impulse_body": float(body),
                    },
                )
            )
        elif impulse.close > win_high:
            j = _last_colored(candles, i, "bearish")
            if j is None:
                continue
            ob = candles[j]
            out.append(
                Detection(
                    kind="order_block_bullish",
                    start_index=j,
                    confirm_index=i,
                    ts=impulse.ts,
                    side="bullish",
                    level_low=float(ob.low),
                    level_high=float(ob.high),
                    meta={
                        "body_zone": sorted([float(ob.open), float(ob.close)]),
                        "impulse_index": i,
                        "impulse_body": float(body),
                    },
                )
            )
    validate_detections(out, candles)
    return out


# ── 3. Supply / Demand bases ────────────────────────────────────────────


def detect_supply_demand(
    candles: Sequence[Candle],
    base_min: int = 3,
    impulse_mult: float = 2.0,
    lookback: int = 20,
) -> list[Detection]:
    """Supply and demand zones: consolidation base broken by an impulse candle.

    DEMAND zone (bullish): a base of >= ``base_min`` consecutive candles
    ending immediately before an up-impulse candle i, where the base must
    satisfy both rules:
      (a) common band: max(low of base candles) <= min(high of base candles)
          — every base candle's range shares one common price interval;
      (b) consolidation: |close of last base candle - open of first base
          candle| <= 0.5 * (SUM of the base candles' individual ranges)
          ["total base range" = path length — documented decision].
    The impulse candle i (immediately after the base) must satisfy:
      * body[i] >= impulse_mult * median(body of the previous ``lookback``
        candles strictly before i), and
      * close[i] > max(high of the base).
    Zone = [min(low of base), max(high of base)], kind "demand_zone",
    side "bullish", start_index = first base candle, confirm_index = i,
    ts = candles[i].ts, meta = {"base_len": int, "impulse_index": int,
    "impulse_body": float}.

    SUPPLY zone is the mirror image: close[i] < min(low of the base), kind
    "supply_zone", side "bearish".

    Base-length selection: candidate bases are extended leftwards from i-1
    while rule (a) holds (band validity is prefix-monotone — once an earlier
    candle breaks the shared band, every longer base is also invalid). Among
    lengths k >= base_min that satisfy (a) AND (b) AND the impulse break for
    that same k, the LONGEST k is used. When no length qualifies, no
    detection is emitted for that impulse.

    Parameters:
        candles: closed candles in strictly increasing ts order (validated).
        base_min: minimum number of consecutive base candles. Must be >= 1.
            Default 3.
        impulse_mult: body multiple of the median body of the previous
            ``lookback`` candles required of the impulse. Must be > 0 and
            finite. Default 2.0.
        lookback: number of candles strictly before i used for the median
            body. Must be >= 1. Default 20.

    Returns:
        list[Detection] in ascending confirm_index order (demand before
        supply when both fire at the same impulse). Pure, deterministic,
        no I/O. A supply/demand zone is a hypothesis region, not a prediction.
    """
    validate_candles(candles)
    base_min = _check_int(base_min, "base_min", minimum=1)
    impulse_mult = _check_real(impulse_mult, "impulse_mult", strictly_positive=True)
    lookback = _check_int(lookback, "lookback", minimum=1)
    n = len(candles)
    out: list[Detection] = []
    for i in range(base_min, n):
        impulse = candles[i]
        window = candles[max(0, i - lookback):i]
        med = _median([c.body for c in window])
        if impulse.body < impulse_mult * med:
            continue
        last_close = candles[i - 1].close
        demand_best = None  # (k, zone_low, zone_high) for the longest k
        supply_best = None
        run_min_low = run_max_low = run_min_high = run_max_high = 0.0
        range_sum = 0.0
        for k in range(1, i + 1):
            b = candles[i - k]
            if k == 1:
                run_min_low = run_max_low = b.low
                run_min_high = run_max_high = b.high
            else:
                if b.low < run_min_low:
                    run_min_low = b.low
                if b.low > run_max_low:
                    run_max_low = b.low
                if b.high < run_min_high:
                    run_min_high = b.high
                if b.high > run_max_high:
                    run_max_high = b.high
            range_sum += b.high - b.low
            if run_max_low > run_min_high:
                break  # common band gone; every longer base stays invalid
            if k < base_min:
                continue
            net_move = abs(last_close - b.open)
            if net_move > 0.5 * range_sum:
                continue
            if impulse.close > run_max_high:
                demand_best = (k, run_min_low, run_max_high)
            if impulse.close < run_min_low:
                supply_best = (k, run_min_low, run_max_high)
        for best, kind, side in (
            (demand_best, "demand_zone", "bullish"),
            (supply_best, "supply_zone", "bearish"),
        ):
            if best is None:
                continue
            k, zone_low, zone_high = best
            out.append(
                Detection(
                    kind=kind,
                    start_index=i - k,
                    confirm_index=i,
                    ts=impulse.ts,
                    side=side,
                    level_low=float(zone_low),
                    level_high=float(zone_high),
                    meta={
                        "base_len": int(k),
                        "impulse_index": i,
                        "impulse_body": float(impulse.body),
                    },
                )
            )
    validate_detections(out, candles)
    return out


# ── 4. Equal highs / equal lows ─────────────────────────────────────────


def detect_equal_levels(
    candles: Sequence[Candle], tol_rel: float = 0.001, window: int = 60
) -> list[Detection]:
    """Equal highs / equal lows: two swings at (nearly) the same level.

    Swing highs are 3-point fractals (high[i] strictly greater than BOTH
    immediate neighbors, 1 <= i <= n-2); a swing at i is confirmed at i+1.
    Swing lows are the mirror image. A PAIR of same-direction swings
    (p1 < p2) is emitted when:
      * p2 - p1 <= ``window`` (candle-index distance between the two
        fractal positions), and
      * |level1 - level2| <= ``tol_rel`` * mean(level1, level2).

    Equal highs emit kind "eq_highs", side "bearish" (resistance label: in
    SMC vocabulary equal highs are liquidity resting ABOVE price, but this
    module labels the level by its resistance role). Equal lows emit kind
    "eq_lows", side "bullish". For both: level_low = level_high = mean of
    the two levels, start_index = p1 (first swing), confirm_index = p2 + 1
    (the second swing's confirm index), ts = candles[p2 + 1].ts, meta =
    {"levels": [level1, level2], "swing_indices": [p1, p2]}.

    Every qualifying pair is reported independently (three clustered swings
    produce up to three pair detections).

    Parameters:
        candles: closed candles in strictly increasing ts order (validated).
        tol_rel: relative level tolerance — the level difference must be at
            most tol_rel * mean level (inclusive). Must be >= 0 and finite
            (0 means exact equality). Default 0.001 (0.1%).
        window: maximum candle-index distance between the two swings.
            Must be >= 1. Default 60.

    Returns:
        list[Detection]: equal-highs pairs first (ordered by second swing,
        then first swing), then equal-lows pairs in the same order. Pure,
        deterministic, no I/O. An equal level is a hypothesis region (a
        possible liquidity pool location), not a prediction.
    """
    validate_candles(candles)
    tol_rel = _check_real(tol_rel, "tol_rel", minimum=0.0)
    window = _check_int(window, "window", minimum=1)
    out: list[Detection] = []
    for kind, side, swings in (
        ("eq_highs", "bearish", _swing_highs(candles)),
        ("eq_lows", "bullish", _swing_lows(candles)),
    ):
        for b in range(len(swings)):
            p2, level2 = swings[b]
            for a in range(b):
                p1, level1 = swings[a]
                if p2 - p1 > window:
                    continue
                mean = (level1 + level2) / 2.0
                if abs(level1 - level2) <= tol_rel * mean:
                    out.append(
                        Detection(
                            kind=kind,
                            start_index=p1,
                            confirm_index=p2 + 1,
                            ts=candles[p2 + 1].ts,
                            side=side,
                            level_low=float(mean),
                            level_high=float(mean),
                            meta={
                                "levels": [float(level1), float(level2)],
                                "swing_indices": [p1, p2],
                            },
                        )
                    )
    validate_detections(out, candles)
    return out


# ── 5. Key levels (swing clusters) ──────────────────────────────────────


def _cluster_detection(kind, side, members, candles):
    """Build a resistance/support Detection from a completed swing cluster."""
    positions = [p for p, _ in members]
    levels = [l for _, l in members]
    level = sum(levels) / len(levels)
    last_pos = positions[-1]
    return Detection(
        kind=kind,
        start_index=positions[0],
        confirm_index=last_pos + 1,
        ts=candles[last_pos + 1].ts,
        side=side,
        level_low=float(level),
        level_high=float(level),
        meta={
            "touches": len(members),
            "level": float(level),
            "swing_indices": positions,
        },
    )


def detect_key_levels(
    candles: Sequence[Candle],
    window: int = 100,
    min_touches: int = 3,
    tol_rel: float = 0.002,
) -> list[Detection]:
    """Key levels: clusters of >= min_touches swings at (nearly) the same level.

    Swings are the same 3-point k=1 fractals used by detect_equal_levels
    (strictly greater/less than BOTH immediate neighbors, confirmed at
    i+1). Clusters are built greedily in chronological order, separately
    for swing highs (resistance) and swing lows (support):

    * A swing (pos, level) joins the open cluster iff
      pos - (first member's pos) <= ``window`` (the whole cluster spans at
      most ``window`` candles) AND |level - mean(current member levels)|
      <= ``tol_rel`` * mean(current member levels).
    * When a joining swing brings the cluster to ``min_touches`` members,
      the level is emitted (confirm_index = the completing swing's confirm
      index, i.e. its position + 1) and the cluster CLOSES — later swings
      start a fresh cluster (an already-emitted level is never extended).
    * A swing that cannot join closes the open cluster without emission
      (if still below min_touches) and starts a new one. No backtracking.

    Emitted fields: kind "resistance" (side "bearish", from swing highs) or
    "support" (side "bullish", from swing lows); level_low = level_high =
    mean of the member levels; start_index = first member's position;
    ts = candles[confirm_index].ts; meta = {"touches": int, "level": float,
    "swing_indices": [positions...]}.

    Parameters:
        candles: closed candles in strictly increasing ts order (validated).
        window: maximum candle-index span of a cluster, measured from its
            FIRST swing to the joining swing. Must be >= 1. Default 100.
        min_touches: cluster size required to emit a level. Must be >= 2
            (a single swing is not a cluster). Default 3.
        tol_rel: maximum relative deviation of a joining level from the
            current cluster mean. Must be >= 0 and finite. Default 0.002.

    Returns:
        list[Detection]: resistance levels first (chronological), then
        support levels (chronological). Pure, deterministic, no I/O. A key
        level is a hypothesis region, not a prediction.
    """
    validate_candles(candles)
    window = _check_int(window, "window", minimum=1)
    min_touches = _check_int(min_touches, "min_touches", minimum=2)
    tol_rel = _check_real(tol_rel, "tol_rel", minimum=0.0)
    out: list[Detection] = []
    for kind, side, swings in (
        ("resistance", "bearish", _swing_highs(candles)),
        ("support", "bullish", _swing_lows(candles)),
    ):
        current: list = []
        for pos, level in swings:
            joined = False
            if current:
                first_pos = current[0][0]
                mean = sum(l for _, l in current) / len(current)
                if (pos - first_pos) <= window and abs(level - mean) <= tol_rel * mean:
                    current.append((pos, level))
                    joined = True
                    if len(current) >= min_touches:
                        out.append(_cluster_detection(kind, side, current, candles))
                        current = []
            if not joined:
                current = [(pos, level)]
    validate_detections(out, candles)
    return out


# ── 6. Zone freshness classification ────────────────────────────────────


def classify_zone_status(
    zone: Detection, candles: Sequence[Candle], upto_index: int
) -> str:
    """Freshness of a zone Detection after its confirmation: untested/touched/broken.

    Scans candles STRICTLY AFTER zone.confirm_index and up to ``upto_index``
    INCLUSIVE — it NEVER reads candles beyond upto_index (and never reads any
    candle at all when upto_index <= zone.confirm_index):

    * "touched": some scanned candle's [low, high] overlaps the zone
      [level_low, level_high] (candle.low <= level_high and
      candle.high >= level_low).
    * "broken": some scanned candle CLOSES beyond the zone's far side —
      for side "bullish": close < level_low; for side "bearish":
      close > level_high; for side "neutral": either side.
    * Priority: "broken" > "touched" > "untested" (a zone that was gapped
      through without any overlap still reports "broken").

    Pure function, returns only a status string (no Detection is emitted).
    Only zone.confirm_index, zone.side, zone.level_low and zone.level_high
    are consulted (zone.ts and meta are irrelevant to the status).

    Parameters:
        zone: a Detection with non-None level bounds satisfying
            level_low <= level_high, a valid side, and a confirm_index in
            [0, len(candles) - 1]. ContractError otherwise.
        candles: the candle series the zone refers to (validated;
            strictly increasing ts order).
        upto_index: last candle index the classifier may read, inclusive.
            Must be an int in [-1, len(candles) - 1] (-1 scans nothing).
            ContractError otherwise. Values below zone.confirm_index simply
            yield "untested" (nothing has happened after confirmation yet).

    Returns:
        "untested" | "touched" | "broken". The status is a description of
        past price interaction with a hypothesis region — it carries no
        implication about future behavior.
    """
    validate_candles(candles)
    if not isinstance(zone, Detection):
        raise ContractError(f"zone must be a Detection, got {type(zone).__name__}")
    if zone.side not in ("bullish", "bearish", "neutral"):
        raise ContractError(f"zone.side invalid: {zone.side!r}")
    if zone.level_low is None or zone.level_high is None:
        raise ContractError("classify_zone_status requires zone level_low/level_high")
    if zone.level_low > zone.level_high:
        raise ContractError(
            f"zone bounds inverted: low {zone.level_low} > high {zone.level_high}"
        )
    if isinstance(upto_index, bool) or not isinstance(upto_index, int):
        raise ContractError(f"upto_index must be an int, got {upto_index!r}")
    n = len(candles)
    if not (-1 <= upto_index < n):
        raise ContractError(f"upto_index {upto_index} out of range [-1, {n - 1}]")
    ci = zone.confirm_index
    if isinstance(ci, bool) or not isinstance(ci, int) or not (0 <= ci < n):
        raise ContractError(f"zone.confirm_index {ci!r} out of range [0, {n - 1}]")
    touched = False
    broken = False
    for j in range(ci + 1, upto_index + 1):
        c = candles[j]
        if c.low <= zone.level_high and c.high >= zone.level_low:
            touched = True
        if zone.side == "bullish":
            if c.close < zone.level_low:
                broken = True
        elif zone.side == "bearish":
            if c.close > zone.level_high:
                broken = True
        else:
            if c.close < zone.level_low or c.close > zone.level_high:
                broken = True
        if broken:
            return "broken"
    if touched:
        return "touched"
    return "untested"
