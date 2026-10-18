"""PHASE-1 TRUTH LAYER — offline unit + E2E tests (2026-10, rebuild).

Run:  py scripts/test_phase1_truth_layer.py     (or python3)

Covers (Phase-A authorization requirements):
  * all 13 reason codes (contract-level fixtures),
  * fail-closed behavior for engine/gate exceptions,
  * environment-variable bypass attempts (must all FAIL to bypass),
  * ALL feed emission paths through the real _run_eoc boundary
    (CSE early-exit, legacy CALL/PUT, fade_default fallback, NEUTRAL,
    market-closed None, exception path),
  * provenance persistence across a process restart (subprocess),
  * REST/WS payload truth-field parity (data-level + source-level),
  * additive-migration safety (idempotence, existing rows preserved,
    retention never 60-min-purges provenance).

No network. No broker. Temp DB only. No credentials read or printed.
"""
import asyncio
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── temp DB BEFORE any repo import (tests never touch a real DB) ──────
_TMPDIR = tempfile.mkdtemp(prefix="phase1_truth_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "signals.db")

sys.path.insert(0, _REPO)

import db as _db                      # noqa: E402
import feed as _feed                  # noqa: E402
from core import signal_contract as sc  # noqa: E402

# The feed/server call db.init() at startup; tests must do the same
# before any emission writes provenance (matches production boot order).
_db.init()

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


# ── fixtures ────────────────────────────────────────────────────────────
def make_candles(n=70, period=60, t0=1_700_000_000):
    return [{"time": t0 + i * period,
             "open": 1.1000 + i * 0.0001,
             "high": 1.1000 + i * 0.0001 + 0.0002,
             "low": 1.1000 + i * 0.0001 - 0.0002,
             "close": 1.1000 + i * 0.0001 + 0.0001}
            for i in range(n)]


GOOD_PROV = {
    "model_id": "cse_v1", "model_version": "next_candle_v1",
    "artifact_sha256": "deadbeef" * 8, "feature_schema_version": "cse_v1_35feat",
    "load_ok": True, "schema_ok": True,
}
EMPTY_REGISTRY = {"registry_path": "model_validation.json",
                  "exists": False, "registry_version": None,
                  "approved": [], "error": None}


def gated(raw=None, candles=None, last_tick=None, prov=None,
          timing=False, registry=None, internal_error=False, **kw):
    now = kw.pop("now", time.time())
    return sc.apply_truth_layer(
        raw, asset="EURUSD_otc", period=60,
        candles=candles if candles is not None else make_candles(),
        last_tick_wall=last_tick if last_tick is not None else now - 2.0,
        timing_verified=timing,
        model_prov=prov if prov is not None else GOOD_PROV,
        registry=registry if registry is not None else EMPTY_REGISTRY,
        now=now, internal_error=internal_error, **kw)


def directional(direction="CALL", conf=62.0):
    return {"signal": direction, "confidence": conf, "raw_confidence": conf,
            "strategy": "test_strategy", "signal_source": "test",
            "ml_probability": 0.61, "reasons": ["unit fixture"]}


print("=" * 72)
print("PHASE-1 TRUTH LAYER TEST SUITE")
print("=" * 72)

# ── A. the 13 reason codes ─────────────────────────────────────────────
print("\n[A] Reason codes (contract level)")
codes = list(sc.ALL_REASON_CODES)
check(len(codes) == 13, "A0 exactly 13 reason codes", f"got {len(codes)}")

r = gated(raw=None)
check(r["signal"] == "NO_SIGNAL" and r["reason_code"] == "DATA_UNAVAILABLE",
      "A1 DATA_UNAVAILABLE (raw None)")

r = gated(directional(), candles=[])
check(r["reason_code"] == "DATA_INVALID", "A2 DATA_INVALID (no candles)")

bad = make_candles(); bad[-1] = dict(bad[-1], close=float("nan"))
r = gated(directional(), candles=bad)
check(r["reason_code"] == "DATA_INVALID", "A3 DATA_INVALID (non-finite OHLC)")

r = gated(directional(), last_tick=time.time() - 400.0)
check(r["reason_code"] == "DATA_STALE", "A4 DATA_STALE (tick age 400s > 90s)")

r = gated(directional(), candles=make_candles(10))
check(r["reason_code"] == "INSUFFICIENT_HISTORY",
      "A5 INSUFFICIENT_HISTORY (10 < 64 candles)")

r = gated(directional(), engine_feature_error=True)
check(r["reason_code"] == "FEATURE_ERROR", "A6 FEATURE_ERROR")

r = gated(directional(), prov=dict(GOOD_PROV, load_ok=False))
check(r["reason_code"] == "MODEL_UNAVAILABLE", "A7 MODEL_UNAVAILABLE")

r = gated({"signal": "CALL", "confidence": 62,
             "ml_probability": 1.5})
check(r["reason_code"] == "MODEL_OUTPUT_INVALID",
      "A8 MODEL_OUTPUT_INVALID (p=1.5 out of range)")

r = gated({"signal": "CALL", "confidence": 200})
check(r["reason_code"] == "MODEL_OUTPUT_INVALID",
      "A9 MODEL_OUTPUT_INVALID (conf=200)")

r = gated({"signal": "NEUTRAL"})
check(r["reason_code"] == "BELOW_ELIGIBILITY_THRESHOLD",
      "A10 BELOW_ELIGIBILITY_THRESHOLD (NEUTRAL engine output)")

bad_t = make_candles(); bad_t[-1] = dict(bad_t[-1], time=None)
r = gated(directional(), candles=bad_t)
check(r["reason_code"] == "TARGET_CANDLE_INVALID",
      "A11 TARGET_CANDLE_INVALID (source candle time missing)")

_c = make_candles()
_el, _why, _chk = sc.evaluate_eligibility({
    "raw_result": directional(), "candles": _c,
    "last_tick_wall": time.time() - 2, "now": time.time(),
    "timing_verified": True, "model_prov": GOOD_PROV,
    "registry": EMPTY_REGISTRY, "live_domain": "otc",
    "period": 60, "target_open_ts": _c[-1]["time"] + 120})
check(_why == "TARGET_CANDLE_INVALID",
      "A12 TARGET_CANDLE_INVALID (target != source+period)")

r = gated(directional(), timing=False)
check(r["reason_code"] == "TIMING_UNVERIFIED",
      "A13 TIMING_UNVERIFIED (default backend has no timesync)")

r = gated(internal_error=True)
check(r["signal"] == "NO_SIGNAL" and r["reason_code"] == "INTERNAL_ERROR"
      and r["truth_status"] == "error",
      "A14 INTERNAL_ERROR (fail-closed on exception flag)")

r = gated(directional(), timing=True)
check(r["reason_code"] == "DATA_SOURCE_UNAUTHORIZED",
      "A15 DATA_SOURCE_UNAUTHORIZED (synced timing, unauthorized source)")

# MODEL_NOT_VALIDATED: reachable only when source IS authorized — proves
# the registry is an independent second gate (defense in depth).
r = gated(directional(), timing=True, data_source_authorization="authorized")
check(r["reason_code"] == "MODEL_NOT_VALIDATED",
      "A16 MODEL_NOT_VALIDATED (authorized + EMPTY registry → still blocked)")

# ── B. contract behavior ───────────────────────────────────────────────
print("\n[B] Contract behavior")
r = gated(directional("PUT", 71.0))
check(r["confidence"] is None and r["strength"] == "NO_SIGNAL",
      "B1 NO_SIGNAL carries no measured confidence")
check(r.get("research_direction") == "PUT"
      and r.get("research_confidence") == 71.0
      and r.get("research_strategy") == "test_strategy",
      "B2 research_* fields preserve engine diagnostics")
check(isinstance(r.get("eligibility_checks"), dict),
      "B3 eligibility_checks recorded")
check(r.get("research_only") is True, "B4 research_only flag set")
check(r.get("data_source_authorization") == "unauthorized"
      and r.get("data_source_id") == "quotex_ws_unofficial",
      "B5 source identity/authorization attached")

secret_raw = directional()
secret_raw["qx_token"] = "SUPERSECRET"
secret_raw["cookies"] = "session=abc123"
secret_raw["ssid"] = "ssid-fixture"
r = gated(secret_raw)
_blob = json.dumps(r)
check("SUPERSECRET" not in _blob and "session=abc123" not in _blob
      and "ssid-fixture" not in _blob,
      "B6 top-level credential-shaped keys stripped from output")
# KNOWN LIMITATION (byte-exact restored contract): _strip_secret_like is
# single-level — a NESTED dict containing credential-shaped keys would
# survive. Documented in docs/PHASE1_TRUTH_LAYER.md; engines do not emit
# nested credentials today. Tested here so the limitation is explicit,
# not silent:
_nested = directional()
_nested["payload"] = {"api_key": "nested-fixture"}
check("nested-fixture" in json.dumps(gated(_nested)),
      "B6b (documented limitation) nested credential keys are NOT "
      "recursively stripped by the restored contract")

reg_crypto = {"approved": [{"model_id": "cse_v1",
                            "artifact_sha256": "deadbeef" * 8,
                            "asset_domain": "crypto",
                            "oos_evidence": {"method": "walk_forward"}}]}
r = gated(directional(), timing=True,
          data_source_authorization="authorized", registry=reg_crypto)
check(r["reason_code"] == "MODEL_NOT_VALIDATED",
      "B7 registry domain mismatch (crypto prior ≠ otc live) rejected")

reg_noev = {"approved": [{"model_id": "cse_v1",
                          "artifact_sha256": "deadbeef" * 8,
                          "asset_domain": "otc"}]}
r = gated(directional(), timing=True,
          data_source_authorization="authorized", registry=reg_noev)
check(r["reason_code"] == "MODEL_NOT_VALIDATED",
      "B8 registry entry without OOS evidence rejected")

reg_hash = {"approved": [{"model_id": "cse_v1",
                          "artifact_sha256": "other" * 16,
                          "asset_domain": "otc",
                          "oos_evidence": {"method": "x"}}]}
r = gated(directional(), timing=True,
          data_source_authorization="authorized", registry=reg_hash)
check(r["reason_code"] == "MODEL_NOT_VALIDATED",
      "B9 registry entry with artifact-hash mismatch rejected")

state = sc.truth_layer_state()
check(state["actionable_signals_possible"] is False,
      "B10 truth_layer_state: actionable_signals_possible=False")
check(state["validation_registry"]["entries"] == 0
      and state["validation_registry"]["exists"] is False,
      "B11 truth_layer_state: registry absent/empty (correct state)")
check(len(state["reason_codes"]) == 13,
      "B12 truth_layer_state lists the 13 reason codes")

_regfile = os.path.join(_TMPDIR, "model_validation.json")
with open(_regfile, "w", encoding="utf-8") as f:
    f.write("{not json at all")
_loaded = sc.load_validation_registry(_regfile)
check(_loaded["approved"] == [] and _loaded.get("error"),
      "B13 malformed registry file → empty registry (fail-closed)")

cf = sc.build_contract_fields("EURUSD_otc", 60, 1700000000.0,
                              1700000060.0, created_ts=1700000000.5)
for k in ("signal_id", "symbol", "truth_status", "reason_code",
          "eligibility_policy_version", "contract_schema_version",
          "created_at_utc", "target_candle_open_time_utc",
          "data_source_authorization", "validation_status"):
    if k not in cf:
        check(False, "B14 contract fields whitelisted", f"missing {k}")
        break
else:
    check(cf["symbol"] == "EURUSD_otc" and cf["created_at_epoch"] == 1700000000.5,
          "B14 contract fields whitelisted")

# ── C. environment-variable bypass attempts ────────────────────────────
print("\n[C] Env bypass attempts (all must FAIL to bypass)")
_BYPASS_ENV = {
    "QX_DATA_SOURCE_AUTHORIZED": "1",
    "QX_AUTHORIZED": "true",
    "DATA_SOURCE_AUTHORIZED": "1",
    "SIGNAL_AUTHORIZED": "1",
    "QX_ALLOW_SIGNALS": "1",
    "QX_TRUTH_LAYER_OFF": "1",
    "TRUTH_LAYER_ENABLED": "0",
    "QX_TRUTH_LAYER": "off",
    "QX_MODEL_VALIDATED": "1",
    "MODEL_VALIDATED": "1",
    "QX_VALIDATION_REGISTRY": "/nonexistent/fake.json",
    "QX_REGISTRY_PATH": _regfile,
    "STALE_SECS": "999999",
    "QX_FRESHNESS_SECS": "999999",
    "QX_TIMING_VERIFIED": "1",
    "QX_USE_RAW_WS": "1",
    "QX_SOURCE_AUTH": "authorized",
    "ALLOW_UNAUTHORIZED_SOURCE": "1",
}
_saved_env = {k: os.environ.get(k) for k in _BYPASS_ENV}
os.environ.update(_BYPASS_ENV)
try:
    sc2 = importlib.reload(sc)
    check(sc2.DATA_SOURCE_AUTHORIZATION == "unauthorized",
          "C1 env battery → authorization still 'unauthorized' after reload")
    check(sc2.truth_layer_state()["actionable_signals_possible"] is False,
          "C2 env battery → truth state still non-actionable after reload")
    check(gated(directional(), timing=True)["reason_code"]
          == "DATA_SOURCE_UNAUTHORIZED",
          "C3 env battery → gate still returns DATA_SOURCE_UNAUTHORIZED")
    _src = open(os.path.join(_REPO, "core", "signal_contract.py"),
                encoding="utf-8").read()
    check("environ" not in _src,
          "C4 contract source contains no os.environ reads")
finally:
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

check(gated(directional(), last_tick=time.time() - 200.0)["reason_code"]
      == "DATA_STALE",
      "C5 STALE_SECS=999999 env cannot weaken the 90s freshness ceiling")

# ── D. E2E: ALL emission paths through the REAL _run_eoc boundary ─────
print("\n[D] E2E feed emission paths (real QuotexFeed code)")


class _FakeClient:
    """Stands in for the broker client: honest attrs only."""

    def __init__(self, synced=False):
        self._server_time_synced = synced


class _FakeCSEEngine:
    def __init__(self, payload):
        self._payload = payload

    def current(self):
        return self._payload


_STREAM_SEQ = [0]


def make_stream(candles=None, period=60, asset="EURUSD_otc",
                tick_count=30, synced=False):
    # Unique candle block per stream: the provenance table freezes on
    # UNIQUE(asset, period, target_candle_open_epoch) — distinct t0 per
    # fixture keeps every emission path's row independently assertable.
    _STREAM_SEQ[0] += 1
    st = _feed._AssetStream(asset=asset, period=period)
    st.candles = candles if candles is not None else make_candles(
        t0=1_700_000_000 + _STREAM_SEQ[0] * 1_000_000)
    st.ticks.extend([st.candles[-1]["close"]] * 10)
    st.candle_open_time = st.candles[-1]["time"] + period
    st.candle_open_price = st.candles[-1]["close"]
    st.candle_open_is_real = True
    st.candle_tick_count = tick_count
    st.last_real_tick_wall = time.time() - 1.0
    st.payout = 85
    return st


def fresh_feed(synced=False):
    f = _feed.QuotexFeed()
    f._pairs_list = []
    f._client = _FakeClient(synced=synced)
    return f


async def _run(f, stream):
    return await f._run_eoc(stream)


# D1 — CSE early-exit path (the primary signal source today)
_f = fresh_feed()
_st = make_stream()
_nc_save, _eng_save = _feed._nc_enabled, _feed._nc_get_engine
_feed._nc_enabled = True
_feed._nc_get_engine = lambda a, p: _FakeCSEEngine({
    "direction": "CALL", "confidence": 58, "p_green": 0.58,
    "target_time": _st.candles[-1]["time"] + 60, "state": "PRIOR",
    "pattern": "ggg", "factors": []})
try:
    _r = asyncio.run(_run(_f, _st))
finally:
    _feed._nc_enabled, _feed._nc_get_engine = _nc_save, _eng_save
check(_r["signal"] == "NO_SIGNAL" and _r["reason_code"] == "TIMING_UNVERIFIED",
      "D1 CSE early-exit gated (default backend → TIMING_UNVERIFIED)")
check(_r.get("research_direction") == "CALL"
      and _r.get("research_strategy") == "next_candle_v1",
      "D1b CSE preference preserved as research")

# D2 — acceptance criterion: synced raw-WS timing + unauthorized source.
# PHASE-2A (2026-10, rebuild): the timing gate now consults the TIME
# PROVIDER (core/time_provider.py) — a boolean previously set is no
# longer accepted as evidence.  The fixture therefore notes REAL
# measured samples into the provider exactly the way quotex_ws.py's
# timesync handler does (>= 3 fresh consistent samples => SYNCED).
# The ASSERTION below is UNCHANGED from Phase A: synced timing +
# unauthorized source must still yield NO_SIGNAL with
# DATA_SOURCE_UNAUTHORIZED and preserved research fields.
from core.time_provider import get_time_provider as _gtp  # noqa: E402
_tp = _gtp()
_tp.reset_for_tests()
for _off in (0.4, 0.5, 0.6, 0.5):
    _tp.note_sample(time.time() + _off)
_f = fresh_feed(synced=True)
_st = make_stream()
_feed._nc_enabled = True
_feed._nc_get_engine = lambda a, p: _FakeCSEEngine({
    "direction": "PUT", "confidence": 63, "p_green": 0.37,
    "target_time": _st.candles[-1]["time"] + 60, "state": "PRIOR",
    "pattern": "rrr", "factors": []})
try:
    _r = asyncio.run(_run(_f, _st))
finally:
    _feed._nc_enabled, _feed._nc_get_engine = _nc_save, _eng_save
    _tp.reset_for_tests()   # isolate the provider for later tests
check(_r["signal"] == "NO_SIGNAL"
      and _r["reason_code"] == "DATA_SOURCE_UNAUTHORIZED",
      "D2 unauthorized source → NO_SIGNAL/DATA_SOURCE_UNAUTHORIZED")
check(_r.get("research_direction") == "PUT",
      "D2b research direction preserved (research-only)")

# D3 — fade_default fallback path: fabricated conf-50 CALL/PUT must be
# research-only now, never an actionable signal.
_f = fresh_feed()
_st = make_stream()
_feed._nc_enabled = False


async def _neutral_core(asset, period, closed, ticks, running_ticks=None,
                        stream=None):
    return ({"signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
             "score": 0, "reasons": ["fixture: zero theories voted"],
             "regime": {}}, None)


_f._analyze_core = _neutral_core
try:
    _r = asyncio.run(_run(_f, _st))
finally:
    _feed._nc_enabled = _nc_save
check(_r["signal"] == "NO_SIGNAL",
      "D3 fade_default path → NO_SIGNAL (fail-closed)")
check(_r.get("research_strategy") == "fade_default"
      and _r.get("research_confidence") == 50
      and _r.get("research_direction") in ("CALL", "PUT"),
      "D3b fade_default preference preserved as research (conf-50 kept "
      "as diagnostic, never actionable)")
_prov_rows = _db.provenance_recent("EURUSD_otc")
check(any(row.get("research_strategy") == "fade_default"
          for row in _prov_rows),
      "D3c fade_default provenance row persisted")

# D4 — legacy CALL/PUT path (strategy engine result)
_f = fresh_feed()
_st = make_stream()
_feed._nc_enabled = False


async def _call_core(asset, period, closed, ticks, running_ticks=None,
                     stream=None):
    return ({"signal": "CALL", "confidence": 70, "strength": "STRONG",
             "score": 3, "strategy": "confluence_v1",
             "reasons": ["fixture"], "regime": {"regime": "TREND"}}, None)


_f._analyze_core = _call_core
try:
    _r = asyncio.run(_run(_f, _st))
finally:
    _feed._nc_enabled = _nc_save
check(_r["signal"] == "NO_SIGNAL"
      and _r.get("research_direction") == "CALL",
      "D4 legacy CALL path gated → NO_SIGNAL (research CALL preserved)")

# D5 — history-gate-suppressed NEUTRAL path
_f = fresh_feed()
_st = make_stream()
_feed._nc_enabled = False


async def _suppressed_core(asset, period, closed, ticks,
                           running_ticks=None, stream=None):
    return ({"signal": "NEUTRAL", "confidence": 0, "strength": "NEUTRAL",
             "score": 0, "_history_gate_suppressed": True,
             "reasons": ["fixture: measured-bad pair"], "regime": {}}, None)


_f._analyze_core = _suppressed_core
try:
    _r = asyncio.run(_run(_f, _st))
finally:
    _feed._nc_enabled = _nc_save
check(_r["signal"] == "NO_SIGNAL"
      and _r["reason_code"] == "BELOW_ELIGIBILITY_THRESHOLD",
      "D5 suppressed-NEUTRAL path gated (honest abstention)")

# D6 — market-closed guard → None (non-emission, no provenance row)
_f = fresh_feed()
_st = make_stream()
_st.market_closed = True
_before = _db.provenance_counts()["total"]
_r = asyncio.run(_run(_f, _st))
check(_r is None and _db.provenance_counts()["total"] == _before,
      "D6 market-closed → no emission, no provenance row")

# D7 — exception inside the engine chain → INTERNAL_ERROR, never CALL/PUT
_f = fresh_feed()
_st = make_stream()
_feed._nc_enabled = False


async def _boom_core(asset, period, closed, ticks, running_ticks=None,
                     stream=None):
    raise RuntimeError("engine exploded (fixture)")


_f._analyze_core = _boom_core
try:
    _r = asyncio.run(_run(_f, _st))
finally:
    _feed._nc_enabled = _nc_save
check(_r["signal"] == "NO_SIGNAL" and _r["reason_code"] == "INTERNAL_ERROR",
      "D7 engine exception → NO_SIGNAL/INTERNAL_ERROR (fail-closed)")
check(_db.provenance_counts()["by_reason_code"].get("INTERNAL_ERROR", 0) >= 1,
      "D7b INTERNAL_ERROR provenance row persisted")

# D8 — zero-tick candle guard → None (non-emission)
_f = fresh_feed()
_st = make_stream(tick_count=0)
_st.ticks.clear()
_r = asyncio.run(_run(_f, _st))
check(_r is None, "D8 zero-tick candle → no emission")

# D9 — gate module unavailable → fail-closed NO_SIGNAL (never open)
_f = fresh_feed()
_st = make_stream()
_sc_save = _feed._sc
_feed._sc = None
try:
    _r = asyncio.run(_run(_f, _st))
finally:
    _feed._sc = _sc_save
check(_r["signal"] == "NO_SIGNAL" and _r["reason_code"] == "INTERNAL_ERROR",
      "D9 contract module unavailable → NO_SIGNAL (fail-closed)")

# D10 — gate itself raising → fail-closed NO_SIGNAL
_f = fresh_feed()
_st = make_stream()
_orig_apply = _feed._sc.apply_truth_layer
_feed._sc.apply_truth_layer = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("gate exploded (fixture)"))
try:
    _r = asyncio.run(_run(_f, _st))
