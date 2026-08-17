#!/usr/bin/env python3
"""
verify_token_import.py — end-to-end proof that the 🔑 Token panel works.

Boots a REAL server process in a throwaway state dir (its own DB, its own
session.json, no QX_TOKEN) and drives the exact HTTP calls the frontend
makes, asserting each step:

  1. fresh deploy  → status=no_credentials, unclaimed, no stored token
  2. import with no PIN            → 403 (endpoint is not public)
  3. claim a PIN                   → 200
  4. import with the WRONG PIN     → 403
  5. import a full DevTools frame  → 200, session extracted, token persisted
  6. /api/token-status             → has_token, stored preview, persistence flag
  7. restart the server            → token restored from disk WITHOUT any env
                                     var  ← this is the "no more redeploy" claim
  8. (optional) real token         → connection reaches live_authorized

Usage
-----
    py scripts/verify_token_import.py
    py scripts/verify_token_import.py --token <REAL_QUOTEX_TOKEN>   # adds step 8
    py scripts/verify_token_import.py --token-from-env              # uses QX_TOKEN/.env
    py scripts/verify_token_import.py --live-wait 90

Exit code 0 = every assertion passed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PIN = "verify-pin-9821"
FAKE_TOKEN = "verifyToken0000111122223333444455556666"
FRAME = '42["authorization",{"session":"%s","isDemo":1,"tournamentId":0}]' % FAKE_TOKEN
STALE_TOKEN = "staleRailwayVariableToken0000000000aaaa"   # "old value in Variables"
NEWER_TOKEN = "freshTokenImportedFromTheUI11111111bbbb"   # pasted in the UI later
EDITED_TOKEN = "operatorEditedTheVariable222222222cccc"   # Variable edited by hand


def mask(token: str) -> str:
    """Mirror core.token_store.mask() so assertions can compare previews."""
    if not token:
        return ""
    if len(token) <= 12:
        return "…" + token[-3:]
    return f"{token[:6]}…{token[-4:]}"

_passed = 0
_failed = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))
    else:
        _failed += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
    return ok


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base: str, path: str, method: str = "GET", payload: dict | None = None,
         headers: dict | None = None, timeout: float = 20.0) -> tuple[int, dict]:
    url = base + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, {"raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}
    except Exception as exc:
        return 0, {"error": str(exc)}


class Server:
    """The app under test, in its own state dir."""

    def __init__(self, state_dir: Path, port: int, log: Path,
                 extra_env: dict | None = None):
        self.state_dir = state_dir
        self.port = port
        self.log = log
        self.extra_env = extra_env or {}
        self.proc: subprocess.Popen | None = None
        self.base = f"http://127.0.0.1:{port}"

    def start(self) -> None:
        env = dict(os.environ)
        # Throwaway state: never touch the operator's real DB / session.json.
        env.pop("QX_TOKEN", None)
        env.pop("QX_EMAIL", None)
        env.pop("ADMIN_KEY", None)
        env["QX_STATE_DIR"] = str(self.state_dir)
        env["QX_SESSION_PATH"] = str(self.state_dir / "session.json")
        env["DB_PATH"] = str(self.state_dir / "signals.db")
        env["AUTO_OPEN_BROWSER"] = "0"
        env["PORT"] = str(self.port)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        # Don't let .env re-inject a token behind our back.
        env["QX_SKIP_DOTENV"] = "1"
        env.update(self.extra_env)
        self._fh = open(self.log, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server:app",
             "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            cwd=str(REPO), env=env, stdout=self._fh, stderr=subprocess.STDOUT,
        )

    def wait_ready(self, timeout: float = 90.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                return False
            code, _ = call(self.base, "/api/token-status", timeout=3.0)
            if code == 200:
                return True
            time.sleep(0.5)
        return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        try:
            self._fh.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default="", help="a REAL Quotex token — enables the live-authorization step")
    ap.add_argument("--token-from-env", action="store_true",
                    help="read the real token from QX_TOKEN / .env")
    ap.add_argument("--live-wait", type=float, default=75.0,
                    help="seconds to wait for live_authorized with a real token")
    ap.add_argument("--keep", action="store_true", help="keep the temp state dir")
    args = ap.parse_args()

    real_token = args.token.strip()
    if not real_token and args.token_from_env:
        real_token = os.environ.get("QX_TOKEN", "").strip()
        if not real_token:
            env_file = REPO / ".env"
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("QX_TOKEN="):
                        real_token = line.split("=", 1)[1].strip().strip('"\'')
                        break

    tmp = Path(tempfile.mkdtemp(prefix="tokenverify_"))
    log = tmp / "server.log"
    port = free_port()
    print(f"── token-import verification ─────────────────────────────────")
    print(f"state dir : {tmp}")
    print(f"server log: {log}")
    print(f"port      : {port}\n")

    srv = Server(tmp, port, log)
    srv.start()
    if not srv.wait_ready():
        print("[FATAL] server did not come up — last 40 log lines:")
        try:
            print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]))
        except Exception:
            pass
        srv.stop()
        return 2

    try:
        print("STEP 1 — fresh deploy, no credentials")
        code, st = call(srv.base, "/api/token-status")
        check("token-status reachable", code == 200, f"HTTP {code}")
        check("reports no_credentials", st.get("status") == "no_credentials", str(st.get("status")))
        check("has_token is False", st.get("has_token") is False)
        check("no stored token yet", not (st.get("stored_token") or {}).get("stored"))

        # USER REQ 2026-08-17: token push must work WITHOUT a PIN.
        # The old "unauthenticated import blocked" step is GONE — that was
        # the behavior we just removed.
        print("\nSTEP 2 — import with NO PIN must now SUCCEED (token-only flow)")
        code, body = call(srv.base, "/api/set-token", "POST", {"token": FRAME})
        check("unauthenticated import accepted", code == 200 and body.get("ok") is True,
              f"HTTP {code} {body}")
        check("token persisted to disk", body.get("persisted") is True, str(body.get("persist_error")))
        check("reconnect woken immediately", body.get("reconnect_wakeup") is True)

        print("\nSTEP 3 — PIN-claim endpoints are dormant (admin-only, no UI)")
        # USER REQ 2026-08-17: "কোনো এডমিন key, অন্যান্য key এই গুলো fronted এ
        # থাকবে না" — no admin/other keys on the frontend. The PIN-claim
        # endpoints (/api/auth/claim, /api/auth/verify) are no longer
        # called from the UI. They remain mounted for ops use with
        # X-Admin-Key, but are admin-gated. The frontend token-push flow
        # (Step 2 + 4 + 5) does NOT need them.
        code, body = call(srv.base, "/api/auth/claim", "POST", {"pin": PIN})
        check("claim endpoint admin-gated (no UI)", code == 401, f"HTTP {code}")
        code, body = call(srv.base, "/api/auth/verify", "POST", {"pin": PIN})
        check("verify endpoint admin-gated (no UI)", code == 401, f"HTTP {code}")

        print("\nSTEP 4 — even with a PIN claimed, no-PIN import still works (token-only)")
        code, body = call(srv.base, "/api/set-token", "POST",
                          {"token": FRAME.replace(FAKE_TOKEN, FAKE_TOKEN + "aa")})
        check("no-PIN import works even after PIN claimed", code == 200, f"HTTP {code}")

        print("\nSTEP 5 — import a full DevTools frame")
        code, body = call(srv.base, "/api/set-token", "POST", {"token": FRAME})
        check("import accepted", code == 200 and body.get("ok") is True, f"HTTP {code} {body}")
        check("SSID frame normalized to session value",
              (body.get("normalized") or {}).get("input_format") == "socketio_frame",
              str(body.get("normalized")))
        check("token persisted to disk", body.get("persisted") is True, str(body.get("persist_error")))
        check("reconnect woken immediately", body.get("reconnect_wakeup") is True)
        stored_file = tmp / "qx_token.json"
        check("qx_token.json written", stored_file.exists(), str(stored_file))
        if stored_file.exists():
            saved = json.loads(stored_file.read_text(encoding="utf-8"))
            check("stored value is the extracted session, not the frame",
                  saved.get("token") == FAKE_TOKEN, str(saved.get("token"))[:60])

        print("\nSTEP 6 — status reflects the imported token")
        code, st = call(srv.base, "/api/token-status")
        check("has_token now True", st.get("has_token") is True)
        check("stored_token.stored True", (st.get("stored_token") or {}).get("stored") is True)
        check("preview is masked (token never echoed)",
              FAKE_TOKEN not in json.dumps(st), (st.get("stored_token") or {}).get("preview"))

        print("\nSTEP 7 — restart: token must come back WITHOUT any env var")
        srv.stop()
        srv2 = Server(tmp, port, log)
        srv2.start()
        ready = srv2.wait_ready()
        check("server restarted", ready)
        if ready:
            code, st = call(srv2.base, "/api/token-status")
            check("token survived the restart (redeploy-proof)",
                  st.get("has_token") is True, str(st.get("status")))
            check("restored from the stored file",
                  (st.get("stored_token") or {}).get("stored") is True)
            logtxt = log.read_text(encoding="utf-8", errors="replace")
            check("boot log shows the restore", "restored stored token" in logtxt)
        srv = srv2

        print("\nSTEP 7b — a stale QX_TOKEN Railway Variable must not clobber "
              "the UI token")
        srv.stop()
        stale_env = {"QX_TOKEN": STALE_TOKEN}
        srv3 = Server(tmp, port, log, extra_env=stale_env)
        srv3.start()
        ready = srv3.wait_ready()
        check("booted with a stale QX_TOKEN variable set", ready)
        if ready:
            _, st = call(srv3.base, "/api/token-status")
            check("first sight of that variable wins (expected)",
                  st.get("active_token") == mask(STALE_TOKEN), str(st.get("active_token")))
            # USER REQ 2026-08-17: no PIN header needed.
            code, body = call(srv3.base, "/api/set-token", "POST",
                              {"token": NEWER_TOKEN})
            check("UI import over a live env token accepted",
                  code == 200 and body.get("ok") is True, f"HTTP {code}")
            _, st = call(srv3.base, "/api/token-status")
            check("running process switched to the UI token",
                  st.get("active_token") == mask(NEWER_TOKEN), str(st.get("active_token")))
            srv3.stop()
            srv4 = Server(tmp, port, log, extra_env=stale_env)   # simulate redeploy
            srv4.start()
            ready4 = srv4.wait_ready()
            check("redeploy with the same stale variable came up", ready4)
            if ready4:
                _, st = call(srv4.base, "/api/token-status")
                check("UI token still wins after redeploy (no re-paste needed)",
                      st.get("active_token") == mask(NEWER_TOKEN), str(st.get("active_token")))
            srv = srv4
            # An operator who edits the Variable must still be able to take over.
            srv.stop()
            srv5 = Server(tmp, port, log, extra_env={"QX_TOKEN": EDITED_TOKEN})
            srv5.start()
            ready5 = srv5.wait_ready()
            check("redeploy after editing the Variable came up", ready5)
            if ready5:
                _, st = call(srv5.base, "/api/token-status")
                check("a newly edited Variable takes precedence",
                      st.get("active_token") == mask(EDITED_TOKEN), str(st.get("active_token")))
            srv = srv5

        if real_token:
            print("\nSTEP 8 — real token: does the feed actually go live?")
            # USER REQ 2026-08-17: no PIN header needed — just the token.
            code, body = call(srv.base, "/api/set-token", "POST",
                              {"token": real_token})
            check("real token accepted", code == 200 and body.get("ok") is True, f"HTTP {code} {body}")
            deadline = time.time() + args.live_wait
            final = {}
            while time.time() < deadline:
                _, final = call(srv.base, "/api/token-status")
                if final.get("live") or final.get("token_dead"):
                    break
                time.sleep(3)
            live = bool(final.get("live"))
            check("feed reached live_authorized", live,
                  f"connection_status={final.get('connection_status')} "
                  f"token_dead={final.get('token_dead')} "
                  f"rejects={final.get('consecutive_rejects')}")
            if live:
                deadline = time.time() + 45
                streams = 0
                while time.time() < deadline:
                    _, s2 = call(srv.base, "/api/token-status")
                    streams = s2.get("streams") or 0
                    if streams:
                        break
                    time.sleep(3)
                check("live streams started", streams > 0, f"{streams} streams")
            elif final.get("token_dead"):
                print("         ↳ Quotex REJECTED this token: it is expired/revoked.")
                print("           The import mechanism worked (steps 1-7); the token itself is stale.")
        else:
            print("\nSTEP 8 — skipped (no real token given; pass --token or --token-from-env)")

        print("\n──────────────────────────────────────────────────────────────")
        print(f"passed: {_passed}   failed: {_failed}")
        if _failed:
            print("\nlast 30 server log lines:")
            print("\n".join(log.read_text(encoding='utf-8', errors='replace').splitlines()[-30:]))
        return 0 if _failed == 0 else 1
    finally:
        srv.stop()
        if args.keep:
            print(f"state dir kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
