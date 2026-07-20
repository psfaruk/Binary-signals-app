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

    FIX (Bug #15, 2026-07-17): the previous filter had a "stream not found
    → send to all (safety)" fallback that fired when the asset's stream
    had already been torn down (e.g., post idle-eviction). That caused
    stale-asset messages to be broadcast to every viewer. Now if a stream
    exists but has no interested_cids, we skip the broadcast for that
    (asset, period); if no stream exists at all, we also skip — the
    viewers are clearly not watching this asset anymore.
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
        target_cids = []  # default: skip (was: list(clients.keys()))
        try:
            stream_key = (msg_asset, msg_period) if msg_period else None
            if stream_key and hasattr(feed, '_streams'):
                stream = feed._streams.get(stream_key)
                if stream and stream.interested_cids:
                    target_cids = list(stream.interested_cids)
                # else: stream gone or no viewers → skip (was: send to all)
        except Exception:
            # On unexpected error, fall back to broadcast-all to avoid
            # silently dropping important messages (status/pairs).
            target_cids = list(clients.keys())

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
    # Initialize brain tables
    from core.brain import init_brain
    init_brain()
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
    """Return both Real Market and OTC Market pair lists.

    Returns:
        {
          "real_pairs": [...],          # real-market pairs (no _otc suffix), payout >= 70%
          "otc_pairs":  [...],          # OTC pairs (_otc suffix), payout >= 85%
          "payout_floor_real": 70,
          "payout_floor_otc":  85,
          "pairs":        [...],        # BACKWARD COMPAT: combined list
          "payout_floor": 85,
        }
    """
    return feed.available_pairs()


@app.get("/api/pairs/{category}")
async def get_pairs_by_category(category: str):
    """Return only the pair list for the requested category.

    Args:
        category: "real" or "otc" (case-insensitive)

    Returns:
        {"category": "real", "pairs": [...], "payout_floor": 70}
        or 404 if category is unknown.
    """
    cat = category.lower().strip()
    all_pairs = feed.available_pairs()
    if cat == "real":
        return {
            "category": "real",
            "pairs": all_pairs["real_pairs"],
            "payout_floor": all_pairs["payout_floor_real"],
        }
    if cat == "otc":
        return {
            "category": "otc",
            "pairs": all_pairs["otc_pairs"],
            "payout_floor": all_pairs["payout_floor_otc"],
        }
    raise HTTPException(
        status_code=404,
        detail=f"unknown category {category!r}; expected 'real' or 'otc'")


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
        "has_client": getattr(feed, '_client', None) is not None,
        "streams": {},
        "pairs_count": len(feed._pairs_list) if hasattr(feed, '_pairs_list') else 0,
        "real_pairs_count": len(feed._real_pairs_list) if hasattr(feed, '_real_pairs_list') else 0,
        "otc_pairs_count":  len(feed._otc_pairs_list)  if hasattr(feed, '_otc_pairs_list')  else 0,
        "env": {
            "QX_TOKEN": "***" if os.environ.get("QX_TOKEN") else "(not set)",
            "QX_EMAIL": os.environ.get("QX_EMAIL", "(not set)"),
            "QX_PASSWORD": "***" if os.environ.get("QX_PASSWORD") else "(not set)",
            "USE_SIM": os.environ.get("USE_SIM", "0"),
            "QX_USE_RAW_WS": os.environ.get("QX_USE_RAW_WS", "0"),
            "PAYOUT_FLOOR_REAL": os.environ.get("QX_PAYOUT_FLOOR_REAL", "70"),
            "PAYOUT_FLOOR_OTC":  os.environ.get("QX_PAYOUT_FLOOR_OTC",
                                                os.environ.get("QX_PAYOUT_FLOOR", "85")),
            "SIGNAL_DELAY_SEC": os.environ.get("SIGNAL_DELAY_SEC", "3.0"),
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
    Visit /api/stats in browser to see which modules are performing well.

    FIX (BUG-3, 2026-07-18): previously had a local MODULE_NAMES dict that
    was missing `trend_follow` (the Real engine's 6th module), silently
    undercounting Real-engine signals in the per-module report. Now uses
    the shared `core.stats.compute_module_stats()` which sources module
    names from `core.constants.MODULE_NAMES` — the single source of truth.

    FIX (BUG-I, 2026-07-20): also reports DB-adaptation status — which
    pairs have enough graded samples for adaptation, and what the current
    learned weights are.
    """
    from core.stats import compute_module_stats
    stats = compute_module_stats(_db.DB_PATH)

    # Add adaptation status per pair
    try:
        from engines.otc.config import weight_adapter as _otc_adapter
        from engines.real.config import weight_adapter as _real_adapter
        adaptation_status = {}
        for asset in list(_otc_adapter.pair_configs.keys()) + list(_real_adapter.pair_configs.keys()):
            adapter = _otc_adapter if asset.endswith("_otc") else _real_adapter
            stats_data = _db.per_module_accuracy(asset, period=60, n=200)
            adapted = adapter.get_weights(asset, period=60, use_db=False)
            adapted_db = adapter.get_weights(asset, period=60, use_db=True)
            adaptation_status[asset] = {
                "has_enough_samples": any(
                    s.get("total", 0) >= 20 for s in stats_data.values()
                ),
                "static_weights": adapted,
                "adapted_weights": adapted_db,
                "module_accuracy": {
                    m: {"win_rate": s.get("win_rate"), "total": s.get("total", 0)}
                    for m, s in stats_data.items() if s.get("total", 0) > 0
                },
            }
        stats["adaptation_status"] = adaptation_status
    except Exception as e:
        stats["adaptation_error"] = str(e)

    return stats


@app.get("/api/brain")
async def brain_summary():
    """Brain summary — learning status, accuracy, insight count."""
    from core.brain import get_brain_summary
    return get_brain_summary()


@app.get("/api/brain/insights")
async def brain_insights(limit: int = 50):
    """Get auto-generated insights and recommendations."""
    from core.brain import get_insights
    return {"insights": get_insights(active_only=True, limit=limit)}


@app.get("/api/brain/learning")
async def brain_learning(asset: str = None, limit: int = 100):
    """Get learned weights per pair per module."""
    from core.brain import get_learning
    return {"learning": get_learning(asset=asset, limit=limit)}


@app.get("/api/brain/analyze")
async def brain_analyze():
    """Trigger brain analysis manually."""
    from core.brain import analyze_and_learn
    await asyncio.to_thread(analyze_and_learn)
    return {"status": "analysis complete"}


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

# Allowed candle periods (seconds). Anything outside this whitelist is
# rejected up-front — feed.ensure_stream would silently misbehave on
# invalid periods (negative, fractional, or unreasonably large).
# FIX (Bug #16, 2026-07-17): previously any integer was accepted, which
# could create bogus streams (e.g., period=-1 or period=999999) that
# wasted resources and corrupted the stream registry.
# FIX (AUDIT-CORE #1, 2026-07-19): import from core.constants instead
# of duplicating the literal set. The previous local definition would
# silently drift if core.constants.ALLOWED_PERIODS changed — e.g. if a
# new period (e.g. 15s) was added to the canonical constant, the WS
# endpoint would still reject it. Now both server and constants share
# one source of truth.
from core.constants import ALLOWED_PERIODS as _ALLOWED_PERIODS


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global cid_counter
    await ws.accept()
    cid_counter += 1
    cid = f"client-{cid_counter}"
    clients[cid] = ws
    print(f"[server] {cid} connected ({len(clients)} total)")

    # FIX (AUDIT-CORE #8, 2026-07-19): WS idle timeout. Previously a
    # client that connected and never sent would hold the WS open
    # forever, registered in `clients`, counted in len(clients). A
    # malicious/buggy client could exhaust server FDs by opening
    # thousands of idle connections. Now we apply a per-receive timeout
    # of WS_IDLE_TIMEOUT (default 300s) — if no message arrives within
    # that window, we close the connection with code 1008 (policy
    # violation) and free the slot.
    WS_IDLE_TIMEOUT = float(os.environ.get("WS_IDLE_TIMEOUT", "300.0"))

    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=WS_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                print(f"[server] {cid} idle timeout ({WS_IDLE_TIMEOUT}s) — closing")
                try:
                    await ws.close(code=1008, reason="idle timeout")
                except Exception:
                    pass
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            t = msg.get("type")

            if t == "subscribe":
                asset = msg.get("asset", "")
                # Validate period up-front. Reject anything not in the
                # whitelist with a clear error instead of letting the feed
                # create a bogus stream.
                try:
                    period = int(msg.get("period", 60))
                except (TypeError, ValueError):
                    period = 0  # forces the validation failure below
                if period not in _ALLOWED_PERIODS:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "error": f"invalid period {period!r}; allowed: "
                                 f"{sorted(_ALLOWED_PERIODS)}",
                    }))
                    continue
                # Optional `category` field: "real" or "otc". The server
                # now ENFORCES consistency between category and asset — if
                # a client sends category="real" but asset="EURUSD_otc"
                # (or vice versa), the subscribe is REJECTED with an error.
                # FIX (2026-07-17): previously the server silently honored
                # the asset name even on mismatch, which let an OTC pair
                # be analyzed by the Real engine (defeating the whole point
                # of having two engines).
                category = (msg.get("category") or "").lower().strip()
                if category and category not in ("real", "otc"):
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "error": f"invalid category {category!r}; "
                                 f"expected 'real' or 'otc'",
                    }))
                    continue
                # If category is specified, validate it matches the asset.
                if category:
                    expected_cat = "otc" if asset.endswith("_otc") else "real"
                    if category != expected_cat:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "error": f"category/asset mismatch: category={category!r} "
                                     f"but asset {asset!r} belongs to {expected_cat!r}. "
                                     f"Switch to the {expected_cat!r} category in the 3-dot "
                                     f"menu to subscribe to this pair.",
                        }))
                        continue
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
                try:
                    period = int(msg.get("period", 60))
                except (TypeError, ValueError):
                    period = 60
                if period not in _ALLOWED_PERIODS:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "error": f"invalid period {period!r}",
                    }))
                    continue
                # FIX (AUDIT-CORE #7, 2026-07-19): wrap the synchronous
                # SQLite call in asyncio.to_thread so the event loop is
                # not blocked. Previously this endpoint would block all
                # other WS clients for the duration of the query — under
                # load this caused visible tick stutter for everyone else.
                sigs = await asyncio.to_thread(_db.get_recent_signals, asset, period, 50)
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