finally:
    _feed._sc.apply_truth_layer = _orig_apply
check(_r["signal"] == "NO_SIGNAL" and _r["reason_code"] == "INTERNAL_ERROR",
      "D10 gate exception → NO_SIGNAL/INTERNAL_ERROR (fail-closed)")

# D11 — frozen-first-write: re-emission for the SAME target candle does
# not add a second provenance row (research history never rewritten).
_f = fresh_feed()
_st = make_stream()
_feed._nc_enabled = False
_f._analyze_core = _call_core
try:
    _r1 = asyncio.run(_run(_f, _st))
    _r2 = asyncio.run(_run(_f, _st))
finally:
    _feed._nc_enabled = _nc_save
_tgt = _st.candles[-1]["time"] + 60
_rows = [r for r in _db.provenance_recent("EURUSD_otc")
         if r.get("target_candle_open_epoch") == _tgt]
check(_r1["signal"] == "NO_SIGNAL" and _r2["signal"] == "NO_SIGNAL"
      and len(_rows) == 1,
      "D11 same-target re-emission → exactly one provenance row")

# ── E. signal_log isolation + provenance persistence + parity ──────────
print("\n[E] Ledger isolation, persistence, REST/WS parity")

# E1 — fade_default produces ZERO actionable signal_log rows (A.3 #4):
# grading only logs CALL/PUT predictions; a gated NO_SIGNAL is never a
# trade. Simulate the grader's own predicate on the gated dict.
_r = gated(directional("CALL", 50))
_gradeable = _r.get("signal") in ("CALL", "PUT")
check(_gradeable is False,
      "E1 gated fade/NO_SIGNAL is never gradeable into signal_log")
