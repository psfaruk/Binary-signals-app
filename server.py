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

# Load .env from project root (if it exists — Railway uses env vars directly)
from dotenv import load_dotenv
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
    print("[server] .env ফাইল loaded")
else:
    print("[server] .env ফাইল নেই — Railway env vars ব্যবহার করা হচ্ছে")
# NOTE: Do NOT try to create .env on Railway (read-only filesystem).
# Railway injects env vars directly — .env file is for local dev only.

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
# ── Quotex backend selection ────────────────────────────────────────────────
# QX_USE_RAW_WS=0 (DEFAULT): vendored pyquotex with Firefox TLS cipher suite
#                             → bypasses Cloudflare without Playwright/curl_cffi
# QX_USE_RAW_WS=1:           raw WebSocket backend (quotex_ws.py)
#                             → lighter but Cloudflare blocks login on datacenter IPs
_HAS_PYQUOTEX = False
try:
    import pyquotex  # noqa
    _HAS_PYQUOTEX = True
except ImportError:
    pass

_USE_RAW_WS = os.environ.get("QX_USE_RAW_WS", "0") == "1"
if _USE_RAW_WS:
    # Raw WebSocket backend — pyquotex not required
    _HAS_PYQUOTEX = True
    print("[server] QX_USE_RAW_WS=1 — raw WebSocket backend "
          "(pyquotex optional)")
else:
    print("[server] QX_USE_RAW_WS=0 — vendored pyquotex with Firefox TLS "
          "(Cloudflare bypass)")

if os.environ.get("USE_SIM") == "1":
    _HAS_PYQUOTEX = False
    print("[server] USE_SIM=1 — forcing simulated feed")

# Auto-fallback to sim if no Quotex credentials and not forcing sim
if _HAS_PYQUOTEX and not os.environ.get("QX_TOKEN", "").strip() and not os.environ.get("QX_EMAIL", "").strip():
    _HAS_PYQUOTEX = False
    print("[server] no Quotex credentials — falling back to SIMULATED feed")

if _HAS_PYQUOTEX:
    from feed import QuotexFeed as _Feed
    print("[server] real feed available — using REAL Quotex feed")
else:
    from sim_feed import QuotexFeed as _Feed
    print("[server] real feed NOT available — using SIMULATED feed")

feed = _Feed()
clients: dict[str, WebSocket] = {}   # cid -> ws
cid_counter = 0


async def broadcast(msg: dict):
    """Push a message to connected clients — PARALLEL sends.

    FIX (2026-07-15 audit): previously sent ALL messages to ALL clients,
    wasteful when N viewers watch M different pairs (each got N×M msgs).
    Now filters by asset/period: only clients interested in the message's
    asset/period receive it. Messages without asset/period (pairs, status,
    signals list) go to everyone.
    """
    if not clients:
        return
    data = json.dumps(msg)
    msg_asset = msg.get("asset")
    msg_period = msg.get("period")

    # If no asset/period in message, broadcast to all (pairs, status, etc.)
    if not msg_asset:
        target_cids = list(clients.keys())
    else:
        # Filter: only send to clients interested in this asset/period.
        # feed tracks interested_cids per stream — look it up.
        target_cids = list(clients.keys())  # default: all
        try:
            stream_key = (msg_asset, msg_period) if msg_period else None
            if stream_key and hasattr(feed, '_streams'):
                stream = feed._streams.get(stream_key)
                if stream and stream.interested_cids:
                    target_cids = list(stream.interested_cids)
                elif stream:
                    # Stream exists but no interested viewers — skip
                    target_cids = []
                # else: stream not found, send to all (safety)
        except Exception:
            pass  # on any error, fall back to broadcast-all

    tasks = []
    cids = []
    for cid in target_cids:
        ws = clients.get(cid)
        if ws is None:
            continue
        cids.append(cid)
        tasks.append(ws.send_text(data))
    if not tasks:
        return
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # Remove dead clients
    for cid, result in zip(cids, results):
        if isinstance(result, Exception):
            clients.pop(cid, None)


