"""PHASE-2A TIME PROVIDER tests (2026-10, rebuild).

Run:  py scripts/test_phase2_time_provider.py     (or python3)

Covers:
  * local-clock honesty (LocalClockProvider: UNVERIFIED forever,
    local_only=True, never claims broker time);
  * evidence thresholds (>= 3 samples before SYNCED; offset = median;
    uncertainty = max deviation);
  * SYNCED -> STALE on age; STALE is not verified;
  * reconnect invalidation (evidence cleared, must re-accumulate);
  * malformed samples rejected (8 kinds, counted);
  * out-of-order + duplicate server timestamps (counted, rejected);
  * offset-jump reset (big step re-verifies from scratch; small drift
    does not);
  * monotonic elapsed time immune to wall-clock steps;
  * pyquotex-like provider (no samples) -> gate stays False even when
    a legacy boolean latch was previously set True;
  * integration with the Phase-1 Truth Layer: synced-but-unauthorized
    -> still NO_SIGNAL (DATA_SOURCE_UNAUTHORIZED), research preserved.

No network. No broker. No credentials. Pure in-process.
"""
import math
import sys
import time

import os
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core import time_provider as _tpmod          # noqa: E402
from core.time_provider import (                  # noqa: E402
    LocalClockProvider, SourceTimeProvider,
    SYNC_UNVERIFIED, SYNC_SYNCED, SYNC_STALE,
    get_time_provider)

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


def fresh(staleness=120.0):
    return SourceTimeProvider(staleness_secs=staleness)


def note_consistent(p, n=4, offset=0.5):
    """Note n mutually-consistent samples: each is the CURRENT local
    wall clock + the same offset, exactly like consecutive real timesync
    frames (server_ts strictly increasing; per-sample offset constant)."""
    for i in range(n):
        r = p.note_sample(time.time() + offset)
        assert r["action"] == "accepted", r
    return p


print("=" * 72)
print("PHASE-2A TIME PROVIDER TEST SUITE")
print("=" * 72)

# ── [A] LocalClockProvider: total honesty ──────────────────────────────
print("\n[A] LocalClockProvider (pyquotex-path reality)")
lp = LocalClockProvider()
check(lp.local_only is True and lp.sync_status() == SYNC_UNVERIFIED
      and lp.timing_verified() is False,
      "A1 local-only, UNVERIFIED forever, gate False")
_d = lp.describe()
check(_d["backend"] == "local_clock" and _d["offset_secs"] is None
      and _d["sample_count"] == 0 and "never broker time"
      in _d["note"].lower() or "device utc" in _d["note"].lower(),
      "A2 describe() honest (no offset, no samples, labeled device UTC)")
_before = time.time()
_now = lp.now()
check(abs(_now - _before) < 1.0,
      "A3 now() is the device clock (no offset applied)")

# ── [B] SourceTimeProvider: evidence thresholds ───────────────────────
print("\n[B] Evidence thresholds")
p = fresh()
check(p.sync_status() == SYNC_UNVERIFIED and p.timing_verified() is False,
      "B1 fresh provider → UNVERIFIED")
p.note_sample(time.time() + 0.5)
p.note_sample(time.time() + 0.5)
check(p.sync_status() == SYNC_UNVERIFIED,
      "B2 two samples still UNVERIFIED (uncertainty needs >= 3)")
p.note_sample(time.time() + 0.5)
p.note_sample(time.time() + 0.5)
check(p.sync_status() == SYNC_SYNCED and p.timing_verified() is True,
      "B3 three+ consistent fresh samples → SYNCED (gate True)")
_off = p.offset_estimate()
check(_off is not None and abs(_off - 0.5) < 0.2,
      "B4 offset estimate ≈ median of samples", str(_off))
p2 = fresh()
for o in (0.2, 0.5, 0.8, 0.8):     # server-monotonic offsets
    p2.note_sample(time.time() + o)
check(p2.uncertainty_secs() is not None
      and p2.uncertainty_secs() >= 0.35,
      "B5 uncertainty = max deviation from the median (conservative)",
      str(p2.uncertainty_secs()))

# ── [C] Staleness: SYNCED -> STALE ─────────────────────────────────────
print("\n[C] Staleness")
p = fresh(staleness=120.0)
_old_mono = time.monotonic() - 1000.0          # backdated receipt
for i in range(4):
    p.note_sample(time.time() + 0.5, recv_monotonic=_old_mono + i)
check(p.sync_status() == SYNC_STALE and p.timing_verified() is False,
      "C1 aged evidence (1000s > 120s window) → STALE, gate False")