_acc = _feed.QuotexFeed._accuracy(
    None, {"close": 1.0, "open": 0.9},
    {"signal": "NO_SIGNAL", "reason_code": "TIMING_UNVERIFIED"})
check(_acc is None,
      "E1b grader skips NO_SIGNAL predictions (win-rate ledger clean)")

# E2 — provenance persistence across a process RESTART (subprocess)
_counts = _db.provenance_counts()
_probe = (
    "import json, sys; sys.path.insert(0, %r); import db; "
    "c = db.provenance_counts(); print(json.dumps(c))"
) % _REPO
_env = dict(os.environ)
_env["DB_PATH"] = os.path.join(_TMPDIR, "signals.db")
_out = subprocess.run([sys.executable, "-c", _probe], env=_env,
                      capture_output=True, text=True, timeout=60)
try:
    _fresh = json.loads(_out.stdout.strip().splitlines()[-1])
except Exception as _exc:
    _fresh = {"error": f"{_exc}: {_out.stderr[-200:]}"}
check(_fresh.get("total") == _counts["total"] and _counts["total"] > 0,
      "E2 provenance rows survive a fresh process (restart safety)",
      f"parent={_counts} fresh={_fresh}")

# E3 — WS payload parity: the gated dict (exactly what the tick/eoc
# broadcasts carry as msg.prediction) holds the full truth field set.
_TRUTH_FIELDS = ("signal", "truth_status", "reason_code",
                 "research_direction", "research_confidence",
                 "research_strategy", "research_only",
                 "eligibility_policy_version",
                 "contract_schema_version", "created_at_utc",
                 "data_source_authorization", "signal_id")