# ── Lifespan handler (modern FastAPI — replaces deprecated on_event) ───────
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup + shutdown lifecycle (replaces @app.on_event)."""
    print("[server] lifespan: startup beginning")
    _db.init()
    # Start feed in background task
    feed_task = asyncio.create_task(feed.run(broadcast))
    print("[server] lifespan: feed task started")
    # Auto-open browser (local dev only — disabled on Railway via env var)
    _auto_open_browser()
    print("[server] lifespan: startup complete")
    yield
    print("[server] lifespan: shutdown beginning")
    await feed.shutdown()
    feed_task.cancel()
    try:
        await feed_task
    except asyncio.CancelledError:
        pass
    print("[server] lifespan: shutdown complete")


app = FastAPI(lifespan=lifespan)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


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


# (lifespan handler above replaces the deprecated @app.on_event startup/shutdown)


# ── HTTP routes ───────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    """Railway healthcheck endpoint — returns 200 if the process is up."""
    return {"ok": True}


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


@app.get("/api/debug")
async def debug_info():
    """Diagnostic endpoint — shows connection state, stream status, errors.
    Visit /api/debug in browser to see why candles aren't coming."""
    import time as _time
    debug = {
        "timestamp": _time.time(),
        "connected": feed._connected,
        "has_client": feed._client is not None,
        "streams": {},
        "pairs_count": len(feed._pairs_list) if hasattr(feed, '_pairs_list') else 0,
        "env": {
            "QX_TOKEN": "***" if os.environ.get("QX_TOKEN") else "(not set)",
            "QX_EMAIL": os.environ.get("QX_EMAIL", "(not set)"),
            "QX_PASSWORD": "***" if os.environ.get("QX_PASSWORD") else "(not set)",
            "USE_SIM": os.environ.get("USE_SIM", "0"),
            "QX_USE_RAW_WS": os.environ.get("QX_USE_RAW_WS", "0"),
            "PAYOUT_FLOOR": os.environ.get("QX_PAYOUT_FLOOR", "85"),
            "SIGNAL_DELAY_SEC": os.environ.get("SIGNAL_DELAY_SEC", "0.0"),
        },
    }
    # Stream details
    if hasattr(feed, '_streams'):
        for key, s in feed._streams.items():
            debug["streams"][f"{key[0]}@{key[1]}s"] = {
                "candles_count": len(s.candles) if hasattr(s, 'candles') else 0,
                "ticks_count": len(s.ticks) if hasattr(s, 'ticks') else 0,
                "last_real_tick_wall": getattr(s, 'last_real_tick_wall', 0),
                "always_on": getattr(s, 'always_on', False),
                "interested_cids": list(getattr(s, 'interested_cids', set())),
                "sub_started": getattr(s, 'sub_started', False),
            }
    # Recent errors (if tracked)
    if hasattr(feed, '_last_error'):
        debug["last_error"] = feed._last_error
    return debug


