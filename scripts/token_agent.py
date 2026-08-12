#!/usr/bin/env python3
"""
token_agent.py — mint Quotex SSIDs locally and push them to the live app.

WHY THIS EXISTS
---------------
`core/qx_session.py` refreshes the Quotex token by replaying the operator's
browser cookies. That works perfectly from the operator's own machine — and
NOT from Railway. Measured 2026-08-13 from the deployed container (outbound
IP 152.55.176.179, Cloudflare colo SJC):

    market-qx.trade   403  Cloudflare "Just a moment…" JS challenge
    market-qx.info    200  logged-out page, no window.settings token
    qxbroker.com      403  challenge
    quotex.io         200  logged-out page

…tested with the complete, known-good cookie jar (recaller +
laravel_session + __vid_l3) that had authenticated from the operator's IP
minutes earlier. Quotex does not honour the session from a foreign
datacenter IP, and `cf_clearance` cannot bridge the gap because Cloudflare
binds it to the IP that solved the challenge.

The token itself is NOT IP-bound — a token minted at home authorises the
WebSocket from Railway just fine (verified: connect ok, 92 instruments).
So the fix is to mint where it works and deliver the result:

    [operator's PC]  cookies → fresh SSID → POST /api/set-token → [Railway]

Run it once, or leave it running with --loop, or install it as a scheduled
task with --install-task (Windows) so it survives reboots.

USAGE
-----
    py scripts/token_agent.py                 # mint + push once
    py scripts/token_agent.py --loop          # every QX_AGENT_INTERVAL_H hours
    py scripts/token_agent.py --install-task  # Windows scheduled task
    py scripts/token_agent.py --status        # what the live app thinks

CONFIG (.env)
-------------
    QX_APP_URL          https://…up.railway.app   (the live app)
    QX_APP_ADMIN_KEY    the app's ADMIN_KEY  ) either one authorises
    QX_APP_PIN          the 🔑 panel's PIN   ) /api/set-token
    QX_AGENT_INTERVAL_H hours between refreshes in --loop (default 3)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

if (ROOT / ".env").exists():
    load_dotenv(ROOT / ".env")

from core import qx_session, token_store  # noqa: E402

DEFAULT_URL = "https://binary-signals-app-production.up.railway.app"


def app_url() -> str:
    return (os.environ.get("QX_APP_URL", "").strip() or DEFAULT_URL).rstrip("/")


def _auth() -> dict:
    """Whatever credential the app will accept, as request JSON + headers."""
    key = os.environ.get("QX_APP_ADMIN_KEY", "").strip()
    pin = os.environ.get("QX_APP_PIN", "").strip()
    body, headers = {}, {}
    if key:
        body["admin_key"] = key
        headers["X-Admin-Key"] = key
    if pin:
        body["pin"] = pin
        headers["X-App-Pin"] = pin
    return {"body": body, "headers": headers}


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{_stamp()}] {msg}", flush=True)


async def push(token: str) -> bool:
    """Hand the freshly minted token to the live app."""
    import httpx

    auth = _auth()
    if not auth["body"]:
        log("✘ no QX_APP_ADMIN_KEY or QX_APP_PIN in .env — cannot authorise "
            "the push. Add one and re-run.")
        return False

    url = f"{app_url()}/api/set-token"
    payload = {"token": token, "source": "token-agent", **auth["body"]}
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(url, json=payload, headers=auth["headers"])
    except Exception as exc:
        log(f"✘ push failed: {type(exc).__name__}: {exc}")
        return False

    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:200]}
    if r.status_code == 200 and body.get("ok"):
        log(f"✔ pushed {body.get('preview')} — {body.get('message')}")
        return True
    log(f"✘ app rejected the token (HTTP {r.status_code}): "
        f"{body.get('error') or body}")
    return False


async def wait_until_live(timeout: float = 90.0) -> bool:
    """Confirm the app actually went live — a push that does not is a failure."""
    import httpx

    deadline = time.time() + timeout
    last = ""
    async with httpx.AsyncClient(timeout=15) as c:
        while time.time() < deadline:
            try:
                s = (await c.get(f"{app_url()}/api/token-status")).json()
            except Exception:
                await asyncio.sleep(4)
                continue
            if s.get("live"):
                log(f"✔ LIVE — {s.get('streams')} streams, "
                    f"{s.get('connection_status')}")
                return True
            if s.get("connection_status") != last:
                last = s.get("connection_status")
                log(f"  … {last}")
            await asyncio.sleep(4)
    log("✘ app did not reach live_authorized within "
        f"{timeout:.0f}s — check /api/token-status")
    return False


async def cycle(verify_live: bool = True) -> bool:
    """One mint → push → confirm pass."""
    if not qx_session.configured():
        log("✘ no session cookies configured — set QX_COOKIES in .env")
        return False

    token, detail = await qx_session.refresh_token(reason="agent", force=True)
    if not token:
        log(f"✘ could not mint a token: {detail}")
        log("   Log in to Quotex in your browser, then: F12 → Console → "
            "copy(document.cookie) → update QX_COOKIES in .env")
        return False

    acct = qx_session.status().get("account_email")
    log(f"✔ minted {token_store.mask(token)} via {detail} (account {acct})")

    if not await push(token):
        return False
    return await wait_until_live() if verify_live else True


async def show_status() -> None:
    import httpx

    log(f"local  : cookies={qx_session.status()['cookie_names']}")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            s = (await c.get(f"{app_url()}/api/token-status")).json()
        log(f"live   : {s.get('status')} live={s.get('live')} "
            f"streams={s.get('streams')} token={s.get('active_token')}")
        a = s.get("auto_session") or {}
        log(f"remote : auto_session configured={a.get('configured')} "
            f"failures={a.get('consecutive_failures')} "
            f"last_error={(a.get('last_error') or '')[:120]}")
    except Exception as exc:
        log(f"live   : unreachable — {type(exc).__name__}: {exc}")


def install_task(interval_h: float) -> int:
    """Register a Windows scheduled task so this survives reboots."""
    if sys.platform != "win32":
        print("--install-task is Windows-only. On Linux/macOS use cron:")
        print(f"  0 */{int(interval_h)} * * * cd {ROOT} && "
              f"{sys.executable} scripts/token_agent.py")
        return 1
    name = "QuotexTokenAgent"
    cmd = f'"{sys.executable}" "{ROOT / "scripts" / "token_agent.py"}"'
    minutes = max(30, int(interval_h * 60))
    args = ["schtasks", "/Create", "/TN", name, "/TR", cmd,
            "/SC", "MINUTE", "/MO", str(minutes), "/F"]
    r = subprocess.run(args, capture_output=True, text=True)
    print(r.stdout or r.stderr)
    if r.returncode == 0:
        print(f"✔ scheduled task '{name}' runs every {minutes} min.")
        print(f"  Remove with:  schtasks /Delete /TN {name} /F")
    return r.returncode


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true", help="run forever")
    ap.add_argument("--status", action="store_true", help="show state and exit")
    ap.add_argument("--install-task", action="store_true",
                    help="register a Windows scheduled task")
    ap.add_argument("--no-verify", action="store_true",
                    help="do not wait for the app to report live")
    ap.add_argument("--interval-hours", type=float,
                    default=float(os.environ.get("QX_AGENT_INTERVAL_H", "3")))
    args = ap.parse_args()

    log(f"target app: {app_url()}")

    if args.install_task:
        return install_task(args.interval_hours)
    if args.status:
        await show_status()
        return 0
    if not args.loop:
        return 0 if await cycle(not args.no_verify) else 1

    period = max(1800.0, args.interval_hours * 3600.0)
    log(f"loop mode — refreshing every {period / 3600:.1f}h (Ctrl-C to stop)")
    while True:
        try:
            await cycle(not args.no_verify)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log(f"✘ cycle error: {type(exc).__name__}: {exc}")
        await asyncio.sleep(period)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nstopped.")