_missing = [k for k in _TRUTH_FIELDS if k not in _r]
check(not _missing, "E3 WS prediction payload carries all truth fields",
      f"missing {_missing}")

# E4 — REST parity (source-level regression guard): /api/signals/latest
# must extract the same truth fields + None-safe confidence.
_srv = open(os.path.join(_REPO, "server.py"), encoding="utf-8").read()
_missing_srv = [k for k in _TRUTH_FIELDS if f'"{k}"' not in _srv]
check(not _missing_srv,
      "E4 REST /api/signals/latest carries the same truth fields",
      f"missing {_missing_srv}")
check('"confidence": (round(confidence, 1)' in _srv
      and "if isinstance(confidence, (int, float))" in _srv,
      "E4b REST confidence is None-safe (no fabricated 0)")
check('"/api/truth-layer"' in _srv or '@app.get("/api/truth-layer")' in _srv,
      "E4c /api/truth-layer endpoint exists")
check('"research_only": True' in _srv and '"actionable": False' in _srv,
      "E4d /api/live-prediction labels live_pred research-only")

# E5 — truth_layer_state is exactly what /api/truth-layer serves.
_state = sc.truth_layer_state()
check(_state["actionable_signals_possible"] is False
      and _state["data_source_authorization"] == "unauthorized"
      and _state["policy_version"] == sc.POLICY_VERSION,
      "E5 /api/truth-layer content: blockers reported honestly")

