"""Phase C1 — data-integrity validation with quarantine + exclusion ledger.

Every observation entering the clean evaluation pipeline passes through this
module. Invalid observations are REJECTED or QUARANTINED with a stable
machine-readable reason — never silently repaired. Every exclusion is
recorded and reported.

Outcome convention (documented, single source of truth for this package):

    GREEN  <- close >  open   (a CALL prediction is correct iff GREEN)
    RED    <- close <  open   (a PUT  prediction is correct iff RED)
    FLAT   <- close == open   (tie: NEVER labeled GREEN or RED; excluded from
                               direction accuracy, counted and reported)

Timestamp convention: epoch SECONDS (UTC). Loaders converting from
milliseconds must do so explicitly before validation (see
``load_binance_1m`` in scripts/backtest_clean_eval.py).

Candle validation reasons (stable strings):

    asset_invalid        — missing/empty/non-string asset identity
    period_invalid       — timeframe not a positive integer number of seconds
    ts_invalid           — open timestamp missing / not a finite number
    ts_misaligned        — open timestamp not on the period grid
                           (t % period != 0)
    ohlc_missing         — one of open/high/low/close missing/non-numeric
    ohlc_non_finite      — NaN or +/-inf price
    price_non_positive   — any price <= 0
    ohlc_inconsistent    — high < max(open, close), low > min(open, close),
                           or low > high
    duplicate_candle     — same (asset, period, t) already accepted
    out_of_order         — open timestamp <= previous accepted timestamp
    missing_interval     — gap: t > prev_t + period (gap size recorded)
    zero_range           — open == high == low == close (policy:
                           "quarantine" by default — a flat line candle
                           carries no directional information and, on OTC
                           feeds, usually indicates a data problem; policy
                           may be set to "flag" to accept-but-flag instead)

Tie candles (open == close, non-zero range) are ACCEPTED and labeled FLAT —
they are a real market outcome, not a data defect. They are excluded from
directional accuracy and counted in ``tie_count`` (the documented tie
policy; see docs/phase_c_i/PHASE_C_BACKTEST_INTEGRITY.md).

Prediction validation reasons (stable strings):

    prediction_invalid   — malformed prediction record (missing fields)
    direction_invalid    — direction not in {CALL, PUT} (NO_SIGNAL records
                           are handled by the coverage metric, not here)
    duplicate_prediction — 2nd+ prediction for the same
                           (asset, period, target_t): FIRST kept, rest
                           excluded (frozen-first-write semantics, matching
                           the signal_provenance UNIQUE constraint)
    target_missing       — no candle exists at (asset, period, target_t)
    target_quarantined   — target candle exists but was quarantined
    evidence_leakage     — evidence timestamp > prediction cutoff
                           (cutoff = target candle open time; evidence at
                           exactly the cutoff — a source candle closing as
                           the target opens — is legitimate)
    post_gap_target      — the target candle immediately follows a series
                           gap: the predictor's feature window is
                           discontinuous, so the observation is excluded
                           from headline metrics (counted, reported)
    tie_outcome          — target outcome is FLAT (excluded from direction
                           metrics; counted; NOT a defect)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

GREEN = "GREEN"
RED = "RED"
FLAT = "FLAT"

CALL = "CALL"
PUT = "PUT"

# Stable reason codes (exported for tests + report consumers)
CANDLE_REASONS = (
    "asset_invalid", "period_invalid", "ts_invalid", "ts_misaligned",
    "ohlc_missing", "ohlc_non_finite", "price_non_positive",
    "ohlc_inconsistent", "duplicate_candle", "out_of_order",
    "missing_interval", "zero_range",
)
PREDICTION_REASONS = (
    "prediction_invalid", "direction_invalid", "duplicate_prediction",
    "target_missing", "target_quarantined", "evidence_leakage",
    "post_gap_target", "tie_outcome",
)

_EPS = 1e-12


def outcome_of(open_: float, close: float) -> str:
    """Documented outcome convention. FLAT is never silently GREEN/RED."""
    if close > open_ + _EPS:
        return GREEN
    if close < open_ - _EPS:
        return RED
    return FLAT


def _finite_number(v: Any) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f not in (float("inf"), float("-inf"))


@dataclass
class CandleCheckPolicy:
    """Configurable validation policy. Defaults are the conservative choice."""

    zero_range: str = "quarantine"  # "quarantine" | "flag"


@dataclass
class ValidCandle:
    asset: str
    period: int
    t: int                # open timestamp, epoch seconds, period-aligned
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    outcome: str = FLAT   # GREEN / RED / FLAT (documented convention)
    gap_before: bool = False  # previous accepted candle was not t - period

    def as_dict(self) -> Dict[str, Any]:
        return {
            "asset": self.asset, "period": self.period, "time": self.t,
            "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "volume": self.volume,
            "outcome": self.outcome, "gap_before": self.gap_before,
        }


@dataclass
class CandleIntegrityReport:
    accepted: List[ValidCandle] = field(default_factory=list)
    quarantined: List[Dict[str, Any]] = field(default_factory=list)
    # quarantined entry: {"reason", "asset", "period", "t", "detail"}
    flags: List[Dict[str, Any]] = field(default_factory=list)
    tie_count: int = 0
    gap_count: int = 0
    gap_sizes: List[int] = field(default_factory=list)
    duplicate_count: int = 0
    out_of_order_count: int = 0
    per_asset: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def summary(self) -> Dict[str, Any]:
        by_reason: Dict[str, int] = {}
        for q in self.quarantined:
            by_reason[q["reason"]] = by_reason.get(q["reason"], 0) + 1
        return {
            "total_input": self.total_input,
            "accepted": len(self.accepted),
            "quarantined": len(self.quarantined),
            "quarantined_by_reason": dict(sorted(by_reason.items())),
            "flagged": len(self.flags),
            "tie_count": self.tie_count,
            "gap_count": self.gap_count,
            "gap_sizes_periods": sorted(set(self.gap_sizes)),
            "duplicate_count": self.duplicate_count,
            "out_of_order_count": self.out_of_order_count,
            "per_asset": dict(self.per_asset),
        }

    @property
    def total_input(self) -> int:
        return len(self.accepted) + len(self.quarantined)

    def index(self) -> Dict[Tuple[str, int, int], ValidCandle]:
        return {(c.asset, c.period, c.t): c for c in self.accepted}


def validate_candle_series(
    rows: Iterable[Dict[str, Any]],
    policy: Optional[CandleCheckPolicy] = None,
) -> CandleIntegrityReport:
    """Validate candles in ARRIVAL order (out-of-order detection requires it).

    Accepted candle keys: ``asset`` (str), ``period`` (int seconds),
    ``t`` (epoch seconds open time), ``o``/``h``/``l``/``c`` (floats),
    optional ``v``. The long forms ``time``/``open``/``high``/``low``/
    ``close`` are also accepted (repo convention).
    """
    pol = policy or CandleCheckPolicy()
    rep = CandleIntegrityReport()
    seen: Dict[Tuple[str, int, int], int] = {}
    last_accepted: Dict[str, Tuple[int, int]] = {}  # asset -> (period, t)

    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            rep.quarantined.append({"reason": "asset_invalid", "asset": None,
                                    "period": None, "t": None,
                                    "detail": f"row {i}: not a dict"})
            continue

        asset = raw.get("asset")
        if not isinstance(asset, str) or not asset.strip():
            rep.quarantined.append({"reason": "asset_invalid", "asset": asset,
                                    "period": raw.get("period"),
                                    "t": raw.get("t", raw.get("time")),
                                    "detail": f"row {i}"})
            continue

        def _bump(asset: str) -> None:
            st = rep.per_asset.setdefault(
                asset, {"input": 0, "accepted": 0, "quarantined": 0})
            st["input"] += 1

        _bump(asset)

        period = raw.get("period")
        if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
            rep.quarantined.append({"reason": "period_invalid", "asset": asset,
                                    "period": period,
                                    "t": raw.get("t", raw.get("time")),
                                    "detail": f"row {i}"})
            rep.per_asset[asset]["quarantined"] += 1
            continue

        t = raw.get("t", raw.get("time"))
        if t is None or isinstance(t, bool) or not _finite_number(t):
            rep.quarantined.append({"reason": "ts_invalid", "asset": asset,
                                    "period": period, "t": t,
                                    "detail": f"row {i}"})
            rep.per_asset[asset]["quarantined"] += 1
            continue
        t = int(float(t))
        if t <= 0 or t % period != 0:
            rep.quarantined.append({"reason": "ts_misaligned", "asset": asset,
                                    "period": period, "t": t,
                                    "detail": f"row {i}: t%period!=0"})
            rep.per_asset[asset]["quarantined"] += 1
            continue

        o = raw.get("o", raw.get("open"))
        h = raw.get("h", raw.get("high"))
        l = raw.get("l", raw.get("low"))
        c = raw.get("c", raw.get("close"))
        if any(v is None for v in (o, h, l, c)):
            rep.quarantined.append({"reason": "ohlc_missing", "asset": asset,
                                    "period": period, "t": t,
                                    "detail": f"row {i}"})
            rep.per_asset[asset]["quarantined"] += 1
            continue
        if not all(_finite_number(v) for v in (o, h, l, c)):
            rep.quarantined.append({"reason": "ohlc_non_finite", "asset": asset,
                                    "period": period, "t": t,
                                    "detail": f"row {i}"})
            rep.per_asset[asset]["quarantined"] += 1
            continue
        o, h, l, c = float(o), float(h), float(l), float(c)
        if any(v <= 0 for v in (o, h, l, c)):
            rep.quarantined.append({"reason": "price_non_positive",
                                    "asset": asset, "period": period, "t": t,
                                    "detail": f"row {i}"})
            rep.per_asset[asset]["quarantined"] += 1
            continue
        if h < max(o, c) - _EPS or l > min(o, c) + _EPS or l > h + _EPS:
            rep.quarantined.append({"reason": "ohlc_inconsistent",
                                    "asset": asset, "period": period, "t": t,
                                    "detail": f"row {i}: h={h} l={l} o={o} c={c}"})
            rep.per_asset[asset]["quarantined"] += 1
            continue

        key = (asset, period, t)
        if key in seen:
            rep.duplicate_count += 1
            rep.quarantined.append({"reason": "duplicate_candle", "asset": asset,
                                    "period": period, "t": t,
                                    "detail": f"row {i}: duplicate of row {seen[key]}"})
            rep.per_asset[asset]["quarantined"] += 1
            continue

        if asset in last_accepted:
            prev_period, prev_t = last_accepted[asset]
            if t <= prev_t:
                rep.out_of_order_count += 1
                rep.quarantined.append({"reason": "out_of_order", "asset": asset,
                                        "period": period, "t": t,
                                        "detail": f"row {i}: prev accepted t={prev_t}"})
                rep.per_asset[asset]["quarantined"] += 1
                continue
            if prev_period == period and t > prev_t + period:
                gap = (t - prev_t) // period - 1
                rep.gap_count += 1
                rep.gap_sizes.append(int(gap))

        if o == h == l == c:
            if pol.zero_range == "quarantine":
                rep.quarantined.append({"reason": "zero_range", "asset": asset,
                                        "period": period, "t": t,
                                        "detail": f"row {i}"})
                rep.per_asset[asset]["quarantined"] += 1
                continue
            rep.flags.append({"reason": "zero_range", "asset": asset,
                              "period": period, "t": t, "detail": f"row {i}"})

        vol = raw.get("v", raw.get("volume"))
        vol = float(vol) if _finite_number(vol) else None

        prev = last_accepted.get(asset)
        gap_before = prev is not None and (prev[0] != period
                                           or prev[1] != t - period)
        candle = ValidCandle(
            asset=asset, period=period, t=t, open=o, high=h, low=l, close=c,
            volume=vol, outcome=outcome_of(o, c), gap_before=gap_before,
        )
        seen[key] = i
        if candle.outcome == FLAT:
            rep.tie_count += 1
        rep.accepted.append(candle)
        rep.per_asset[asset]["accepted"] += 1
        last_accepted[asset] = (period, t)

    return rep


# ---------------------------------------------------------------------------
# Prediction validation (C1 signal-side)
# ---------------------------------------------------------------------------

@dataclass
class PredictionIntegrityReport:
    accepted: List[Dict[str, Any]] = field(default_factory=list)
    excluded: List[Dict[str, Any]] = field(default_factory=list)
    # excluded entry: {"reason", "asset", "period", "target_t", "detail"}
    no_signal_count: int = 0

    def summary(self) -> Dict[str, Any]:
        by_reason: Dict[str, int] = {}
        for e in self.excluded:
            by_reason[e["reason"]] = by_reason.get(e["reason"], 0) + 1
        return {
            "total_input": self.total_input,
            "accepted": len(self.accepted),
            "no_signal_count": self.no_signal_count,
            "excluded": len(self.excluded),
            "excluded_by_reason": dict(sorted(by_reason.items())),
        }

    @property
    def total_input(self) -> int:
        return (len(self.accepted) + len(self.excluded)
                + self.no_signal_count)


def validate_predictions(
    preds: Iterable[Dict[str, Any]],
    candle_report: CandleIntegrityReport,
) -> PredictionIntegrityReport:
    """Validate prediction records against the validated candle universe.

    Prediction keys: ``asset`` (str), ``period`` (int), ``target_t`` (epoch
    seconds — the open time of the candle being predicted), ``direction``
    ("CALL" | "PUT" | "NO_SIGNAL"), ``evidence_ts`` (epoch seconds — the
    wall-clock moment ALL information used by the prediction became
    available; for a closed-candle source predicting the next candle this is
    the source candle's close time), optional ``prob`` (a probability of
    GREEN in (0,1) — only when genuinely produced), optional ``predictor``.

    A NO_SIGNAL record is NOT an error: it counts toward coverage
    (non-actionable) and is passed through as ``no_signal_count``.
    """
    rep = PredictionIntegrityReport()
    idx = candle_report.index()
    quarantined_idx = {
        (q.get("asset"), q.get("period"), q.get("t"))
        for q in candle_report.quarantined
        if q.get("t") is not None
    }
    seen_targets: Dict[Tuple[str, int, int], int] = {}

    for i, raw in enumerate(preds):
        if not isinstance(raw, dict):
            rep.excluded.append({"reason": "prediction_invalid", "asset": None,
                                 "period": None, "target_t": None,
                                 "detail": f"row {i}: not a dict"})
            continue
        asset = raw.get("asset")
        period = raw.get("period")
        target_t = raw.get("target_t")
        direction = raw.get("direction")
        evidence_ts = raw.get("evidence_ts")

        def _bad(reason: str, detail: str) -> None:
            rep.excluded.append({"reason": reason, "asset": asset,
                                 "period": period, "target_t": target_t,
                                 "detail": f"row {i}: {detail}"})

        if (not isinstance(asset, str) or isinstance(period, bool)
                or not isinstance(period, int) or period <= 0
                or target_t is None or isinstance(target_t, bool)
                or not _finite_number(target_t) or int(float(target_t)) <= 0):
            _bad("prediction_invalid", "malformed identity fields")
            continue
        target_t = int(float(target_t))

        if direction == "NO_SIGNAL":
            rep.no_signal_count += 1
            continue
        if direction not in (CALL, PUT):
            _bad("direction_invalid", f"direction={direction!r}")
            continue

        key = (asset, period, target_t)
        if key in seen_targets:
            _bad("duplicate_prediction",
                 f"duplicate of row {seen_targets[key]} (first kept)")
            continue
        seen_targets[key] = i

        candle = idx.get(key)
        if candle is None:
            if key in quarantined_idx:
                _bad("target_quarantined",
                     "target candle exists but was quarantined by the "
                     "integrity validator")
            else:
                _bad("target_missing",
                     "no accepted candle at (asset, period, target_t)")
            continue

        if evidence_ts is None or isinstance(evidence_ts, bool) \
                or not _finite_number(evidence_ts):
            _bad("prediction_invalid", "evidence_ts missing/non-numeric")
            continue
        evidence_ts = float(evidence_ts)
        cutoff = float(target_t)
        if evidence_ts > cutoff + _EPS:
            _bad("evidence_leakage",
                 f"evidence_ts {evidence_ts} > cutoff {cutoff}")
            continue

        if candle.gap_before:
            _bad("post_gap_target",
                 "feature window discontinuous across a series gap")
            continue

        prob = raw.get("prob")
        if prob is not None:
            if (isinstance(prob, bool) or not _finite_number(prob)
                    or not (0.0 < float(prob) < 1.0)):
                _bad("prediction_invalid",
                     f"prob={prob!r} not in open interval (0,1)")
                continue
            prob = float(prob)

        if candle.outcome == FLAT:
            rep.excluded.append({
                "reason": "tie_outcome", "asset": asset, "period": period,
                "target_t": target_t, "direction": direction,
                "detail": "target candle closed FLAT (tie policy: excluded "
                          "from direction metrics, counted)"})
            continue

        rep.accepted.append({
            "asset": asset, "period": period, "target_t": target_t,
            "direction": direction, "outcome": candle.outcome,
            "prob": prob, "predictor": raw.get("predictor"),
            "evidence_ts": evidence_ts,
            "correct": (direction == CALL and candle.outcome == GREEN)
                       or (direction == PUT and candle.outcome == RED),
        })

    return rep
