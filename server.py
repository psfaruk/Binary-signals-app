"""
Binary Signal App — WebSocket server.
Serves static frontend and bridges QuotexFeed to browser clients.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Load .env from project root
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# Ensure QX_ROOT points to a valid temp dir on Linux/Mac
if sys.platform != "win32":
    os.environ.setdefault("QX_ROOT",
                          os.path.join(os.environ.get("TMPDIR", "/tmp"), "plybit_cache"))

import db as _db

# Auto-detect: use real feed.py if pyquotex is available, else sim_feed.
# Set USE_SIM=1 to force the simulated feed even when pyquotex is installed.
_HAS_PYQUOTEX = False
try:
    import pyquotex  # noqa
    _HAS_PYQUOTEX = True
except ImportError:
    pass

if os.environ.get("USE_SIM") == "1":
    _HAS_PYQUOTEX = False
    print("[server] USE_SIM=1 — forcing simulated feed")

if _HAS_PYQUOTEX:
    from feed import QuotexFeed as _Feed
    print("[server] pyquotex available — using REAL Quotex feed")
else:
    from sim_feed import QuotexFeed as _Feed
    print("[server] pyquotex NOT available — using SIMULATED feed")

app = FastAPI()
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ── Global feed + client manager ─────────────────────────────────────────────

feed = _Feed()
clients: dict[str, WebSocket] = {}   # cid -> ws
cid_counter = 0


async def broadcast(msg: dict):
    """Push a message to every connected client."""
    data = json.dumps(msg)
    dead = []
    for cid, ws in clients.items():
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(cid)
    for cid in dead:
        clients.pop(cid, None)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    _db.init()
    asyncio.create_task(feed.run(broadcast))


@app.on_event("shutdown")
async def on_shutdown():
    await feed.shutdown()


# ── HTTP routes ───────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/pairs")
async def get_pairs():
    return feed.available_pairs()


@app.get("/api/status")
async def status():
    return {
        "connected": feed._connected,
        "streams": feed.stream_status(),
    }


@app.get("/api/history/{asset}/{period}")
async def get_history(asset: str, period: int):
    snap = feed.snapshot(asset, period)
    if snap:
        return snap
    return {"candles": [], "prediction": None}


@app.get("/api/signals/{asset}/{period}")
async def get_signals(asset: str, period: int, limit: int = 50):
    return {"signals": _db.get_recent_signals(asset, period, limit)}


@app.get("/api/signals/{asset}/{period}/{ctime}")
async def get_signal_detail(asset: str, period: int, ctime: int):
    """Return full detail for a single signal (win/loss reason, theories, etc.)."""
    detail = _db.get_signal_detail(asset, period, ctime)
    if detail:
        return detail
    return {"error": "not found"}, 404


# ── WebSocket endpoint ───────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global cid_counter
    await ws.accept()
    cid_counter += 1
    cid = f"client-{cid_counter}"
    clients[cid] = ws
    print(f"[server] {cid} connected ({len(clients)} total)")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            t = msg.get("type")

            if t == "subscribe":
                asset = msg.get("asset", "")
                period = int(msg.get("period", 60))
                result = await feed.ensure_stream(asset, period, cid=cid)
                await ws.send_text(json.dumps(result))

            elif t == "pairs":
                await ws.send_text(json.dumps(
                    {"type": "pairs", **feed.available_pairs()}))

            elif t == "status":
                await ws.send_text(json.dumps({
                    "type": "status",
                    "connected": feed._connected,
                    "streams": feed.stream_status(),
                }))

            elif t == "signals":
                asset = msg.get("asset", "")
                period = int(msg.get("period", 60))
                sigs = _db.get_recent_signals(asset, period, 50)
                await ws.send_text(json.dumps({
                    "type": "signals",
                    "signals": sigs,
                }))

    except WebSocketDisconnect:
        print(f"[server] {cid} disconnected")
    except Exception as e:
        print(f"[server] {cid} error: {e}")
    finally:
        clients.pop(cid, None)
        await feed.drop_interest(cid)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")