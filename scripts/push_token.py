#!/usr/bin/env python3
"""
Push a freshly-extracted Quotex token to both the live app and Railway.

This does NOT log in to Quotex — it only distributes a token you already
obtained from your own browser session (DevTools → Network → Authorization
payload → "session" field). Email/password auto-login from Railway is
intentionally not attempted: Cloudflare blocks it, and doing so previously
got the Quotex account itself blocked for repeated automated failures
(see the MANUAL-TOKEN-MODE fix in server.py).

Usage:
  python3 scripts/push_token.py "PASTE_YOUR_FRESH_TOKEN_HERE"

What it does:
  1. POSTs the token to /api/set-token on the live app — takes effect
     immediately, no redeploy needed.
  2. Runs `railway variables set QX_TOKEN=...` so the token also survives
     the next redeploy (requires the Railway CLI to be logged in and this
     repo folder to be linked — both already set up).
  3. Prints /api/token-status afterward so you can see it worked.
"""
import json
import shutil
import subprocess
import sys
import urllib.request

# FIX (2026-08-05): the old "-09db" host 404s. The Railway service was
# recreated during volume troubleshooting and got a new URL; this is the
# current one (confirmed against `railway status` / a live /api/token-status).
LIVE_URL = "https://binary-signals-app-production.up.railway.app"
MIN_TOKEN_LENGTH = 20


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <token>")
        sys.exit(1)

    token = sys.argv[1].strip()
    if len(token) < MIN_TOKEN_LENGTH:
        print(f"Token too short ({len(token)} chars) — doesn't look like a real Quotex token.")
        sys.exit(1)

    # 1. Push live (instant, no redeploy)
    print("-> Pushing to live app (instant)...")
    req = urllib.request.Request(
        f"{LIVE_URL}/api/set-token",
        data=json.dumps({"token": token}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print("   ", resp.read().decode())
    except Exception as e:
        print(f"   FAILED: {e}")

    # 2. Persist to Railway Variables (survives next redeploy).
    # --skip-deploys: the live instance was already updated via /api/set-token
    # above, so we don't need Railway to trigger a redeploy on top of that.
    print("-> Persisting to Railway Variables...")
    # shutil.which resolves the platform-specific shim (e.g. railway.cmd on
    # Windows) -- a plain "railway" arg fails under subprocess on Windows
    # because CreateProcess doesn't try PATHEXT extensions like a shell does.
    railway_bin = shutil.which("railway")
    if not railway_bin:
        print("   FAILED: 'railway' CLI not found on PATH.")
    else:
        try:
            subprocess.run(
                [railway_bin, "variable", "set", f"QX_TOKEN={token}", "--skip-deploys"],
                check=True,
            )
        except Exception as e:
            print(f"   FAILED: {e}")

    # 3. Confirm
    print("-> Checking /api/token-status...")
    try:
        with urllib.request.urlopen(f"{LIVE_URL}/api/token-status", timeout=15) as resp:
            print("   ", resp.read().decode())
    except Exception as e:
        print(f"   FAILED: {e}")


if __name__ == "__main__":
    main()
