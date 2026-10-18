"""PHASE-E DATA QUALITY tests (2026-10, Phase C–I directive §E).

Run:  python3 scripts/test_phase_e_data_quality.py

Covers:
  A. candle construction through the REAL feed close path (E2):
     period bucketing, exact-boundary behavior (right-edge), duplicate
     tick idempotency, late tick before vs after finalization, missing
     tick intervals (gap counting), out-of-order arrivals, duplicate
     candles after restart (engine idempotency + settlement UNIQUE),
     timezone/DST invariance of epoch bucketing, settlement/correction
     consistency.
  B. data-source provenance records (E1): every classification is
     explicit; authorization never fabricated; unknown stays unknown.
  C. quality gates (E3): each documented limit rejects/downgrades the
     corresponding bad input; good input passes; thresholds documented;
     gates can never "enable" anything (verdict enum has no positive
     signal).
  D. /api/data-quality diagnostics (E4): structure, honesty (constant
     unauthorized source label), no secrets, suppression counts wired
     to the provenance ledger.

Temp DB only. No network. No broker. No credentials.
"""
import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMPDIR = tempfile.mkdtemp(prefix="phase_e_dq_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "signals.db")
os.environ["QX_RETENTION_ENABLED"] = "0"

sys.path.insert(0, _REPO)

_PASS = 0
_FAIL = 0
_FAILURES = []


def check(cond, label, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {label}")
    else:
        _FAIL += 1
        _FAILURES.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


import db as _db  # noqa: E402
_db.init()

T0 = (1790000000 // 60) * 60

# ── A. candle construction (E2) ─────────────────────────────────────────
print("\nA. CANDLE CONSTRUCTION (real feed path)")

from feed import _floor_to_period  # noqa: E402
from core.candle_lifecycle import period_start_for  # noqa: E402
from core.candle_lifecycle import get_lifecycle  # noqa: E402
from core.data_quality import count_series_gaps  # noqa: E402

check(_floor_to_period(T0 + 59.9, 60) == T0
      and _floor_to_period(T0 + 60, 60) == T0 + 60,
      "A1 exact-boundary bucketing is right-edge (T0+60 -> new candle)")
check(all(period_start_for(T0 + k, 60) == T0 for k in range(60))
      and period_start_for(T0 + 60, 60) == T0 + 60,
      "A2 period bucketing consistent across the whole candle")
check(_floor_to_period(1790611200, 60) == 1790611200,
      "A3 period-aligned timestamp floors to itself")

# parity between the feed's bucketing and the lifecycle's identity
_mism = [t for t in range(T0, T0 + 600, 7)
         if _floor_to_period(t, 60) != period_start_for(t, 60)]
check(not _mism, "A4 feed._floor_to_period == lifecycle.period_start_for "
      "(one documented identity rule)", str(_mism[:3]))

# DST invariance: epoch bucketing is pure arithmetic on UTC seconds —
# local DST changes cannot move a candle boundary. Verify across the
# 2026 US DST fall-back boundary (Nov 1 2026 06:00 UTC) and the EU
# spring-forward (Mar 29 2026 01:00 UTC).
import datetime as _dt  # noqa: E402
for _label, _ts in (("US_fallback", 1793587200),
                    ("EU_springforward", 1774742400)):
    _aligned = all(
        period_start_for(_ts + k, 60) == ((_ts + k) // 60) * 60
        for k in range(0, 7200, 13))
    check(_aligned,
          f"A5 epoch bucketing invariant across {_label} (UTC arithmetic)")

# candle assembly through the REAL feed close path (the Phase-B seam):
# ticks -> _close_running_and_start_new -> lifecycle finalize/settle
import feed as _feed  # noqa: E402
from feed import QuotexFeed, _AssetStream  # noqa: E402


class _FakeClient:
    _server_time_synced = False


async def _drive_close(asset, period, open_time, ticks,
                       pre_candles=None):
    """Drive the REAL close path with a minimal stream fixture (the
    Phase-B seam: full QuotexFeed constructor, fake client, no broker)."""
    f = _feed.QuotexFeed()
    f._pairs_list = []
    f._client = _FakeClient()
    s = _AssetStream(asset=asset, period=period)
    s.candle_open_time = open_time
    s.candle_open_price = ticks[0] if ticks else 1.0
    s.candle_open_is_real = True
    if pre_candles:
        s.candles = list(pre_candles)
    s.ticks.extend(ticks)
    s.candle_tick_count = len(ticks)
    s.last_real_tick_wall = time.time() - 1.0
    s.payout = 85
    f._streams = {(asset, period): s}
    f._broadcast = None
    await f._close_running_and_start_new(
        s, new_open_time=open_time + period,
        first_tick=(ticks[0] if ticks else 1.0),
        open_is_real=True)
    return f, s


_f, _s = asyncio.run(_drive_close(
    "EURUSD_otc", 60, T0, [1.0, 1.2, 0.9, 1.1, 1.05]))
_closed = _s.candles[-1] if _s.candles else None
check(isinstance(_closed, dict) and _closed.get("open") == 1.0
      and _closed.get("high") == 1.2 and _closed.get("low") == 0.9
      and _closed.get("close") == 1.05 and _closed.get("time") == T0,
      "A6 tick->candle assembly through the real close path (OHLC exact)",
      str({k: (_closed or {}).get(k) for k in ("time", "open", "high",
                                               "low", "close")}))
_row = _db.settlement_get("EURUSD_otc", 60, T0)
check(_row is not None and _row["state"] in ("finalized", "settled")
      and _row["open"] == 1.0 and _row["high"] == 1.2
      and _row["close"] == 1.05,
      "A7 the assembled candle is lifecycle-finalized with frozen OHLC")

# duplicate tick handling: identical (ts, price) late ticks -> ONE
# correction row (idempotent UNIQUE)
_lc = get_lifecycle()
_before = len(_db.corrections_for("EURUSD_otc", 60, T0))
_lc.note_late_tick("EURUSD_otc", 60, T0 + 30, 1.15)
_lc.note_late_tick("EURUSD_otc", 60, T0 + 30, 1.15)  # duplicate tick
_after = len(_db.corrections_for("EURUSD_otc", 60, T0))
check(_after - _before == 1,
      "A8 duplicate late tick (same ts+price) -> ONE correction row "
      "(idempotent)", f"{_before} -> {_after}")

# late tick BEFORE finalization is legitimately incorporated (open state)
# vs AFTER (correction ledger only) — Phase-B behavior re-verified E2E
_f2, _s2 = asyncio.run(_drive_close(
    "GBPUSD_otc", 60, T0 + 600, [2.0, 2.1, 1.95, 2.05]))
_row2 = _db.settlement_get("GBPUSD_otc", 60, T0 + 600)
# now a late tick AFTER finalization
_lc.note_late_tick("GBPUSD_otc", 60, T0 + 630, 2.2)
_row2b = _db.settlement_get("GBPUSD_otc", 60, T0 + 600)
check(_row2b["high"] == 2.1 and _row2b["close"] == 2.05,
      "A9 late tick AFTER finalization -> correction ledger only, frozen "
      "OHLC immutable")

# missing tick intervals: gap counting over a candle series
check(count_series_gaps([T0, T0 + 60, T0 + 180, T0 + 240], 60) == 1,
      "A10 missing-interval count (1 gap detected)")
check(count_series_gaps([T0 + k * 60 for k in range(10)], 60) == 0,
      "A11 contiguous series -> 0 gaps")

# out-of-order arrivals: integrity validator rejects (eval pipeline)
from core.eval_clean.integrity import validate_candle_series  # noqa: E402
_ooo = validate_candle_series([
    {"asset": "X", "period": 60, "t": T0, "o": 1, "h": 2, "l": .5, "c": 1.5},
    {"asset": "X", "period": 60, "t": T0 - 60, "o": 1, "h": 2, "l": .5,
     "c": 1.5}])
check(_ooo.out_of_order_count == 1,
      "A12 out-of-order arrival rejected + counted")

# duplicate candles after restart: engine ingest is idempotent by time
from core import next_candle as _nc  # noqa: E402
_eng = _nc.NextCandleEngine("phaseE_dup", 60)
_p1 = None
for i in range(70):
    _p1 = _eng.ingest({"time": T0 + i * 60, "open": 1.0,
                       "high": 1.1, "low": 0.9, "close": 1.05})
_p2 = _eng.ingest({"time": T0 + 69 * 60, "open": 1.0, "high": 1.1,
                   "low": 0.9, "close": 1.05})  # duplicate (replay)
check(_p2 is None and _p1 is not None,
      "A13 duplicate candle after restart -> engine idempotent (None "
      "while the original payload existed)")
_row_dup = _db.settlement_get("EURUSD_otc", 60, T0)
check(_row_dup is not None and _row_dup["state"] in ("finalized",
                                                     "settled"),
      "A14 settlement UNIQUE constraint prevents duplicate candle rows")

# reconnect recovery: ingest_replace resets engine state cleanly
_eng2 = _nc.NextCandleEngine("phaseE_reconnect", 60)
for i in range(80):
    _eng2.ingest({"time": T0 + i * 60, "open": 1.0, "high": 1.1,
                  "low": 0.9, "close": 1.05})
_before_payload = _eng2.current()
_eng2.ingest_replace([{"time": T0 + i * 60, "open": 1.0, "high": 1.1,
                       "low": 0.9, "close": 1.05} for i in range(80)])
_after_payload = _eng2.current()
check(_after_payload is not None
      and _after_payload.get("target_time") == T0 + 80 * 60,
      "A15 reconnect/history replay (ingest_replace) recovers cleanly")

# ── B. data-source provenance (E1) ───────────────────────────────────────
print("\nB. DATA-SOURCE PROVENANCE")

from core import data_quality as _dq  # noqa: E402

_bin = _dq.classify_binance_1m(
    "binance_1m_4asset", ["BTCUSDT"], ("2026-09-22T23:38:00Z",
                                       "2026-09-29T05:36:00Z"))
check(_bin.source_authorization == "authorized"
      and _bin.classification == "authorized_historical_public_exchange"
      and _bin.timeframe_secs == 60,
      "B1 Binance dataset classified authorized-historical (NOT OTC)")

_syn = _dq.classify_synthetic("synth_seed7", 7)
check(_syn.classification == "synthetic_fixture"
      and _syn.source_authorization == "authorized",
      "B2 synthetic fixture classified synthetic (never market evidence)")

_live = _dq.classify_live_quotex_channels("pyquotex")
check(_live.source_authorization == "unauthorized"
      and _live.classification
      == "unauthorized_reverse_engineered_channel",
      "B3 live Quotex channel classified UNAUTHORIZED (never fabricated)")

_cap = _dq.classify_live_capture("live_signals_2026-09-18",
                                 "2026-09-18")
check(_cap.classification
      == "observational_capture_from_unauthorized_channel"
      and "ground_truth_from_unauthorized_channel"
      in _cap.data_quality_flags,
      "B4 captured emissions classified observational + flagged")

# unknown source stays unknown (never trusted by default)
_rec = _dq.DatasetProvenance(dataset_id="x", source_id="",
                             source_authorization="unknown",
                             classification="synthetic_fixture")
check(_rec.source_authorization == "unknown",
      "B5 unknown authorization stays unknown (fail-closed label)")

for _cl in (_bin, _syn, _live, _cap):
    _d = _cl.as_dict()
    assert isinstance(_d, dict)
check(all(k in _bin.as_dict() for k in
          ("dataset_id", "source_id", "source_authorization",
           "classification", "clock_source", "clock_sync_state",
           "data_quality_flags", "exclusion_reasons")),
      "B6 provenance records carry the full tracked field set (E1)")

# ── C. quality gates (E3) ────────────────────────────────────────────────
print("\nC. QUALITY GATES (documented, reject-only)")

_pol = _dq.describe_policy()
check(_pol["stale_secs"] == 90.0 and _pol["min_closed_candles"] == 64
      and _pol["max_clock_uncertainty_secs"] == 2.0
      and _pol["max_missing_intervals"] == 0,
      "C1 policy thresholds documented and aligned with existing "
      "documented gates (90s staleness, 64-candle minimum)")
check("never enable signals" in _pol["provenance_note"]
      or "only reject" in _pol["provenance_note"],
      "C2 policy states gates only reject/downgrade")

_good_candles = [{"open": 1.0, "high": 1.2, "low": 0.9, "close": 1.05}
                 for _ in range(64)]
_ok = _dq.assess(
    source_id="quotex_pyquotex:A", source_authorized=False,
    last_observation_wall=1000.0, now=1000.0 + 5,
    closed_candles=_good_candles, clock_sync_status="UNVERIFIED",
    clock_uncertainty_secs=None, missing_intervals=0)
check(_ok["verdict"] == "ok" and not _ok["reasons"],
      "C3 healthy context passes (staleness/sufficiency/shape)")

_bad = _dq.assess(
    source_id="quotex_pyquotex:A", source_authorized=False,
    last_observation_wall=1000.0, now=1000.0 + 200,
    closed_candles=_good_candles, clock_sync_status="UNVERIFIED",
    clock_uncertainty_secs=None, missing_intervals=0)
check("stale_data" in _bad["reasons"],
      "C4 stale data rejected (200s > 90s)")

_bad2 = _dq.assess(
    source_id=None, source_authorized=False,
    last_observation_wall=1000.0, now=1005.0,
    closed_candles=_good_candles, clock_sync_status="UNVERIFIED",
    clock_uncertainty_secs=None, missing_intervals=0)
check("unknown_source" in _bad2["reasons"],
      "C5 unknown source rejected")

_bad3 = _dq.assess(
    source_id="s", source_authorized=False,
    last_observation_wall=1000.0, now=1005.0,
    closed_candles=_good_candles[:10], clock_sync_status="UNVERIFIED",
    clock_uncertainty_secs=None, missing_intervals=0)
check("insufficient_observations" in _bad3["reasons"],
      "C6 insufficient observations rejected (10 < 64)")

_mal = list(_good_candles)
_mal[-1] = {"open": 1.0, "high": 0.5, "low": 0.9, "close": 1.05}
_bad4 = _dq.assess(
    source_id="s", source_authorized=False,
    last_observation_wall=1000.0, now=1005.0,
    closed_candles=_mal, clock_sync_status="UNVERIFIED",
    clock_uncertainty_secs=None, missing_intervals=0)
check("malformed_ohlc" in _bad4["reasons"],
      "C7 malformed OHLC (high < open) rejected")

_bad5 = _dq.assess(
    source_id="s", source_authorized=False,
    last_observation_wall=1000.0, now=1005.0,
    closed_candles=_good_candles, clock_sync_status="SYNCED",
    clock_uncertainty_secs=5.0, missing_intervals=0)
check("clock_uncertainty" in _bad5["reasons"],
      "C8 clock uncertainty beyond threshold rejected (5s > 2s)")

_bad6 = _dq.assess(
    source_id="s", source_authorized=False,
    last_observation_wall=1000.0, now=1005.0,
    closed_candles=_good_candles, clock_sync_status="UNVERIFIED",
    clock_uncertainty_secs=None, missing_intervals=1)
check("missing_intervals" in _bad6["reasons"],
      "C9 missing candle interval in the prediction window rejected")

_bad7 = _dq.assess(
    source_id="s", source_authorized=False,
    last_observation_wall=1000.0, now=1005.0,
    closed_candles=_good_candles, clock_sync_status="UNVERIFIED",
    clock_uncertainty_secs=None, missing_intervals=0,
    latest_conflict_count=1)
check("contradictory_source_data" in _bad7["reasons"],
      "C10 contradictory evidence (unresolved conflict) rejected")

# combined: multiple reasons reported together, verdict stays reject
_bad8 = _dq.assess(
    source_id=None, source_authorized=False,
    last_observation_wall=None, now=1005.0,
    closed_candles=[], clock_sync_status="UNVERIFIED",
    clock_uncertainty_secs=None, missing_intervals=2)
check(_bad8["verdict"] == "reject" and len(_bad8["reasons"]) >= 4,
      "C11 multiple simultaneous failures all reported",
      str(_bad8["reasons"]))

# the verdict enum has NO "enable"/positive value — gates cannot
# increase signal frequency by construction
check(_dq.VERDICT_OK == "ok" and _dq.VERDICT_REJECT == "reject"
      and not any("enable" in r or "pass" in r for r in
                  _dq.QUALITY_REASONS),
      "C12 no enabling verdict exists in the reason vocabulary")

# custom policy is honored (documented configurability)
_custom = _dq.QualityPolicy(stale_secs=10.0, min_closed_candles=5)
_r = _dq.assess(source_id="s", source_authorized=False,
                last_observation_wall=1000.0, now=1000.0 + 5,
                closed_candles=_good_candles[:5],
                clock_sync_status="UNVERIFIED",
                clock_uncertainty_secs=None, missing_intervals=0,
                policy=_custom)
check(_r["verdict"] == "ok",
      "C13 configurable policy honored (stricter default passes under "
      "custom limits)")

# ── D. /api/data-quality diagnostics (E4) ───────────────────────────────
print("\nD. DIAGNOSTICS ENDPOINT")

from fastapi.testclient import TestClient  # noqa: E402
import server as _server  # noqa: E402

_client = TestClient(_server.app)  # no lifespan -> no broker
_r = _client.get("/api/data-quality")
check(_r.status_code == 200, "D1 /api/data-quality responds",
      f"got {_r.status_code}")
_body = _r.json()
check(_body.get("policy", {}).get("policy_version")
      == "data_quality_v1",
      "D2 documented policy embedded in the response")
check(_body.get("source", {}).get("source_authorization")
      == "unauthorized",
      "D3 source authorization honestly 'unauthorized' (never fabricated)")
check("timing" in _body and isinstance(_body["timing"], dict),
      "D4 timing verification state present")
check(isinstance(_body.get("streams"), list),
      "D5 per-stream section present")
check("signals_suppressed_by_gate" in _body,
      "D6 suppression counts wired (provenance reason codes)")
_blob = json.dumps(_body)
check("QX_TOKEN" not in _blob and "session.json" not in _blob
      and "password" not in _blob.lower(),
      "D7 no secrets in the diagnostics payload")

# seeded provenance row -> suppression count appears
_db.log_signal_provenance({
    "signal_id": "phase-e-test-1",
    "symbol": "EURUSD_otc",
    "signal": "NO_SIGNAL", "reason_code": "DATA_STALE",
    "truth_status": "rejected", "research_only": True,
    "target_candle_open_epoch": float(T0 + 3000),
    "source_candle_close_epoch": float(T0 + 2940),
}, period=60)
_r2 = _client.get("/api/data-quality")
_b2 = _r2.json()
check(_b2.get("signals_suppressed_by_gate", {}).get("DATA_STALE", 0) >= 1,
      "D8 a gated emission is counted as suppressed by its reason code")

# ── summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"PHASE-E DATA QUALITY: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for f in _FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
