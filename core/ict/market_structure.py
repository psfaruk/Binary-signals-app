"""ICT/SMC market-structure research detectors (Phase L1, Agent A).

Fractal swing structure with k-candle confirmation lag; research feature,
no predictive claim.

Three pure, deterministic detectors over closed candles:

* ``detect_swings``            -- fractal swing highs/lows (strict greater/
                                 less comparisons, ties disqualify,
                                 confirm_index = swing_index + k).
* ``detect_structure_breaks`` -- BOS / CHoCH: first CLOSE-break of the most
                                 recent active (confirmed, unbroken) swing
                                 level on each side.
* ``detect_trend_transitions``-- changes of the HH/HL vs LH/LL state derived
                                 from the last two confirmed swing highs and
                                 lows.

Honesty rules that bind this module (truth-first repository):

* No-lookahead: a Detection with ``confirm_index == c`` may depend only on
  ``candles[:c+1]``. A fractal at index i is knowable only after candle
  i+k has closed, so every swing carries ``confirm_index = i + k`` and
  becomes usable for break/trend logic only from candle ``i + k`` onward.
  The battery proves this mechanically with
  ``core.ict.helpers.assert_no_lookahead`` (multi-variant perturbation).
* "Trend" here is a descriptive label of the observed swing structure --
  not a forecast of future price behaviour. BOS / CHoCH are structural
  labels for how price closed relative to previously confirmed swing
  levels; they are research observations, not trade signals.
* This module is a research feature for the Phase L evaluation campaign.
  Nothing in it claims predictive value, and nothing here can emit an
  actionable signal (that remains the Truth Layer's job upstream).

All public functions are pure (no I/O, no globals), deterministic, validate
their candle input with ``validate_candles``, and validate their output with
``validate_detections`` before returning. Empty candle input raises
``ContractError`` (the frozen contract requires non-empty series); a
non-empty series shorter than ``2k + 1`` candles cannot confirm any swing
and returns ``[]``.
"""
from __future__ import annotations

from typing import Optional, Sequence

from .types import (
    Candle,
    ContractError,
    Detection,
    validate_candles,
    validate_detections,
)

__all__ = [
    "detect_swings",
    "detect_structure_breaks",
    "detect_trend_transitions",
]

_SWING_KINDS = ("swing_high", "swing_low")


# ---------------------------------------------------------------------------
# internal helpers (pure, no I/O)
# ---------------------------------------------------------------------------

