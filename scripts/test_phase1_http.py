"""PHASE-1 TRUTH LAYER — HTTP endpoint tests (2026-10, rebuild).

Run:  py scripts/test_phase1_http.py

Exercises the REAL FastAPI app (server.py) through fastapi.testclient:
  * GET /api/truth-layer — must report actionable_signals_possible=false
    with the honest blocker state (empty registry, unauthorized source),
  * GET /api/signals/latest — REST truth-field parity (None-safe
    confidence, research fields present on every row),
  * GET /api/live-prediction/{asset} — running-candle diagnostic labeled
    research-only with the truth block,
  * GET /static/js/signal_states.js — the pure mapping module is served.

SAFETY: TestClient is created WITHOUT a lifespan context, so the feed
task (feed.run) never starts — zero broker contact. The local training
daemon started at server import runs on the temp DB only and dies with
this process. No credentials are read or printed.
"""
import json
import os
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TMPDIR = tempfile.mkdtemp(prefix="phase1_http_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "signals.db")
os.environ["QX_PUBLIC_READ"] = "1"
os.environ.pop("QX_TOKEN", None)
os.environ.pop("QX_EMAIL", None)
os.environ.pop("QX_PASSWORD", None)

sys.path.insert(0, _REPO)

import db as _db                     # noqa: E402
import feed as _feed                 # noqa: E402
import server as _server             # noqa: E402  (starts LOCAL daemons only)
from fastapi.testclient import TestClient  # noqa: E402

_db.init()
client = TestClient(_server.app)     # NO lifespan → no feed.run → no broker

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


print("=" * 72)
print("PHASE-1 HTTP ENDPOINT TESTS")
print("=" * 72)

# ── /api/truth-layer ────────────────────────────────────────────────────
print("\n[H1] GET /api/truth-layer")
r = client.get("/api/truth-layer")
check(r.status_code == 200, "status 200", str(r.status_code))
body = r.json()
check(body.get("actionable_signals_possible") is False,
      "actionable_signals_possible = false", json.dumps(body)[:200])
check(body.get("data_source_authorization") == "unauthorized",
      "data_source_authorization = unauthorized")
reg = body.get("validation_registry") or {}
check(reg.get("entries") == 0 and reg.get("exists") is False,
      "registry absent/empty (honest: nothing validated)")
check(len(body.get("reason_codes") or []) == 13,
      "13 reason codes listed")
check(body.get("policy_version") and body.get("contract_schema_version"),
      "policy/contract versions present")
prov = body.get("emission_provenance") or {}
check(prov.get("actionable") == 0,
      "emission provenance: zero actionable rows",
      json.dumps(prov)[:200])
_blob = json.dumps(body)
check("QX_TOKEN" not in _blob and "password" not in _blob.lower(),
      "no credential-shaped content in the public snapshot")

# ── /api/signals/latest — REST truth parity ─────────────────────────────
print("\n[H2] GET /api/signals/latest")
r = client.get("/api/signals/latest")
check(r.status_code == 200, "status 200", str(r.status_code))
body = r.json()
signals = body.get("signals") or []
check(isinstance(signals, list), "signals is a list")
if signals:
    row = signals[0]
    for k in ("truth_status", "reason_code", "research_only",
              "research_direction", "data_source_authorization",
              "signal_id"):
        if k not in row:
            check(False, "row carries truth fields", f"missing {k}")
            break
    else:
        check(True, "row carries truth fields")
    check(row.get("signal") in ("PENDING", "NO_SIGNAL", "CALL", "PUT",
                                "NEUTRAL"),
          "signal value sane", str(row.get("signal")))
    check("confidence" in row, "confidence key present")
    check(row.get("confidence") is None or isinstance(row.get("confidence"),
                                                       (int, float)),
          "confidence None-safe (null, never fabricated)")
    check(row.get("reason_code") == "DATA_UNAVAILABLE"
          and row.get("research_only") is True,
          "no-stream fallback row honestly reports DATA_UNAVAILABLE",
          json.dumps({k: row.get(k) for k in
                      ("signal", "reason_code", "research_only")}))
else:
    check(True, "no running streams → empty list is honest")

# ── /api/live-prediction — research labeling ────────────────────────────
print("\n[H3] GET /api/live-prediction/{asset}")
r = client.get("/api/live-prediction/EURUSD_otc")
check(r.status_code == 200, "status 200 (stream not running is valid)",
      str(r.status_code))
check(r.json().get("error") == "stream not running",
      "honest 'stream not running' when no stream")

# inject a FAKE stream (no client, no ticks — pure state) and verify the
# truth block labels the diagnostic research-only.  NOTE: the stream
# registry lives on the server's module-level QuotexFeed INSTANCE
# (server.feed), NOT on the feed module (server.py L149: feed = _Feed()).
_fake = _feed._AssetStream(asset="EURUSD_otc", period=60)
_fake.prediction = None
_fake._live_pred = {"ready": True, "direction": "CALL", "confidence": 70,
                    "p_close_green": 0.7, "phase": "MID"}
_streams = getattr(_server.feed, "_streams", None)
check(isinstance(_streams, dict),
      "server.feed._streams registry accessible")
if _streams is None:
    _streams = {}
_server.feed._streams[("EURUSD_otc", 60)] = _fake
try:
    r = client.get("/api/live-prediction/EURUSD_otc")
    body = r.json()
    truth = body.get("truth") or {}
    check(body.get("live_pred", {}).get("direction") == "CALL",
          "live_pred diagnostic present")
    check(truth.get("research_only") is True and truth.get("actionable") is False,
          "truth block: research_only=true, actionable=false",
          json.dumps(truth)[:200])
    check(truth.get("reason_code") == "DATA_SOURCE_UNAUTHORIZED",
          "truth block reason = DATA_SOURCE_UNAUTHORIZED")
    check(truth.get("actionable_signals_possible") is False,
          "truth block echoes actionable_signals_possible=false")
finally:
    _server.feed._streams.pop(("EURUSD_otc", 60), None)

# ── static mapping module is served ─────────────────────────────────────
print("\n[H4] GET /static/js/signal_states.js")
r = client.get("/static/js/signal_states.js")
check(r.status_code == 200, "status 200", str(r.status_code))
check("mapSignalState" in r.text and "ALL_REASON_CODES" in r.text,
      "the pure mapping module is served to the browser")

r = client.get("/")
check(r.status_code == 200 and "signal_states.js" in r.text,
      "app.html includes signal_states.js before common.js")

# ── summary ─────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"PHASE-1 HTTP: {_PASS} passed, {_FAIL} failed, {_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for f in _FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
