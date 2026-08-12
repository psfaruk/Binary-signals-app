#!/usr/bin/env python3
"""
minter_service.py — a tiny always-on service whose only job is to mint
Quotex SSIDs from a region Quotex actually serves, and push them to the
main app.

WHY A SEPARATE SERVICE
----------------------
Quotex geo-blocks by COUNTRY, not by IP or datacenter. Proven 2026-08-13
from the main Railway service (us-west): the trade page came back HTTP 200
carrying, in plain text,

    "Unfortunately, Quotex is currently not available in your region.
     (United States)"

So no cookie, proxy header or User-Agent tweak can fix it from a US host —
but the same cookies work perfectly from a served region. Meanwhile the
Quotex *WebSocket* is NOT geo-blocked: the main app streams happily from
us-west with a token minted elsewhere.

That splits the problem cleanly:

    [minter, southeast-asia]  cookies --> SSID --> POST /api/set-token
    [main app, us-west]       keeps its volume, its DB, its live socket

Moving the main service instead would mean moving its volume (signals.db,
all the learned state) across regions, which Railway cannot do in place.
This service carries no volume and no state, so it is free to live wherever
Quotex is happy.

It exposes /healthz purely so Railway's healthcheck has something to talk
to — the real work happens in the background loop.

CONFIG
------
    QX_SERVICE_ROLE=minter    (set by run_service.sh to select this entrypoint)
    QX_COOKIES                the Quotex cookie jar
    QX_APP_URL                the main app's public URL
    QX_APP_ADMIN_KEY          its ADMIN_KEY, to authorise /api/set-token
    QX_AGENT_INTERVAL_H       hours between refreshes (default 3)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from fastapi import FastAPI  # noqa: E402

from scripts import token_agent  # noqa: E402
from core import qx_session  # noqa: E402

app = FastAPI(title="qx-token-minter")

_state = {
    "started_at": time.time(),
    "cycles": 0,
    "last_ok": None,
    "last_error": None,
    "last_run": None,
    "region_ok": None,
}


@app.get("/healthz")
async def healthz():
    """Always 200 while the process is alive.

    Deliberately NOT tied to mint success: if the cookies expire, this
    service must stay up and keep reporting *why* it is failing. A red
    healthcheck would make Railway restart-loop it and bury the reason.
    """
    return {"ok": True, **status_body()}


@app.get("/")
async def root():
    return status_body()


def status_body() -> dict:
    st = qx_session.status()
    return {
        "role": "qx-token-minter",
        "target_app": token_agent.app_url(),
        "uptime_sec": round(time.time() - _state["started_at"]),
        "cycles": _state["cycles"],
        "last_run": _state["last_run"],
        "last_ok": _state["last_ok"],
        "last_error": _state["last_error"],
        "region_serves_quotex": _state["region_ok"],
        "cookies": st.get("cookie_names"),
        "account": st.get("account_email"),
        "refresh_count": st.get("refresh_count"),
    }


async def loop() -> None:
    interval = max(1800.0, float(os.environ.get("QX_AGENT_INTERVAL_H", "3")) * 3600)
    token_agent.log(f"minter starting — target {token_agent.app_url()}, "
                    f"every {interval / 3600:.1f}h")
    if not qx_session.configured():
        token_agent.log("✘ no QX_COOKIES set — nothing to mint with")
    while True:
        _state["last_run"] = time.time()
        try:
            ok = await token_agent.cycle(verify_live=True)
            _state["cycles"] += 1
            if ok:
                _state["last_ok"] = time.time()
                _state["last_error"] = None
                _state["region_ok"] = True
            else:
                err = qx_session.status().get("last_error") or "push failed"
                _state["last_error"] = err
                # The geo-block announces itself in plain text, so say so
                # rather than leaving "it failed" in the logs.
                if "not available in your region" in str(err) or "logged-out" in str(err):
                    _state["region_ok"] = False
        except Exception as exc:
            _state["last_error"] = f"{type(exc).__name__}: {exc}"
            token_agent.log(f"✘ cycle crashed: {_state['last_error']}")
        await asyncio.sleep(interval)


@app.on_event("startup")
async def _start():
    asyncio.create_task(loop())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