# E6 — provenance counts: zero actionable rows, research reasons present.
_pc = _db.provenance_counts()
check(_pc["actionable"] == 0, "E6 zero actionable provenance rows",
      f"counts={_pc}")
check(("TIMING_UNVERIFIED" in _pc["by_reason_code"]
       or "DATA_SOURCE_UNAUTHORIZED" in _pc["by_reason_code"]),
      "E6b research reason codes recorded in provenance")

# E7 — structural INSERT column parity (_SP_COLUMNS vs actual schema).
_db.init()
with _db._read_cursor() as _c:
    _cols = [r["name"] for r in _c.execute(
        "PRAGMA table_info(signal_provenance)").fetchall()]
check(set(_db._SP_COLUMNS) <= set(_cols) and len(_cols) == len(
    _db._SP_COLUMNS) + 1,  # + id
    "E7 _SP_COLUMNS matches signal_provenance schema exactly",
    f"schema={_cols}")

# E8 — baseline signal_log writer still works unchanged (no regression).
_db.log_signal("TESTPAIR_otc", 60, int(time.time()), "PENDING", 0, None,
               "[]", None, "pending", strength="NEUTRAL", agree=0)
with _db._read_cursor() as _c:
    _n = _c.execute("SELECT COUNT(*) FROM signal_log "
                    "WHERE asset='TESTPAIR_otc'").fetchone()[0]
check(_n == 1, "E8 baseline signal_log INSERT unchanged")

# ── F. migration safety ────────────────────────────────────────────────
print("\n[F] Additive migration safety")
_before_total = _db.provenance_counts()["total"]
with _db._read_cursor() as _c:
    _before_sl = _c.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
_db.init()  # idempotent re-run
_db.init()
check(_db.provenance_counts()["total"] == _before_total,
      "F1 init() idempotent — provenance rows preserved across re-runs")
with _db._read_cursor() as _c:
    _after_sl = _c.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
check(_after_sl == _before_sl,
      "F2 signal_log rows preserved (no rewrite/delete)")

# F3 — retention: provenance is count-pruned like signal_log, NEVER added
# to the 60-minute time purge (_POLICY).
import core.retention as _retention  # noqa: E402
check(all(t[0] != "signal_provenance" for t in _retention._POLICY),
      "F3 signal_provenance NOT in the 60-min time-purge policy")
_stats = _retention.apply_retention(ohlc_secs=3600, data_secs=60)
check(_db.provenance_counts()["total"] == _before_total,
      "F3b retention pass preserves young provenance rows "
      "(count-based only)")

# ── summary ─────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"PHASE-1 TRUTH LAYER: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for f in _FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
