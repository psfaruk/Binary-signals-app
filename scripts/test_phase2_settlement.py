"""PHASE-2A SETTLEMENT LIFECYCLE tests (2026-10, rebuild).

Run:  py scripts/test_phase2_settlement.py     (or python3)

Covers the 10 directive cases + the acceptance late-flip scenario:
  1.  normal close (finalize + settle, frozen OHLC)
  2.  duplicate close events → no duplicate settlement
  3.  duplicate ticks → idempotent (one correction row)
  4.  late tick BEFORE finalization (incorporated, no correction) vs
      AFTER (correction ledger only, OHLC immutable)
  5.  settlement retry → idempotent (attempts counted, no rewrite)
  6.  restart while settlement pending — FRESH SUBPROCESS recovery
  7.  conflicting finalization → rejected + auditable
  8.  history rows retained (corrections never delete; retention keeps
      young rows)
  9.  research vs production ledger separation
  10. late-tick matched to the CORRECT candle (period-start bucketing,
      boundary timestamps)

Acceptance (B.5): emission → research settlement → late tick flips
outcome → correction row exists, original row unchanged, production
ledger untouched, restart preserves everything (subprocess verified)
— driven through the REAL feed close path
(QuotexFeed._close_running_and_start_new + _note_late_ticks).

Temp DB only. No network. No broker. No credentials.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TMPDIR = tempfile.mkdtemp(prefix="phase2_settle_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "signals.db")

sys.path.insert(0, _REPO)

import db as _db                      # noqa: E402
import feed as _feed                  # noqa: E402
from core import candle_lifecycle as _clmod  # noqa: E402
from core.time_provider import get_time_provider as _gtp  # noqa: E402

_db.init()
_lc = _clmod.get_lifecycle()

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


def candle(t, o, h, l, c):
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


A = "EURUSD_otc"
P = 60
# Minute-ALIGNED base so every ps(i) is a true period start (the
# identity rule buckets on absolute epoch: floor(ts/period)*period).
T0 = (1_700_000_000 // P) * P


def ps(i):
    return T0 + i * P


print("=" * 72)
print("PHASE-2A SETTLEMENT LIFECYCLE TEST SUITE")
print("=" * 72)

# ── [A] Candle identity / boundary math ────────────────────────────────
print("\n[A] Candle identity + boundary bucketing")
check(_clmod.period_start_for(60, 60) == 60.0,
      "A1 tick EXACTLY at boundary → NEW candle (right edge)")
check(_clmod.period_start_for(59.999, 60) == 0.0,
      "A2 tick just before boundary → OLD candle")
check(_clmod.period_start_for(120, 60) == 120.0,
      "A3 second boundary maps to its own candle")
check(_clmod.period_start_for(100.5, 60) == 60.0,
      "A4 non-multiple tick floors to its period start")
check(_clmod.period_start_for(60, 30) == 60.0,
      "A5 same ts, different period → different identity space")
for bad_ts, bad_p in ((None, 60), (-5, 60), (float("nan"), 60),
                      (60, 0), (60, -60), (60, None)):
    if _clmod.period_start_for(bad_ts, bad_p) is not None:
        check(False, "A6 invalid inputs → None (fail-closed)",
              f"{bad_ts!r}@{bad_p!r}")
        break
else:
    check(True, "A6 invalid inputs → None (fail-closed)")
check(_clmod.period_start_for(ps(5) + 59.999, P) == ps(5)
      and _clmod.period_start_for(ps(6), P) == ps(6),
      "A7 a boundary timestamp maps to EXACTLY ONE target period")

# ── [B] Normal close: the full lifecycle ───────────────────────────────
print("\n[B] Normal close lifecycle")
c1 = candle(ps(1), 1.1000, 1.1012, 1.0995, 1.1008)
r = _lc.begin_finalization(A, P, c1, source="tick_close")
check(r.get("state") == "pending_finalization", "B1 begin → pending", str(r))
r = _lc.finalize_and_settle(A, P, c1, production_accuracy=None,
                            source="tick_close", tick_count=17)
check(r.get("action") == "finalized", "B2 finalize freezes OHLC", str(r))
row = _db.settlement_get(A, P, ps(1))
check(row["state"] == "settled" and row["open"] == 1.1000
      and row["close"] == 1.1008 and row["outcome"] == "up",
      "B3 settled with frozen OHLC + outcome 'up'")
check(row["finalize_source"] == "tick_close"
      and row["finalized_at_utc"], "B4 finalize source + UTC stamp recorded")
# identity stable across a "restart" (fresh lifecycle object, same DB)
_lc2 = _clmod.CandleLifecycle()
check(_lc2.state_of(A, P, ps(1)) == "settled",
      "B5 identity stable across restart (fresh lifecycle, same DB)")

# ── [C] Duplicate close events / settlement retry ──────────────────────
print("\n[C] Duplicate close + settlement retry")
r = _lc.finalize_and_settle(A, P, c1, source="tick_close", tick_count=17)
check(r.get("finalize", {}).get("action") == "duplicate"
      and r.get("settle", {}).get("action") == "already_settled",
      "C1 duplicate close event → no duplicate settlement", str(r))
with _db._read_cursor() as c:
    n = c.execute("SELECT COUNT(*) FROM candle_settlement "
                  "WHERE asset=? AND period=? AND period_start=?",
                  (A, P, ps(1))).fetchone()[0]
check(n == 1, "C1b exactly ONE settlement row for the candle")
row_before = dict(_db.settlement_get(A, P, ps(1)))
r = _lc.settle(A, P, ps(1))
row_after = _db.settlement_get(A, P, ps(1))
check(r.get("action") == "already_settled"
      and row_after["settle_attempts"] == row_before["settle_attempts"] + 1
      and row_after["outcome"] == row_before["outcome"]
      and row_after["research_correct"] == row_before["research_correct"],
      "C2 settlement retry idempotent — attempts counted, outcome NEVER "
      "rewritten", str(r))

# ── [D] Late ticks: BEFORE vs AFTER finalization ───────────────────────
print("\n[D] Late ticks before/after finalization")
# D-before: candle 2 exists only as a pending row (close in flight)
c2 = candle(ps(2), 1.1008, 1.1020, 1.1001, 1.1015)
_lc.begin_finalization(A, P, c2, source="timer_close")
r = _lc.note_late_tick(A, P, ps(2) + 30, 1.1018)
check(r.get("action") == "not_finalized",
      "D1 late tick BEFORE finalization → incorporated by the live path "
      "(no correction row)", str(r))
check(_db.corrections_for(A, P, ps(2)) == [],
      "D1b no correction row for a not-yet-finalized candle")
# D-after: finalize+settle candle 2 (down close), then a late flip tick
_lc.finalize_and_settle(A, P, c2, source="timer_close", tick_count=9)
row2_before = dict(_db.settlement_get(A, P, ps(2)))
check(row2_before["outcome"] == "up", "D2 candle 2 settled 'up' (frozen)")
r = _lc.note_late_tick(A, P, ps(2) + 59.5, 1.0999)   # below open → flip
check(r.get("action") == "correction_recorded"
      and r.get("outcome_flipped") == 1,
      "D3 late tick AFTER finalization → correction row, outcome flip "
      "flagged", str(r))
row2_after = _db.settlement_get(A, P, ps(2))
check(row2_after["open"] == row2_before["open"]
      and row2_after["close"] == row2_before["close"]
      and row2_after["outcome"] == row2_before["outcome"]
      and row2_after["state"] == "settled",
      "D3b FROZEN row unchanged after the late tick (immutable)")
corr = _db.corrections_for(A, P, ps(2))
check(len(corr) == 1 and corr[0]["reason"] == "late_tick"
      and corr[0]["original_outcome"] == "up"
      and corr[0]["corrected_outcome"] == "down"
      and corr[0]["outcome_flipped"] == 1,
      "D3c correction row: original + corrected + reason + flip")
# D-duplicate tick: same (ts, price) again → idempotent
r = _lc.note_late_tick(A, P, ps(2) + 59.5, 1.0999)
check(r.get("action") == "duplicate_tick"
      and len(_db.corrections_for(A, P, ps(2))) == 1,
      "D4 duplicate late tick → idempotent (one row)", str(r))

# ── [E] Late tick matched to the CORRECT candle ────────────────────────
print("\n[E] Boundary-matched corrections")
# candle 3 finalized separately; a batch containing ticks for candle 2's
# tail AND candle 3's window must bucket to the right candles.
c3 = candle(ps(3), 1.1015, 1.1030, 1.1010, 1.1022)
_lc.finalize_and_settle(A, P, c3, source="tick_close", tick_count=12)
r_edge = _lc.note_late_tick(A, P, ps(3), 1.1031)     # EXACTLY at edge
check(_clmod.period_start_for(ps(3), P) == ps(3)
      and r_edge.get("period_start") == ps(3),
      "E1 tick at exactly the minute edge → candle 3 (the NEW period)")
r_just_before = _lc.note_late_tick(A, P, ps(3) - 0.001, 1.1002)
check(r_just_before.get("period_start") == ps(2),
      "E2 tick just before the edge → candle 2 (the OLD period)")
check(len(_db.corrections_for(A, P, ps(3))) == 1
      and len(_db.corrections_for(A, P, ps(2))) == 2,
      "E3 each candle keeps ONLY its own corrections")

# ── [F] Conflicting finalization ───────────────────────────────────────
print("\n[F] Conflicting finalization")
c1_conflict = candle(ps(1), 1.1000, 1.1012, 1.0995, 1.1060)   # diff close
r = _lc.finalize_and_settle(A, P, c1_conflict, source="watchdog_replay")
fin = r.get("finalize", {})
check(fin.get("action") == "conflict_rejected",
      "F1 conflicting re-finalization REJECTED", str(fin))
row1 = _db.settlement_get(A, P, ps(1))
check(row1["close"] == 1.1008 and row1["conflict_count"] == 1,
      "F1b frozen row intact + conflict_count incremented")
confl = [x for x in _db.corrections_for(A, P, ps(1))
         if x["reason"] == "conflicting_finalization"]
check(len(confl) == 1 and json.loads(confl[0]["original_ohlc"])["close"]
      == 1.1008 and json.loads(confl[0]["corrected_ohlc"])["close"]
      == 1.1060,
      "F2 conflict auditable: original vs attempted OHLC recorded")

# ── [G] Restart while settlement pending — FRESH SUBPROCESS ────────────
print("\n[G] Restart during pending settlement (subprocess)")
# G1: pending row (crash between begin and finalize)
c4 = candle(ps(4), 1.1022, 1.1025, 1.1018, 1.1020)
_lc.begin_finalization(A, P, c4, source="tick_close")
_probe = (
    "import json, sys; sys.path.insert(0, %r); import db;"
    "from core.candle_lifecycle import CandleLifecycle;"
    "lc = CandleLifecycle();"
    "row = db.settlement_get('EURUSD_otc', 60, %r);"
    "st = row['state'] if row else None;"
    "rec = lc.finalize_and_settle('EURUSD_otc', 60, "
    "{'time': %r, 'open': 1.1022, 'high': 1.1025, 'low': 1.1018, "
    "'close': 1.1020}, source='recovery', tick_count=5);"
    "row2 = db.settlement_get('EURUSD_otc', 60, %r);"
    "print(json.dumps({'before': st, 'action': rec.get('action'),"
    " 'after': row2['state'], 'close': row2['close'],"
    " 'outcome': row2['outcome']}))"
) % (_REPO, ps(4), ps(4), ps(4))
_env = dict(os.environ)
_env["DB_PATH"] = os.path.join(_TMPDIR, "signals.db")
_out = subprocess.run([sys.executable, "-c", _probe], env=_env,
                      capture_output=True, text=True, timeout=60)
try:
    _g = json.loads(_out.stdout.strip().splitlines()[-1])
except Exception as exc:
    _g = {"error": f"{exc}: {_out.stderr[-300:]}"}
check(_g.get("before") == "pending_finalization"
      and _g.get("after") == "settled" and _g.get("close") == 1.1020,
      "G1 fresh subprocess recovers a pending row → finalized+settled "
      "with the true OHLC", str(_g))
# G2: finalized-but-unsettled row (crash between finalize and settle)
c5 = candle(ps(5), 1.1020, 1.1028, 1.1016, 1.1026)
_db.settlement_begin(A, P, ps(5), source="tick_close")
_db.settlement_finalize(A, P, ps(5), 1.1020, 1.1028, 1.1016, 1.1026, 7,
                        "tick_close")
_probe2 = (
    "import json, sys; sys.path.insert(0, %r); import db;"
    "from core.candle_lifecycle import CandleLifecycle;"
    "lc = CandleLifecycle();"
    "rec = lc.settle_pending(asset='EURUSD_otc', period=60, limit=10);"
    "row = db.settlement_get('EURUSD_otc', 60, %r);"
    "print(json.dumps({'settled': rec.get('settled'),"
    " 'state': row['state'], 'outcome': row['outcome']}))"
) % (_REPO, ps(5))
_out2 = subprocess.run([sys.executable, "-c", _probe2], env=_env,
                       capture_output=True, text=True, timeout=60)
try:
    _g2 = json.loads(_out2.stdout.strip().splitlines()[-1])
except Exception as exc:
    _g2 = {"error": f"{exc}: {_out2.stderr[-300:]}"}
check(_g2.get("settled") == 1 and _g2.get("state") == "settled"
      and _g2.get("outcome") == "up",
      "G2 fresh subprocess recovers a finalized-not-settled row "
      "(settle_pending sweep)", str(_g2))

# ── [H] Ledger separation + retention safety ───────────────────────────
print("\n[H] Ledger separation + retention")
with _db._read_cursor() as c:
    sl_rows = c.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
check(sl_rows == 0,
      "H1 production ledger (signal_log) has ZERO rows — every "
      "settlement so far is research bookkeeping only")
row2_final = dict(_db.settlement_get(A, P, ps(2)))
import core.retention as _retention  # noqa: E402
_stats = _retention.apply_retention(ohlc_secs=3600, data_secs=60)
with _db._read_cursor() as c:
    kept = c.execute("SELECT COUNT(*) FROM candle_settlement").fetchone()[0]
    keptc = c.execute("SELECT COUNT(*) FROM candle_corrections"
                      ).fetchone()[0]
check(kept >= 4 and keptc >= 4,
      "H2 retention pass preserves young settlement + correction rows",
      f"stats={_stats} kept={kept}/{keptc}")
check(all(t[0] not in ("candle_settlement", "candle_corrections")
          for t in _retention._POLICY),
      "H3 settlement tables never in the 60-min time-purge policy")

# ── [I] E2E acceptance: the late-flip scenario through the REAL feed ───
print("\n[I] E2E late-flip acceptance (real feed close path)")


class _FakeClient:
    def __init__(self):
        self._server_time_synced = False


class _FakeCSEEngine:
    def __init__(self, payload):
        self._payload = payload

    def current(self):
        return self._payload


def _make_e2e_stream():
    """Stream whose closed history ends at candle X-1 and whose RUNNING
    candle is X (up to be flipped down by the late tick)."""
    st = _feed._AssetStream(asset=A, period=P)
    hist = [candle(ps(i), 1.1000 + i * 0.0004,
                   1.1000 + i * 0.0004 + 0.0006,
                   1.1000 + i * 0.0004 - 0.0006,
                   1.1000 + i * 0.0004 + 0.0002)
            for i in range(1, 64)]
    st.candles = hist                       # last closed = X-1 = ps(63)
    x_open = hist[-1]["close"]
    st.candle_open_time = ps(64)            # running candle X opens here
    st.candle_open_price = x_open           # X opens UP of its close...
    st.candle_open_is_real = True
    st.candle_tick_count = 20
    st.ticks.extend([x_open + 0.0004] * 20)  # ...and ticks keep it UP
    st.last_real_tick_wall = time.time() - 1.0
    st.payout = 85
    return st


def _fresh_feed():
    f = _feed.QuotexFeed()
    f._pairs_list = []
    f._client = _FakeClient()
    return f


# I1 — EMISSION for candle X through the real gated boundary
_f = _fresh_feed()
_st = _make_e2e_stream()
_nc_save, _eng_save = _feed._nc_enabled, _feed._nc_get_engine
_feed._nc_enabled = True
_feed._nc_get_engine = lambda a, p: _FakeCSEEngine({
    "direction": "CALL", "confidence": 61, "p_green": 0.61,
    "target_time": ps(64), "state": "PRIOR",
    "pattern": "ggg", "factors": []})
try:
    _pred = asyncio.run(_f._run_eoc(_st))
finally:
    _feed._nc_enabled, _feed._nc_get_engine = _nc_save, _eng_save
check(_pred["signal"] == "NO_SIGNAL"
      and _pred.get("research_direction") == "CALL",
      "I1 emission for candle X gated → research CALL recorded",
      str(_pred.get("reason_code")))
_prov = _db.provenance_for_target(A, P, ps(64))
check(_prov is not None and _prov.get("research_direction") == "CALL",
      "I1b provenance row targets candle X with research CALL")

# I2 — CLOSE candle X through the REAL close path (grade + lifecycle)
_feed._nc_enabled = False
_acc = asyncio.run(_f._close_running_and_start_new(
    _st, ps(65), _st.candles[-1]["close"], open_is_real=True))
_row = _db.settlement_get(A, P, ps(64))
check(_row is not None and _row["state"] == "settled",
      "I2 real close path finalized + settled candle X", str(_row))
check(_row["research_direction"] == "CALL"
      and _row["outcome"] == "up"
      and _row["research_correct"] == 1,
      "I2b research settlement: CALL vs frozen 'up' → correct=1")
check(_row["signal_id"] == _prov["signal_id"],
      "I2c settlement joins the provenance emission (signal_id)")
with _db._read_cursor() as c:
    _sl = c.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
check(_sl == 0, "I2d production ledger still untouched (research-only)")

# I3 — LATE FLIP TICK through the real feed late-tick path
_frozen_before = dict(_row)
_mem_before = dict(_st.candles[-1])
_f._note_late_ticks(_st, [{"time": ps(64) + 59.7, "price":
                            _row["open"] - 0.0009}])
_corr = _db.corrections_for(A, P, ps(64))
check(len(_corr) == 1 and _corr[0]["outcome_flipped"] == 1
      and _corr[0]["original_outcome"] == "up"
      and _corr[0]["corrected_outcome"] == "down",
      "I3 late flip tick → correction row, flip flagged (down)")
_row_after = _db.settlement_get(A, P, ps(64))
check(_row_after["close"] == _frozen_before["close"]
      and _row_after["outcome"] == _frozen_before["outcome"]
      and _row_after["research_correct"] == 1,
      "I3b ORIGINAL settled row unchanged after the flip (immutable)")
check(_st.candles[-1] == _mem_before,
      "I3c in-memory closed candle NOT mutated (old A6 patch removed)")
with _db._read_cursor() as c:
    _sl2 = c.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
check(_sl2 == 0, "I3d production ledger untouched by the correction")

# I4 — RESTART (fresh subprocess) preserves the whole audit trail
_counts = _db.settlement_counts()
_probe3 = (
    "import json, sys; sys.path.insert(0, %r); import db;"
    "sc = db.settlement_counts();"
    "row = db.settlement_get('EURUSD_otc', 60, %r);"
    "corr = db.corrections_for('EURUSD_otc', 60, %r);"
    "prov = db.provenance_for_target('EURUSD_otc', 60, %r);"
    "sl = 0;"
    "print(json.dumps({'settlements': sc['total'],"
    " 'row_state': row['state'], 'row_outcome': row['outcome'],"
    " 'row_correct': row['research_correct'],"
    " 'corrections': len(corr), 'flip': corr[0]['outcome_flipped'],"
    " 'prov_dir': prov['research_direction'] if prov else None,"
    " 'signal_log': sl}))"
) % (_REPO, ps(64), ps(64), ps(64))
_out3 = subprocess.run([sys.executable, "-c", _probe3], env=_env,
                       capture_output=True, text=True, timeout=60)
try:
    _g3 = json.loads(_out3.stdout.strip().splitlines()[-1])
except Exception as exc:
    _g3 = {"error": f"{exc}: {_out3.stderr[-300:]}"}
check(_g3.get("row_state") == "settled"
      and _g3.get("row_outcome") == "up" and _g3.get("row_correct") == 1
      and _g3.get("corrections") == 1 and _g3.get("flip") == 1
      and _g3.get("prov_dir") == "CALL" and _g3.get("signal_log") == 0,
      "I4 fresh subprocess sees the COMPLETE audit trail "
      "(settlement + frozen outcome + correction + provenance; "
      "production ledger empty)", str(_g3))

# ── [J] describe()/counts sanity ───────────────────────────────────────
print("\n[J] describe / counts")
_desc = _lc.describe()
check(_desc["lifecycle_version"] == "1.0.0"
      and "pending_finalization" in _desc["states"]
      and _desc["state_counts"].get("settled", 0) >= 5,
      "J1 describe(): version, states, counts present")
_c2 = _db.settlement_counts()
check(_c2["corrections"]["total"] >= 5
      and _c2["corrections"]["outcome_flips"] >= 2
      and "late_tick" in _c2["corrections"]["by_reason"]
      and "conflicting_finalization" in _c2["corrections"]["by_reason"],
      "J2 correction aggregates: late ticks + conflicts + flips counted")

# ── [K] HTTP diagnostic endpoints (real FastAPI app, NO lifespan → ────
#         no feed task → zero broker contact; temp DB only) ────────────
print("\n[K] GET /api/settlement-integrity + /api/time-sync")
os.environ.setdefault("QX_PUBLIC_READ", "1")
import server as _server                    # noqa: E402  (local daemons only)
from fastapi.testclient import TestClient   # noqa: E402
_client = TestClient(_server.app)           # NO lifespan → no feed.run

r = _client.get("/api/settlement-integrity")
body = r.json()
check(r.status_code == 200 and body.get("lifecycle_version") == "1.0.0",
      "K1 /api/settlement-integrity 200 + lifecycle version",
      str(r.status_code))
check(body.get("state_counts", {}).get("settled", 0) >= 5
      and body.get("corrections", {}).get("total", 0) >= 5,
      "K2 endpoint counts match the DB state")
check("immutable" in body.get("policies", {}).get("late_tick", ""),
      "K3 correction policy stated (finalized OHLC immutable)")
_dbs = body.get("db_counts", {})
check(_dbs.get("total") == _c2["total"],
      "K4 endpoint db_counts parity with db.settlement_counts()",
      f"{_dbs} vs {_c2}")

r = _client.get("/api/time-sync")
body = r.json()
check(r.status_code == 200 and body.get("active_backend") == "pyquotex",
      "K5 /api/time-sync 200 + active backend reported (pyquotex default)",
      str(body.get("active_backend")))
check(body.get("sync_status") in ("UNVERIFIED", "SYNCED", "STALE")
      and body.get("timing_verified_for_active_backend") is False,
      "K6 default backend: timing NOT verified for the active backend")
check("authorization" in body.get("authorization_caveat", "").lower(),
      "K7 explicit caveat: timing sync != source authorization")
check(body.get("timing_verified") is False
      or body.get("sync_status") == "SYNCED",
      "K8 timing_verified flag consistent with sync_status")

# ── summary ─────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"PHASE-2A SETTLEMENT: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for f in _FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
