"""PHASE E — market data quality, provenance and staleness gates.

Three concerns, one honest module:

E1 — DATA-SOURCE PROVENANCE (``DatasetProvenance`` / ``classify_*``):
every dataset or observation class the app can evaluate carries an
explicit provenance record: source identity, authorization status,
asset/timeframe, observation window, receipt timestamp, clock source
and sync state, data classification (historical / synthetic /
simulated / authorized-feed / unauthorized-channel-capture) and
data-quality flags. Authorization is NEVER fabricated: the live Quotex
channels are recorded as ``unauthorized_reverse_engineered_channel``
exactly as the repo's own documentation states, and an unknown source
is never marked trusted.

E3 — STALENESS / QUALITY GATES (``QualityPolicy`` / ``assess``):
configurable, DOCUMENTED limits. The gates only REJECT or DOWNGRADE —
they can never enable or increase signal frequency (default-deny by
construction). Thresholds are protocol constants of THIS app, recorded
via ``describe_policy()``; none of them is claimed to be a broker
requirement.

E4 — the read-only diagnostics endpoint ``GET /api/data-quality``
(server.py) consumes this module; it exposes no credentials or session
material.

All checks fail toward the unsafe-looking side being REJECTED: a stale
feed, an unknown source, insufficient history, malformed OHLC, excess
clock uncertainty, a gapped prediction window, or contradictory
evidence each produce a non-actionable verdict with a stable reason
string.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── E3 policy constants (documented; NOT broker requirements) ────────────


@dataclass
class QualityPolicy:
    """Documented, configurable quality limits.

    Defaults align with the app's EXISTING documented thresholds rather
    than inventing new ones:

    * ``stale_secs`` = 90 — matches feed.STALE_SECS (the repo's
      documented feed-freshness threshold, env-overridable there as
      STALE_SECS).
    * ``min_closed_candles`` = 64 — matches the CSE engine's documented
      live gate (feed.py: `len(closed) >= 64`).
    * ``max_clock_uncertainty_secs`` = 2.0 — THIS MODULE's protocol
      constant: the SourceTimeProvider's uncertainty is the max deviation
      from the median offset; >2 s of spread means the samples disagree
      more than any documented candle-boundary tolerance we can justify.
      It is our threshold, not a verified broker tolerance.
    * ``max_missing_intervals`` = 0 — a prediction window must be
      gap-free (a single missing candle breaks the engine's sequence
      context; mirrors eval_clean's ``post_gap_target`` rule).
    """

    stale_secs: float = 90.0
    min_closed_candles: int = 64
    max_clock_uncertainty_secs: float = 2.0
    max_missing_intervals: int = 0
    require_known_source: bool = True
    require_valid_ohlc: bool = True


DEFAULT_POLICY = QualityPolicy()

# Stable quality-verdict reasons (machine-readable)
QUALITY_REASONS = (
    "stale_data",              # last observation older than stale_secs
    "unknown_source",          # source identity absent/unknown
    "insufficient_observations",  # fewer closed candles than the minimum
    "malformed_ohlc",          # latest candle OHLC invalid
    "clock_uncertainty",       # timing uncertainty beyond the threshold
    "missing_intervals",       # gaps inside the prediction window
    "contradictory_source_data",  # unresolved conflicts on the latest candle
)

VERDICT_OK = "ok"
VERDICT_REJECT = "reject"


def describe_policy(policy: Optional[QualityPolicy] = None) -> Dict[str, Any]:
    pol = policy or DEFAULT_POLICY
    return {
        "policy_version": "data_quality_v1",
        "stale_secs": pol.stale_secs,
        "min_closed_candles": pol.min_closed_candles,
        "max_clock_uncertainty_secs": pol.max_clock_uncertainty_secs,
        "max_missing_intervals": pol.max_missing_intervals,
        "require_known_source": pol.require_known_source,
        "require_valid_ohlc": pol.require_valid_ohlc,
        "provenance_note": (
            "Thresholds are documented protocol constants of this app "
            "(aligned with feed.STALE_SECS=90 and the engine's 64-candle "
            "gate). None is a verified broker requirement. Gates only "
            "reject/downgrade — they can never enable signals."),
    }


def _finite(v: Any) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f not in (float("inf"), float("-inf"))


def assess(
    *,
    source_id: Optional[str],
    source_authorized: bool,
    last_observation_wall: Optional[float],
    now: float,
    closed_candles: Optional[Sequence[Dict[str, Any]]] = None,
    clock_sync_status: str = "UNVERIFIED",
    clock_uncertainty_secs: Optional[float] = None,
    missing_intervals: int = 0,
    latest_conflict_count: int = 0,
    policy: Optional[QualityPolicy] = None,
) -> Dict[str, Any]:
    """Assess one prediction-time data context. Returns a verdict dict:

    {"verdict": "ok"|"reject", "reasons": [...], "flags": {...},
     "checks": {...}} — ``reject`` reasons come from QUALITY_REASONS.
    A source that is not authorized is reported as an honest flag but is
    NOT this module's gate (the signal contract owns authorization); the
    quality module owns staleness/shape/consistency honesty.
    """
    pol = policy or DEFAULT_POLICY
    reasons: List[str] = []
    checks: Dict[str, Any] = {}

    # 1 — source identity
    checks["source_known"] = bool(source_id)
    if pol.require_known_source and not source_id:
        reasons.append("unknown_source")

    # 2 — staleness (monotonic wall clock supplied by the caller)
    checks["last_observation_wall"] = last_observation_wall
    age = None
    if _finite(last_observation_wall) and _finite(now):
        age = float(now) - float(last_observation_wall)
        checks["age_secs"] = round(age, 3)
        checks["stale"] = age > pol.stale_secs
        if checks["stale"]:
            reasons.append("stale_data")
    else:
        checks["stale"] = True
        reasons.append("stale_data")

    # 3 — sufficiency
    n_closed = len(closed_candles) if closed_candles is not None else 0
    checks["closed_candles"] = n_closed
    checks["sufficient"] = n_closed >= pol.min_closed_candles
    if not checks["sufficient"]:
        reasons.append("insufficient_observations")

    # 4 — latest candle OHLC shape
    latest = closed_candles[-1] if closed_candles else None
    ohlc_ok = True
    if latest is not None:
        try:
            o, h, l, c = (float(latest.get(k)) for k in
                          ("open", "high", "low", "close"))
            ohlc_ok = (all(_finite(v) for v in (o, h, l, c))
                       and min(o, h, l, c) > 0
                       and h >= max(o, c) - 1e-12
                       and l <= min(o, c) + 1e-12
                       and l <= h + 1e-12)
        except (TypeError, ValueError, KeyError):
            ohlc_ok = False
    checks["latest_ohlc_valid"] = ohlc_ok
    if pol.require_valid_ohlc and not ohlc_ok:
        reasons.append("malformed_ohlc")

    # 5 — clock uncertainty (only meaningful with measured sync)
    checks["clock_sync_status"] = clock_sync_status
    unc = clock_uncertainty_secs
    checks["clock_uncertainty_secs"] = (
        round(float(unc), 3) if _finite(unc) else None)
    if clock_sync_status == "SYNCED" and _finite(unc) \
            and float(unc) > pol.max_clock_uncertainty_secs:
        checks["clock_uncertain"] = True
        reasons.append("clock_uncertainty")
    else:
        checks["clock_uncertain"] = False

    # 6 — missing intervals inside the prediction window
    checks["missing_intervals"] = int(missing_intervals or 0)
    if int(missing_intervals or 0) > pol.max_missing_intervals:
        reasons.append("missing_intervals")

    # 7 — contradictory evidence (unresolved finalization conflicts)
    checks["latest_conflict_count"] = int(latest_conflict_count or 0)
    if int(latest_conflict_count or 0) > 0:
        reasons.append("contradictory_source_data")

    return {
        "verdict": VERDICT_OK if not reasons else VERDICT_REJECT,
        "reasons": reasons,
        "checks": checks,
        "source_authorized": bool(source_authorized),
        "note": ("authorization is gated by the signal contract "
                 "(core/signal_contract.py); this module gates "
                 "staleness/shape/consistency only"),
    }


# ── E1 — dataset provenance records ─────────────────────────────────────


@dataclass
class DatasetProvenance:
    """Provenance record for any dataset/observation class."""

    dataset_id: str
    source_id: str
    source_authorization: str          # "unauthorized" | "authorized" | "unknown"
    classification: str                # see CLASSIFICATIONS below
    assets: Optional[List[str]] = None
    timeframe_secs: Optional[int] = None
    observation_window_utc: Optional[Tuple[str, str]] = None
    receipt_utc: Optional[str] = None
    clock_source: str = "device_local"
    clock_sync_state: str = "UNVERIFIED"
    data_quality_flags: List[str] = field(default_factory=list)
    exclusion_reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "source_id": self.source_id,
            "source_authorization": self.source_authorization,
            "classification": self.classification,
            "assets": self.assets,
            "timeframe_secs": self.timeframe_secs,
            "observation_window_utc": self.observation_window_utc,
            "receipt_utc": self.receipt_utc,
            "clock_source": self.clock_source,
            "clock_sync_state": self.clock_sync_state,
            "data_quality_flags": list(self.data_quality_flags),
            "exclusion_reasons": list(self.exclusion_reasons),
        }


CLASSIFICATIONS = (
    "authorized_historical_public_exchange",
    "synthetic_fixture",
    "simulated_demo_feed",
    "authorized_live_feed",
    "unauthorized_reverse_engineered_channel",
    "observational_capture_from_unauthorized_channel",
)


def classify_binance_1m(dataset_id: str, assets: Sequence[str],
                        window_utc: Tuple[str, str],
                        receipt_utc: Optional[str] = None) -> DatasetProvenance:
    """Public Binance 1m exchange data committed in the repo: authorized
    historical data for research — real market, NOT OTC."""
    return DatasetProvenance(
        dataset_id=dataset_id,
        source_id="binance_public_api_1m",
        source_authorization="authorized",
        classification="authorized_historical_public_exchange",
        assets=list(assets), timeframe_secs=60,
        observation_window_utc=window_utc, receipt_utc=receipt_utc,
        clock_source="exchange_server_time", clock_sync_state="N/A_HISTORICAL")


def classify_synthetic(dataset_id: str, seed: Any) -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id=dataset_id, source_id=f"synthetic_generator_seed_{seed}",
        source_authorization="authorized",
        classification="synthetic_fixture",
        assets=["SYNTH"], timeframe_secs=60,
        clock_source="generator_clock", clock_sync_state="N/A_SYNTHETIC")


def classify_live_quotex_channels(backend: str) -> DatasetProvenance:
    """The LIVE app's data channels — honestly unauthorized (the repo's
    own docs describe reverse-engineered TLS-spoofing/cookie-replay
    access; a prior account block is self-documented)."""
    return DatasetProvenance(
        dataset_id=f"live_feed_{backend}",
        source_id=f"quotex_{backend}",
        source_authorization="unauthorized",
        classification="unauthorized_reverse_engineered_channel",
        clock_source="device_local_unless_raw_ws_timesync",
        clock_sync_state="UNVERIFIED")


def classify_live_capture(dataset_id: str,
                          capture_utc: str) -> DatasetProvenance:
    """Captured records of the app's own past emissions: an observational
    audit artifact whose ground-truth labels derive from the unauthorized
    channel — usable for audit, never as market evidence."""
    return DatasetProvenance(
        dataset_id=dataset_id,
        source_id="app_production_capture",
        source_authorization="unauthorized",
        classification="observational_capture_from_unauthorized_channel",
        receipt_utc=capture_utc,
        data_quality_flags=["ground_truth_from_unauthorized_channel"])


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def count_series_gaps(candle_times: Sequence[int], period: int) -> int:
    """Missing-interval count for a time-sorted candle series: number of
    places where consecutive candle open-times are not exactly
    ``period`` apart."""
    gaps = 0
    prev = None
    for t in candle_times:
        t = int(t)
        if prev is not None and t > prev + period:
            gaps += (t - prev) // period - 1
        prev = t
    return gaps
