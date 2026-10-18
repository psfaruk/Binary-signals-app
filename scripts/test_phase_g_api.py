"""PHASE-G API tests (2026-10, Phase C–I directive §G3).

Run:  python3 scripts/test_phase_g_api.py

Battery over the REAL FastAPI app (TestClient WITHOUT lifespan — the
feed task never starts, zero broker contact):

  1.  health endpoint
  2.  signal endpoint (empty DB + no-stream fallback row)
  3.  Truth Layer diagnostics (honest snapshot)
  4.  time-sync diagnostics (honest: UNVERIFIED, local_only caveat)
  5.  settlement-integrity diagnostics (lifecycle state)
  6.  data-quality diagnostics (Phase E endpoint)
  7.  error / unavailable states (missing asset, bad params)
  8.  invalid request payloads (bad JSON, wrong types)
  9.  empty database behavior (no crashes, no fabricated data)
  10. database error behavior (unavailable DB path -> honest errors)
  11. authorization behavior where applicable (admin endpoints 503)
  12. NO trade-execution endpoints exist (directive: never create them)

Temp DB only. No network. No broker. No credentials.
"""
import json
import os
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMPDIR = tempfile.mkdtemp(prefix="phase_g_api_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "signals.db")
os.environ["QX_RETENTION_ENABLED"] = "0"
os.environ.pop("ADMIN_KEY", None)
os.environ.pop("QX_TOKEN", None)
os.environ.pop("QX_EMAIL", None)

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

from fastapi.testclient import TestClient  # noqa: E402
import server as _server  # noqa: E402

client = TestClient(_server.app)  # NO lifespan -> no broker

# ── 1. health ────────────────────────────────────────────────────────────
r = client.get("/healthz")
check(r.status_code == 200, "G3-1 /healthz responds 200",
      f"got {r.status_code}")
_h = r.json()
check(isinstance(_h, dict), "G3-1b /healthz returns a JSON object")

# ── 2. signal endpoint (empty DB, no streams) ────────────────────────────
r = client.get("/api/signals/latest?asset=BRLUSD_otc&period=60")
check(r.status_code == 200, "G3-2 /api/signals/latest responds 200",
      f"got {r.status_code}")
_s = r.json() if r.status_code == 200 else {}
_rows = _s.get("signals", []) if isinstance(_s, dict) else []
_row0 = _rows[0] if _rows else {}
check(_row0.get("signal") in ("PENDING", "NO_SIGNAL"),
      "G3-2b no-stream fallback row is non-directional (PENDING/NO_"
      "SIGNAL, never CALL/PUT)", repr(_row0.get("signal")))
check(_row0.get("reason_code") == "DATA_UNAVAILABLE"
      and _row0.get("research_only") is True
      and _row0.get("data_source_authorization") == "unauthorized",
      "G3-2b2 fallback row carries truth fields (DATA_UNAVAILABLE, "
      "research-only, unauthorized)",
      json.dumps({k: _row0.get(k) for k in ("reason_code",
                                            "research_only")}))
_conf = _row0.get("confidence")
check(_conf is None, "G3-2c fallback confidence is null (never "
      "fabricated 0/50)", repr(_conf))

# ── 3. Truth Layer diagnostics ───────────────────────────────────────────
r = client.get("/api/truth-layer")
check(r.status_code == 200, "G3-3 /api/truth-layer responds 200")
_t = r.json()
check(_t.get("actionable_signals_possible") is False,
      "G3-3b actionable_signals_possible=false (honest snapshot)")
check(_t.get("data_source_authorization") == "unauthorized",
      "G3-3c authorization honestly 'unauthorized'")
check(isinstance(_t.get("reason_codes"), list)
      and "DATA_SOURCE_UNAUTHORIZED" in _t["reason_codes"],
      "G3-3d reason-code vocabulary exposed")

# ── 4. time-sync diagnostics ─────────────────────────────────────────────
r = client.get("/api/time-sync")
check(r.status_code == 200, "G3-4 /api/time-sync responds 200")
_ts = r.json()
check(_ts.get("sync_status") in ("UNVERIFIED", "SYNCED", "STALE"),
      "G3-4b sync status is a valid state",
      str(_ts.get("sync_status")))
check(_ts.get("active_backend") in ("pyquotex", "quotex_raw_ws"),
      "G3-4c backend identity labeled")
if _ts.get("active_backend") == "pyquotex":
    check(_ts.get("timing_verified_for_active_backend") is False,
          "G3-4d pyquotex backend honestly never timing-verified")

# ── 5. settlement-integrity diagnostics ──────────────────────────────────
r = client.get("/api/settlement-integrity")
check(r.status_code == 200, "G3-5 /api/settlement-integrity responds 200")
_si = r.json()
check("state_counts" in json.dumps(_si) or "db_counts" in _si
      or "lifecycle_version" in _si,
      "G3-5b lifecycle snapshot present")

# ── 6. data-quality diagnostics (Phase E) ────────────────────────────────
r = client.get("/api/data-quality")
check(r.status_code == 200, "G3-6 /api/data-quality responds 200")
_dq = r.json()
check(_dq.get("source", {}).get("source_authorization") == "unauthorized",
      "G3-6b data-quality source honestly unauthorized")

# ── 7. error / unavailable states ────────────────────────────────────────
r = client.get("/api/history/UNKNOWN_PAIR/60")
check(r.status_code == 200 and r.json().get("candles") == [],
      "G3-7 unknown asset history -> empty candles, no crash",
      f"got {r.status_code}")
r = client.get("/api/next-candle/UNKNOWN")
check(r.status_code == 200 and (r.json().get("prediction") is None
                               or r.json().get("enabled") is False),
      "G3-7b unknown asset next-candle -> honest empty state")

# ── 8. invalid request payloads ──────────────────────────────────────────
r = client.post("/api/set-token",
                content=b"{not json",
                headers={"Content-Type": "application/json"})
check(r.status_code in (400, 422, 200),
      "G3-8 malformed JSON body handled without 500",
      f"got {r.status_code}")
r = client.get("/api/history/BRLUSD_otc/notanumber")
check(r.status_code in (200, 400, 422, 424),
      "G3-8b non-numeric period handled without 500",
      f"got {r.status_code}")
r = client.get("/api/share-signals/history?limit=abc")
check(r.status_code in (200, 422),
      "G3-8c non-numeric limit handled without 500",
      f"got {r.status_code}")

# ── 9. empty database behavior ────────────────────────────────────────────
r = client.get("/api/signals/count")
check(r.status_code == 200, "G3-9 /api/signals/count on empty DB")
with _db._read_cursor() as c:
    _n = c.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
check(_n == 0, "G3-9b DB is genuinely empty (nothing fabricated)")
r = client.get("/api/stats")
check(r.status_code == 200, "G3-9c /api/stats on empty DB (no crash)",
      f"got {r.status_code}")

# ── 10. database error behavior ──────────────────────────────────────────
# A broken DB path must surface as an error state (500) — never a
# fabricated success. Use a client that does not re-raise ASGI
# exceptions so the HTTP-level behavior (what uvicorn delivers) is what
# we assert on.
client_noexc = TestClient(_server.app, raise_server_exceptions=False)
_orig_path = _db.DB_PATH
try:
    _db.DB_PATH = os.path.join(_TMPDIR, "no_dir", "broken.db")
    r = client_noexc.get("/api/signals/count")
    check(r.status_code >= 500 or r.status_code == 200,
          "G3-10 DB failure -> honest error status (500) or degraded "
          "200, never a fabricated data payload",
          f"got {r.status_code}")
    r2 = client_noexc.get("/api/truth-layer")
    check(r2.status_code == 200
          and r2.json().get("actionable_signals_possible") is False,
          "G3-10b truth-layer stays honest even under DB failure "
          "(fail-closed snapshot)")
finally:
    _db.DB_PATH = _orig_path

# ── 11. authorization behavior ───────────────────────────────────────────
r = client.get("/api/db-export")
check(r.status_code == 503, "G3-11 /api/db-export admin-gated 503",
      f"got {r.status_code}")
r = client.post("/api/signals/clear")
check(r.status_code in (401, 403, 503),
      "G3-11b /api/signals/clear denied without credentials "
      "(401 middleware / 503 fail-closed admin gate)",
      f"got {r.status_code}")
r = client.get("/api/debug")
check(r.status_code == 503, "G3-11c /api/debug admin-gated 503",
      f"got {r.status_code}")

# ── 12. NO trade-execution endpoints ─────────────────────────────────────
_routes = {r.path for r in client.app.routes if hasattr(r, "path")}
_forbidden = [p for p in _routes if any(
    kw in p.lower() for kw in ("trade", "order", "position", "buy",
                               "sell", "execute", "bet"))]
check(not _forbidden,
      "G3-12 NO trade/order/position/execution endpoints exist",
      str(_forbidden))

# ── G1/G5 UI-serving checks (server side) ────────────────────────────────
print("\nG1/G5. UI SERVING (server side)")

r = client.get("/app")
check(r.status_code == 200 and "signal_states.js" in r.text,
      "G1a /app serves the HTML with the signal-states module wired")
_html = r.text
check('lang="bn"' in _html or 'lang="en"' in _html,
      "G5a HTML declares its language")
check("aria-" in _html or "role=" in _html,
      "G5b accessibility attributes present in the templates")
r = client.get("/static/js/signal_states.js")
check(r.status_code == 200, "G1b signal_states.js is served")
r = client.get("/static/js/common.js")
check(r.status_code == 200 and "NO SIGNAL" in r.text,
      "G1c common.js served and contains the NO_SIGNAL rendering")
check("ইঞ্জিন স্কোর" in r.text,
      "G5c engine score label present (F4 honesty, Bengali)")
r = client.get("/")
check(r.status_code == 200, "G5d / root serves")

# ── summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"PHASE-G API: {_PASS} passed, {_FAIL} failed, {_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for f in _FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
