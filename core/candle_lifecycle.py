"""PHASE-2A CANDLE LIFECYCLE — settlement state machine (2026-10, rebuild).
=====================================================================
Governing principles (Phase 2A directive):

  * Candles get an UNAMBIGUOUS IDENTITY and an EXPLICIT lifecycle.
  * Late ticks can NEVER silently mutate finalized OHLC — they are
    accepted into a CORRECTION LEDGER only (additive rows).
  * Corrections are auditable (original + corrected + reason + time).
  * Research and production ledgers stay SEPARATE: the production
    ledger (signal_log, written by the Phase-0 grader) is never
    rewritten by lifecycle events; the research ledger (this module's
    settlement of signal_provenance emissions) records outcomes from
    the FROZEN candle, never from corrected values.

Candle identity
---------------
    (asset, period, period_start)
      period_start = floor(ts / period) * period   [epoch seconds]
Identity is stable across restarts: it derives only from the tick
timestamp and the stream period.  A tick EXACTLY at a period boundary
belongs to the NEW candle (right edge), matching feed's
`_floor_to_period` bucketing.

Lifecycle states (persisted in db.candle_settlement.state)
-----------------------------------------------------------
    open                    running candle, ticks accumulating
                            (in-memory only — no row yet)
    pending_finalization    close initiated; a crash in this window
                            leaves a recoverable row (no OHLC frozen)
    finalized               OHLC FROZEN (immutable from here on)
    settled                 research outcome recorded from the frozen
                            values + the emission provenance row

Allowed transitions (everything else is rejected + audited):
    (none)            -> pending_finalization   (begin_finalization)
    pending_finalization -> finalized           (finalize)
    finalized         -> settled                (settle)
    settled           -> settled                (idempotent retry only;
                                                 outcome columns NEVER
                                                 rewritten)
    finalized/settled -> (no OHLC rewrite, ever) a second finalize with
                         DIFFERENT OHLC = CONFLICT: rejected + audited
                         into candle_corrections.

Late-tick policy
----------------
    candle not finalized (open/pending/unknown) -> tick is NOT late:
        incorporate normally (feed's live path owns it).
    candle finalized/settled                    -> CORRECTION LEDGER
        row only: original OHLC/outcome preserved verbatim, the late
        evidence recorded as corrected_* values.  The frozen record is
        never edited; the production ledger is never touched.

Concurrency: db operations run single-transaction with conditional
UPDATE ... WHERE state=... guards.  The application is a SINGLE
process (uvicorn event loop + helper threads); cross-process writers
are not supported and the risk is documented in
docs/PHASE2A_SETTLEMENT.md.  No env overrides; no secrets.
=====================================================================
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import db as _db

LIFECYCLE_VERSION = "1.0.0"

STATE_OPEN = "open"
STATE_PENDING = "pending_finalization"
STATE_FINALIZED = "finalized"
STATE_SETTLED = "settled"

# Float comparison epsilon for OHLC conflict detection.  Values written
# by the same close path are bitwise-identical; anything beyond 1e-9
# drift on ANY of o/h/l/c is a genuinely different finalization.
_EPS = 1e-9

# Ledger labels (research vs production separation)
LEDGER_RESEARCH = "research"
LEDGER_PRODUCTION = "production"

# Late-tick correction window: a late tick older than this (relative to
# the candle's own close) is still recorded in the ledger (nothing is
# dropped — audits want the full picture), but flagged stale so
# downstream consumers can exclude ancient replays.
LATE_TICK_STALE_AFTER_SECS = 300.0


def period_start_for(ts: Any, period: Any) -> Optional[float]:
    """Canonical candle-identity bucket for a tick timestamp.

    A tick EXACTLY at a boundary (ts % period == 0) belongs to the NEW
    candle opening at that boundary (right-edge), matching
    feed._floor_to_period.  Invalid inputs -> None (fail-closed).
    """
    try:
        ts_f = float(ts)
        p = int(period)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    if ts_f != ts_f or ts_f in (float("inf"), float("-inf")):  # NaN/inf
        return None
    if ts_f < 0:
        return None
    return float(int(ts_f // p) * p)


def candle_key(asset: str, period: int, period_start: float) -> tuple:
    """The stable identity tuple."""
    return (str(asset), int(period), float(period_start))


def _utc_iso(ts: Optional[float]) -> Optional[str]:
    try:
        if ts is None:
            return None
        ts_f = float(ts)
        if ts_f <= 0:
            return None
        return datetime.fromtimestamp(ts_f, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def outcome_for(open_: Optional[float], close: Optional[float]) -> Optional[str]:
    """Candle direction from FROZEN values: up / down / flat."""
    try:
        o, c = float(open_), float(close)
    except (TypeError, ValueError):
        return None
    if o is None or c is None:
        return None
    if c > o:
        return "up"
    if c < o:
        return "down"
    return "flat"


def research_correct_for(direction: Optional[str],
                         outcome: Optional[str]) -> Optional[int]:
    """Was the research (non-actionable) direction right?
    CALL<->up, PUT<->down; flat or missing direction -> None."""
    if direction not in ("CALL", "PUT") or outcome not in ("up", "down"):
        return None
    return 1 if ((direction == "CALL" and outcome == "up")
                 or (direction == "PUT" and outcome == "down")) else 0


class CandleLifecycle:
    """The settlement state machine (see module docstring)."""

    # ── transition 1: begin ─────────────────────────────────────────────
    def begin_finalization(self, asset: str, period: int,
                           candle: Dict[str, Any],
                           source: str = "unknown") -> Dict[str, Any]:
        """Mark finalization BEGUN for a candle (idempotent insert of a
        pending_finalization row).  Called at the top of the close path;
        a crash between here and finalize() leaves this recoverable
        row.  Never raises on bad input — returns an error action."""
        try:
            ps = period_start_for(candle.get("time"), period)
            if not asset or int(period) <= 0 or ps is None:
                return {"action": "skipped", "reason": "invalid identity"}
            return _db.settlement_begin(str(asset), int(period), ps,
                                        str(source))
        except Exception as exc:
            return {"action": "error", "reason":
                    f"{type(exc).__name__}: {exc}"}

    # ── transitions 2+3: finalize + settle ──────────────────────────────
    def finalize_and_settle(self, asset: str, period: int,
                            candle: Dict[str, Any],
                            production_accuracy: Optional[str] = None,
                            source: str = "unknown",
                            tick_count: int = 0) -> Dict[str, Any]:
        """Freeze the finalized OHLC and record the research settlement
        (idempotent; conflict-safe).  Optionally sweeps older unsettled
        rows for the same asset (crash recovery)."""
        try:
            ps = period_start_for(candle.get("time"), period)
            if not asset or int(period) <= 0 or ps is None:
                return {"action": "skipped", "reason": "invalid identity"}
            fin = _db.settlement_finalize(
                str(asset), int(period), ps,
                candle.get("open"), candle.get("high"),
                candle.get("low"), candle.get("close"),
                int(tick_count or 0), str(source))
            st = self.settle(asset, period, ps,
                             production_accuracy=production_accuracy)
            # Recovery sweep: settle any older finalized-not-settled rows
            # for this asset (e.g. a crash between finalize and settle of
            # a previous candle).  Pending rows cannot be settled (no
            # frozen OHLC) — they stay recoverable.
            swept = self.settle_pending(asset=asset, period=int(period),
                                        limit=16)
            return {"action": fin.get("action", "error"),
                    "finalize": fin, "settle": st,
                    "recovered": swept.get("settled", 0)}
        except Exception as exc:
            return {"action": "error", "reason":
                    f"{type(exc).__name__}: {exc}"}

    # ── transition 3 alone: settle (idempotent, recovery-safe) ──────────
    def settle(self, asset: str, period: int, period_start: float,
               production_accuracy: Optional[str] = None) -> Dict[str, Any]:
        """Settle ONE finalized candle: research outcome computed from
        the FROZEN row (never from corrected values) + the emission
        provenance join (signal_provenance.target_candle_open_epoch ==
        period_start).  Idempotent: a settled row's outcome columns are
        never rewritten; retries only bump settle_attempts."""
        try:
            row = _db.settlement_get(asset, period, period_start)
            if row is None:
                return {"action": "no_row"}
            if row.get("state") == STATE_PENDING:
                return {"action": "pending_finalization"}
            # frozen OHLC from the DB row (authoritative)
            prov = _db.provenance_for_target(asset, period, period_start)
            outcome = outcome_for(row.get("open"), row.get("close"))
            direction = (prov or {}).get("research_direction")
            return _db.settlement_settle(
                asset, period, period_start,
                signal_id=(prov or {}).get("signal_id"),
                research_direction=direction,
                research_strategy=(prov or {}).get("research_strategy"),
                outcome=outcome,
                research_correct=research_correct_for(direction, outcome),
                production_accuracy=production_accuracy)
        except Exception as exc:
            return {"action": "error", "reason":
                    f"{type(exc).__name__}: {exc}"}

    # ── recovery sweep ──────────────────────────────────────────────────
    def settle_pending(self, asset: Optional[str] = None,
                       period: Optional[int] = None,
                       limit: int = 100) -> Dict[str, Any]:
        """Settle every FINALIZED-not-yet-settled row (optionally for one
        asset).  PENDING rows are reported but left alone (their OHLC
        was never frozen — the next close of that candle recovers them).
        Used after restarts and opportunistically after each close."""
        out = {"settled": 0, "left_pending": 0, "actions": []}
        try:
            rows = _db.settlement_unsettled(asset, period, int(limit))
            for row in rows:
                if row.get("state") == STATE_FINALIZED:
                    st = self.settle(row.get("asset"), row.get("period"),
                                     row.get("period_start"))
                    if st.get("action") in ("settled", "already_settled"):
                        out["settled"] += 1
                    out["actions"].append(st)
                else:
                    out["left_pending"] += 1
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    # ── late ticks: correction ledger ONLY ──────────────────────────────
    def note_late_tick(self, asset: str, period: int, tick_epoch: Any,
                       tick_price: Any) -> Dict[str, Any]:
        """Record a late tick for an ALREADY-FINALIZED candle.

        If the target candle is not finalized (open / pending / never
        seen), the tick is NOT late for the lifecycle — the feed's live
        path owns it ({'action': 'not_finalized'}).  If it IS finalized
        or settled, a correction row is added: the ORIGINAL frozen
        OHLC/outcome is preserved verbatim; the late evidence is
        recorded as the implied corrected values.  The frozen record,
        the production ledger (signal_log) and the research settlement
        are never modified.  Idempotent on duplicate ticks (same ts +
        price) via the table's UNIQUE key."""
        try:
            ps = period_start_for(tick_epoch, period)
            if ps is None:
                return {"action": "rejected", "reason": "invalid tick ts"}
            price = float(tick_price)
            if price != price or price in (float("inf"), float("-inf")):
                return {"action": "rejected", "reason": "invalid price"}
            row = _db.settlement_get(asset, period, ps)
            if row is None or row.get("state") in (STATE_OPEN,
                                                   STATE_PENDING):
                return {"action": "not_finalized",
                        "period_start": ps,
                        "state": (row or {}).get("state")}
            # candle is finalized/settled -> correction ledger only
            orig = {"open": row.get("open"), "high": row.get("high"),
                    "low": row.get("low"), "close": row.get("close")}
            corrected = {
                "open": row.get("open"),
                "high": max(row.get("high") or price, price),
                "low": min(row.get("low") if row.get("low") is not None
                           else price, price),
                "close": price}
            orig_outcome = outcome_for(orig["open"], orig["close"])
            corr_outcome = outcome_for(corrected["open"], corrected["close"])
            flipped = int(orig_outcome != corr_outcome) if (
                orig_outcome is not None and corr_outcome is not None
            ) else None
            age = (ps + period) - float(tick_epoch)
            stale = bool(age > LATE_TICK_STALE_AFTER_SECS)
            inserted = _db.correction_insert(
                asset, period, ps,
                reason="late_tick",
                original_ohlc=json.dumps(orig, separators=(",", ":")),
                corrected_ohlc=json.dumps(corrected, separators=(",", ":")),
                late_tick_epoch=float(tick_epoch),
                late_tick_price=price,
                original_outcome=orig_outcome,
                corrected_outcome=corr_outcome,
                outcome_flipped=flipped,
                ledger=LEDGER_RESEARCH,
                stale=stale)
            return {"action": "correction_recorded" if inserted
                    else "duplicate_tick",
                    "period_start": ps,
                    "state": row.get("state"),
                    "outcome_flipped": flipped,
                    "stale_replay": stale,
                    "original_outcome": orig_outcome,
                    "corrected_outcome": corr_outcome}
        except Exception as exc:
            return {"action": "error", "reason":
                    f"{type(exc).__name__}: {exc}"}

    # ── read-only helpers ───────────────────────────────────────────────
    def state_of(self, asset: str, period: int,
                 period_start: float) -> Optional[str]:
        try:
            row = _db.settlement_get(asset, period, period_start)
            return row.get("state") if row else None
        except Exception:
            return None

    def corrections_for(self, asset: str, period: int,
                        period_start: float) -> List[Dict[str, Any]]:
        try:
            return _db.corrections_for(asset, period, period_start)
        except Exception:
            return []

    def describe(self) -> Dict[str, Any]:
        """Public, secret-free snapshot for /api/settlement-integrity."""
        try:
            counts = _db.settlement_counts()
        except Exception as exc:
            counts = {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "lifecycle_version": LIFECYCLE_VERSION,
            "states": [STATE_OPEN, STATE_PENDING, STATE_FINALIZED,
                       STATE_SETTLED],
            "state_counts": counts.get("state_counts", {}),
            "corrections": counts.get("corrections", {}),
            "policies": {
                "candle_identity": "asset + period + period_start "
                                   "(floor(ts/period)*period, right-edge "
                                   "boundaries)",
                "late_tick": "correction ledger only — finalized OHLC is "
                             "immutable; originals never rewritten",
                "conflicting_finalization": "rejected + audited into "
                                            "candle_corrections",
                "settlement_retry": "idempotent — outcome columns never "
                                    "rewritten; attempts counted",
                "research_vs_production": "research settlement from "
                                          "signal_provenance (frozen "
                                          "values); production ledger "
                                          "(signal_log) never touched by "
                                          "corrections",
                "retention": "count-based prune (newest per pair, same "
                             "policy as signal_log); never the 60-min "
                             "time purge",
            },
            "note": ("Settlement integrity is bookkeeping only — it does "
                     "not make any signal actionable. The Phase-1 Truth "
                     "Layer gates every emission independently."),
        }


# ── module singleton ───────────────────────────────────────────────────
_LIFECYCLE: Optional[CandleLifecycle] = None


def get_lifecycle() -> CandleLifecycle:
    global _LIFECYCLE
    if _LIFECYCLE is None:
        _LIFECYCLE = CandleLifecycle()
    return _LIFECYCLE
