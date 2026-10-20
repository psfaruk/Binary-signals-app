"""SMC/ICT research feature contracts — Phase L (locked at L0).

THE shared contract for every core/ict module. Frozen dataclasses, strict
candle invariants, and the no-lookahead Detection bookkeeping rule.

Hard rules for all detectors in this package:
  * Pure functions: no I/O, no globals mutation, deterministic.
  * A Detection with confirm_index == c may depend ONLY on candles[:c+1].
  * ts must equal candles[confirm_index].ts.
  * side is one of "bullish" | "bearish" | "neutral".
  * meta must be JSON-serializable (str/int/float/bool/None/list/dict).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

SIDES = ("bullish", "bearish", "neutral")


class ContractError(ValueError):
    """Raised when candles or detections violate the Phase-L contract."""


@dataclass(frozen=True)
class Candle:
    """One closed 1-minute candle. ts = unix SECONDS (open time).

    Mirrors scripts/backtest_clean_eval.load_binance_1m rows
    ({"t": ms//1000, "o","h","l","c","v"}).
    """

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def direction(self) -> str:
        if self.close > self.open:
            return "bullish"
        if self.close < self.open:
            return "bearish"
        return "neutral"

    def mid(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass(frozen=True)
class Detection:
    """A confirmed structure/pattern/zone observation.

    confirm_index semantics: the detection became knowable only after the
    candle at confirm_index CLOSED. Detectors must never read candles beyond
    confirm_index. start_index <= confirm_index points at the structure's
    first candle. level_low/level_high bound a zone when applicable
    (else None). meta carries module-specific strict fields.
    """

    kind: str
    start_index: int
    confirm_index: int
    ts: int
    side: str
    level_low: Optional[float] = None
    level_high: Optional[float] = None
    meta: dict = field(default_factory=dict)


def candle_from_row(row: dict, asset: str = "") -> Candle:
    """Build a Candle from a loader row {"t","o","h","l","c","v"}.

    Accepts t in seconds or milliseconds (heuristic: > 10^12 = ms).
    """
    t = int(row["t"])
    if t > 10_000_000_000:  # ms -> s
        t //= 1000
    vol = row.get("v")
    return Candle(
        ts=t,
        open=float(row["o"]),
        high=float(row["h"]),
        low=float(row["l"]),
        close=float(row["c"]),
        volume=(float(vol) if vol is not None else None),
    )


def validate_candles(candles: Sequence[Candle], strict_order: bool = True) -> None:
    """Raise ContractError on invariant violations.

    Checks: non-empty, OHLC sanity (high>=max(o,c), low<=min(o,c), low<=high,
    all finite, prices > 0), and (strict_order) strictly increasing ts.
    """
    if not candles:
        raise ContractError("candles must be non-empty")
    prev_ts: Optional[int] = None
    for i, c in enumerate(candles):
        for name in ("open", "high", "low", "close"):
            v = getattr(c, name)
            if v != v or v in (float("inf"), float("-inf")):  # NaN/inf
                raise ContractError(f"candle[{i}].{name} not finite")
            if v <= 0:
                raise ContractError(f"candle[{i}].{name} must be > 0")
        if c.high < max(c.open, c.close):
            raise ContractError(
                f"candle[{i}]: high {c.high} < max(open,close) {max(c.open, c.close)}"
            )
        if c.low > min(c.open, c.close):
            raise ContractError(
                f"candle[{i}]: low {c.low} > min(open,close) {min(c.open, c.close)}"
            )
        if c.low > c.high:
            raise ContractError(f"candle[{i}]: low {c.low} > high {c.high}")
        if c.volume is not None and (c.volume < 0 or c.volume != c.volume):
            raise ContractError(f"candle[{i}].volume invalid: {c.volume}")
        if strict_order and prev_ts is not None and c.ts <= prev_ts:
            raise ContractError(
                f"candle[{i}].ts {c.ts} not strictly after previous {prev_ts}"
            )
        prev_ts = c.ts


def validate_detections(
    detections: Sequence[Detection], candles: Sequence[Candle]
) -> None:
    """Validate Detection contract against the candle list they refer to."""
    n = len(candles)
    for i, d in enumerate(detections):
        if d.side not in SIDES:
            raise ContractError(f"detection[{i}].side invalid: {d.side!r}")
        if not (0 <= d.start_index <= d.confirm_index < n):
            raise ContractError(
                f"detection[{i}] index range invalid: "
                f"start={d.start_index} confirm={d.confirm_index} n={n}"
            )
        if d.ts != candles[d.confirm_index].ts:
            raise ContractError(
                f"detection[{i}].ts {d.ts} != candles[{d.confirm_index}].ts "
                f"{candles[d.confirm_index].ts}"
            )
        if d.level_low is not None and d.level_high is not None:
            if d.level_low > d.level_high:
                raise ContractError(
                    f"detection[{i}] zone inverted: low {d.level_low} > high {d.level_high}"
                )
        try:
            import json

            json.dumps(d.meta)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"detection[{i}].meta not JSON-serializable: {exc}")


__all__ = [
    "Candle",
    "Detection",
    "ContractError",
    "SIDES",
    "candle_from_row",
    "validate_candles",
    "validate_detections",
]