check(p.offset_estimate() is not None,
      "C2 the estimate itself is still REPORTED (transparent) even "
      "while stale")

# ── [D] Reconnect invalidation ─────────────────────────────────────────
print("\n[D] Reconnect invalidation")
p = fresh()
note_consistent(p)
check(p.sync_status() == SYNC_SYNCED, "D1 pre-reconnect: SYNCED")
r = p.on_reconnect()
check(p.sync_status() == SYNC_UNVERIFIED and p.timing_verified() is False
      and r.get("samples_dropped", 0) >= 3,
      "D2 reconnect clears ALL evidence → UNVERIFIED (never trust a "
      "previously-set state)", str(r))
note_consistent(p)
check(p.sync_status() == SYNC_SYNCED,
      "D3 evidence re-accumulates from NEW samples after reconnect")
check(p.describe().get("reconnect_resets") == 1,
      "D4 reconnect reset counted (auditable)")

# ── [E] Malformed samples (8 kinds, all rejected + counted) ────────────
print("\n[E] Malformed sample battery")
p = fresh()
m0 = p.describe()["samples_rejected_malformed"]
for bad in (None, True, False, "1717171717", {"time": 1},
            [1717171717], float("nan"), float("inf"), float("-inf"),
            0, -1717171717, time.time() + 10_000_000.0):
    r = p.note_sample(bad)
    if r["action"] != "rejected":
        check(False, "E1 malformed battery rejected", f"{bad!r} → {r}")
        break
else:
    check(True, "E1 malformed battery (None/bool/str/dict/list/NaN/"
          "±inf/0/negative/absurd-offset) all rejected")
check(p.describe()["samples_rejected_malformed"] - m0 == 12,
      "E2 each rejection counted",
      str(p.describe()["samples_rejected_malformed"] - m0))
check(p.sync_status() == SYNC_UNVERIFIED,
      "E3 malformed input never establishes sync")

# ── [F] Out-of-order + duplicate timestamps ────────────────────────────
print("\n[F] Out-of-order / duplicates")
p = fresh()
t = time.time()
p.note_sample(t + 0.5)
r = p.note_sample(t - 30.0)                    # backwards in server time
check(r["action"] == "rejected"
      and "out_of_order" in r["reason"],
      "F1 out-of-order server_ts rejected + counted", str(r))
r = p.note_sample(t + 0.5)                     # exact duplicate
check(r["action"] == "rejected"
      and "duplicate" in r["reason"],
      "F2 duplicate server_ts rejected + counted", str(r))
_d = p.describe()
check(_d["samples_rejected_out_of_order"] == 1
      and _d["samples_rejected_duplicate"] == 1,
      "F3 counters separate out_of_order vs duplicate")

# ── [G] Offset jumps ───────────────────────────────────────────────────
print("\n[G] Offset-jump handling")
p = fresh()
note_consistent(p, n=4, offset=0.5)
check(p.sync_status() == SYNC_SYNCED, "G1 consistent 0.5s offset → SYNCED")
r = p.note_sample(time.time() + 30.0)          # +29.5s jump
check(r["action"] == "accepted"
      and "offset_jump_reset_evidence" in r["reason"],
      "G2 big offset jump RESETS evidence (re-verify, never blend)",
      str(r))
check(p.sync_status() == SYNC_UNVERIFIED,
      "G3 after a jump: back to UNVERIFIED until re-accumulated")
check(p.describe().get("offset_jump_resets") == 1
      and p.describe().get("last_offset_jump_utc"),
      "G4 jump recorded with a UTC stamp (auditable)")
p = fresh()
note_consistent(p, n=4, offset=0.5)
p.note_sample(time.time() + 0.9)               # +0.4s drift — small
check(p.sync_status() == SYNC_SYNCED,
      "G5 small drift (< jump threshold) does NOT reset evidence")

# ── [H] Monotonic elapsed time: wall-clock-step immunity ───────────────
print("\n[H] Monotonic elapsed-time immunity")
# A SYNCED provider (freshness math must survive the wall step below).
p = fresh()
note_consistent(p, n=4, offset=0.5)
check(p.sync_status() == SYNC_SYNCED, "H0 pre-jump: SYNCED")
m0 = p.monotonic_now()
time.sleep(0.05)
_el = p.elapsed_since(m0)
check(0.03 <= _el <= 2.0, "H1 elapsed_since uses the monotonic clock",
      str(_el))
_orig_time_time = time.time
try:
    time.time = lambda: _orig_time_time() + 3600.0   # +1 hour wall step
    _el2 = p.elapsed_since(m0)
    _st = p.sync_status()
