"""scripts/test_phase_j_security.py — PHASE-J security hardening battery.

Covers the four Phase-J fixes (owner authorization 2026-10: "সব গুলো deeply
সমাধান করো" — closing Phase-D documented blockers B3/B4):

  A. TOKEN-PUSH OPERATOR GATE (B3)
     — set-token POST/GET, session/cookies, session/refresh, reconnect
       all require ADMIN_KEY or a claimed+verified PIN (fail-closed 401
       with setup-code guidance on fresh deployments).
     — GET /api/set-token?token=... is REJECTED (410) — the full token
       must never appear in a URL (access-log leak fix).
     — GET /api/set-token with X-Qx-Token header + PIN works.
  B. FIRST-CLAIM SETUP CODE (B4a)
     — claim without code / wrong code -> 403; right code -> 200;
       code erased; second claim rejected; ADMIN_KEY path unaffected;
       expired code rejected.
  C. REAL RATE LIMITING (B4b)
     — 429 + Retry-After + truthful X-RateLimit-* headers; per-key caps
       for api_key_write; kill switch; window rollover.
  D. CROSS-PROCESS SETTLEMENT SAFETY (B4c)
     — two REAL subprocesses racing finalize/settle/shadow ops;
       exactly one winner per transition; conflict audited; correction
       history loses ZERO entries.
  E. LEAK-FIX REGRESSIONS
     — feed.py session.json print is 6+4-masked (source-level);
       alerts.py no longer advertises the token-in-URL form.

Run: python3 scripts/test_phase_j_security.py   (exit 0 = all pass)
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMPDIR = tempfile.mkdtemp(prefix="phase_j_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "signals.db")
os.environ["QX_RETENTION_ENABLED"] = "0"
os.environ["QX_STATE_DIR"] = os.path.join(_TMPDIR, "state")
os.environ["QX_SESSION_PATH"] = os.path.join(_TMPDIR, "session.json")
os.environ.pop("ADMIN_KEY", None)
os.environ.pop("QX_TOKEN", None)
os.environ.pop("QX_EMAIL", None)
os.environ["QX_RATE_LIMIT"] = "0"     # off for A/B; C turns it on per-test

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
        _FAILURES.append(label)
        print(f"  FAIL {label}" + (f"  [{detail}]" if detail else ""))


print("=" * 72)
print("PHASE-J SECURITY HARDENING BATTERY")
print("=" * 72)

# ── shared app instance (no lifespan -> no broker) ─────────────────────
from fastapi.testclient import TestClient  # noqa: E402
import server as _server  # noqa: E402
from core import token_store as ts  # noqa: E402
from core import rate_limit as rl  # noqa: E402

client = TestClient(_server.app, raise_server_exceptions=False)

# ══ A. TOKEN-PUSH OPERATOR GATE ════════════════════════════════════════
print("\nA. TOKEN-PUSH OPERATOR GATE (B3)")

r = client.post("/api/set-token", json={"token": "x" * 30})
check(r.status_code == 401 and r.json().get("setup_code_required") is True,
      "A1 POST set-token no-cred -> 401 + setup-code guidance",
      f"got {r.status_code}")
r = client.get("/api/set-token?token=A" * 1)
check(r.status_code == 410,
      "A2 GET set-token?token=... -> 410 (token-in-URL REMOVED)",
      f"got {r.status_code}")
for method, path in [("post", "/api/session/cookies"),
                     ("post", "/api/session/refresh"),
                     ("post", "/api/reconnect"),
                     ("get", "/api/reconnect")]:
    if method == "post":
        r = client.post(path, json={})
    else:
        r = client.get(path)
    check(r.status_code == 401,
          f"A3 {method.upper()} {path} gated -> 401",
          f"got {r.status_code}")

# claim a PIN via the setup code (flow verified fully in section B)
_code = ts.ensure_setup_code()
r = client.post("/api/auth/claim",
                json={"pin": "123456", "setup_code": _code})
check(r.status_code == 200 and r.json().get("ok") is True,
      "A4 setup-code claim succeeds (precondition for gated pushes)",
      r.text[:120])

r = client.post("/api/set-token", json={"token": "x" * 30},
                headers={"X-App-Pin": "999999"})
check(r.status_code == 403,
      "A5 POST set-token WRONG PIN -> 403", f"got {r.status_code}")
r = client.post("/api/set-token", json={"token": "shortandbad"},
                headers={"X-App-Pin": "123456"})
check(r.status_code == 200 and r.json().get("ok") is False
      and r.json().get("error"),
      "A6 POST set-token right PIN passes gate; invalid token honestly "
      "rejected", r.text[:120])
r = client.get("/api/set-token",
               headers={"X-Qx-Token": "y" * 30, "X-App-Pin": "123456"})
check(r.status_code == 200,
      "A7 GET set-token via X-Qx-Token header + PIN -> 200",
      f"got {r.status_code}")
r = client.get("/api/set-token", headers={"X-Qx-Token": "y" * 30})
check(r.status_code == 401,
      "A8 GET set-token header-form WITHOUT PIN -> 401",
      f"got {r.status_code}")
# public reads unaffected
ok_public = True
for p in ["/api/truth-layer", "/api/auth/state", "/api/session/status",
          "/api/signals/latest", "/api/stats", "/api/data-quality"]:
    if client.get(p).status_code != 200:
        ok_public = False
check(ok_public, "A9 public read endpoints unaffected (200)")

# ══ B. FIRST-CLAIM SETUP CODE ══════════════════════════════════════════
print("\nB. FIRST-CLAIM SETUP CODE (B4a)")

# fresh state dir for a clean claim flow
state2 = tempfile.mkdtemp(prefix="phase_j_state2_")
os.environ["QX_STATE_DIR"] = state2
# token_store reads env per call, so this re-points it.

r = client.post("/api/auth/claim", json={"pin": "654321"})
check(r.status_code == 403 and r.json().get("setup_code_required") is True,
      "B1 claim without setup code -> 403", f"got {r.status_code}")
r = client.post("/api/auth/claim",
                json={"pin": "654321", "setup_code": "WRONG-CODE-NOW"})
check(r.status_code == 403,
      "B2 claim with WRONG code -> 403", f"got {r.status_code}")
code2 = ts.ensure_setup_code()
check(bool(re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}", code2)),
      "B3 generated code format XXXX-XXXX-XXXX", str(code2))
r = client.post("/api/auth/claim",
                json={"pin": "654321", "setup_code": code2})
check(r.status_code == 200 and r.json().get("ok") is True,
      "B4 claim with RIGHT code -> 200", r.text[:120])
auth_file = os.path.join(state2, "app_auth.json")
rec = json.load(open(auth_file))
check("setup_code_hash" not in rec and rec.get("pin_hash"),
      "B5 setup code ERASED after claim; PIN hash stored")
r = client.post("/api/auth/claim",
                json={"pin": "999999", "setup_code": code2})
check(r.status_code == 400 and not r.json().get("ok"),
      "B6 second claim rejected even with the (now dead) code",
      f"got {r.status_code}")

# expired code rejected (simulate by backdating)
state3 = tempfile.mkdtemp(prefix="phase_j_state3_")
os.environ["QX_STATE_DIR"] = state3
code3 = ts.ensure_setup_code()
rec3 = json.load(open(os.path.join(state3, "app_auth.json")))
rec3["setup_code_created"] = time.time() - (25 * 3600)   # > 24h TTL
json.dump(rec3, open(os.path.join(state3, "app_auth.json"), "w"))
r = client.post("/api/auth/claim",
                json={"pin": "654321", "setup_code": code3})
check(r.status_code == 403,
      "B7 EXPIRED code (>24h) rejected", f"got {r.status_code}")

# ADMIN_KEY path: no setup code needed when ADMIN_KEY is set
state4 = tempfile.mkdtemp(prefix="phase_j_state4_")
os.environ["QX_STATE_DIR"] = state4
os.environ["ADMIN_KEY"] = "phase-j-admin-key"
try:
    r = client.post("/api/auth/claim", json={"pin": "7654321"},
                    headers={"X-Admin-Key": "phase-j-admin-key"})
    check(r.status_code == 200 and r.json().get("ok") is True,
          "B8 ADMIN_KEY claim without setup code -> 200", r.text[:120])
    # wrong admin key on a FRESH unclaimed state (fail-closed check)
    state5 = tempfile.mkdtemp(prefix="phase_j_state5_")
    os.environ["QX_STATE_DIR"] = state5
    r = client.post("/api/auth/claim", json={"pin": "111111"},
                    headers={"X-Admin-Key": "definitely-wrong-key"})
    check(r.status_code == 403,
          "B9 ADMIN_KEY set but WRONG key -> 403 (fail-closed)",
          f"got {r.status_code}")
    os.environ["QX_STATE_DIR"] = state4
    r = client.post("/api/set-token", json={"token": "z" * 30},
                    headers={"X-Admin-Key": "phase-j-admin-key"})
    check(r.status_code == 200,
          "B10 token push via ADMIN_KEY -> 200", f"got {r.status_code}")
finally:
    os.environ.pop("ADMIN_KEY", None)

# restore the section-A PIN state for later sections
os.environ["QX_STATE_DIR"] = os.path.join(_TMPDIR, "state")

# ══ C. REAL RATE LIMITING ══════════════════════════════════════════════
print("\nC. REAL RATE LIMITING (B4b)")
os.environ["QX_RATE_LIMIT"] = "1"
rl.reset()

os.environ["QX_RL_AUTH"] = "3"
statuses = [client.post("/api/auth/verify", json={"pin": "123456"}).status_code
            for _ in range(5)]
check(statuses[:3] == [200, 200, 200] and 429 in statuses[3:],
      "C1 auth bucket: first 3 pass, 4th -> 429", str(statuses))
r = client.post("/api/auth/verify", json={"pin": "123456"})
check(r.status_code == 429 and r.headers.get("Retry-After")
      and r.headers.get("X-RateLimit-Remaining") == "0"
      and r.headers.get("X-RateLimit-Limit") == "3",
      "C2 429 carries Retry-After + truthful X-RateLimit-* headers",
      str(dict(r.headers)))
os.environ.pop("QX_RL_AUTH", None)
rl.reset()

# token_push bucket
os.environ["QX_RL_TOKEN_PUSH"] = "2"
sts = [client.post("/api/reconnect", json={},
                   headers={"X-App-Pin": "123456"}).status_code
       for _ in range(4)]
check(200 in sts and sts.count(429) >= 1,
      "C3 token_push bucket limit enforced (429 appears)", str(sts))
os.environ.pop("QX_RL_TOKEN_PUSH", None)
rl.reset()

# global bucket (generous default — verify headers present, not a block)
r = client.get("/api/signals/latest")
check(r.headers.get("X-RateLimit-Limit") is not None
      and r.headers.get("X-RateLimit-Remaining") is not None,
      "C4 global bucket carries truthful headers on success",
      str(dict(r.headers)))
rl.reset()

# per-API-key cap finally enforced
from core import api_keys  # noqa: E402
raw, info = api_keys.create_key("phase-j-test", rate_limit_per_min=3)
r = client.post("/api/share-signals/save",
                headers={"Authorization": f"Bearer {raw}"}, json={})
sts = [client.post("/api/share-signals/save",
                   headers={"Authorization": f"Bearer {raw}"}, json={}
                   ).status_code for _ in range(5)]
check(429 in sts,
      "C5 api-key's OWN advertised cap (3/min) actually enforced",
      str(sts))
api_keys.revoke_key(info.id)
rl.reset()

# kill switch
os.environ["QX_RATE_LIMIT"] = "0"
rl.reset()
sts = [client.post("/api/auth/verify", json={"pin": "123456"}).status_code
       for _ in range(15)]
check(all(s == 200 for s in sts),
      "C6 kill switch QX_RATE_LIMIT=0 disables enforcement", str(set(sts)))
os.environ["QX_RATE_LIMIT"] = "1"
rl.reset()

# ══ D. CROSS-PROCESS SETTLEMENT SAFETY ═════════════════════════════════
print("\nD. CROSS-PROCESS SETTLEMENT SAFETY (B4c)")

WORKER = os.path.join(_REPO, "scripts", "phase_j_race_worker.py")


def race(mode_a, mode_b):
    """Spawn two workers concurrently; return parsed results.

    The schema is initialized FIRST (single subprocess) so the racing
    workers exercise the real tables — the race itself stays strictly
    two-process.
    """
    db_path = os.path.join(tempfile.mkdtemp(prefix="phase_j_race_"),
                           "race.db")
    # 1) schema init (sequential, one process)
    env = dict(os.environ)
    env["DB_PATH"] = db_path
    env["QX_RATE_LIMIT"] = "0"
    init_rc = subprocess.run(
        [sys.executable, "-c",
         "import os,sys; sys.path.insert(0, os.getcwd()); "
         "import db; db.init()"],
        env=env, cwd=_REPO, capture_output=True, timeout=120).returncode
    if init_rc != 0:
        return [{"error": f"schema init failed rc={init_rc}"},
                {"error": "schema init failed"}]
    # 2) the actual two-process race
    go = os.path.join(os.path.dirname(db_path), "GO")
    procs = []
    for i, mode in enumerate((mode_a, mode_b)):
        wenv = dict(env)
        wenv["PHASE_J_VARIANT"] = str(i)   # deterministic per-worker variant
        p = subprocess.Popen(
            [sys.executable, WORKER, mode, go, db_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=wenv,
            text=True, cwd=_REPO)
        procs.append((mode, p))
    time.sleep(0.4)          # both workers are now waiting at the barrier
    open(go, "w").write("go")
    results = []
    for mode, p in procs:
        out, err = p.communicate(timeout=60)
        parsed = None
        for line in out.splitlines():
            if line.startswith("RESULT:"):
                parsed = json.loads(line[7:])
        if parsed is None:
            parsed = {"error": f"no result; rc={p.returncode}; "
                               f"stderr={err[-300:]}"}
        # surface NESTED error-returns (db functions return dicts with
        # an 'error' key instead of raising)
        for a in (parsed.get("actions") or []):
            if isinstance(a, dict) and a.get("error"):
                parsed["error"] = a["error"]
        results.append(parsed)
    return results


res = race("finalize_same", "finalize_same")
acts = [r.get("actions") for r in res if not r.get("error")]
check(all(not r.get("error") for r in res),
      "D1 concurrent finalize_same: no crashes/errors", str(res))
if len(acts) == 2:
    finals = [a[1] for a in acts]
    check(sorted(finals) == ["duplicate", "finalized"],
          "D2 exactly ONE winner; the other is 'duplicate'",
          str(finals))
else:
    check(False, "D2 exactly ONE winner; the other is 'duplicate'",
          "missing results")

res = race("finalize_conflict", "finalize_conflict")
acts = [r.get("actions") for r in res if not r.get("error")]
check(all(not r.get("error") for r in res),
      "D3 concurrent conflicting finalizations: no crashes", str(res))
if len(acts) == 2:
    acts_flat = sorted(a[0] for a in acts)
    check("conflict_rejected" in acts_flat and "finalized" in acts_flat,
          "D4 conflicting close: one freeze wins, other conflict_rejected",
          str(acts_flat))
else:
    check(False, "D4 conflicting close: one freeze wins, other "
                 "conflict_rejected", "missing results")

res = race("settle", "settle")
acts = [r.get("actions") for r in res if not r.get("error")]
check(all(not r.get("error") for r in res),
      "D5 concurrent settle: no crashes", str(res))
if len(acts) == 2:
    st = sorted(a[0] for a in acts)
    check(st == ["already_settled", "settled"],
          "D6 exactly ONE settles; loser already_settled", str(st))
else:
    check(False, "D6 exactly ONE settles; loser already_settled",
          "missing results")

res = race("shadow", "shadow")
check(all(not r.get("error") for r in res),
      "D7 concurrent shadow record/settle/correct: no crashes", str(res))
shared = [r for r in res if r.get("actions") and len(r["actions"]) == 5]
if len(shared) == 2:
    own_counts = [r["actions"][2] for r in shared]
    shared_counts = [r["actions"][3] for r in shared]
    check(all(c == 5 for c in own_counts),
          "D8 own-row correction histories complete (5/5 each)",
          str(own_counts))
    check(any(c == 10 for c in shared_counts),
          "D9 SHARED row correction history: all 10 entries survive "
          "(zero lost updates — the old deferred-tx version could lose "
          "some)", str(shared_counts))
    settled = [r["actions"][1] for r in shared]
    check(all(s is True for s in settled),
          "D10 each worker's own shadow row settled exactly once",
          str(settled))
else:
    check(False, "D8 own-row correction histories complete",
          f"unexpected results {res}")

# ══ E. LEAK-FIX REGRESSIONS ════════════════════════════════════════════
print("\nE. LEAK-FIX REGRESSIONS")

feed_src = open(os.path.join(_REPO, "feed.py"), encoding="utf-8").read()
check("token[:8]" not in feed_src and "token[:6]}…{token[-4:]}" in feed_src,
      "E1 feed.py session.json print now 6+4 masked (was 8-char prefix)")
alerts_src = open(os.path.join(_REPO, "alerts.py"), encoding="utf-8").read()
check("set-token?token=" not in alerts_src,
      "E2 alerts.py no longer advertises the token-in-URL form")
server_src = open(os.path.join(_REPO, "server.py"), encoding="utf-8").read()
check("visit /api/set-token?token=" not in server_src
      and "GET  /api/set-token?token=" not in server_src,
      "E3 server.py boot guidance no longer advertises token-in-URL")
panel_src = open(os.path.join(_REPO, "static", "js", "token-panel.js"),
                 encoding="utf-8").read()
check("X-App-Pin" in panel_src and "tk-claim" in panel_src
      and "authHeaders" in panel_src,
      "E4 token panel sends X-App-Pin + has the first-run claim section")

# ══ summary ═════════════════════════════════════════════════════════════
print()
print("=" * 72)
n_total = _PASS + _FAIL
print(f"PHASE-J SECURITY: {_PASS} passed, {_FAIL} failed, {n_total} total")
if _FAILURES:
    print("FAILURES:")
    for f in _FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
