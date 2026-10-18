"""PHASE-2A TIME PROVIDER — honest time evidence (2026-10, rebuild).
=================================================================
Governing principle (Phase 2A directive, verbatim):

  Local time is never labeled as broker time; sync evidence EXPIRES on
  reconnect; synchronization is never claimed merely because a
  connection exists or a boolean was previously set.

Providers
---------
LocalClockProvider   always local-only, honestly labeled: sync status
                    is UNVERIFIED forever, `now()` is the device UTC
                    clock.  This is the pyquotex-path reality: that
                    backend has NO server-time mechanism at all.
SourceTimeProvider   evidence-based: accepts ONLY measured source-time
                    samples (raw-WS `timesync` frames), estimates the
                    local->source offset from them, and reports
                    UNVERIFIED until there is enough FRESH, CONSISTENT
                    evidence.  Fail-closed on malformed / out-of-order /
                    jumping samples.

Evidence policy (documented thresholds, fail-closed direction)
--------------------------------------------------------------
* >= 3 accepted samples before ANY offset/uncertainty is believed
  (a median needs 3 points; 1 sample is an anecdote, not evidence).
* Staleness window 120 s: Quotex sends timesync frames every ~5-30 s,
  so 120 s of silence = 4-24x the expected cadence = evidence EXPIRED
  (status STALE; the timing gate treats STALE as unverified).
* Offset jump > 5 s between consecutive accepted samples => evidence
  RESET (a step that large means broker-clock step, local-clock step,
  or unreliable samples) — re-verify from scratch; never blend the jump
  into the estimate.
* Samples implying |offset| > 1 day are MALFORMED (rejected), not
  treated as a jump.
* Elapsed-time / staleness math uses time.monotonic() ONLY — a wall-
  clock step (NTP adjust, suspend/resume) can never corrupt freshness.
  Wall clocks are used solely for the offset estimate itself (that is
  what an offset IS) and for human-readable UTC labels.

Timing sync != source authorization: this module says NOTHING about
whether the data source is authorized.  A SYNCED provider plus an
unauthorized source still yields NO_SIGNAL (DATA_SOURCE_UNAUTHORIZED)
— the Phase-1 Truth Layer keeps that ordering.

No secrets.  No network.  No env overrides — there is no environment
variable that can force sync status to SYNCED.
=================================================================
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

PROVIDER_VERSION = "1.0.0"

# ── sync statuses (stable, machine-readable) ───────────────────────────
SYNC_UNVERIFIED = "UNVERIFIED"
SYNC_SYNCED = "SYNCED"
SYNC_STALE = "STALE"

# ── evidence policy constants (see module docstring) ───────────────────
DEFAULT_STALENESS_SECS = 120.0
DEFAULT_MAX_ABS_OFFSET_SECS = 86400.0      # 1 day — beyond = malformed
DEFAULT_MAX_OFFSET_JUMP_SECS = 5.0
MIN_SAMPLES_FOR_ESTIMATE = 3
MAX_SAMPLES = 64

_LOCK = threading.Lock()


def _utc_iso(ts: Optional[float]) -> Optional[str]:
    try:
        if ts is None:
            return None
        ts_f = float(ts)
        if not math.isfinite(ts_f) or ts_f <= 0:
            return None
        return datetime.fromtimestamp(ts_f, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


class BaseTimeProvider:
    """Interface shared by every provider (duck-typed)."""

    backend = "abstract"
    local_only = True

    # -- device clock (honestly local) ------------------------------------
    def local_utc(self) -> float:
        """Device wall clock, epoch seconds UTC. ALWAYS local — never
        presented as broker/source time."""
        return time.time()

    # -- monotonic elapsed time (wall-clock-step immune) ------------------
    def monotonic_now(self) -> float:
        """Monotonic clock reading (NOT wall time).  Use for elapsed
        math: elapsed = monotonic_now() - earlier_reading."""
        return time.monotonic()

    def elapsed_since(self, earlier_monotonic: float) -> Optional[float]:
        """Seconds elapsed since a earlier monotonic_now() reading.
        Wall-clock steps cannot corrupt this value."""
        try:
            e = float(earlier_monotonic)
        except (TypeError, ValueError):
            return None
        if not _finite(e):
            return None
        return max(0.0, time.monotonic() - e)

    # -- source-aligned estimate ------------------------------------------
    def now(self) -> float:
        """Best estimate of 'current time'.  LocalClockProvider: the
        device clock.  SourceTimeProvider: device clock + offset
        estimate ONLY while SYNCED; otherwise the device clock alone
        (honest: no evidence, no claim)."""
        return time.time()

    def offset_estimate(self) -> Optional[float]:
        """Local->source offset in seconds (source - local), or None
        when there is no believable evidence."""
        return None

    def uncertainty_secs(self) -> Optional[float]:
        """Conservative uncertainty of the offset estimate (max sample
        deviation from the median), or None without evidence."""
        return None

    def sync_status(self) -> str:
        """UNVERIFIED / SYNCED / STALE (STALE still means: not to be
        trusted — the timing gate treats it as unverified)."""
        return SYNC_UNVERIFIED

    def timing_verified(self) -> bool:
        """The Phase-1 Truth Layer timing gate input: True ONLY while
        sync status is SYNCED (fresh, consistent, enough samples)."""
        return self.sync_status() == SYNC_SYNCED

    # -- public description (secret-free) ----------------------------------
    def describe(self) -> Dict[str, Any]:
        raise NotImplementedError


class LocalClockProvider(BaseTimeProvider):
    """The honest provider for backends with NO server-time mechanism
    (the default pyquotex path).  Everything local; sync UNVERIFIED
    forever; never claims broker time."""

    backend = "local_clock"

    def sync_status(self) -> str:
        return SYNC_UNVERIFIED

    def describe(self) -> Dict[str, Any]:
        return {
            "provider_version": PROVIDER_VERSION,
            "backend": self.backend,
            "local_only": True,
            "sync_status": SYNC_UNVERIFIED,
            "offset_secs": None,
            "uncertainty_secs": None,
            "staleness_window_secs": None,
            "seconds_since_last_sample": None,
            "sample_count": 0,
            "last_sync_utc": None,
            "samples_accepted": 0,
            "samples_rejected_malformed": 0,
            "samples_rejected_out_of_order": 0,
            "samples_rejected_duplicate": 0,
            "offset_jump_resets": 0,
            "reconnect_resets": 0,
            "last_offset_jump_utc": None,
            "note": (
                "Local clock only — this backend has no source-time "
                "mechanism.  Times shown are device UTC, never broker "
                "time.  Timing gate: UNVERIFIED (fail-closed)."),
        }


class SourceTimeProvider(BaseTimeProvider):
    """Evidence-based provider over measured source-time samples.

    The only production feeder is the raw-WS `timesync` frame handler
    (quotex_ws.py) — an UNAUTHORIZED data source.  Timing evidence and
    source authorization are SEPARATE concerns: SYNCED here says the
    broker clock is measurable; it says NOTHING about permission to
    trade on that data (the Phase-1 contract keeps that gate).
    """

    backend = "raw_ws_source_time"
    local_only = False  # source time IS measurable on this backend

    def __init__(self,
                 staleness_secs: float = DEFAULT_STALENESS_SECS,
                 max_abs_offset_secs: float = DEFAULT_MAX_ABS_OFFSET_SECS,
                 max_offset_jump_secs: float = DEFAULT_MAX_OFFSET_JUMP_SECS):
        self._staleness = float(staleness_secs)
        self._max_abs = float(max_abs_offset_secs)
        self._max_jump = float(max_offset_jump_secs)
        # (server_ts, local_wall_at_receipt, monotonic_at_receipt)
        self._samples: Deque[Tuple[float, float, float]] = deque(
            maxlen=MAX_SAMPLES)
        self._last_server_ts: Optional[float] = None
        # audit counters (cumulative; survive resets)
        self._n_accepted = 0
        self._n_malformed = 0
        self._n_out_of_order = 0
        self._n_duplicate = 0
        self._n_jump_resets = 0
        self._n_reconnect_resets = 0
        self._last_jump_utc: Optional[str] = None

    # ── sample ingestion (fail-closed) ──────────────────────────────────
    def note_sample(self, server_ts: Any,
                    recv_monotonic: Optional[float] = None) -> Dict[str, Any]:
        """Record ONE measured source-time sample.

        Returns an audit dict {"action": "accepted"|"rejected", "reason"}.
        Malformed kinds rejected (each counted):
          None, bool, non-numeric, NaN, +inf, -inf, <= 0, and
          |offset| > max_abs_offset (absurd — 1 day of skew is a broken
          frame, not evidence).
        Out-of-order (server_ts < last accepted) and EXACT duplicates
        (server_ts == last accepted) are rejected and counted
        separately.  An offset JUMP larger than max_offset_jump_secs
        resets the evidence (re-verification from scratch).
        """
        with _LOCK:
            # -- malformed battery ----------------------------------------
            if server_ts is None:
                return self._reject("malformed", "none")
            if isinstance(server_ts, bool):
                return self._reject("malformed", "bool")
            if isinstance(server_ts, (str, bytes, dict, list, tuple)):
                return self._reject("malformed", "non_numeric")
            if not _finite(server_ts):
                return self._reject("malformed", "non_finite")
            ts = float(server_ts)
            if ts <= 0:
                return self._reject("malformed", "non_positive")
            local_wall = time.time()
            if abs(ts - local_wall) > self._max_abs:
                return self._reject("malformed", "absurd_offset")

            # -- ordering --------------------------------------------------
            if self._last_server_ts is not None:
                if ts == self._last_server_ts:
                    self._n_duplicate += 1
                    return self._reject("duplicate", "duplicate_timestamp")
                if ts < self._last_server_ts:
                    self._n_out_of_order += 1
                    return self._reject("out_of_order", "server_ts went "
                                        "backwards")

            # -- offset-jump detection ------------------------------------
            if self._samples:
                prev_off = (self._samples[-1][0] - self._samples[-1][1])
                cur_off = ts - local_wall
                if abs(cur_off - prev_off) > self._max_jump:
                    # Evidence RESET: never blend a big step into the
                    # estimate.  The new sample starts re-verification.
                    self._samples.clear()
                    self._n_jump_resets += 1
                    self._last_jump_utc = _utc_iso(local_wall)
                    self._last_server_ts = ts
                    self._samples.append((ts, local_wall,
                                          recv_monotonic
                                          if _finite(recv_monotonic)
                                          else time.monotonic()))
                    self._n_accepted += 1
                    return {"action": "accepted", "reason":
                            "offset_jump_reset_evidence"}

            self._last_server_ts = ts
            self._samples.append((ts, local_wall,
                                  recv_monotonic if _finite(recv_monotonic)
                                  else time.monotonic()))
            self._n_accepted += 1
            return {"action": "accepted", "reason": ""}

    def _reject(self, kind: str, reason: str) -> Dict[str, Any]:
        if kind == "malformed":
            self._n_malformed += 1
        return {"action": "rejected", "reason": f"{kind}: {reason}"}

    # ── reconnect invalidation ──────────────────────────────────────────
    def on_reconnect(self) -> Dict[str, Any]:
        """A (re)connection invalidates ALL sync evidence: the new
        connection's clock relationship must be re-established by new
        measured samples before SYNCED can be claimed again."""
        with _LOCK:
            had = len(self._samples)
            self._samples.clear()
            self._last_server_ts = None
            self._n_reconnect_resets += 1
            return {"action": "evidence_cleared", "samples_dropped": had}

    # ── status / estimates ──────────────────────────────────────────────
    def sync_status(self) -> str:
        with _LOCK:
            if len(self._samples) < MIN_SAMPLES_FOR_ESTIMATE:
                return SYNC_UNVERIFIED
            age = time.monotonic() - self._samples[-1][2]
            if age > self._staleness:
                return SYNC_STALE
            return SYNC_SYNCED

    def offset_estimate(self) -> Optional[float]:
        with _LOCK:
            if len(self._samples) < MIN_SAMPLES_FOR_ESTIMATE:
                return None
            offs = sorted(s[0] - s[1] for s in self._samples)
            n = len(offs)
            mid = n // 2
            med = offs[mid] if n % 2 else (offs[mid - 1] + offs[mid]) / 2.0
            return round(med, 6)

    def uncertainty_secs(self) -> Optional[float]:
        """Conservative: max |offset_i - median| across the sample set."""
        with _LOCK:
            if len(self._samples) < MIN_SAMPLES_FOR_ESTIMATE:
                return None
            offs = [s[0] - s[1] for s in self._samples]
            srt = sorted(offs)
            n = len(srt)
            mid = n // 2
            med = srt[mid] if n % 2 else (srt[mid - 1] + srt[mid]) / 2.0
            return round(max(abs(o - med) for o in offs), 6)

    def now(self) -> float:
        """Source-aligned estimate while SYNCED; device clock otherwise
        (no evidence -> no claim)."""
        if self.sync_status() == SYNC_SYNCED:
            off = self.offset_estimate()
            if off is not None:
                return time.time() + off
        return time.time()

    # ── description (secret-free, complete) ─────────────────────────────
    def describe(self) -> Dict[str, Any]:
        with _LOCK:
            n = len(self._samples)
            last_mono = self._samples[-1][2] if n else None
            last_srv = self._samples[-1][0] if n else None
        status = self.sync_status()
        return {
            "provider_version": PROVIDER_VERSION,
            "backend": self.backend,
            "local_only": False,
            "sync_status": status,
            "offset_secs": (self.offset_estimate()
                            if status != SYNC_UNVERIFIED else None),
            "uncertainty_secs": (self.uncertainty_secs()
                                 if status != SYNC_UNVERIFIED else None),
            "staleness_window_secs": self._staleness,
            "seconds_since_last_sample": (
                round(time.monotonic() - last_mono, 1)
                if last_mono is not None else None),
            "sample_count": n,
            "min_samples_for_estimate": MIN_SAMPLES_FOR_ESTIMATE,
            "last_sync_utc": _utc_iso(last_srv),
            "samples_accepted": self._n_accepted,
            "samples_rejected_malformed": self._n_malformed,
            "samples_rejected_out_of_order": self._n_out_of_order,
            "samples_rejected_duplicate": self._n_duplicate,
            "offset_jump_resets": self._n_jump_resets,
            "reconnect_resets": self._n_reconnect_resets,
            "last_offset_jump_utc": self._last_jump_utc,
            "note": (
                "Timing sync != source authorization: SYNCED means the "
                "broker clock is measurable on the raw-WS backend — it "
                "says NOTHING about trading permission.  The Phase-1 "
                "Truth Layer independently gates on data-source "
                "authorization and model validation."),
        }

    # ── test/recovery helper (NOT used in production paths) ─────────────
    def reset_for_tests(self) -> None:
        """Clear all evidence AND counters.  Test isolation only."""
        with _LOCK:
            self._samples.clear()
            self._last_server_ts = None
            self._n_accepted = 0
            self._n_malformed = 0
            self._n_out_of_order = 0
            self._n_duplicate = 0
            self._n_jump_resets = 0
            self._n_reconnect_resets = 0
            self._last_jump_utc = None


# ── module singleton ───────────────────────────────────────────────────
# The application-wide provider.  Only quotex_ws (raw-WS backend) feeds
# it samples; with no samples it stays UNVERIFIED (which is exactly the
# honest state for the default pyquotex backend — no fake evidence).
_PROVIDER: Optional[SourceTimeProvider] = None
_PROVIDER_LOCK = threading.Lock()


def get_time_provider() -> SourceTimeProvider:
    """The shared application time provider (source-time evidence)."""
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            _PROVIDER = SourceTimeProvider()
        return _PROVIDER