finally:
    time.time = _orig_time_time
check(abs(_el2 - _el) < 1.0,
      "H2 a +3600s WALL-clock step cannot corrupt elapsed time",
      f"{_el} vs {_el2}")
check(_st == SYNC_SYNCED,
      "H3 wall-clock step cannot corrupt freshness/staleness math "
      "(monotonic-based; status unchanged)", str(_st))
# Negative elapsed guard
check(p.elapsed_since(m0 + 1e9) == 0.0,
      "H4 future monotonic reading clamps to 0 (never negative)")

# ── [I] now() honesty ──────────────────────────────────────────────────
print("\n[I] now() honesty")
p = fresh()
_l = time.time()
check(abs(p.now() - _l) < 1.0,
      "I1 UNSYNCED provider: now() = device clock (no claim)")
p = fresh()
note_consistent(p, n=4, offset=0.5)
check(0.3 <= p.now() - time.time() <= 0.7,
      "I2 SYNCED provider: now() = device + measured offset")

# ── [J] Gate integration: pyquotex-like + synced-but-unauthorized ──────
print("\n[J] Truth-Layer gate integration")


class _FakePyquotexClient:
    """pyquotex-like client: NO timesync mechanism — but a legacy
    boolean latch was previously set True (exactly the state Phase 2A
    must NOT trust)."""
    _server_time_synced = True
    _server_time_offset = 42.0


import feed as _feed                            # noqa: E402


class _GateHarness(_feed.QuotexFeed):
    def __init__(self, client):
        self._client = client


_h = _GateHarness(_FakePyquotexClient())
_shared = get_time_provider()
_shared.reset_for_tests()
check(_h._timing_verified() is False,
      "J1 pyquotex-like: boolean latch + offset attr are NOT evidence — "
      "gate stays False (no provider samples)")
check(abs(_h._srv_now() - time.time()) < 0.05,
      "J1b _srv_now ignores the unevidenced client offset (local only)")
# now note REAL samples the way quotex_ws does → gate opens for TIMING
# (still blocked later by authorization, which is the point)
for _o in (0.4, 0.5, 0.6, 0.5):
    _shared.note_sample(time.time() + _o)
check(_h._timing_verified() is True,
      "J2 with >= 3 fresh measured samples the gate opens for TIMING")
_off_before = _shared.offset_estimate()
check(abs(_h._srv_now() - time.time() - _off_before) < 0.05,
      "J2b _srv_now applies the evidenced offset")
_shared.on_reconnect()
check(_h._timing_verified() is False,
      "J3 reconnect invalidation closes the timing gate again")
_shared.reset_for_tests()

# synced-but-unauthorized → still NO_SIGNAL (contract level, like
# Phase A D2 — the assertion is the Phase-A acceptance criterion)
from core import signal_contract as sc          # noqa: E402
r = sc.apply_truth_layer(
    {"signal": "PUT", "confidence": 71, "ml_probability": 0.62,
     "strategy": "fixture"},
    asset="EURUSD_otc", period=60,
    candles=[{"time": 1717171740 + i * 60, "open": 1.1,
              "high": 1.1, "low": 1.1, "close": 1.1}
             for i in range(70)],
    last_tick_wall=time.time() - 1.0,
    timing_verified=True,
    now=time.time())
check(r["signal"] == "NO_SIGNAL"
      and r["reason_code"] == "DATA_SOURCE_UNAUTHORIZED"
      and r.get("research_direction") == "PUT",
      "J4 synced-but-UNAUTHORIZED → NO_SIGNAL, research preserved "
      "(timing sync ≠ source authorization)")

# ── [K] describe() completeness ────────────────────────────────────────
print("\n[K] describe completeness")
p = fresh()
note_consistent(p, n=5, offset=0.5)
d = p.describe()
_missing = [k for k in ("provider_version", "backend", "local_only",
                        "sync_status", "offset_secs", "uncertainty_secs",
                        "staleness_window_secs",
                        "seconds_since_last_sample", "sample_count",
                        "last_sync_utc", "samples_accepted",
                        "samples_rejected_malformed",
                        "samples_rejected_out_of_order",
                        "samples_rejected_duplicate",
                        "offset_jump_resets", "reconnect_resets") if
            k not in d]
check(not _missing, "K1 describe() carries the full evidence field set",
      f"missing {_missing}")
check("authorization" in d["note"].lower(),
      "K2 describe() states the authorization caveat explicitly")

# ── summary ─────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"PHASE-2A TIME PROVIDER: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for f in _FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