def _validate_k(k: int) -> None:
    """Reject invalid fractal widths: k must be an int >= 1 (bools rejected)."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ContractError(f"k must be an int >= 1, got {k!r}")


def _derive_trend(recent_highs: Sequence[float], recent_lows: Sequence[float]) -> str:
    """Trend label from the last two confirmed swing highs/lows (levels only).

    HH + HL -> "bullish"; LH + LL -> "bearish"; anything else -- one side
    missing (fewer than two confirmed swings), a tied extreme, or a mixed
    pattern -- -> "neutral".
    """
    if len(recent_highs) < 2 or len(recent_lows) < 2:
        return "neutral"
    h_prev, h_last = recent_highs[-2], recent_highs[-1]
    l_prev, l_last = recent_lows[-2], recent_lows[-1]
    if h_last > h_prev and l_last > l_prev:
        return "bullish"
    if h_last < h_prev and l_last < l_prev:
        return "bearish"
    return "neutral"


def _validated_swings(
    candles: Sequence[Candle], k: int, swings: Optional[Sequence[Detection]]
) -> list[Detection]:
    """Return caller-provided swings after strict structural validation, or
    compute them with ``detect_swings(candles, k)`` when ``swings is None``.

    Provided swings must be exactly the output of
    ``detect_swings(candles, k)``: kind in {"swing_high", "swing_low"};
    meta carries {"level", "swing_index"}; start_index == swing_index;
    k <= swing_index <= n-1-k; confirm_index == swing_index + k; the level
    equals the referenced candle's high/low; level_low/level_high equal the
    level; no duplicate (kind, swing_index) pairs. Violations raise
    ContractError so a mismatched precomputed input can never silently
    produce structure that the candle series does not support.
    """
    if swings is None:
        return detect_swings(candles, k)
    sws = list(swings)
    validate_detections(sws, candles)
    n = len(candles)
    seen: set[tuple[str, int]] = set()
    for i, d in enumerate(sws):
        if d.kind not in _SWING_KINDS:
            raise ContractError(
                f"swings[{i}].kind {d.kind!r} is not one of {_SWING_KINDS}"
            )
        s = d.meta.get("swing_index")
        lvl = d.meta.get("level")
        if isinstance(s, bool) or not isinstance(s, int):
            raise ContractError(
                f"swings[{i}].meta.swing_index must be int, got {s!r}"
            )
        if isinstance(lvl, bool) or not isinstance(lvl, (int, float)):
            raise ContractError(
                f"swings[{i}].meta.level must be numeric, got {lvl!r}"
            )
        lvl = float(lvl)
        if d.start_index != s:
            raise ContractError(
                f"swings[{i}].start_index {d.start_index} != meta.swing_index {s}"
            )
        if s < k or s > n - 1 - k:
            raise ContractError(
                f"swings[{i}] swing_index {s} outside [k, n-1-k] = [{k}, {n - 1 - k}]"
            )
        if d.confirm_index != s + k:
            raise ContractError(
                f"swings[{i}].confirm_index {d.confirm_index} != "
                f"swing_index + k = {s + k}"
            )
        ref = candles[s].high if d.kind == "swing_high" else candles[s].low
        if lvl != float(ref):
            raise ContractError(
                f"swings[{i}] level {lvl} != candles[{s}]."
                f"{'high' if d.kind == 'swing_high' else 'low'} {float(ref)}"
            )
        if (
            d.level_low is None
            or d.level_high is None
            or float(d.level_low) != lvl
            or float(d.level_high) != lvl
        ):
            raise ContractError(
                f"swings[{i}] level_low/level_high must equal the swing level {lvl}"
            )
        key = (d.kind, s)
        if key in seen:
            raise ContractError(f"duplicate swing occurrence {key}")
        seen.add(key)
    return sws


def _swing_events(swings: Sequence[Detection]) -> list[tuple[int, int, str, float]]:
    """Swing confirmations as (confirm_index, swing_index, kind, level),
    sorted by (confirm_index, swing_index, high-before-low).

    Because confirm_index == swing_index + k, all swings confirming at one
    candle share the same swing_index (a candle that is both a swing high
    and a swing low -- an outside bar); the sort key is therefore fully
    determined and the event stream is deterministic.
    """
    events: list[tuple[int, int, str, float]] = []
    for d in swings:
        events.append(
            (
                int(d.confirm_index),
                int(d.meta["swing_index"]),
                d.kind,
                float(d.meta["level"]),
            )
        )
    events.sort(key=lambda e: (e[0], e[1], 0 if e[2] == "swing_high" else 1))
    return events


# ---------------------------------------------------------------------------
# public detectors
# ---------------------------------------------------------------------------

def detect_swings(candles: Sequence[Candle], k: int = 2) -> list[Detection]:
    """Detect fractal swing highs/lows with an honest k-candle confirmation lag.

    Definition (strict; ties disqualify on either side): candle i is a
    swing high iff ``high[i] > high[i-j]`` AND ``high[i] > high[i+j]`` for
    all j in 1..k; a swing low is mirrored on lows. Equal neighbouring
    extremes cancel the candidate on that side, so flat tops/bottoms never
    produce swings.

    Confirmation honesty: the fractal at index i is knowable only after
    candle i+k has closed, so every swing Detection carries
    ``confirm_index = i + k`` and depends only on ``candles[:i+k+1]``.

    Parameters:
        candles: closed candles with strictly increasing ts, validated by
            ``validate_candles`` (empty input raises ContractError).
        k: fractal width in candles on EACH side; also the confirmation lag
            in candles; int >= 1, default 2. A series shorter than 2k+1
            cannot confirm any swing and returns [].

    Returns:
        list[Detection] ordered by ascending swing_index (a candle that is
        both a swing high and a swing low -- an outside bar -- lists the
        swing_high first). kind "swing_high" (side "bearish": swing highs
        act as resistance) or "swing_low" (side "bullish": support);
        start_index = swing_index; level_low = level_high = the swing
        price; meta = {"level": float, "swing_index": int}.
    """
    _validate_k(k)
    validate_candles(candles)
    cs = list(candles)
    n = len(cs)
    out: list[Detection] = []
    if n < 2 * k + 1:
        validate_detections(out, cs)
        return out
    for i in range(k, n - k):
        hi = cs[i].high
        lo = cs[i].low
        is_high = True
        is_low = True
        for j in range(1, k + 1):
            if is_high and not (hi > cs[i - j].high and hi > cs[i + j].high):
                is_high = False
            if is_low and not (lo < cs[i - j].low and lo < cs[i + j].low):
                is_low = False
            if not is_high and not is_low:
                break
        if is_high:
            out.append(
                Detection(
                    kind="swing_high",
                    start_index=i,
                    confirm_index=i + k,
                    ts=cs[i + k].ts,
                    side="bearish",
                    level_low=float(hi),
                    level_high=float(hi),
                    meta={"level": float(hi), "swing_index": int(i)},
                )
            )
        if is_low:
            out.append(
                Detection(
                    kind="swing_low",
                    start_index=i,
                    confirm_index=i + k,
                    ts=cs[i + k].ts,
                    side="bullish",
                    level_low=float(lo),
                    level_high=float(lo),
                    meta={"level": float(lo), "swing_index": int(i)},
                )
            )
    validate_detections(out, cs)
    return out


def detect_structure_breaks(
    candles: Sequence[Candle],
    k: int = 2,
    swings: "list[Detection] | None" = None,
) -> list[Detection]:
    """Detect BOS / CHoCH: first close-breaks of active swing levels.

    Semantics (all derived only from swings already confirmed at or before
    the breaking candle -- mechanically no lookahead):

    * Active level: a swing becomes usable only from its confirm candle
      (``confirm_index <= i`` when processing candle i) and stops being a
      break target once broken. The target on each side is the most recent
      UNBROKEN confirmed swing of that side. A close STRICTLY above the
      swing-high target (strictly below the swing-low target) breaks it;
      an equal close does not break.
    * Cascade: after a level breaks, the next-most-recent unbroken level on
      the same side becomes the target; if the same close also exceeds that
      level it breaks too, and so on. Emission order within one candle:
      high-side breaks (newest level first), then low-side breaks.
    * First break only: each swing OCCURRENCE can produce at most one break
      detection. A later swing that forms at the same price after the
      opposite side updated is a fresh occurrence and may break again.
    * Trend: derived from the last two confirmed swing highs and lows
      (HH+HL -> "bullish", LH+LL -> "bearish", otherwise "neutral",
      including insufficient swings). The trend is a function of the swing
      sequence only -- breaks do not feed back into it. Swings confirming
      at the breaking candle itself are included (they are knowable at that
      candle's close).
    * Kind mapping: close above the swing-high target while trend is
      "bearish" -> "choch_bullish" (break against the current structure);
      otherwise (bullish or neutral) -> "bos_bullish". Mirrored: close below
      the swing-low target while trend is "bullish" -> "choch_bearish";
      otherwise -> "bos_bearish".

    Parameters:
        candles: closed candles with strictly increasing ts, validated by
            ``validate_candles`` (empty input raises ContractError).
        k: fractal width / confirmation lag, int >= 1, default 2.
        swings: optional precomputed output of ``detect_swings(candles, k)``
            -- structurally validated and reused as-is; None -> recomputed
            from the candles.

    Returns:
        list[Detection] ordered by ascending breaking candle
        (confirm_index); within one candle, high-side breaks first.
        start_index = the broken swing's index; side "bullish" for upward
        breaks / "bearish" for downward; level_low = level_high = the
        broken level; meta = {"broken_level": float, "broken_swing_index":
        int, "trend_before": str}.
    """
    _validate_k(k)
    validate_candles(candles)
    cs = list(candles)
    n = len(cs)
    sws = _validated_swings(cs, k, swings)
    events = _swing_events(sws)

    out: list[Detection] = []
    recent_highs: list[float] = []  # levels of confirmed swing highs, in order
    recent_lows: list[float] = []  # levels of confirmed swing lows, in order
    stack_high: list[tuple[int, float]] = []  # unbroken highs, oldest..newest
    stack_low: list[tuple[int, float]] = []  # unbroken lows, oldest..newest

    p = 0
    m = len(events)
    for i in range(n):
        # ingest every swing whose confirm candle is i (knowable at close i)
        while p < m and events[p][0] == i:
            _conf, s, kind, lvl = events[p]
            if kind == "swing_high":
                recent_highs.append(lvl)
                stack_high.append((s, lvl))
            else:
                recent_lows.append(lvl)
                stack_low.append((s, lvl))
            p += 1
        trend = _derive_trend(recent_highs, recent_lows)
        close_i = cs[i].close
        # high side: cascade through every unbroken level the close exceeds
        while stack_high and close_i > stack_high[-1][1]:
            s, lvl = stack_high.pop()
            kind_out = "choch_bullish" if trend == "bearish" else "bos_bullish"
            out.append(
                Detection(
                    kind=kind_out,
                    start_index=s,
                    confirm_index=i,
                    ts=cs[i].ts,
                    side="bullish",
                    level_low=lvl,
                    level_high=lvl,
                    meta={
                        "broken_level": lvl,
                        "broken_swing_index": s,
                        "trend_before": trend,
                    },
                )
            )
        # low side: cascade through every unbroken level the close breaks below
        while stack_low and close_i < stack_low[-1][1]:
            s, lvl = stack_low.pop()
            kind_out = "choch_bearish" if trend == "bullish" else "bos_bearish"
            out.append(
                Detection(
                    kind=kind_out,
                    start_index=s,
                    confirm_index=i,
                    ts=cs[i].ts,
                    side="bearish",
                    level_low=lvl,
                    level_high=lvl,
                    meta={
                        "broken_level": lvl,
                        "broken_swing_index": s,
                        "trend_before": trend,
                    },
                )
            )
    validate_detections(out, cs)
    return out


def detect_trend_transitions(
    candles: Sequence[Candle],
    k: int = 2,
    swings: "list[Detection] | None" = None,
) -> list[Detection]:
    """Detect changes of the swing-derived trend state (HH/HL vs LH/LL).

    The trend state is a pure function of the confirmed swing sequence:
    with the last two confirmed swing highs (h_prev, h_last) and lows
    (l_prev, l_last): h_last > h_prev AND l_last > l_prev -> "bullish";
    h_last < h_prev AND l_last < l_prev -> "bearish"; anything else
    (insufficient swings, ties, mixed) -> "neutral".

    A transition Detection is emitted each time the state CHANGES. The new
    state is established at the confirm candle of the swing that completed
    the new pattern (the later of the two swings forming HH+HL, or LH+LL).
    Because confirm_index == swing_index + k, all swings confirming at one
    candle share the same swing_index (only possible for an outside bar
    that is both a swing high and a swing low); the state is recomputed
    once per confirm candle AFTER ingesting all of that candle's swings,
    so at most one transition is emitted per candle and the reported
    meta.swing_index is unambiguous.

    Parameters:
        candles: closed candles with strictly increasing ts, validated by
            ``validate_candles`` (empty input raises ContractError).
        k: fractal width / confirmation lag, int >= 1, default 2.
        swings: optional precomputed output of ``detect_swings(candles, k)``
            -- structurally validated and reused as-is; None -> recomputed
            from the candles.

    Returns:
        list[Detection] ordered by ascending confirm_index. kind
        "trend_bullish" / "trend_bearish" / "trend_neutral"; side equals the
        new state ("neutral" side for trend_neutral); start_index =
        meta.swing_index = the swing that established the new state;
        level_low/level_high are None (a trend change is not a price
        level); meta = {"prev": str, "new": str, "swing_index": int}.
    """
    _validate_k(k)
    validate_candles(candles)
    cs = list(candles)
    n = len(cs)
    sws = _validated_swings(cs, k, swings)
    events = _swing_events(sws)

    out: list[Detection] = []
    state = "neutral"
    recent_highs: list[float] = []
    recent_lows: list[float] = []

    p = 0
    m = len(events)
    for i in range(n):
        trigger: Optional[int] = None
        # ingest every swing whose confirm candle is i (knowable at close i)
        while p < m and events[p][0] == i:
            _conf, s, kind, lvl = events[p]
            if kind == "swing_high":
                recent_highs.append(lvl)
            else:
                recent_lows.append(lvl)
            trigger = s  # all swings confirming at candle i share this index
            p += 1
        if trigger is None:
            continue  # no swing confirmed here -> the state cannot change
        new_state = _derive_trend(recent_highs, recent_lows)
        if new_state != state:
            out.append(
                Detection(
                    kind=f"trend_{new_state}",
                    start_index=trigger,
                    confirm_index=i,
                    ts=cs[i].ts,
                    side=new_state,
                    level_low=None,
                    level_high=None,
                    meta={"prev": state, "new": new_state, "swing_index": trigger},
                )
            )
            state = new_state
    validate_detections(out, cs)
    return out
