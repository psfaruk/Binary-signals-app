"""
Binary Signal App — WebSocket server.
Serves static frontend and bridges QuotexFeed to browser clients.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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
#
# QX_USE_RAW_WS=1 (default) enables the raw WebSocket backend (quotex_ws.py)
# which bypasses pyquotex entirely and speaks Socket.IO v3 directly to
# Quotex — see quotex-smooth-candle-mystery.txt for the protocol spec.
# When QX_USE_RAW_WS=1, pyquotex does NOT need to be installed.
_HAS_PYQUOTEX = False
try:
    import pyquotex  # noqa
    _HAS_PYQUOTEX = True
except ImportError:
    pass

_USE_RAW_WS = os.environ.get("QX_USE_RAW_WS", "1") == "1"
if _USE_RAW_WS:
    # Raw WebSocket backend — pyquotex not required
    _HAS_PYQUOTEX = True
    print("[server] QX_USE_RAW_WS=1 — raw WebSocket backend "
          "(pyquotex optional)")

if os.environ.get("USE_SIM") == "1":
    _HAS_PYQUOTEX = False
    print("[server] USE_SIM=1 — forcing simulated feed")

if _HAS_PYQUOTEX:
    from feed import QuotexFeed as _Feed
    print("[server] real feed available — using REAL Quotex feed")
else:
    from sim_feed import QuotexFeed as _Feed
    print("[server] real feed NOT available — using SIMULATED feed")

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

def _auto_open_browser():
    """Open the default browser to the app URL after server starts.
    Runs in a background thread so it doesn't block the event loop.
    Waits ~5 seconds for the server to be ready before opening."""
    import threading
    import time as _time
    import webbrowser as _wb
    import os as _os

    port = _os.environ.get("PORT", "8000")
    url = f"http://localhost:{port}"

    def _open():
        _time.sleep(5)  # wait for server to be ready
        try:
            _wb.open(url)
            print(f"[server] browser opened: {url}")
        except Exception as exc:
            print(f"[server] could not open browser: {exc}")
            print(f"[server] manually open: {url}")

    # Skip auto-open if disabled (e.g., on Railway/production)
    if _os.environ.get("AUTO_OPEN_BROWSER", "1") == "0":
        return

    threading.Thread(target=_open, daemon=True).start()


@app.on_event("startup")
async def on_startup():
    _db.init()
    asyncio.create_task(feed.run(broadcast))
    # Auto-open browser 5 seconds after startup (local dev convenience).
    # Disabled when AUTO_OPEN_BROWSER=0 (set this on Railway/production).
    _auto_open_browser()


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
    raise HTTPException(status_code=404, detail="not found")


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