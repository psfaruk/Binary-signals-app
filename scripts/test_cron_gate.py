"""CRON-GATE SECURITY TESTS (audit fix C-1, 2026-11-27).

Run:  py scripts/test_cron_gate.py

Exercises the REAL cron app (api/cron/refresh.py) through
fastapi.testclient against the SECURITY-FIX (fail-closed CRON_SECRET):

  R1  no CRON_SECRET configured        -> 503 (fail-closed, nobody in)
  R2  secret set, no auth header        -> 401
  R3  secret set, spoofed x-vercel-cron header alone -> 401
      (regression: the pre-fix code let ANY client through with a
       forgeable `x-vercel-cron: 1` header, even with CRON_SECRET set)
  R4  secret set, secret in QUERY STRING-> 401
      (regression: secrets must never ride in URLs / access logs)
  R5  secret set, wrong Bearer          -> 401
  R6  secret set, CORRECT Bearer        -> gate passes; with QX_TOKEN
      unset the endpoint then refuses with its own 503 "QX_TOKEN not
      set" — which is the distinct post-gate response proving the
      bearer was accepted without ever starting a burst.
  R7  correct Bearer + x-vercel-cron     -> gate still passes (header is
      accepted alongside the secret, never instead of it).

SAFETY: TestClient is created WITHOUT a lifespan context; QX_TOKEN is
unset for every case, so the burst path can never run. No credentials
are read or printed. Temp DB only.
"""
import os
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TMPDIR = tempfile.mkdtemp(prefix="cron_gate_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "signals.db")
os.environ["AUTO_OPEN_BROWSER"] = "0"
os.environ["QX_SKIP_DOTENV"] = "1"
os.environ.pop("QX_TOKEN", None)
os.environ.pop("QX_EMAIL", None)
os.environ.pop("QX_PASSWORD", None)
os.environ.pop("QX_COOKIES", None)
os.environ.pop("CRON_SECRET", None)

sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "api", "cron"))

# Import the cron app module the way Vercel does (api/cron/refresh.py).
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "cron_refresh_module", os.path.join(_REPO, "api", "cron", "refresh.py"))
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(_mod.app)  # NO lifespan -> no burst can ever start

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
print("CRON GATE SECURITY TESTS (audit fix C-1)")
print("=" * 72)

# ── R1: fail-closed when CRON_SECRET unset ─────────────────────────────
r = client.get("/api/cron/refresh")
check(r.status_code == 503, "R1 unset-secret -> 503 (fail-closed)",
      f"got {r.status_code} {r.text[:80]}")
check("CRON_SECRET is not set" in r.text,
      "R1 body names the missing env var", r.text[:120])

# ── R2..R5: configured, unauthenticated variants ──────────────────────
_SECRET = "test-cron-secret-0123456789abcdef"
os.environ["CRON_SECRET"] = _SECRET

r = client.get("/api/cron/refresh")
check(r.status_code == 401, "R2 no auth header -> 401",
      f"got {r.status_code}")

r = client.get("/api/cron/refresh",
               headers={"x-vercel-cron": "1"})
check(r.status_code == 401,
      "R3 spoofed x-vercel-cron header alone -> 401 (bypass closed)",
      f"got {r.status_code}")

r = client.get(f"/api/cron/refresh?secret={_SECRET}")
check(r.status_code == 401, "R4 secret in query string -> 401 (rejected)",
      f"got {r.status_code}")

r = client.get("/api/cron/refresh",
               headers={"authorization": "Bearer wrong-secret"})
check(r.status_code == 401, "R5 wrong Bearer -> 401", f"got {r.status_code}")

# ── R6: correct Bearer passes the gate ─────────────────────────────────
# QX_TOKEN is unset, so the endpoint's own post-gate 503 ("QX_TOKEN not
# set") is the observable proof of acceptance — the burst cannot start.
r = client.get("/api/cron/refresh",
               headers={"authorization": f"Bearer {_SECRET}"})
check(r.status_code == 503 and "QX_TOKEN not set" in r.text,
      "R6 correct Bearer passes gate (reaches QX_TOKEN check)",
      f"got {r.status_code} {r.text[:90]}")

# ── R7: x-vercel-cron WITH the correct Bearer still passes ─────────────
r = client.get("/api/cron/refresh",
               headers={"authorization": f"Bearer {_SECRET}",
                        "x-vercel-cron": "1"})
check(r.status_code == 503 and "QX_TOKEN not set" in r.text,
      "R7 header alongside correct Bearer still passes",
      f"got {r.status_code} {r.text[:90]}")

# ── cleanup ────────────────────────────────────────────────────────────
os.environ.pop("CRON_SECRET", None)

print("=" * 72)
if _FAIL == 0:
    print(f"  ALL CRON GATE CHECKS PASSED  ({_PASS}/{_PASS + _FAIL})")
else:
    print(f"  CRON GATE CHECKS FAILED ({_PASS} passed, {_FAIL} failed):")
    for _f in _FAILURES:
        print(f"    - {_f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