@app.get("/api/stats")
async def module_stats():
    """Per-module performance report from signal_log.
    Visit /api/stats in browser to see which modules are performing well."""
    import json as _json
    import sqlite3
    from collections import defaultdict

    MODULE_NAMES = {
        "candle_reaction": "Candle Reaction",
        "running_tick": "Running Tick",
        "pattern": "Pattern",
        "indicator": "Indicator",
        "key_level": "Key Level",
        "otc_pattern": "OTC Pattern",
    }

    db_path = _db.DB_PATH
    if not os.path.exists(db_path):
        return {"error": "signals.db not found", "db_path": db_path}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    total = cur.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
    if total == 0:
        conn.close()
        return {"total_signals": 0, "message": "No signals logged yet"}

    module_stats = defaultdict(lambda: {
        "CALL": {"correct": 0, "wrong": 0},
        "PUT": {"correct": 0, "wrong": 0},
    })
    pair_module_stats = defaultdict(lambda: defaultdict(lambda: {
        "CALL": {"correct": 0, "wrong": 0},
        "PUT": {"correct": 0, "wrong": 0},
    }))

    rows = cur.execute("""
        SELECT asset, signal, accuracy, reasons
        FROM signal_log
        WHERE signal IN ('CALL', 'PUT')
        ORDER BY ts DESC
    """).fetchall()

    for row in rows:
        asset = row["asset"]
        final_signal = row["signal"]
        accuracy = row["accuracy"]
        reasons_raw = row["reasons"] or "[]"
        try:
            reasons = _json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw
        except (ValueError, TypeError):
            reasons = []
        if not isinstance(reasons, list):
            reasons = []

        for reason in reasons:
            reason_str = str(reason)
            if not reason_str.startswith("["):
                continue
            end_bracket = reason_str.find("]")
            if end_bracket == -1:
                continue
            module = reason_str[1:end_bracket].strip()
            if module not in MODULE_NAMES:
                continue
            reason_upper = reason_str.upper()
            if "PUT" in reason_upper or "BEAR" in reason_upper or "SELLER" in reason_upper:
                direction = "PUT"
            elif "CALL" in reason_upper or "BULL" in reason_upper or "BUYER" in reason_upper:
                direction = "CALL"
            else:
                continue

            if direction == final_signal:
                if accuracy == "correct":
                    module_stats[module][direction]["correct"] += 1
                    pair_module_stats[asset][module][direction]["correct"] += 1
                elif accuracy == "wrong":
                    module_stats[module][direction]["wrong"] += 1
                    pair_module_stats[asset][module][direction]["wrong"] += 1
            else:
                if accuracy == "wrong":
                    module_stats[module][direction]["correct"] += 1
                    pair_module_stats[asset][module][direction]["correct"] += 1
                elif accuracy == "correct":
                    module_stats[module][direction]["wrong"] += 1
                    pair_module_stats[asset][module][direction]["wrong"] += 1

    # Build per-module summary
    modules_summary = []
    for module_key, display_name in MODULE_NAMES.items():
        stats = module_stats.get(module_key, {})
        call_c = stats.get("CALL", {}).get("correct", 0)
        call_w = stats.get("CALL", {}).get("wrong", 0)
        put_c = stats.get("PUT", {}).get("correct", 0)
        put_w = stats.get("PUT", {}).get("wrong", 0)
        total_c = call_c + put_c
        total_w = call_w + put_w
        total_all = total_c + total_w
        win_pct = (total_c / total_all * 100) if total_all else 0
        call_total = call_c + call_w
        call_win = (call_c / call_total * 100) if call_total else 0
        put_total = put_c + put_w
        put_win = (put_c / put_total * 100) if put_total else 0
        modules_summary.append({
            "module": module_key,
            "display_name": display_name,
            "total": total_all,
            "correct": total_c,
            "wrong": total_w,
            "win_pct": round(win_pct, 1),
            "call_win_pct": round(call_win, 1),
            "put_win_pct": round(put_win, 1),
            "call_correct": call_c,
            "call_wrong": call_w,
            "put_correct": put_c,
            "put_wrong": put_w,
        })

    # Overall accuracy
    acc_rows = cur.execute("""
        SELECT accuracy, COUNT(*) as n
        FROM signal_log
        WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
        GROUP BY accuracy
    """).fetchall()
    total_correct = sum(r["n"] for r in acc_rows if r["accuracy"] == "correct")
    total_wrong = sum(r["n"] for r in acc_rows if r["accuracy"] == "wrong")
    total_graded = total_correct + total_wrong
    overall_win = (total_correct / total_graded * 100) if total_graded else 0

    # Per-pair breakdown
    pairs_summary = {}
    for asset, pair_data in pair_module_stats.items():
        pair_modules = {}
        for module_key, display_name in MODULE_NAMES.items():
            stats = pair_data.get(module_key)
            if not stats:
                continue
            call_c = stats["CALL"]["correct"]
            call_w = stats["CALL"]["wrong"]
            put_c = stats["PUT"]["correct"]
            put_w = stats["PUT"]["wrong"]
            total_c = call_c + put_c
            total_w = call_w + put_w
            total_all = total_c + total_w
            if total_all == 0:
                continue
            pair_modules[module_key] = {
                "display_name": display_name,
                "total": total_all,
                "correct": total_c,
                "wrong": total_w,
                "win_pct": round(total_c / total_all * 100, 1) if total_all else 0,
            }
        if pair_modules:
            pairs_summary[asset] = pair_modules

    conn.close()

    return {
        "total_signals": total,
        "total_graded": total_graded,
        "overall_win_pct": round(overall_win, 1),
        "total_correct": total_correct,
        "total_wrong": total_wrong,
        "modules": modules_summary,
        "pairs": pairs_summary,
    }


@app.get("/api/signals/{asset}/{period}")
async def get_signals(asset: str, period: int, limit: int = 50):
    return {"signals": _db.get_recent_signals(asset, period, limit)}


@app.get("/api/signals/{asset}/{period}/{ctime}")
async def get_signal_detail(asset: str, period: int, ctime: int):
    """Return full detail for a single signal (win/loss reason, regime, etc.)."""
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
    # Railway detection: disable auto browser open, force headless
    if os.environ.get("RAILWAY_PROJECT_ID") or os.environ.get("RAILWAY_SERVICE_ID"):
        os.environ.setdefault("AUTO_OPEN_BROWSER", "0")
        os.environ.setdefault("HEADLESS", "1")
        print("[server] Railway environment detected — headless mode, no browser auto-open")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")