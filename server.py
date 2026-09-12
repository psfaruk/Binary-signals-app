"""
Binary Signal App — WebSocket server.
Serves static frontend and bridges QuotexFeed to browser clients.
"""
import asyncio
import hmac
import json
import logging
import math
import os
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse, Response, JSONResponse

# Load .env from project root (if it exists — Railway uses env vars directly)
from dotenv import load_dotenv
_env_path = Path(__file__).parent / ".env"
# QX_SKIP_DOTENV=1 keeps a test/verification run from picking up the operator's
# real .env credentials (scripts/verify_token_import.py relies on this).
if _env_path.exists() and os.environ.get("QX_SKIP_DOTENV", "0") != "1":
    load_dotenv(_env_path)
    print("[server] .env ফাইল loaded")
else:
    print("[server] .env ফাইল নেই — Railway env vars ব্যবহার করা হচ্ছে")

# Ensure QX_ROOT points to a valid temp dir on Linux/Mac
if sys.platform != "win32":
    os.environ.setdefault("QX_ROOT",
                          os.path.join(os.environ.get("TMPDIR", "/tmp"), "plybit_cache"))

import db as _db

if os.environ.get("QX_USE_RAW_WS", "0") == "1":
    print("[server] QX_USE_RAW_WS=1 — raw WebSocket backend " "(pyquotex optional)")
else:
    print("[server] QX_USE_RAW_WS=0 — vendored pyquotex with Firefox TLS " "(Cloudflare bypass)")

# Always import the real feed. sim_feed.py is no longer used.
import feed as _feed_mod
from feed import QuotexFeed as _Feed
print("[server] ✅ using REAL Quotex feed (live data only — sim mode disabled)")

# ── Persisted-token bootstrap ────────────────────────────────────────────────
# A token pushed from the frontend UI is written to the Railway volume by
# core.token_store. Load it BEFORE the token check below (and before the feed
# ever connects) so a redeploy comes back live on its own instead of waiting
# for the operator to paste the token again. An explicit QX_TOKEN env var
# always wins — that is the operator's deliberate override.
from core import token_store as _token_store

def _bootstrap_persisted_token() -> None:
    try:
        token, reason = _token_store.resolve_boot_token()
    except Exception as exc:
        print(f"[server] stored-token bootstrap failed: {exc}")
        return
    if not token:
        print(f"[server] no token in env and none stored in "
              f"{_token_store.state_dir()} — waiting for one from the UI "
              f"(🔑 Token button)")
        return
    os.environ["QX_TOKEN"] = token
    preview = _token_store.mask(token)
    if reason == "stored-newer":
        print(f"[server] ✅ restored stored token {preview} — it was imported "
              f"from the UI after the current QX_TOKEN variable appeared, so "
              f"it wins over the (older) Railway Variable")
    elif reason == "stored":
        rec = _token_store.load_token() or {}
        age_min = (time.time() - float(rec.get("saved_at") or 0)) / 60.0
        print(f"[server] ✅ restored stored token {preview} "
              f"(saved {age_min:.0f} min ago via {rec.get('source', '?')}) "
              f"from {_token_store.state_dir()}")
    elif reason == "env-new":
        print(f"[server] using QX_TOKEN env var {preview} (new value — "
              f"takes precedence over any stored token)")
    else:
        print(f"[server] using QX_TOKEN env var {preview}")

_bootstrap_persisted_token()

# ── Auto-session bootstrap ───────────────────────────────────────────────────
# core/qx_session.py can mint a brand-new SSID from the operator's stored
# Quotex cookies, so an expired token is no longer an outage that waits for a
# human. Report at boot whether that safety net is actually armed — silently
# missing cookies is exactly how this app used to spend a night offline.
def _report_auto_session() -> None:
    try:
        from core import qx_session
    except Exception as exc:
        print(f"[server] ⚠️  auto-session unavailable: {exc}")
        return
    if not qx_session.enabled():
        print("[server] auto-session DISABLED (QX_AUTO_REFRESH=0) — tokens "
              "must be pushed by hand")
        return
    if qx_session.configured():
        st = qx_session.status()
        print(f"[server] 🔄 auto-session ARMED — cookies "
              f"({len(st.get('cookie_names') or [])} stored, "
              f"cf_clearance={'yes' if st.get('has_cf_clearance') else 'no'}) "
              f"→ will mint fresh tokens from {st.get('web_hosts')}")
        if not st.get("persistent"):
            print("[server] ⚠️  cookie store is NOT on a persistent volume — "
                  "it will be lost on redeploy")
    else:
        print("[server] ⚠️  auto-session NOT configured — no session cookies "
              "stored. Import them once in the 🔑 Token panel and the app "
              "will never need a manual token again.")

_report_auto_session()

# Token status check — log a clear, actionable error if missing.
_QX_TOKEN = os.environ.get("QX_TOKEN", "").strip()
_QX_EMAIL = os.environ.get("QX_EMAIL", "").strip()
if not _QX_TOKEN and not _QX_EMAIL:
    print("[server] ❌❌❌ CRITICAL: no Quotex credentials found ❌❌❌")
    print("[server]    QX_TOKEN env var is not set.")
    print("[server]    The server will start (so /api/set-token is reachable)")
    print("[server]    but ALL stream subscriptions will return errors until")
    print("[server]    a token is provisioned. To fix:")
    print("[server]      1. Set QX_TOKEN in Railway Variables (one-time)")
    print("[server]      2. OR visit /api/set-token?token=YOUR_TOKEN in browser")
    print("[server]      3. OR send POST /api/set-token with JSON body")
    print("[server]    See RAILWAY_TOKEN_SETUP.md for full instructions.")
elif _QX_TOKEN:
    print(f"[server] ✅ QX_TOKEN found ({_QX_TOKEN[:8]}...{_QX_TOKEN[-4:]})")
else:
    print("[server] ⚠️  QX_TOKEN not set — app will wait for a token.")
    print("[server]    Email/password login is DISABLED (Cloudflare blocks Railway IPs).")
    print("[server]    To get live data, push a token via ONE of:")
    print("[server]      1. Set QX_TOKEN in Railway Variables (persists across redeploys)")
    print("[server]      2. POST /api/set-token {\"token\":\"YOUR_TOKEN\"} (runtime, no redeploy)")
    print("[server]      3. GET  /api/set-token?token=YOUR_TOKEN (browser-friendly)")
    print("[server]    See RAILWAY_TOKEN_SETUP.md for token extraction instructions.")

feed = _Feed()
clients: dict[str, WebSocket] = {}   # cid -> ws
cid_counter = 0
_MAX_WS_CLIENTS = int(os.environ.get("MAX_WS_CLIENTS", "200"))
_logger = logging.getLogger("server")
_WS_IDLE_TIMEOUT = float(os.environ.get("WS_IDLE_TIMEOUT", "300.0"))
_MAX_WS_MSG_BYTES = int(os.environ.get("MAX_WS_MSG_BYTES", str(1 << 20)))
_ALLOWED_WS_ORIGINS = [
    o.strip().lower() for o in os.environ.get(
        "ALLOWED_WS_ORIGINS",
        "http://localhost,http://127.0.0.1,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",") if o.strip()
]

def _autodetect_railway_origins():
    origins = set()
    # Railway provides these vars on every deployed service.
    for var in ("RAILWAY_PUBLIC_DOMAIN", "RAILWAY_STATIC_URL",
                "RAILWAY_DOMAIN", "RAILWAY_PUBDOMAIN"):
        val = os.environ.get(var, "").strip()
        if val:
            # Normalize: ensure scheme, strip trailing slash.
            if not val.startswith(("http://", "https://")):
                val = f"https://{val}"
            origins.add(val.lower().rstrip("/"))
    # Also support `PORT`-based Railway preview domains.
    service_name = os.environ.get("RAILWAY_SERVICE_NAME", "").strip()
    if service_name:
        pass
    return origins

_auto_origins = _autodetect_railway_origins()
if _auto_origins:
    for o in _auto_origins:
        if o not in _ALLOWED_WS_ORIGINS:
            _ALLOWED_WS_ORIGINS.append(o)
    print(f"[server] ✅ Auto-detected Railway WS origins: {sorted(_auto_origins)}")
_PATTERNS_INITIALIZED = False
_BRAIN_ANALYZE_LOCK = asyncio.Lock()
_PATTERNS_REFRESH_LOCK = asyncio.Lock()
_AUTO_TUNE_APPLY_LOCK = asyncio.Lock()

def _check_admin_key(provided: Optional[str]) -> None:
    """Constant-time admin-key comparison.
    FIX (A-19 D-03 / S-4): previously FAIL-OPEN — if ADMIN_KEY env var was
    unset, ALL admin endpoints became unauthenticated silently. Now: FAIL-
    CLOSED. If ADMIN_KEY is unset, admin endpoints return 503 with a clear
    message telling the operator to set the env var. This prevents accidental
    public exposure of /api/debug, /api/db-download, /api/signals/clear etc.
    on fresh Railway deploys where the operator forgot to set ADMIN_KEY.
    """
    expected = os.environ.get("ADMIN_KEY", "").strip()
    if not expected:
        # FAIL-CLOSED: refuse admin endpoints until ADMIN_KEY is set.
        raise HTTPException(
            status_code=503,
            detail="ADMIN_KEY env var is not set. Admin endpoints are disabled "
                   "until the operator sets ADMIN_KEY in Railway → Variables."
        )
    if not provided:
        raise HTTPException(status_code=403, detail="forbidden")
    if not hmac.compare_digest(provided.strip(), expected):
        raise HTTPException(status_code=403, detail="forbidden")

async def broadcast(msg: dict):
    """Push a message to connected clients — PARALLEL sends."""
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

            if not target_cids:
                sim = getattr(feed, '_sim_delegate', None)
                if sim is not None and hasattr(sim, '_streams'):
                    sim_stream = sim._streams.get(stream_key)
                    if sim_stream and sim_stream.interested_cids:
                        target_cids = list(sim_stream.interested_cids)
        except (AttributeError, TypeError, KeyError):
            # On lookup error, skip the broadcast for this (asset, period)
            # rather than risk leaking the message to non-subscribers.
            target_cids = []

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
    for cid, result in zip(cids, results):
        if isinstance(result, BaseException):
            clients.pop(cid, None)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup + shutdown lifecycle (replaces @app.on_event)."""
    global _PATTERNS_INITIALIZED
    print("[server] lifespan: startup beginning")

    def _sync_init():
        _db.init()
        # PERSISTENCE-FIX (2026-09-09): periodic online backup of signals.db
        # (volume-backed or repo-local) + TARGET-75 gate table init.
        try:
            _db.start_backup_scheduler()
        except Exception as _bk_exc:
            print(f"[server] backup scheduler init failed (non-fatal): {_bk_exc}")
        try:
            from core.target_gate import _ensure_table as _tg_ensure
            _conn = _db._conn()
            try:
                _tg_ensure(_conn)
            finally:
                _conn.close()
            print("[server] TARGET-75 gate table ready")
        except Exception as _tg_exc:
            print(f"[server] target-gate table init failed (non-fatal): {_tg_exc}")
        # Initialize brain tables
        from core.brain import init_brain
        init_brain()
        try:
            from core.time_patterns import init_patterns, recompute_from_signal_log
            init_patterns()
            summary = recompute_from_signal_log(min_samples=3)
            total_patterns = sum(sum(dims.values()) for dims in summary.values()) if summary else 0
            print(f"[server] patterns loaded: {total_patterns} entries across " f"{len(summary)} pairs")
        except Exception as _e:
            print(f"[server] pattern init failed (non-fatal): {_e}")
        try:
            from core.algorithm_monitor import init_algorithm_monitor
            init_algorithm_monitor()
            print("[server] algorithm monitor initialized")
        except Exception as _e:
            print(f"[server] algorithm monitor init failed (non-fatal): {_e}")

    await asyncio.to_thread(_sync_init)
    _PATTERNS_INITIALIZED = True

    def _on_feed_done(task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            _logger.exception("feed task died", exc_info=exc)
            print(f"[server] FATAL: feed task died: " f"{type(exc).__name__}: {exc}")
        else:
            _logger.error("feed task exited NORMALLY (unexpected) — " "this means feed.run() returned without raising. " "The feed is now dead; restart the container or " "investigate feed.run()'s exit paths.")
            print("[server] FATAL: feed task exited normally (unexpected) " "— feed is dead. Restart the container.")
            # Tag the task so /healthz can detect this state.
            app.state.feed_dead_normal_exit = True
    feed_task = asyncio.create_task(feed.run(broadcast))
    feed_task.add_done_callback(_on_feed_done)
    # Expose the feed task so /healthz can check it (Problem 38).
    app.state.feed_task = feed_task
    print("[server] lifespan: feed task started")
    # Auto-open browser (local dev only — disabled on Railway via env var)
    _auto_open_browser()
    print("[server] lifespan: startup complete")
    yield
    print("[server] lifespan: shutdown beginning")
    # Flush learned agent weights before the container goes away — otherwise
    # everything learned since the last periodic save is lost on redeploy.
    try:
        from core.agent_brain import agent
        n_saved = await asyncio.to_thread(agent.save_state)
        print(f"[server] agent: persisted {n_saved} learned model(s)")
    except Exception as _e:
        print(f"[server] agent save on shutdown failed (non-fatal): {_e}")
    await feed.shutdown()
    feed_task.cancel()
    try:
        await feed_task
    except asyncio.CancelledError as _e:
        print(f"[silent-except] server.py:368 {type(_e).__name__}: {_e}")
        pass
    print("[server] lifespan: shutdown complete")

app = FastAPI(lifespan=lifespan)
static_dir = Path(__file__).parent / "static"

# ── API key system (PHASE-3-FIX, 2026-08-13) ─────────────────────────────────
# User requirement: "এই অ্যাপ url ব্যবহার করে সমস্ত ডাটা সিগন্যাল সিগন্যাল যে কেউ
# দেখতে পারবে। কোনো বাধা থাকবে না। প্রয়োজন হলে একটি api key সিস্টেম তৈরি করেন।"
# Public read endpoints (signals, pairs, history) are accessible WITHOUT any
# auth. API keys are OPTIONAL — for programmatic clients that want higher rate
# limits and audit trails. Admin PIN (existing) guards token-management.
from core import api_keys as _api_keys  # noqa: E402

@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """Classify every request and enforce auth policy.

    • public_read  → no auth required (default behavior, anyone with URL)
    • api_key_write → require valid API key (Bearer token or ?api_key=)
    • admin        → require admin PIN (X-App-Pin) or ADMIN_KEY
    • unknown      → behave as public_read when QX_PUBLIC_READ=1 (default),
                      otherwise require API key (fail-closed)

    The middleware is intentionally lightweight — it never touches the DB for
    public reads, so anonymous traffic stays fast.

    FIX: middleware must return JSONResponse (not raise HTTPException) because
    FastAPI's BaseHTTPMiddleware doesn't translate exceptions into proper
    status codes — they bubble up as 500.
    """
    path = request.url.path
    # Skip auth for static assets, root, and WebSocket (WS auth handled in /ws).
    if (path.startswith("/static/") or path == "/" or path == "/favicon.ico"
            or path == "/ws" or path == "/healthz" or path == "/app"
            or path in ("/otc.html", "/real.html", "/alltime_otc.html")):
        return await call_next(request)

    category = _api_keys.classify_request(path)

    if category == "public_read":
        # Anyone can read — no auth check.
        return await call_next(request)

    if category == "api_key_write":
        key = _api_keys.extract_key_from_request(request)
        if not key:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "API key required. Pass via 'Authorization: Bearer qxa_...' "
                              "or '?api_key=qxa_...'. Create one at /api/keys (admin).",
                },
            )
        info = _api_keys.verify_key(key)
        if not info:
            return JSONResponse(
                status_code=403,
                content={"detail": "invalid or revoked API key"},
            )
        # Attach key info for downstream handlers.
        request.state.api_key = info
        return await call_next(request)

    if category == "admin":
        # Admin routes: require admin PIN (X-App-Pin) OR ADMIN_KEY (X-Admin-Key).
        # The actual PIN verification is done inside each handler via
        # _request_secrets() / _check_admin_key(). Here we just enforce that
        # SOME admin credential is present.
        pin = request.headers.get("X-App-Pin", "")
        admin_key = request.headers.get("X-Admin-Key", "")
        if not pin and not admin_key:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "admin endpoint — provide X-App-Pin (operator PIN) or "
                              "X-Admin-Key header.",
                },
            )
        return await call_next(request)

    # Unknown route — default to public read when QX_PUBLIC_READ=1 (default).
    if _api_keys.is_public_read_enabled():
        return await call_next(request)
    # Fail-closed mode — require API key.
    key = _api_keys.extract_key_from_request(request)
    if not key or not _api_keys.verify_key(key):
        return JSONResponse(
            status_code=401,
            content={"detail": "QX_PUBLIC_READ=0 — API key required for this endpoint."},
        )
    return await call_next(request)


# ── API key management endpoints ─────────────────────────────────────────────

def _require_admin_for_api_keys(request: Request) -> None:
    """API key management requires either ADMIN_KEY or operator PIN.

    Reuses the existing _check_admin_key helper when ADMIN_KEY is set.
    Otherwise falls back to PIN verification (same as token-import endpoints).
    """
    admin_key = request.headers.get("X-Admin-Key", "")
    if admin_key:
        _check_admin_key(admin_key)
        return
    # Fall back to PIN (X-App-Pin header only — body not read here to keep
    # this helper synchronous).
    pin = request.headers.get("X-App-Pin", "")
    if not pin:
        raise HTTPException(
            status_code=401,
            detail="X-Admin-Key or X-App-Pin header required to manage API keys.",
        )
    try:
        ok, _reason = _verify_operator_pin(pin)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PIN verification failed: {exc}")
    if not ok:
        raise HTTPException(status_code=403, detail=f"invalid PIN: {_reason}")


def _verify_operator_pin(pin: str) -> tuple[bool, str]:
    """Verify the operator PIN via the existing auth system.

    Reuses the same logic as /api/set-token. If no PIN has been claimed yet,
    the first PIN becomes the operator PIN (same as token-import flow).
    """
    import hashlib as _hashlib
    import sqlite3 as _sqlite3
    try:
        with _db._read_cursor() as cur:
            cur.execute("SELECT value FROM _meta WHERE key='operator_pin_hash'")
            row = cur.fetchone()
        if not row:
            # No PIN claimed — first call claims it. Allow through; the
            # subsequent POST will set the PIN via the existing claim flow.
            return True, "no-pin-claimed-yet"
        # sqlite3.Row supports both index and key access.
        stored_hash = row["value"] if isinstance(row, _sqlite3.Row) else row[0]
        candidate = _hashlib.sha256(pin.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(candidate, stored_hash):
            return False, "PIN does not match"
        return True, "ok"
    except Exception as exc:
        return False, f"PIN check error: {exc}"


@app.get("/api/keys")
async def api_keys_list(request: Request):
    """List all API keys (admin only). Returns key metadata, never the raw key."""
    _require_admin_for_api_keys(request)
    keys = _api_keys.list_keys()
    return {
        "total": len(keys),
        "keys": [
            {
                "id": k.id,
                "label": k.label,
                "key_prefix": k.key_prefix,
                "created": k.created,
                "last_used": k.last_used,
                "active": k.active,
                "rate_limit_per_min": k.rate_limit_per_min,
                "total_requests": k.total_requests,
            } for k in keys
        ],
    }


@app.post("/api/keys")
async def api_keys_create(request: Request):
    """Create a new API key (admin only). Returns the raw key ONCE."""
    _require_admin_for_api_keys(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    label = (body.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label is required")
    rate_limit = int(body.get("rate_limit_per_min", 60))
    rate_limit = max(1, min(rate_limit, 600))
    raw_key, info = _api_keys.create_key(label, rate_limit)
    return {
        "raw_key": raw_key,  # shown only here — store client-side
        "info": {
            "id": info.id,
            "label": info.label,
            "key_prefix": info.key_prefix,
            "created": info.created,
            "active": info.active,
            "rate_limit_per_min": info.rate_limit_per_min,
        },
        "message": "Store the raw_key securely — it will not be shown again.",
    }


@app.delete("/api/keys/{key_id}")
async def api_keys_revoke(key_id: int, request: Request):
    """Revoke (deactivate) an API key by ID (admin only)."""
    _require_admin_for_api_keys(request)
    ok = _api_keys.revoke_key(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="key not found")
    return {"revoked": True, "id": key_id}


@app.get("/api/keys/verify")
async def api_keys_verify_endpoint(request: Request):
    """Verify the calling client's API key. Public endpoint (no admin needed).

    Useful for clients to test their key. Returns key metadata if valid.
    """
    key = _api_keys.extract_key_from_request(request)
    if not key:
        return {"valid": False, "reason": "no API key provided"}
    info = _api_keys.verify_key(key)
    if not info:
        return {"valid": False, "reason": "invalid or revoked"}
    return {
        "valid": True,
        "label": info.label,
        "key_prefix": info.key_prefix,
        "rate_limit_per_min": info.rate_limit_per_min,
        "total_requests": info.total_requests,
    }


from starlette.staticfiles import StaticFiles as _StarletteStaticFiles
class _NoCacheStaticFiles(_StarletteStaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
        resp.headers["Pragma"] = "no-cache"  # HTTP/1.0 legacy
        resp.headers["Expires"] = "0"        # HTTP/1.0 legacy
        return resp

app.mount("/static", _NoCacheStaticFiles(directory=str(static_dir)), name="static")

# ── Lifecycle ─────────────────────────────────────────────────────────────────

def _auto_open_browser():
    """Open the default browser to the app URL after server starts."""
    if os.environ.get("AUTO_OPEN_BROWSER", "1").lower() in ("0", "false", "no", "off"):
        return

    port = os.environ.get("PORT", "8000")
    url = f"http://localhost:{port}"

    def _open():
        import urllib.request
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{url}/healthz", timeout=1.0) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.25)
        try:
            webbrowser.open(url)
            print(f"[server] browser opened: {url}")
        except Exception as exc:
            print(f"[server] could not open browser: {exc}")
            print(f"[server] manually open: {url}")

    threading.Thread(target=_open, daemon=True).start()

# (lifespan handler above replaces the deprecated @app.on_event startup/shutdown)

# ── HTTP routes ───────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz(request: Request):
    """Railway healthcheck endpoint.
    FIX (A-19 D-01): previously always returned HTTP 200 — Railway only checks
    status codes, so a dead feed (token expired, WS disconnected, all streams
    stale) passed healthcheck forever and the container was never restarted.
    Now: returns 503 when feed_healthy=False so Railway's restart policy
    actually triggers.
    """
    feed_task = getattr(request.app.state, "feed_task", None)
    feed_task_alive = bool(feed_task is not None and not feed_task.done())
    feed_dead_normal_exit = bool(getattr(request.app.state,
                                         "feed_dead_normal_exit", False))
    feed_healthy = feed_task_alive and not feed_dead_normal_exit
    payload = {"ok": feed_healthy, "feed_connected": bool(getattr(feed, "_connected", False)), "feed_task_alive": feed_task_alive, "feed_dead_normal_exit": feed_dead_normal_exit, "feed_healthy": feed_healthy}
    # 503 when feed is dead so Railway restarts the container
    if not feed_healthy:
        return JSONResponse(status_code=503, content=payload)
    return payload

@app.get("/api/token-status")
async def token_status():
    """Returns whether the app has live Quotex credentials."""
    qx_token = os.environ.get("QX_TOKEN", "").strip()
    qx_email = os.environ.get("QX_EMAIL", "").strip()
    has_token = bool(qx_token)
    has_email = bool(qx_email)

    connection_status = "disconnected"
    # FIX: _consecutive_rejects / _token_dead_at live on the Quotex client
    # (quotex_ws.QuotexWSClient), never on the feed — reading them off `feed`
    # meant this endpoint reported token_dead=False forever, so the UI could
    # never tell "expired token" from "still connecting".
    _client_now = getattr(feed, "_client", None)
    consecutive_rejects = int(getattr(_client_now, "_consecutive_rejects", 0) or 0)
    token_dead = bool(getattr(_client_now, "_token_dead_at", 0))
    try:
        client = getattr(feed, "_client", None)
        if hasattr(client, "_authorized") or hasattr(client, "_connected"):
            authorized = bool(getattr(client, "_authorized", False))
            connected = bool(getattr(client, "_connected", False))
        else:
            authorized = bool(feed._connected)
            connected = bool(feed._connected)
        if authorized and connected:
            connection_status = "live_authorized"
        elif connected and not authorized:
            connection_status = "connected_unauth"
        elif token_dead:
            connection_status = "token_dead_backoff"
        else:
            connection_status = "disconnected"
    except Exception as _e:
        print(f"[silent-except] server.py:527 {type(_e).__name__}: {_e}")
        pass

    if has_token:
        preview = f"{qx_token[:8]}...{qx_token[-4:]}" if len(qx_token) > 12 else "(short)"
        if connection_status == "live_authorized":
            status = "live_token"
            message = f"QX_TOKEN is set ({preview}) — connected + authorized. Live data flowing."
        elif connection_status == "token_dead_backoff":
            status = "token_dead"
            message = (f"⛔ Quotex REJECTED the token {consecutive_rejects}x consecutively. " f"Token is likely EXPIRED or REVOKED by Quotex. Refresh the SSID and " f"set it via /api/set-token to restore live data.")
        elif connection_status in ("connected_unauth", "disconnected"):
            status = "token_set_but_connecting"
            message = (f"QX_TOKEN is set ({preview}) but Quotex connection is " f"'{connection_status}'. Will retry shortly.")
        else:
            status = "live_token"
            message = f"QX_TOKEN is set ({preview}) — connection state: {connection_status}"
    elif has_email:
        status = "email_only_no_token"
        message = ("QX_EMAIL is set but no QX_TOKEN — email/password login " "is DISABLED on Railway (Cloudflare blocks datacenter IPs "
                   "and Quotex may ban the account for repeated failures). " "Push a Quotex token via /api/set-token to get live data.")
    else:
        status = "no_credentials"
        message = ("❌ No Quotex credentials — open the 🔑 Token panel in the app "
                   "and paste a fresh Quotex session token to go live.")
    try:
        stored = _token_store.token_meta()
        auth = _token_store.auth_state()
    except Exception as exc:
        stored, auth = {"stored": False, "error": str(exc)}, {"claimed": False}
    live_streams = 0
    try:
        live_streams = len(getattr(feed, "_streams", {}) or {})
    except Exception:
        pass

    # Auto-session changes what the operator should DO about a dead token:
    # with cookies stored the app heals itself, so the panel must not keep
    # demanding a manual paste.
    try:
        from core import qx_session
        auto_session = qx_session.status()
    except Exception as exc:
        auto_session = {"enabled": False, "configured": False, "error": str(exc)}
    if auto_session.get("configured") and auto_session.get("enabled"):
        if status == "no_credentials":
            status = "auto_session_pending"
            message = ("No token yet — but session cookies are stored, so the "
                       "app is minting one itself. This takes a few seconds.")
        elif token_dead:
            message += (" Auto-refresh is armed and will replace it "
                        "automatically — no action needed.")
    elif status == "no_credentials":
        message = ("❌ No Quotex credentials — open the 🔑 Token panel and "
                   "either paste a session token, or paste your Quotex "
                   "cookies once so the app can refresh its own tokens "
                   "forever.")

    return {"status": status, "has_token": has_token, "has_email": has_email, "connection_status": connection_status, "consecutive_rejects": consecutive_rejects, "token_dead": token_dead, "sim_mode_disabled": True, "message": message, "action": "refresh_token" if token_dead else ("set_token" if status == "no_credentials" else None),
            "active_token": _token_store.mask(qx_token),
            "live": connection_status == "live_authorized",
            "streams": live_streams,
            "stored_token": stored,
            "auto_session": auto_session,
            "auth": auth}


# ── Frontend token import: access control ────────────────────────────────────
# The UI needs a way to push a token without a Railway redeploy. Leaving
# /api/set-token open on a public Railway domain would let anyone swap the
# feed's credentials, so it is gated by a PIN the operator claims on first
# use (or by ADMIN_KEY when that env var is set). See core/token_store.py.

def _request_secrets(body: dict, request: Request) -> tuple[str, str]:
    """Pull (pin, admin_key) from headers or JSON body."""
    body = body if isinstance(body, dict) else {}
    pin = (request.headers.get("X-App-Pin") or body.get("pin") or "").strip()
    admin_key = (request.headers.get("X-Admin-Key")
                 or body.get("x_admin_key")
                 or body.get("admin_key") or "").strip()
    return pin, admin_key


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


@app.get("/api/auth/state")
async def auth_state():
    """Does this deployment already have an access PIN? (no secrets returned)"""
    return _token_store.auth_state()


@app.post("/api/auth/claim")
async def auth_claim(request: Request):
    """First-run claim of the access PIN from the UI."""
    body = await _json_body(request)
    pin = (body.get("pin") or "").strip()
    result = _token_store.claim_pin(pin)
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)
    return {"ok": True, "message": "PIN set — keep it, you need it to import "
                                   "tokens from now on.", **_token_store.auth_state()}


@app.post("/api/auth/change-pin")
async def auth_change_pin(request: Request):
    """Rotate the PIN (needs the old PIN, or ADMIN_KEY as master key)."""
    body = await _json_body(request)
    old_pin, admin_key = _request_secrets(body, request)
    result = _token_store.change_pin((body.get("new_pin") or "").strip(),
                                     old_pin=old_pin, admin_key=admin_key)
    if not result.get("ok"):
        return JSONResponse(status_code=403, content=result)
    return {"ok": True, "message": "PIN updated."}


@app.post("/api/auth/verify")
async def auth_verify(request: Request):
    """Check a PIN without doing anything — lets the UI unlock its form."""
    body = await _json_body(request)
    pin, admin_key = _request_secrets(body, request)
    allowed, reason = _token_store.authorize(pin=pin, admin_key=admin_key)
    if not allowed:
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": reason})
    return {"ok": True, "via": reason}

@app.get("/")
async def index():
    """Root — serve the consolidated app.html (PHASE-4-FIX, 2026-08-13).

    Replaces the old router (index.html → otc.html/real.html/alltime_otc.html).
    The app.html file is a single-page app that reads ?market= from the URL
    or `marketCategory` from localStorage to decide which market to show.
    """
    return FileResponse(static_dir / "app.html",
                        headers={"Cache-Control": "no-store, no-cache, max-age=0, must-revalidate"})


@app.get("/app")
async def app_alias():
    """Alias for / — same consolidated app.html."""
    return FileResponse(static_dir / "app.html",
                        headers={"Cache-Control": "no-store, no-cache, max-age=0, must-revalidate"})


# Legacy routes — redirect to the consolidated app so old bookmarks keep working.
@app.get("/otc.html")
async def legacy_otc():
    """Legacy OTC page — redirect to consolidated app."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/?market=otc", status_code=302)


@app.get("/real.html")
async def legacy_real():
    """Legacy Real page — redirect to consolidated app."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/?market=real", status_code=302)


@app.get("/alltime_otc.html")
async def legacy_alltime_otc():
    """Legacy All-OTC page — redirect to consolidated app."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/?market=alltime_otc", status_code=302)

@app.get("/api/set-token")
async def set_token_get(token: str, x_admin_key: Optional[str] = None,
                        pin: Optional[str] = None):
    """Set Quotex token at runtime — no restart needed (browser-friendly).

    USER REQ 2026-08-17: just `?token=...` is enough — no PIN, no admin key
    needed on the frontend. Auth gate is open by design (see
    core/token_store.authorize). Admin-key is still accepted for backwards
    compatibility with scripts/token_agent.py and scripts/minter_service.py.
    """
    allowed, reason = _token_store.authorize(pin=pin or "",
                                             admin_key=x_admin_key or "")
    if not allowed:
        raise HTTPException(status_code=403, detail=_auth_error(reason))
    return await _apply_token(token, source="api-get")

@app.post("/api/set-token")
async def set_token_post(request: Request):
    """Set Quotex token via POST — used by the 🔑 Token panel in the UI.

    USER REQ 2026-08-17: "টোকেন দিলেই ডেটা আসবে" — paste a token, get live
    data. No PIN, no admin key required. The body only needs `{token: "..."}`.
    """
    body = await _json_body(request)
    if not body:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "invalid JSON body"})
    pin, admin_key = _request_secrets(body, request)
    # authorize() always returns (True, "open") now — kept for audit trail.
    allowed, reason = _token_store.authorize(pin=pin, admin_key=admin_key)
    if not allowed:
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": _auth_error(reason),
                                     "reason": reason,
                                     "auth": _token_store.auth_state()})
    return await _apply_token(body.get("token", ""), source=body.get("source") or "ui")


def _auth_error(reason: str) -> str:
    if reason == "unclaimed":
        return ("This app has no access PIN yet. Set one first "
                "(POST /api/auth/claim {\"pin\":\"…\"} — the 🔑 Token panel "
                "does it for you), then import the token with that PIN.")
    return reason


async def _apply_token(token: str, source: str = "api"):
    """Apply a new Quotex token at runtime — and persist it across redeploys."""
    raw_len = len(token or "")
    token, norm_meta = _token_store.normalize_token(token)
    err = _token_store.validate_token(token)
    if err:
        return {"ok": False, "error": err, "input_chars": raw_len}

    # Set the token in the environment so _connect() picks it up
    os.environ["QX_TOKEN"] = token

    # Persist to the Railway volume so the NEXT deploy boots live without
    # anyone re-pasting it. This is the whole point of the UI import — the
    # old flow only touched os.environ, which dies with the container.
    persist_ok, persist_err = True, None
    try:
        _token_store.save_token(token, source=source)
    except Exception as exc:
        persist_ok, persist_err = False, str(exc)
        print(f"[server] ⚠️  token persist failed: {exc}")

    feed._connected = False
    feed._abandoned = False
    feed._reconnect_attempts = 0
    feed._last_error = None
    feed._last_error_time = 0
    feed._token_update_pending = True

    # Drop the client that was authorized with the OLD token, and clear any
    # "token is dead" backoff it recorded — otherwise a freshly pasted token
    # sits behind a 60s penalty earned by the expired one.
    old_client = getattr(feed, "_client", None)
    if old_client is not None:
        try:
            old_client._token_dead_at = 0
            old_client._consecutive_rejects = 0
        except Exception:
            pass
        try:
            await old_client.close()
        except Exception as exc:
            print(f"[server] old client close failed (non-fatal): {exc}")
        feed._client = None

    for s in getattr(feed, '_streams', {}).values():
        try:
            s.idle_since = None
        except (AttributeError, TypeError) as _e:
            print(f"[silent-except] server.py:699 {type(_e).__name__}: {_e}")
            pass

    try:
        from quotex_ws import QuotexWSClient
        QuotexWSClient.save_token_only(token)
        print(f"[server] token saved to session.json ({token[:8]}...)")
    except Exception as e:
        print(f"[server] session.json save error: {e}")

    # Wake the feed's reconnect backoff immediately (it can be sleeping for
    # up to 120s), so "paste token → live" takes seconds, not minutes.
    woke = False
    try:
        woke = bool(feed.notify_token_pushed())
    except Exception as exc:
        print(f"[server] token wake-up failed (non-fatal): {exc}")

    # MODEL-RUN-FIX (2026-09-12): wake the fast-train daemon too — a fresh
    # token unblocks the history top-up, so training retries NOW instead of
    # waiting out its 10-minute retry timer.
    try:
        from core.otc_predict import fast_train as _ft_wake
        _ft_wake.notify_token_pushed()
    except Exception as exc:
        print(f"[server] fast-train wake-up failed (non-fatal): {exc}")

    token_preview = _token_store.mask(token)
    print(f"[server] ✅ token updated at runtime: {token_preview} "
          f"(source={source}, format={norm_meta.get('input_format')}, "
          f"persisted={persist_ok})")

    return {"ok": True,
            "message": f"Token set ({token_preview}). Reconnecting to Quotex…",
            "preview": token_preview,
            "timestamp": time.time(),
            "normalized": norm_meta,
            "persisted": persist_ok,
            "persistent_storage": _token_store.is_persistent(),
            "persist_error": persist_err,
            "reconnect_wakeup": woke,
            "sim_mode_disabled_permanently": True,
            "next_step": "Watch /api/token-status — it flips to "
                         "connection_status='live_authorized' within ~10s."}

@app.post("/api/reconnect")
async def force_reconnect():
    """Force an immediate Quotex reconnect without changing the token."""
    try:
        # Reset feed state
        feed._connected = False
        feed._reconnect_attempts = 0
        feed._last_error = None
        feed._last_error_time = 0
        feed._token_update_pending = True

        # Close any stale client
        if hasattr(feed, '_client') and feed._client:
            try:
                await feed._client.close()
            except Exception:
                pass
            feed._client = None

        # Check if manager task is dead and restart it
        existing_mgr = getattr(feed, '_manager_task', None)
        if existing_mgr is None or existing_mgr.done():
            if feed._broadcast is not None:
                feed._manager_task = asyncio.create_task(feed.run(feed._broadcast))
                msg = "Manager task was dead — restarted + reconnect triggered"
            else:
                msg = "Cannot restart manager — no broadcast fn set"
        else:
            msg = "Manager task alive — reconnect triggered (will pick up within 60s)"

        return {"ok": True, "message": msg, "timestamp": time.time(), "next_step": "Wait 10-30 seconds, then check /api/debug"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/reconnect")
async def force_reconnect_get():
    """GET version of /api/reconnect for browser-friendly access."""
    return await force_reconnect()


# ── Auto-session: mint our own tokens instead of asking a human ─────────────
# core/qx_session.py replays the operator's stored Quotex browser cookies to
# mint a fresh SSID whenever the current one expires. These endpoints let the
# UI see how that is going and re-seed the cookies when they finally die —
# both without a redeploy. Cookie import is gated by the same PIN as token
# import: the cookies ARE the account session, so they are at least as
# sensitive as the token they produce.

@app.get("/api/session/status")
async def session_status():
    """How the automatic token refresh is doing. No secrets in the response."""
    try:
        from core import qx_session
        return qx_session.status()
    except Exception as exc:
        return {"enabled": False, "configured": False, "error": str(exc)}


@app.post("/api/session/cookies")
async def session_set_cookies(request: Request):
    """Import a fresh Quotex cookie blob (document.cookie or JSON export).

    USER REQ 2026-08-17: no PIN/admin key required — just `{"cookies": "..."}`.
    Also clears the login_blocked flag (a fresh cookie import means the
    operator re-logged in via browser, so password login is safe to try
    again if it ever needs to).
    """
    body = await _json_body(request)
    pin, admin_key = _request_secrets(body, request)
    allowed, reason = _token_store.authorize(pin=pin, admin_key=admin_key)
    if not allowed:
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": _auth_error(reason),
                                     "auth": _token_store.auth_state()})
    raw = body.get("cookies") or body.get("cookie") or ""
    if not raw.strip():
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "no cookies supplied"})
    from core import qx_session
    result = qx_session.import_cookies(raw, source=body.get("source") or "ui")
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)

    # An import is only meaningful if it can actually mint a token — prove it
    # right now rather than letting the operator find out at 3am when the
    # current token dies.
    token, detail = await qx_session.refresh_token(reason="cookie-import",
                                                   force=True)
    if not token:
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": f"cookies saved, but they did not produce a token: {detail}",
            "cookies": result.get("cookies"),
            "hint": "Log in to Quotex in your browser, then copy document.cookie "
                    "again — the remember_web cookie must be from a live session.",
        })
    await _apply_token(token, source="auto-session")
    return {"ok": True,
            "message": f"Cookies saved and verified — minted {_token_store.mask(token)}. "
                       f"The app will now refresh its own token from here on.",
            "cookies": result.get("cookies"),
            "detail": detail,
            "status": qx_session.status()}


@app.post("/api/session/refresh")
async def session_refresh(request: Request):
    """Force an immediate token refresh from the stored cookies.

    USER REQ 2026-08-17: no PIN/admin key required.
    """
    body = await _json_body(request)
    pin, admin_key = _request_secrets(body, request)
    allowed, reason = _token_store.authorize(pin=pin, admin_key=admin_key)
    if not allowed:
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": _auth_error(reason)})
    from core import qx_session
    if qx_session.is_login_blocked():
        blk = qx_session.status().get("login_block_detail") or {}
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "password login is permanently blocked after the first "
                     "failure. Re-import cookies from a fresh browser session "
                     "to clear the block.",
            "login_block_detail": blk,
        })
    if not qx_session.configured():
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "no session cookies stored — paste them in the "
                     "'Session cookies' box first"})
    token, detail = await qx_session.refresh_token(reason="manual", force=True)
    if not token:
        return JSONResponse(status_code=502,
                            content={"ok": False, "error": detail,
                                     "status": qx_session.status()})
    await _apply_token(token, source="auto-session")
    return {"ok": True, "message": f"Minted {_token_store.mask(token)} — reconnecting…",
            "detail": detail, "status": qx_session.status()}

@app.get("/api/pairs")
async def get_pairs():
    """Return both Real Market and OTC Market pair lists."""
    return feed.available_pairs()

@app.get("/api/pairs/{category}")
async def get_pairs_by_category(category: str):
    """Return only the pair list for the requested category."""
    cat = category.lower().strip()
    all_pairs = feed.available_pairs()
    if cat == "real":
        return {"category": "real", "pairs": all_pairs["real_pairs"], "payout_floor": all_pairs["payout_floor_real"]}
    if cat == "otc":
        return {"category": "otc", "pairs": all_pairs["otc_pairs"], "payout_floor": all_pairs["payout_floor_otc"]}
    raise HTTPException(
        status_code=404,
        detail=f"unknown category {category!r}; expected 'real' or 'otc'")

@app.get("/api/db-download")
async def download_db(request: Request):
    """Download the raw signals.db SQLite file.
    FIX (A-19 D-02 / S-1): added ADMIN_KEY check — was completely public,
    anyone could download the full DB (learned weights + signal history)."""
    _check_admin_key(request.headers.get("X-Admin-Key"))
    import os
    import shutil
    import sqlite3
    import tempfile
    import time as _time

    # PERSISTENCE-FIX (2026-09-09): use db.DB_PATH (the file the feed
    # actually writes) — never a private candidate list again.
    found_path = _db.DB_PATH if os.path.exists(_db.DB_PATH) else None
    if not found_path:
        raise HTTPException(
            status_code=404,
            detail=f"DB file not found. Checked: {candidates}")

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="signals_export_")
    os.close(tmp_fd)
    try:
        src = sqlite3.connect(found_path, timeout=5)
        dst = sqlite3.connect(tmp_path, timeout=5)
        src.backup(dst)
        dst.close()
        src.close()
    except Exception as exc:
        try:
            shutil.copy2(found_path, tmp_path)
        except Exception as copy_exc:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise HTTPException(
                status_code=500,
                detail=f"DB snapshot failed: backup={exc}, copy={copy_exc}")

    ts = _time.strftime("%Y%m%d_%H%M%S", _time.gmtime())
    filename = f"signals_{ts}.db"

    with open(tmp_path, "rb") as f:
        data = f.read()
    try:
        os.unlink(tmp_path)
    except Exception:
        pass

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Content-Length": str(len(data)), "X-DB-Path": found_path, "X-DB-Size": str(len(data))},
    )

@app.get("/api/db-export")
async def export_db_json():
    """Export all DB tables as a single JSON document."""
    import sqlite3
    import json as _json
    import time as _time
    import traceback

    try:
        # PERSISTENCE-FIX (2026-09-09): single source of truth = db.DB_PATH
        # (the file the feed actually writes to). The old private candidate
        # list served an EMPTY DB while 14k real records lived elsewhere.
        found_path = _db.DB_PATH if os.path.exists(_db.DB_PATH) else None
        if not found_path:
            raise HTTPException(
                status_code=404,
                detail=f"DB file not found at resolved path {_db.DB_PATH!r}")

        conn = sqlite3.connect(found_path, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]

        export = {"_export_meta": {
            "exported_at": _time.time(),
            "exported_at_utc": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            "db_path": found_path,
            "tables_count": len(tables),
            "tables": tables,
        }}

        for table in tables:
            try:
                cursor.execute(f"SELECT * FROM {table}")
                rows = [dict(r) for r in cursor.fetchall()]
                export[table] = rows
            except Exception as exc:
                export[table] = {"_error": f"failed to read table {table}: {exc}"}

        counts = {}
        for table in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = cursor.fetchone()[0]
            except Exception:
                counts[table] = -1
        export["_export_meta"]["row_counts"] = counts

        cursor.close()
        conn.close()

        ts = _time.strftime("%Y%m%d_%H%M%S", _time.gmtime())
        return Response(
            content=_json.dumps(export, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="signals_export_{ts}.json"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        # Return error as JSON so we can see what went wrong
        return Response(
            content=_json.dumps({
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }, indent=2),
            media_type="application/json",
            status_code=500,
        )

@app.get("/api/db-info")
async def db_info():
    """Return DB file size, table list, and row counts (no row data)."""
    import sqlite3
    import time as _time_mod
    import traceback

    # PERSISTENCE-FIX (2026-09-09): db.py resolves the REAL live DB path
    # (env > Railway Volume /app/data > repo-local). The old private
    # candidate list here could point at a DIFFERENT file than the one the
    # feed writes to (that is exactly how /api/db-export served an empty
    # DB while 14k real records lived elsewhere). Single source of truth.
    result = {"db_path_env": os.environ.get("DB_PATH"), "cwd": os.getcwd()}
    found_path = None
    try:
        if os.path.exists(_db.DB_PATH):
            found_path = _db.DB_PATH
    except Exception:
        found_path = None
    result["resolved_db_path"] = _db.DB_PATH

    # Diagnostics: what the resolver considered (kept for support parity).
    _legacy_local = os.path.abspath(os.path.join(os.path.dirname(_db.__file__)
                                                 or ".", "signals.db"))
    result["checked_paths"] = [
        {"path": _db.DB_PATH, "exists": os.path.exists(_db.DB_PATH),
         "size": (os.path.getsize(_db.DB_PATH)
                  if os.path.exists(_db.DB_PATH) else 0)},
        {"path": "/app/data/signals.db", "exists": os.path.exists("/app/data/signals.db"),
         "size": (os.path.getsize("/app/data/signals.db")
                  if os.path.exists("/app/data/signals.db") else 0)},
        {"path": _legacy_local, "exists": os.path.exists(_legacy_local),
         "size": (os.path.getsize(_legacy_local)
                  if os.path.exists(_legacy_local) else 0)},
    ]
    result["found_path"] = found_path

    if not found_path:
        result["error"] = "DB file not found in any candidate location"
        # List /app/data contents if it exists
        try:
            if os.path.exists("/app/data"):
                result["app_data_contents"] = os.listdir("/app/data")
            else:
                result["app_data_exists"] = False
        except Exception as e:
            result["app_data_list_error"] = str(e)
        return result

    try:
        file_size = os.path.getsize(found_path)
        mtime = os.path.getmtime(found_path)

        conn = sqlite3.connect(found_path, timeout=5)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]

        table_info = []
        for t in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {t}")
                count = cursor.fetchone()[0]
            except Exception as exc:
                count = -1
            table_info.append({"table": t, "rows": count})

        cursor.close()
        conn.close()

        result.update({
            "file_size_bytes": file_size,
            "file_size_kb": round(file_size / 1024, 1),
            "last_modified": _time_mod.strftime("%Y-%m-%dT%H:%M:%SZ", _time_mod.gmtime(mtime)),
            "tables": table_info,
            "total_rows": sum(t["rows"] for t in table_info if t["rows"] > 0),
            "download_endpoints": {"binary_db": "/api/db-download", "json_export": "/api/db-export"},
        })
    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()

    return result

@app.get("/api/status")
async def status():
    return {"connected": feed._connected, "streams": feed.stream_status()}

@app.get("/api/history/{asset}/{period}")
async def get_history(asset: str, period: int):
    snap = feed.snapshot(asset, period)
    if snap:
        return snap
    return {"candles": [], "prediction": None}

@app.get("/api/debug")
async def debug_info(request: Request):
    """Diagnostic endpoint — shows connection state, stream status, errors."""
    _check_admin_key(request.headers.get("X-Admin-Key"))
    debug = {
        "timestamp": time.time(),
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
            "SIGNAL_DELAY_SEC": os.environ.get("SIGNAL_DELAY_SEC", "0.0"),
        },
    }
    def _collect_streams(feed_obj):
        out = {}
        if hasattr(feed_obj, '_streams'):
            for key, s in feed_obj._streams.items():
                out[f"{key[0]}@{key[1]}s"] = {"candles_count": len(s.candles) if hasattr(s, 'candles') else 0, "ticks_count": len(s.ticks) if hasattr(s, 'ticks') else 0, "last_real_tick_wall": getattr(s, 'last_real_tick_wall', 0), "always_on": getattr(s, 'always_on', False), "interested_cids": list(getattr(s, 'interested_cids', set())), "sub_started": getattr(s, 'sub_started', False), "source": "real_feed"}
        return out

    debug["streams"] = _collect_streams(feed)
    # If sim delegate is active, also collect its streams
    sim = getattr(feed, '_sim_delegate', None)
    if sim is not None:
        sim_streams = _collect_streams(sim)
        for k, v in sim_streams.items():
            v["source"] = "sim_feed"
            # Don't overwrite real feed streams with same key
            if k not in debug["streams"]:
                debug["streams"][k] = v
        debug["sim_mode"] = True
        debug["sim_pairs_count"] = len(getattr(sim, '_pairs_list', []))
    else:
        debug["sim_mode"] = False
    # Recent errors (if tracked)
    if hasattr(feed, '_last_error'):
        debug["last_error"] = feed._last_error
    return debug

@app.get("/api/stats")
async def module_stats():
    """Per-module performance report from signal_log.

    HONESTY-FIX (2026-09-11): the payload now also carries the economics and
    calibration context every trader NEEDS but the old payload hid:
      • breakeven_wr / payout economics — a 50.x% win rate is NOT "almost
        profitable": at 85% payout you need 54.05% just to break even.
      • current + worst consecutive-wrong streaks (revenge-trading guard).
      • calibration_by_confidence — predicted vs actual win rate per
        confidence bucket (the old conf~70 → 50% actual gap, made visible).
      • coverage_note — every-candle mode means most signals are honest
        fallback coverage, not high-confidence trades.
    """
    def _compute_stats_with_adaptation():
        from core.stats import compute_module_stats
        stats = compute_module_stats(_db.DB_PATH)
        try:
            from engines.otc.config import weight_adapter as _otc_adapter
            from engines.real.config import weight_adapter as _real_adapter
            adaptation_status = {}
            seen = set()
            assets = []
            for a in (list(_otc_adapter.pair_configs.keys())
                      + list(_real_adapter.pair_configs.keys())):
                if a not in seen:
                    seen.add(a)
                    assets.append(a)
            for asset in assets:
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
            _logger.exception("adaptation status computation failed")
            stats["adaptation_error"] = "internal error"
        # ── HONESTY-FIX fields (2026-09-11) ─────────────────────────────
        try:
            from feed import _payout_floor_for as _pfloor
            payouts = {}
            for cat, floor in (("otc", _pfloor("EURUSD_otc")),
                               ("real", _pfloor("EURUSD"))):
                payouts[cat] = {
                    "typical_payout_pct": floor,
                    "breakeven_win_pct": round(10000.0 / (100 + floor), 2),
                }
            stats["payout_economics"] = payouts
        except Exception as _pe:
            stats["payout_economics_error"] = str(_pe)
        try:
            stats["streaks"] = _db.consecutive_wrong_streak(period=60)
        except Exception as _se:
            stats["streaks_error"] = str(_se)
        try:
            stats["calibration_by_confidence"] = _db.calibration_by_confidence(
                period=60)
        except Exception as _ce:
            stats["calibration_error"] = str(_ce)
        stats["coverage_note"] = (
            "every-candle mode: most signals are FALLBACK coverage signals "
            "(strict high-confidence gates reject ~98% of candles). Treat "
            "fallback signals as information, not trade advice; check "
            "/api/psychology for the discipline rules.")
        return stats

    return await asyncio.to_thread(_compute_stats_with_adaptation)

@app.get("/api/brain")
async def brain_summary():
    """Brain summary — learning status, accuracy, insight count."""
    from core.brain import get_brain_summary
    return get_brain_summary()

@app.get("/api/brain/insights")
async def brain_insights(limit: int = 50):
    """Get auto-generated insights and recommendations."""
    safe_limit = max(1, min(int(limit), 500))
    from core.brain import get_insights
    return {"insights": get_insights(active_only=True, limit=safe_limit)}

@app.get("/api/brain/learning")
async def brain_learning(asset: Optional[str] = None, limit: int = 100):
    """Get learned weights per pair per module."""
    safe_limit = max(1, min(int(limit), 500))
    from core.brain import get_learning
    return {"learning": get_learning(asset=asset, limit=safe_limit)}

@app.get("/api/brain/analyze")
async def brain_analyze(request: Request):
    """Trigger brain analysis manually."""
    _check_admin_key(request.headers.get("X-Admin-Key"))
    from core.brain import analyze_and_learn
    async with _BRAIN_ANALYZE_LOCK:
        await asyncio.to_thread(analyze_and_learn)
    return {"status": "analysis complete"}

def _ensure_patterns_init():
    """Lazily init patterns once at module level."""
    global _PATTERNS_INITIALIZED
    if _PATTERNS_INITIALIZED:
        return
    try:
        from core.time_patterns import init_patterns
        init_patterns()
        _PATTERNS_INITIALIZED = True
    except Exception as _e:
        print(f"[server] lazy pattern init failed (non-fatal): {_e}")

@app.get("/api/patterns")
async def patterns_summary():
    """Summary of all stored time/session/regime patterns per pair."""
    _ensure_patterns_init()
    from core.time_patterns import get_pattern_summary
    return {"patterns": get_pattern_summary()}

@app.get("/api/patterns/{asset}")
async def patterns_for_asset(asset: str):
    """Full pattern detail for one asset (hour, session, dow, regime, tag)."""
    _ensure_patterns_init()
    from core.time_patterns import get_asset_patterns_detail
    return {"asset": asset, "patterns": get_asset_patterns_detail(asset)}

@app.get("/api/module-analysis")
async def module_analysis(min_samples: int = 30):
    """Deep per-module per-pair per-direction analysis.

    STRAT-FIX 2026-09-09: the queries read the EMPTY `module_votes` table
    (always 0 rows on production), so this endpoint returned blank lists
    forever. Repointed to `brain_module_votes` — the REAL ledger (79k+
    graded votes on production). Also added worst-first views + explicit
    loss counts ("একই স্ট্র্যাটেজি দিয়ে কত গুলো সিগন্যাল লস হয়েছে").
    """
    def _decorate(rows):
        """Attach Wilson bounds + reliability, then sort by the lower bound."""
        out = []
        for r in rows:
            d = dict(r)
            total = d.get("total") or 0
            lo, hi = _wilson_bounds(d.get("correct") or 0, total)
            d["wilson_lo"] = lo
            d["wilson_hi"] = hi
            d["reliable"] = total >= min_samples and (lo > 50.0 or hi < 50.0)
            d["profitable"] = total >= min_samples and lo > 54.05
            out.append(d)
        out.sort(key=lambda x: x["wilson_lo"], reverse=True)
        return out

    # Rolling 30-day window: the ledger accumulates forever; stale votes
    # from removed strategies would pollute the current analysis.
    lookback_ts = time.time() - 30 * 86400

    try:
        with _db._read_cursor() as cur:
            # Global per-module accuracy (real ledger: brain_module_votes)
            cur.execute("""
                SELECT module_name,
                       SUM(module_correct) as correct,
                       COUNT(module_correct) as total,
                       COUNT(module_correct) - SUM(module_correct) as wrong,
                       ROUND(100.0 * SUM(module_correct) / NULLIF(COUNT(module_correct), 0), 1) as win_pct
                FROM brain_module_votes
                WHERE module_correct IS NOT NULL AND ts >= ?
                GROUP BY module_name
            """, (lookback_ts,))
            global_modules = _decorate(cur.fetchall())

            # Per-pair per-module accuracy
            cur.execute("""
                SELECT asset, module_name,
                       SUM(module_correct) as correct,
                       COUNT(module_correct) as total,
                       COUNT(module_correct) - SUM(module_correct) as wrong,
                       ROUND(100.0 * SUM(module_correct) / NULLIF(COUNT(module_correct), 0), 1) as win_pct
                FROM brain_module_votes
                WHERE module_correct IS NOT NULL AND ts >= ?
                GROUP BY asset, module_name
                HAVING total >= ?
            """, (lookback_ts, min_samples))
            pair_modules = _decorate(cur.fetchall())

            # Per-pair per-module per-direction
            cur.execute("""
                SELECT asset, module_name, direction,
                       SUM(module_correct) as correct,
                       COUNT(module_correct) as total,
                       COUNT(module_correct) - SUM(module_correct) as wrong,
                       ROUND(100.0 * SUM(module_correct) / NULLIF(COUNT(module_correct), 0), 1) as win_pct
                FROM brain_module_votes
                WHERE module_correct IS NOT NULL AND ts >= ?
                GROUP BY asset, module_name, direction
                HAVING total >= 2
                ORDER BY asset, module_name, direction
            """, (lookback_ts,))
            pair_module_dirs = [dict(r) for r in cur.fetchall()]

        worst_modules = sorted(global_modules, key=lambda x: x["wilson_lo"])
        worst_pairs = sorted(pair_modules, key=lambda x: x["wilson_lo"])

        return {"global_modules": global_modules, "pair_modules": pair_modules, "pair_module_directions": pair_module_dirs,
                "worst_modules": worst_modules, "worst_pairs": worst_pairs,
                "pair_summary": [{"asset": a, "vote_count": sum(1 for p in pair_modules if p["asset"] == a)}
                                 for a in sorted({p["asset"] for p in pair_modules})],
                "total_vote_records": sum(m['total'] for m in global_modules), "min_samples": min_samples,
                "lookback_days": 30,
                # FIX (BREAKEVEN-CONST-2026-09-07): was hardcoded 51.8 (=93%
                # payout). The app's canonical payout is 85% → breakeven is
                # 100/185 = 54.05% (matches core/breakeven.py and the UI).
                "breakeven_pct": round(100.0 * 100.0 / (100.0 + 85), 2)}
    except Exception as e:
        _logger.exception("module analysis failed")
        return {"error": str(e), "hint": "brain_module_votes table missing or unreadable"}

def _wilson_bounds(correct: int, total: int, z: float = 1.96):
    """95% Wilson score interval for a win rate, returned as (lo, hi) percent."""
    if total <= 0:
        return (0.0, 0.0)
    p = correct / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (round(100 * (centre - margin) / denom, 1),
            round(100 * (centre + margin) / denom, 1))

@app.get("/api/theory-analysis")
async def theory_analysis(period: int = 60, min_samples: int = 30):
    """Per-theory win-rate analysis from the theory_votes table."""
    try:
        conn = _db._conn()
        try:
            # Global per-theory accuracy
            global_rows = conn.execute("""
                SELECT module_name, theory_name, theory_group,
                       SUM(vote_correct) as correct, COUNT(*) as total
                FROM theory_votes
                WHERE vote_correct IS NOT NULL AND period=?
                GROUP BY module_name, theory_name, theory_group
                HAVING total >= ?
                ORDER BY 100.0 * SUM(vote_correct) / COUNT(*) DESC
            """, (period, min_samples)).fetchall()

            global_theories = []
            for r in global_rows:
                win_pct = round(100.0 * r["correct"] / r["total"], 1) if r["total"] else 0
                lo, hi = _wilson_bounds(r["correct"] or 0, r["total"] or 0)
                global_theories.append({
                    "module_name": r["module_name"],
                    "theory_name": r["theory_name"],
                    "theory_group": r["theory_group"],
                    "correct": r["correct"],
                    "total": r["total"],
                    "win_pct": win_pct,
                    "wilson_lo": lo,
                    "wilson_hi": hi,
                    # Distinguishable from a coin flip at 95% confidence.
                    "reliable": lo > 50.0 or hi < 50.0,
                })
            global_theories.sort(key=lambda t: t["wilson_lo"], reverse=True)

            # Per-pair per-theory accuracy
            pair_rows = conn.execute("""
                SELECT asset, module_name, theory_name, theory_group,
                       SUM(vote_correct) as correct, COUNT(*) as total
                FROM theory_votes
                WHERE vote_correct IS NOT NULL AND period=?
                GROUP BY asset, module_name, theory_name, theory_group
                HAVING total >= ?
                ORDER BY asset, 100.0 * SUM(vote_correct) / COUNT(*) DESC
            """, (period, min_samples)).fetchall()

            pair_theories = []
            for r in pair_rows:
                win_pct = round(100.0 * r["correct"] / r["total"], 1) if r["total"] else 0
                lo, hi = _wilson_bounds(r["correct"] or 0, r["total"] or 0)
                pair_theories.append({
                    "asset": r["asset"],
                    "module_name": r["module_name"],
                    "theory_name": r["theory_name"],
                    "theory_group": r["theory_group"],
                    "correct": r["correct"],
                    "total": r["total"],
                    "win_pct": win_pct,
                    "wilson_lo": lo,
                    "wilson_hi": hi,
                    "reliable": lo > 50.0 or hi < 50.0,
                })

            total = conn.execute("SELECT COUNT(*) as n FROM theory_votes WHERE period=?", (period,)).fetchone()

            return {"global_theories": global_theories, "pair_theories": pair_theories, "total_theory_records": total["n"] if total else 0}
        finally:
            conn.close()
    except Exception as e:
        _logger.exception("theory analysis failed")
        return {"error": str(e), "hint": "theory_votes table may not exist yet — run db.init()"}

MIN_SAMPLES_FOR_QUALITY_DECISION = 150

@app.get("/api/quality-analysis")
async def quality_analysis(period: int = 60, hours: int = 0):
    """Win-rate broken down by signal_quality tier (HIGH/MEDIUM/LOW/NONE)."""
    try:
        conn = _db._conn()
        try:
            where = "WHERE period=? AND accuracy IN ('correct','wrong')"
            params = [period]
            if hours > 0:
                where += " AND ctime >= ?"
                params.append(int(time.time()) - hours * 3600)

            rows = conn.execute(f"""
                SELECT COALESCE(signal_quality, 'UNLABELED') as tier,
                       SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) as correct,
                       SUM(CASE WHEN accuracy='wrong' THEN 1 ELSE 0 END) as wrong,
                       COUNT(*) as total
                FROM signal_log
                {where}
                GROUP BY tier
                ORDER BY CASE tier
                    WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2
                    WHEN 'LOW' THEN 3 ELSE 4 END
            """, params).fetchall()

            tiers = []
            for r in rows:
                total = r["total"] or 0
                win_pct = round(100.0 * r["correct"] / total, 1) if total else 0
                tiers.append({
                    "signal_quality": r["tier"],
                    "correct": r["correct"],
                    "wrong": r["wrong"],
                    "total": total,
                    "win_pct": win_pct,
                    "reliable": total >= MIN_SAMPLES_FOR_QUALITY_DECISION,
                })

            return {"tiers": tiers, "min_samples_required": MIN_SAMPLES_FOR_QUALITY_DECISION}
        finally:
            conn.close()
    except Exception as e:
        _logger.exception("quality analysis failed")
        return {"error": str(e), "hint": "signal_quality column may not exist yet — run db.init()"}

@app.get("/api/winrate")
async def api_winrate(period: int = 60, days: Optional[int] = None,
                      category: Optional[str] = None):
    """Per-pair, per-direction (CALL vs PUT) win rates.

    FIX (WINRATE-API-2026-08-31): the app had no endpoint that answered
    "which pair's CALL vs PUT signals are actually winning?". This endpoint
    returns exact final-signal win rates from signal_log for every pair —
    powering the new frontend Win Rate dashboard.

    Query params:
        period   : candle period seconds (default 60)
        days     : lookback window — 1=today-ish, 7, 30; omit for all-time
        category : 'otc' | 'real' (optional filter)

    Response: { ok, period, window_days, overall:{...}, pairs:[{...}] }
    Each row: asset, category, graded, win_pct, call{total,correct,win_pct},
              put{total,correct,win_pct}, last_signal, last_accuracy,
              streak_type, streak_count, last_ctime
    """
    try:
        data = _db.get_directional_winrate(
            period=period, days=days,
            category=category if category in ('otc', 'real') else None,
        )
        return {"ok": True, **data}
    except Exception as e:
        _logger.exception("winrate endpoint failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.get("/api/target-gate")
async def api_target_gate():
    """TARGET-75 controller transparency (2026-09-09).

    Shows, for every (pair, direction): the current confidence bar, the
    rolling win rate the controller sees, and when the bar last moved.
    A pair whose rolling WR < 75% gets a higher bar (fewer but better
    signals); WAIT candles are never graded so they can't drag the WR down.
    """
    try:
        from core import target_gate as _tg
        return {
            "ok": True,
            "target_wr": _tg.TARGET_WR,
            "gate_init": _tg.GATE_INIT,
            "gate_floor": _tg.GATE_FLOOR,
            "gate_cap": _tg.GATE_CAP,
            "rolling_n": _tg.ROLLING_N,
            "gates": _tg.gate_report(),
        }
    except Exception as e:
        _logger.exception("target-gate endpoint failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.get("/api/psychology")
async def api_psychology(period: int = 60, days: int = 7):
    """PSYCHOLOGY-FIX (2026-09-11) — binary-trading discipline, data-driven.

    The live audit (7,098 graded signals, 50.23% WR) proved the predictions
    themselves carry almost no edge in every-candle mode — which makes the
    TRADER'S behaviour the dominant PnL variable. This endpoint computes the
    discipline rules FROM THE APP'S OWN LEDGER instead of generic advice:

      1. BREAKEVEN MATH     — actual WR vs the WR each payout requires
      2. STREAK GUARD       — live + worst loss streak, stop-after-N rule
      3. PAIR SELECTIVITY   — only pairs whose Wilson-LB win rate beats the
                              payout breakeven are recommended (with CALL/PUT
                              split so the trader takes the proven side)
      4. HOUR AWARENESS     — measured best/worst UTC hours for this ledger
      5. STAKE / MATH RULES — fixed fractional staking, no martingale
      6. PSYCHOLOGY RULES   — the five failure modes binary traders die of,
                              each tied to a live number from this payload
    """
    def _compute():
        out = {"ok": True, "period": period, "window_days": days}
        # 1. economics
        try:
            from feed import _payout_floor_for as _pfloor
            be = {}
            for cat, probe in (("otc", "EURUSD_otc"), ("real", "EURUSD")):
                floor = _pfloor(probe)
                be[cat] = {"typical_payout_pct": floor,
                           "breakeven_win_pct": round(10000.0 / (100 + floor), 2)}
            out["economics"] = be
        except Exception as _e:
            out["economics_error"] = str(_e)
        # 2. streaks
        try:
            out["streaks"] = _db.consecutive_wrong_streak(period=period,
                                                          window_days=days)
        except Exception as _e:
            out["streaks_error"] = str(_e)
        # 3. pair selectivity (Wilson-gated)
        try:
            import math as _math

            def _wlb(k, n, z=1.96):
                if n <= 0:
                    return 0.0
                p = k / n
                d = 1 + z * z / n
                c = p + z * z / (2 * n)
                m = z * _math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
                return max(0.0, min(1.0, (c - m) / d))

            data = _db.get_directional_winrate(period=period, days=days,
                                               category=None)
            breakeven = 0.5405  # 85% payout floor default
            try:
                breakeven = out.get("economics", {}).get(
                    "otc", {}).get("breakeven_win_pct", 54.05) / 100.0
            except Exception:
                pass
            rec, avoid = [], []
            for p in (data or {}).get("pairs", []):
                n = p.get("graded") or 0
                wr = p.get("win_pct")
                if n < 50 or wr is None:
                    continue
                wlb = _wlb(round(wr / 100.0 * n), n)
                row = {
                    "asset": p.get("asset"),
                    "graded": n,
                    "win_pct": wr,
                    "wilson_lb_pct": round(wlb * 100, 2),
                    "call_win_pct": (p.get("call") or {}).get("win_pct"),
                    "put_win_pct": (p.get("put") or {}).get("win_pct"),
                }
                if wlb >= breakeven:
                    rec.append(row)
                elif wlb < 0.45 and n >= 100:
                    avoid.append(row)
            out["recommended_pairs"] = sorted(
                rec, key=lambda r: -(r["wilson_lb_pct"]))[:10]
            out["avoid_pairs"] = sorted(
                avoid, key=lambda r: r["wilson_lb_pct"])[:10]
            out["selectivity_rule"] = (
                f"Only trade pairs whose Wilson lower-bound win rate beats the "
                f"payout breakeven ({breakeven*100:.2f}% at the floor payout). "
                f"Everything else is entertainment, not edge.")
        except Exception as _e:
            out["selectivity_error"] = str(_e)
        # 4. hour awareness (measured from the ledger)
        try:
            rows_by_hour = {}
            with _db._read_cursor() as c:
                cutoff = time.time() - days * 86400
                rows = c.execute(
                    """SELECT CAST(strftime('%H', ctime, 'unixepoch') AS INT) h,
                              accuracy FROM signal_log
                       WHERE period=? AND signal IN ('CALL','PUT')
                         AND accuracy IN ('correct','wrong') AND ctime >= ?""",
                    (period, cutoff)).fetchall()
            for r in rows:
                rec = rows_by_hour.setdefault(r["h"], [0, 0])
                rec[0] += 1
                rec[1] += (r["accuracy"] == "correct")
            hours = [{"utc_hour": h, "n": n, "win_pct": round(100 * w / n, 2)}
                     for h, (n, w) in rows_by_hour.items() if n >= 60]
            hours.sort(key=lambda x: -x["win_pct"])
            out["best_hours"] = hours[:3]
            out["worst_hours"] = sorted(hours, key=lambda x: x["win_pct"])[:3]
            out["hour_note"] = (
                "UTC ঘণ্টা অনুযায়ী মাপা উইন রেট — খারাপ ঘণ্টায় সিগন্যাল দেখলেও "
                "ট্রেড না নেওয়াই ডিসিপ্লিন। (বাংলাদেশ সময় = UTC+6)")
        except Exception as _e:
            out["hours_error"] = str(_e)
        # 5-6. fixed rules (universal binary-trading math + psychology)
        out["stake_rules"] = {
            "fixed_stake_pct": "1–2% of balance per trade, never more",
            "martingale": "কখনোই না — লসের পর স্টেক বাড়ালে ৫ লসের স্ট্রিকেই "
                          "অ্যাকাউন্ট শূন্য হয় (মাপা সর্বোচ্চ স্ট্রিক দেখুন উপরে)",
            "stop_after_consecutive_losses": 3,
            "daily_loss_limit_pct": 10,
            "note": "binary-তে প্রতি ট্রেড সম্পূর্ণ স্টেকের ঝুঁকি — ফরেক্সের "
                    "স্টপ-লস নেই, তাই পজিশন সাইজিংই একমাত্র রক্ষা",
        }
        out["psychology_rules"] = [
            {"title": "প্রতিশোধের ট্রেড (Revenge Trading)",
             "detail": ("লসের পরপর বড় স্টেক = মাপা সর্বোচ্চ স্ট্রিক "
                        f"{out.get('streaks', {}).get('max', '?')} লস। ৩ লসের পর "
                        "৩০ মিনিট বিরতি নিন।")},
            {"title": "ওভারট্রেডিং (Every Candle Trading)",
             "detail": ("প্রতিটি ১-মিনিট ক্যান্ডেলে সিগন্যাল আসে (কভারেজ ১০০%) "
                        "কিন্তু ৯৮% ফলব্যাক — সব সিগন্যাল ট্রেড করা মানে কয়েন-"
                        "ফ্লিপ খেলা। শুধু recommended_pairs + ভালো ঘণ্টায় ট্রেড।")},
            {"title": "কনফিডেন্স ফাঁদ (Fake Confidence)",
             "detail": ("কনফিডেন্স ৭০% দেখালেও আসল উইন রেট দেখুন calibration টেবিলে "
                        "(/api/stats) — এখন থেকে ফলব্যাক কনফিডেন্স মাপা এজ "
                        "থেকে হিসাব হয়।")},
            {"title": "মার্টিঙ্গেল (Doubling Down)",
             "detail": ("৮৫% পেআউটে ৫৪.০৫% উইন রেট লাগবে ব্রেকইভেনের জন্য — "
                        "ডাবল-ডাউন এই গ্যাপ বন্ধ করে না, শুধু ধ্বংস ত্বরান্বিত করে।")},
            {"title": "টিল্ট ও ক্লান্তি (Tilt / Fatigue)",
             "detail": ("দিনে ১০% লস লিমিট — লিমিট ছুঁলে অ্যাপ বন্ধ করুন। "
                        "রাত ৩টায় ট্রেড করা মানে সিদ্ধান্তহীনতায় টাকা দাগানো।")},
        ]
        out["honest_summary"] = (
            "সত্য কথা: প্রতি ক্যান্ডেল সিগন্যাল দিতে হলে ১-মিনিটের বাইনারি "
            "মার্কেটে ৫০%±এজ অনিবার্য। লাভজনক হওয়ার একমাত্র পথ: সিলেক্টিভিটি "
            "(Wilson-verified পেয়ার+দিক+ঘণ্টা), পেআউট ≥৮৫%, আর ডিসিপ্লিন।")
        return out
    return await asyncio.to_thread(_compute)

@app.get("/api/pair-deep-stats/{asset}")
async def pair_deep_stats(asset: str, period: int = 60):
    """Deep statistics for a specific pair — all the data needed for calibration."""
    try:
        with _db._read_cursor() as cur:
            # Overall
            cur.execute("""
                SELECT accuracy, COUNT(*) as count
                FROM signal_log
                WHERE asset = ? AND period = ? AND accuracy IN ('correct', 'wrong')
                GROUP BY accuracy
            """, (asset, period))
            overall = {r['accuracy']: r['count'] for r in cur.fetchall()}

            # Per-module from module_votes
            cur.execute("""
                SELECT module_name, direction,
                       SUM(vote_correct) as correct,
                       COUNT(vote_correct) as total,
                       ROUND(100.0 * SUM(vote_correct) / NULLIF(COUNT(vote_correct), 0), 1) as win_pct
                FROM module_votes
                WHERE asset = ? AND vote_correct IS NOT NULL
                GROUP BY module_name, direction
                ORDER BY module_name, direction
            """, (asset,))
            module_votes = [dict(r) for r in cur.fetchall()]

            # Regime distribution
            cur.execute("""
                SELECT regime, accuracy, COUNT(*) as count
                FROM signal_log
                WHERE asset = ? AND period = ? AND accuracy IN ('correct', 'wrong')
                GROUP BY regime, accuracy
            """, (asset, period))
            regime_data = [dict(r) for r in cur.fetchall()]

            # Tag distribution
            cur.execute("""
                SELECT tags, accuracy, COUNT(*) as count
                FROM signal_log
                WHERE asset = ? AND period = ? AND accuracy IN ('correct', 'wrong')
                  AND tags IS NOT NULL AND tags != ''
                GROUP BY tags, accuracy
            """, (asset, period))
            tag_data = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT
                    CASE
                        WHEN confidence < 46 THEN '<46'
                        WHEN confidence < 49 THEN '46-48'
                        WHEN confidence < 51 THEN '49-50'
                        WHEN confidence < 53 THEN '51-52'
                        WHEN confidence < 57 THEN '53-56'
                        ELSE '57+'
                    END as conf_bucket,
                    accuracy,
                    COUNT(*) as count
                FROM signal_log
                WHERE asset = ? AND period = ? AND accuracy IN ('correct', 'wrong')
                GROUP BY conf_bucket, accuracy
            """, (asset, period))
            # FIX (CONFLUENCE-V1 2026-09-02): the old `ORDER BY conf_bucket`
            # sorted LEXICOGRAPHICALLY in SQLite ('<46' after '57+') — the
            # buckets displayed out of order. Sort in Python with an explicit
            # ordinal so the calibration table reads low → high.
            _conf_order = {'<46': 0, '46-48': 1, '49-50': 2, '51-52': 3, '53-56': 4, '57+': 5}
            conf_data = sorted(
                (dict(r) for r in cur.fetchall()),
                key=lambda r: _conf_order.get(r['conf_bucket'], 99))

            # Strength distribution
            cur.execute("""
                SELECT strength, accuracy, COUNT(*) as count
                FROM signal_log
                WHERE asset = ? AND period = ? AND accuracy IN ('correct', 'wrong')
                GROUP BY strength, accuracy
            """, (asset, period))
            strength_data = [dict(r) for r in cur.fetchall()]

        # Time-of-day patterns from pair_hourly_patterns
        hourly_patterns = _db.get_hourly_pattern(asset)

        correct = overall.get('correct', 0)
        wrong = overall.get('wrong', 0)
        total = correct + wrong
        win_pct = round(100.0 * correct / total, 1) if total > 0 else 0

        return {"asset": asset, "period": period, "total_signals": total, "correct": correct, "wrong": wrong, "win_pct": win_pct, "module_votes": module_votes, "regime_distribution": regime_data, "tag_distribution": tag_data, "confidence_distribution": conf_data, "strength_distribution": strength_data, "hourly_patterns": hourly_patterns}
    except Exception as e:
        _logger.exception("pair deep stats failed")
        return {"error": str(e)}

@app.get("/api/time-patterns")
async def time_patterns_all():
    """Time-of-day patterns for ALL pairs — best/worst hours per pair."""
    try:
        with _db._read_cursor() as cur:
            cur.execute("""
                SELECT asset, hour_utc, session, total_signals, correct,
                       wrong, win_pct, best_direction, call_win_pct, put_win_pct
                FROM pair_hourly_patterns
                WHERE total_signals >= 3
                ORDER BY asset, hour_utc
            """)
            rows = [dict(r) for r in cur.fetchall()]

        # Group by pair
        pair_data = {}
        for row in rows:
            pair = row['asset']
            if pair not in pair_data:
                pair_data[pair] = []
            pair_data[pair].append(row)

        # Build summary
        summary = []
        for pair, hours in sorted(pair_data.items()):
            # Filter hours with >= 5 samples for best/worst
            significant_hours = [h for h in hours if h['total_signals'] >= 5]
            if not significant_hours:
                significant_hours = hours  # fallback to all

            best = max(significant_hours, key=lambda x: x['win_pct'])
            worst = min(significant_hours, key=lambda x: x['win_pct'])
            spread = best['win_pct'] - worst['win_pct']

            # Overall pair win rate
            total_correct = sum(h['correct'] for h in hours)
            total_total = sum(h['total_signals'] for h in hours)
            overall_wr = round(100.0 * total_correct / total_total, 1) if total_total > 0 else 0

            # Recommended hours (win >= 55%)
            good_hours = sorted(
                [h for h in hours if h['win_pct'] >= 55 and h['total_signals'] >= 3],
                key=lambda x: -x['win_pct']
            )
            # Avoid hours (win < 40%)
            bad_hours = sorted(
                [h for h in hours if h['win_pct'] < 40 and h['total_signals'] >= 3],
                key=lambda x: x['win_pct']
            )

            volatility = '⚡ extreme' if spread >= 50 else ('🔄 high' if spread >= 30 else 'low')

            summary.append({
                'pair': pair,
                'overall_win_pct': overall_wr,
                'total_signals': total_total,
                'best_hour': {"hour": best['hour_utc'], "win_pct": best['win_pct'], "samples": best['total_signals'], "session": best['session']},
                'worst_hour': {"hour": worst['hour_utc'], "win_pct": worst['win_pct'], "samples": worst['total_signals'], "session": worst['session']},
                'spread': round(spread, 1),
                'volatility': volatility,
                'recommended_hours': [
                    {'hour': h['hour_utc'], 'win_pct': h['win_pct'], 'samples': h['total_signals']}
                    for h in good_hours
                ],
                'avoid_hours': [
                    {'hour': h['hour_utc'], 'win_pct': h['win_pct'], 'samples': h['total_signals']}
                    for h in bad_hours
                ],
                'all_hours': hours,
            })

        return {"total_pairs": len(summary), "pairs": summary}
    except Exception as e:
        _logger.exception("time patterns failed")
        return {"error": str(e)}

@app.get("/api/time-patterns/{asset}")
async def time_patterns_for_pair(asset: str):
    """Time-of-day patterns for a specific pair — all 24 hours."""
    try:
        patterns = _db.get_hourly_pattern(asset)
        if patterns is None:
            patterns = []
        # Current hour adjustment
        from datetime import datetime, timezone
        current_hour = datetime.now(tz=timezone.utc).hour
        adjustment = _db.get_time_confidence_adjustment(asset, current_hour)
        return {"asset": asset, "current_hour_utc": current_hour, "current_adjustment": adjustment, "hourly_patterns": patterns}
    except Exception as e:
        _logger.exception("time patterns for pair failed")
        return {"error": str(e)}

@app.get("/api/quotex-algo-detect")
async def quotex_algo_detect():
    """Detect Quotex algorithm patterns from time-based data."""
    try:
        with _db._read_cursor() as cur:
            # Get all hourly patterns with enough data
            cur.execute("""
                SELECT asset, hour_utc, session, total_signals, correct,
                       wrong, win_pct, best_direction, call_win_pct, put_win_pct
                FROM pair_hourly_patterns
                WHERE total_signals >= 5
                ORDER BY win_pct ASC
            """)
            all_patterns = [dict(r) for r in cur.fetchall()]

        trap_hours = []
        boost_hours = []
        direction_bias = []

        for p in all_patterns:
            # Trap hour: win < 35%
            if p['win_pct'] < 35:
                trap_hours.append({
                    'pair': p['asset'],
                    'hour': p['hour_utc'],
                    'session': p['session'],
                    'win_pct': p['win_pct'],
                    'samples': p['total_signals'],
                    'severity': 'critical' if p['win_pct'] < 25 else 'warning',
                    'description': f"{p['asset']} loses {100-p['win_pct']:.0f}% of trades at {p['hour_utc']:02d}:00 UTC ({p['session']} session)",
                })

            # Boost hour: win > 65%
            if p['win_pct'] > 65:
                boost_hours.append({
                    'pair': p['asset'],
                    'hour': p['hour_utc'],
                    'session': p['session'],
                    'win_pct': p['win_pct'],
                    'samples': p['total_signals'],
                    'description': f"{p['asset']} wins {p['win_pct']:.0f}% of trades at {p['hour_utc']:02d}:00 UTC ({p['session']} session)",
                })

            # Direction bias: call/put win rate difference > 20%
            call_wr = p.get('call_win_pct')
            put_wr = p.get('put_win_pct')
            if call_wr is not None and put_wr is not None:
                diff = abs(call_wr - put_wr)
                if diff > 20 and p['total_signals'] >= 8:
                    favored = 'CALL' if call_wr > put_wr else 'PUT'
                    direction_bias.append({
                        'pair': p['asset'],
                        'hour': p['hour_utc'],
                        'session': p['session'],
                        'favored_direction': favored,
                        'call_win_pct': round(call_wr, 1),
                        'put_win_pct': round(put_wr, 1),
                        'difference': round(diff, 1),
                        'samples': p['total_signals'],
                        'description': f"{p['asset']} at {p['hour_utc']:02d}:00 UTC favors {favored} (CALL {call_wr:.0f}% vs PUT {put_wr:.0f}%)",
                    })

        return {"total_patterns_analyzed": len(all_patterns), "trap_hours": sorted(trap_hours, key=lambda x: x['win_pct']), "boost_hours": sorted(boost_hours, key=lambda x: -x['win_pct']), "direction_bias": sorted(direction_bias, key=lambda x: -x['difference']), "summary": {"trap_hours_count": len(trap_hours), "boost_hours_count": len(boost_hours), "direction_bias_count": len(direction_bias), "insight": "Quotex algorithm appears to manipulate specific pairs at specific hours. Avoid trap hours, trade during boost hours."}}
    except Exception as e:
        _logger.exception("quotex algo detect failed")
        return {"error": str(e)}

@app.get("/api/streaming-status")
async def streaming_status():
    """Streaming architecture status — shows tier1/tier2/tier3 breakdown."""
    import time as _time
    now = _time.time()
    streams = getattr(feed, '_streams', {})
    always_on = [(k, s) for k, s in streams.items() if getattr(s, 'always_on', False)]
    on_demand = [(k, s) for k, s in streams.items() if not getattr(s, 'always_on', False)]

    # Check tick recency
    recent_ticks = 0
    stale_streams = 0
    for k, s in streams.items():
        last_tick = getattr(s, 'last_real_tick_wall', 0)
        if last_tick > 0 and (now - last_tick) < 60:
            recent_ticks += 1
        elif last_tick > 0:
            stale_streams += 1

    return {
        "total_streams": len(streams),
        "always_on_streams": len(always_on),
        "on_demand_streams": len(on_demand),
        "streams_with_recent_ticks": recent_ticks,
        "stale_streams": stale_streams,
        # Read the value feed.py actually enforces. This used to re-read the
        # env var with its own default of "15" while feed.py defaulted to
        # "30", so the dashboard reported a cap that was never in force.
        "max_always_on": getattr(_feed_mod, "MAX_ALWAYS_ON_STREAMS", None),
        "always_on_over_cap": max(
            0, len(always_on) - (getattr(_feed_mod, "MAX_ALWAYS_ON_STREAMS", 0) or 0)),
        "streams_never_ticked": sum(
            1 for _k, s in streams.items()
            if not getattr(s, 'last_real_tick_wall', 0)),
        "total_configured_pairs": len(getattr(feed, '_pairs_list', [])),
        "quotex_limits": {"concurrent_subscriptions": "~15 (silent drops above this)", "tick_rate_per_pair": "~5-10/sec", "ping_interval": "25s", "anti_abuse_threshold": "76 attempts/20min → ban"},
        "recommendation": (
            "All pairs CANNOT stream simultaneously — Quotex rate-limits. " "Use smart streaming (3-tier rotation) for smooth appearance. " "See /api/streaming-status for details."
        ),
        "smart_streaming_available": True,
        "note": "core/smart_streaming.py implemented but not yet wired into feed.py",
    }


def _active_analyzer() -> str:
    """Which signal analyzer feed.py is actually running.

    MUST mirror the `_analyzer` resolution in feed.py._run_eoc: priority is
    AGENT_BRAIN > REALTIME > VERIFIER, and Agent Brain is also the default
    when none of the three env vars is set.
    """
    use_agent = (os.environ.get("QX_AGENT_BRAIN", "0") == "1"
                 or os.environ.get("QX_AGENT_FINAL", "0") == "1")
    use_verifier = os.environ.get("QX_SIGNAL_VERIFIER", "0") == "1"
    if use_agent or not use_verifier:
        return "agent_brain"
    return "signal_verifier"


@app.get("/api/verifier/status")
async def verifier_status():
    """Legacy statistical-verifier status.

    This reported `enabled: true` with all-zero counters whenever Agent Brain
    was the active analyzer (the normal case), because feed.py never called
    the verifier. It now says which analyzer is actually running so the
    dashboard can't present a dormant module's zeros as live data.
    """
    active = _active_analyzer()
    if active != "signal_verifier":
        return {"enabled": False, "active_analyzer": active,
                "note": f"signal_verifier is dormant — {active} is handling "
                        f"signals. See /api/agent/status.",
                "total_verified": 0, "recent_count": 0}
    try:
        from core.signal_verifier import get_verifier_status
        out = get_verifier_status()
        out["active_analyzer"] = active
        return out
    except Exception as e:
        return {"enabled": False, "active_analyzer": active, "error": str(e),
                "hint": "Set QX_SIGNAL_VERIFIER=1 to enable the agent"}


@app.get("/api/verifier/recent")
async def verifier_recent(limit: int = 50):
    """Recent Signal Verifier Agent verdicts (last N signals analyzed)."""
    if _active_analyzer() != "signal_verifier":
        return {"count": 0, "verdicts": [],
                "active_analyzer": _active_analyzer(),
                "note": "dormant module — use /api/agent/live"}
    try:
        from core.signal_verifier import get_verifier_recent
        limit = max(1, min(200, limit))
        verdicts = get_verifier_recent(limit)
        return {
            "count": len(verdicts),
            "verdicts": verdicts,
        }
    except Exception as e:
        return {"count": 0, "verdicts": [], "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# AUTONOMOUS AI AGENT ENDPOINTS (PROD-AGENT-2026-08-06)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/agent/status")
async def agent_status():
    """Agent Brain status — overall stats, uptime, learning progress."""
    try:
        from core.agent_brain import agent
        return agent.get_status()
    except Exception as e:
        return {"enabled": False, "error": str(e),
                "hint": "Set QX_AGENT_BRAIN=1 to enable the autonomous agent"}


@app.get("/api/agent/decisions")
async def agent_decisions(limit: int = 30):
    """Recent agent evaluations, newest first.

    The Agent tab used to rebuild this list client-side by filtering
    /api/agent/live for type == "evaluate". That feed is a shared 200-slot
    ring buffer and tick events crowd the evaluations out, so the panel kept
    showing "No evaluations yet" beside a three-figure decision count. This
    reads a decisions-only buffer instead.
    """
    try:
        from core.agent_brain import agent
        limit = max(1, min(100, limit))
        decisions = agent.get_decisions(limit)
        return {"count": len(decisions), "decisions": decisions,
                "total": agent.total_signals_evaluated}
    except Exception as e:
        return {"count": 0, "decisions": [], "error": str(e)}


@app.get("/api/agent/live")
async def agent_live(limit: int = 50):
    """Agent Brain live thought stream — recent ticks, evaluations, learning events."""
    try:
        from core.agent_brain import agent
        limit = max(1, min(200, limit))
        thoughts = agent.get_live_feed(limit)
        return {
            "count": len(thoughts),
            "thoughts": thoughts,
            "status": agent.get_status(),
        }
    except Exception as e:
        return {"count": 0, "thoughts": [], "error": str(e)}


@app.get("/api/agent/models")
async def agent_models():
    """Agent Brain per-asset learned models — weights, accuracy, samples."""
    try:
        from core.agent_brain import agent
        return {
            "models": agent.get_models(),
            "feature_names": agent.get_status().get("feature_names", []),
        }
    except Exception as e:
        return {"models": [], "error": str(e)}


@app.get("/api/agent/features/{asset}")
async def agent_features(asset: str):
    """Agent Brain live features for a specific asset — real-time values."""
    try:
        from core.agent_brain import agent
        return agent.get_current_features(asset)
    except Exception as e:
        return {"enabled": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# BREAKEVEN GATE ENDPOINTS (DEEP-FIX-2026-08-07)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/breakeven/report")
async def breakeven_report():
    """Full breakeven report — which pairs are profitable, which are disabled."""
    try:
        from core.breakeven import pair_breakeven_report
        return await asyncio.to_thread(pair_breakeven_report)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/breakeven/check/{asset}")
async def breakeven_check(asset: str, period: int = 60, payout: int = None):
    """Check a single pair against its breakeven threshold."""
    try:
        from core.breakeven import is_pair_profitable
        is_prof, reason, wr, be, n = await asyncio.to_thread(
            is_pair_profitable, asset, period, payout)
        return {
            "asset": asset,
            "period": period,
            "profitable": is_prof,
            "win_rate_pct": wr,
            "breakeven_pct": be,
            "sample_count": n,
            "reason": reason,
        }
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# PAIR HEALTH ENDPOINTS (DEEP-FIX-2026-08-07)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/pair-health")
async def pair_health():
    """Per-pair health report — disabled pairs, consecutive loss tracking."""
    try:
        from core.pair_health import get_health_report
        return await asyncio.to_thread(get_health_report)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/pair-health/reset/{asset}")
async def pair_health_reset(asset: str, request: Request):
    """Admin: manually re-enable a pair disabled by health checks."""
    _check_admin_key(request.headers.get("X-Admin-Key"))
    try:
        from core.pair_health import monitor as _ph_monitor
        _ph_monitor.reset_pair(asset)
        return {"ok": True, "asset": asset, "message": f"{asset} manually re-enabled"}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# BACKTEST ENDPOINT (DEEP-FIX-2026-08-07)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/backtest/recent")
async def backtest_recent(days: int = 7, period: int = 60, payout: int = 85):
    """Walk-forward backtest on recent signal_log data.

    Uses only past data (no lookahead). Returns per-pair, per-hour,
    per-quality, and per-strength breakdown with Wilson confidence intervals.
    """
    try:
        from core.backtest import run_backtest
        days = max(1, min(90, days))  # clamp 1-90 days
        period = max(15, min(3600, period))
        return await asyncio.to_thread(run_backtest, days, period, payout)
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# MONITORING SNAPSHOT ENDPOINT (DEEP-FIX-2026-08-07)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/monitoring/snapshot")
async def monitoring_snapshot():
    """Comprehensive monitoring snapshot: algorithm state, quality health,
    recent changes, and active alerts — all in one call for the dashboard."""
    try:
        from core.algorithm_monitor import get_monitoring_snapshot
        return await asyncio.to_thread(get_monitoring_snapshot)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/patterns/refresh")
async def patterns_refresh(request: Request):
    """Recompute ALL patterns from signal_log. Call after backtest or manually."""
    _check_admin_key(request.headers.get("X-Admin-Key"))
    _ensure_patterns_init()
    from core.time_patterns import recompute_from_signal_log
    async with _PATTERNS_REFRESH_LOCK:
        summary = await asyncio.to_thread(recompute_from_signal_log, 3)
    return {"status": "refreshed", "summary": summary}

@app.get("/api/strategies")
async def get_strategies():
    """List all available trading strategies."""
    from core.algorithm_strategy import get_all_strategies
    return {"strategies": get_all_strategies()}

@app.get("/api/current-strategy")
async def get_current_strategy(asset: Optional[str] = None):
    """Get the current trading strategy for one or all assets."""
    from core.algorithm_strategy import get_asset_strategy_summary
    return get_asset_strategy_summary(asset)

@app.get("/api/auto-tune")
async def auto_tune_report():
    """Show current module win rates and auto-tuned weights."""
    from core.auto_tune import get_tuning_report
    return get_tuning_report()

@app.post("/api/auto-tune/apply")
async def auto_tune_apply(request: Request):
    """Manually trigger auto-tune weight recalculation."""
    _check_admin_key(request.headers.get("X-Admin-Key"))
    from core.auto_tune import apply_tuned_weights_to_engines
    async with _AUTO_TUNE_APPLY_LOCK:
        result = await asyncio.to_thread(apply_tuned_weights_to_engines)
    return {"status": "applied", "weights": result}

@app.get("/api/algorithm-changes")
async def algorithm_changes(hours: int = 24, limit: int = 100):
    """Recent algorithm changes across all pairs (default: last 24h)."""
    safe_hours = max(1, min(int(hours), 24 * 30))
    safe_limit = max(1, min(int(limit), 500))
    from core.algorithm_monitor import get_recent_changes, get_change_summary
    changes = get_recent_changes(asset=None, hours=safe_hours, limit=safe_limit)
    summary = get_change_summary(asset=None, hours=safe_hours)
    return {"changes": changes, "summary": summary}

@app.get("/api/algorithm-changes/{asset}")
async def algorithm_changes_for_asset(asset: str, hours: int = 24, limit: int = 50):
    """Recent algorithm changes for one pair."""
    safe_hours = max(1, min(int(hours), 24 * 30))
    safe_limit = max(1, min(int(limit), 500))
    from core.algorithm_monitor import get_recent_changes, get_current_state
    changes = get_recent_changes(asset=asset, hours=safe_hours, limit=safe_limit)
    state = get_current_state(asset)
    return {"asset": asset, "changes": changes, "current_state": state}

@app.get("/api/signals/{asset}/{period}")
async def get_signals(asset: str, period: int, limit: int = 100, before_ctime: Optional[int] = None):
    safe_limit = max(1, min(int(limit), 500))
    signals = await asyncio.to_thread(
        _db.get_recent_signals, asset, period, safe_limit, before_ctime)
    return {"signals": signals}

# FIX (ALL-PAIRS-HISTORY-2026-09-07): USER REQ — "প্রত্যেকটি সিগন্যাল হিস্টোরি
# দেখাতে হবে, কোনো সময়ে কোন সিগন্যাল টি দিলো". Cross-pair history endpoint:
# returns the newest signals across ALL allowlisted pairs with timestamps.
@app.get("/api/signals/all")
async def get_signals_all(
    period: int = 60,
    limit: int = 200,
    before_ctime: Optional[int] = None,
    category: Optional[str] = None,
):
    """Recent signals across ALL pairs (newest first by ctime).

    Query params: period (default 60), limit (max 500), before_ctime
    (pagination cursor), category ('otc' | 'real', optional).
    """
    if period not in _ALLOWED_PERIODS:
        raise HTTPException(status_code=400, detail=f"invalid period {period!r}")
    if category is not None and category not in ("otc", "real"):
        raise HTTPException(status_code=400, detail="category must be 'otc' or 'real'")
    safe_limit = max(1, min(int(limit), 500))
    signals = await asyncio.to_thread(
        _db.get_recent_signals_all, period, safe_limit, before_ctime, category)
    return {
        "signals": signals,
        "asset": "ALL",
        "period": period,
        "before_ctime": before_ctime,
        "count": len(signals),
    }

# FIX (USER-AUG-2026 / OPEN-API): Public endpoint that returns the LATEST
# signal for every pair in a flat list — designed for curl / external
# integrations. Always CALL/PUT (or "PENDING" if no prediction yet).
# No auth required when QX_PUBLIC_READ=1 (railway default).
@app.get("/api/signals/latest")
async def get_latest_signals_all(limit: int = 50, pair: Optional[str] = None):
    """Latest signal snapshot for all pairs — open public endpoint.

    Returns a flat list of {pair, signal, confidence, strength, time, ...}
    for easy consumption by external scripts, Telegram bots, or webhooks.

    Query params:
      limit: max number of pairs to return (default 50, capped at 100)
      pair:  filter to a single pair (e.g. ?pair=EURUSD_otc)

    Example:
      curl https://your-app.up.railway.app/api/signals/latest
      curl https://your-app.up.railway.app/api/signals/latest?pair=EURUSD_otc
    """
    import time as _time
    from core.constants import ALLOWED_PAIRS

    now = _time.time()
    safe_limit = max(1, min(int(limit), 100))

    # Filter to single pair if requested
    if pair:
        pair = pair.strip()
        pairs_to_query = [pair] if pair in ALLOWED_PAIRS else []
    else:
        pairs_to_query = sorted(ALLOWED_PAIRS)[:safe_limit]

    rows = []
    for asset in pairs_to_query:
        pair_type = "OTC" if asset.endswith("_otc") else "Real"
        stream = getattr(feed, '_streams', {}).get((asset, 60))

        if not stream:
            rows.append({
                "pair": asset,
                "type": pair_type,
                "signal": "PENDING",
                "confidence": 0,
                "strength": "—",
                "strategy": "—",
                "candle_time": None,
                "candle_time_str": "—",
                "buyer_pct": None,
                "seller_pct": None,
                "last_update": None,
                "live": False,
            })
            continue

        # Candle close time
        candles = getattr(stream, 'candles', [])
        last_candle = candles[-1] if candles else None
        candle_time = None
        candle_time_str = "—"
        if last_candle and last_candle.get("time"):
            candle_time = int(last_candle["time"])
            from datetime import datetime, timezone
            candle_time_str = datetime.fromtimestamp(candle_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Prediction
        pred = getattr(stream, 'prediction', None) or {}
        signal = pred.get("signal", "PENDING")
        confidence = pred.get("confidence", 0)
        strength = pred.get("strength", "—")
        strategy = pred.get("strategy", "—")
        reasons = pred.get("reasons", [])
        # Top 3 reasons only (for compactness)
        top_reasons = reasons[-3:] if reasons else []

        # Buyer/seller from microstructure
        micro = getattr(stream, '_last_micro', None) or {}
        buyer_pct = micro.get("buy_pct")
        seller_pct = micro.get("sell_pct")

        # Last tick recency
        last_tick = getattr(stream, 'last_real_tick_wall', 0)
        last_update = round(now - last_tick, 0) if last_tick > 0 else None
        is_live = last_tick > 0 and (now - last_tick) < 120

        rows.append({
            "pair": asset,
            "type": pair_type,
            "signal": signal,
            "confidence": round(confidence, 1) if confidence else 0,
            "strength": strength,
            "strategy": strategy,
            "candle_time": candle_time,
            "candle_time_str": candle_time_str,
            "buyer_pct": round(buyer_pct, 1) if buyer_pct is not None else None,
            "seller_pct": round(seller_pct, 1) if seller_pct is not None else None,
            "last_update_sec_ago": last_update,
            "live": is_live,
            "reasons": top_reasons,
        })

    return {
        "endpoint": "/api/signals/latest",
        "timestamp": now,
        "total_pairs": len(rows),
        "live_pairs": sum(1 for r in rows if r["live"]),
        "signals": rows,
        "usage": "GET /api/signals/latest?pair=EURUSD_otc for single pair",
    }

@app.get("/api/signals/{asset}/{period}/{ctime}")
async def get_signal_detail(asset: str, period: int, ctime: int):
    """Return full detail for a single signal (win/loss reason, regime, etc.)."""
    detail = await asyncio.to_thread(_db.get_signal_detail, asset, period, ctime)
    if detail:
        return detail
    raise HTTPException(status_code=404, detail="not found")

# ── OTC-PREDICT-ENGINE endpoints (2026-09-11, PART 17/21/25/28) ──────────

# NOTE: specific routes MUST be registered before the parametric
# /api/prediction/{asset} route or FastAPI captures "models" as an asset.

# FAST-TRAIN (2026-09-12): the bootstrap daemon — user req "5/7 মিনিটের
# মধ্যে মডেল ট্রেইন হবে, রান হবে". Trains on whatever history each pair
# already has (topped up from the same platform) instead of waiting 14 days.
try:
    from core.otc_predict import fast_train as _fast_train
    _fast_train.start_daemon()
except Exception as _ft_exc:  # the feed's health outranks predictions
    print(f"[server] fast-train daemon failed to start: "
          f"{type(_ft_exc).__name__}: {_ft_exc}")

@app.get("/api/prediction/bootstrap")
async def get_prediction_bootstrap():
    """Fast-train bootstrap state (runs, config, last result)."""
    return await asyncio.to_thread(_fast_train.bootstrap_status)

@app.post("/api/prediction/bootstrap")
async def post_prediction_bootstrap():
    """Force a fast-train run right now (non-blocking; one at a time)."""
    st = await asyncio.to_thread(_fast_train.bootstrap_status)
    if st.get("running"):
        return {"started": False, "reason": "already_running", "status": st}
    threading.Thread(target=_fast_train.run_bootstrap,
                     kwargs={"force": True}, daemon=True).start()
    return {"started": True, "status": st}

@app.get("/api/prediction/overview")
async def get_prediction_overview():
    """MODEL-RUN-FIX (2026-09-12): ONE payload for the মডেল tab.

    Answers the user's exact questions in the UI:
      "কোন কোন মডেল কত টুকু ট্রেইন হলো, রেজাল্ট কি frontend এ দেখা যাবে?"
      → daemon state + per-pair training status/rows/accuracy + registry
        bundles + prediction outcomes, merged server-side.
    """
    import sqlite3 as _sql
    from core.otc_predict import tracker as _pred_tracker

    def _build():
        st = _fast_train.bootstrap_status()
        counts = _fast_train._micro_counts()
        conn = _db._conn()
        conn.row_factory = _sql.Row
        try:
            reg_rows = conn.execute(
                "SELECT name, version, scope, asset, trained_at, active, "
                "metrics, path FROM model_registry "
                "ORDER BY created_at DESC LIMIT 80").fetchall()
        finally:
            conn.close()
        models = []
        for r in reg_rows:
            d = dict(r)
            try:
                m = json.loads(d.pop("metrics") or "{}")
                wf = m.get("walk_forward") or {}
                def _h(hkey):
                    g = wf.get(hkey) or {}
                    if not g:
                        return None
                    return {"acc": g.get("acc_pct"),
                            "baseline": (g.get("baselines") or {})
                                        .get("prev_dir"),
                            "shuffle": g.get("shuffle_acc"),
                            "model": g.get("selected"),
                            "test_n": g.get("n")}
                d["status"] = m.get("status")
                d["trained_rows"] = m.get("rows")
                d["trainer"] = m.get("trainer")
                d["t1"] = _h("y1_up")
                d["t2"] = _h("y2_up")
            except Exception:
                pass
            models.append(d)
        try:
            analytics = _pred_tracker.prediction_analytics()
        except Exception as exc:
            analytics = {"error": f"{type(exc).__name__}: {exc}"}
        # PREDICT-FLOW-FIX (2026-09-12): the user waited an hour with 11
        # models registered and the results card showing only "—" — with no
        # way to see WHY. Now the overview carries the live predictor's
        # runtime truth (candle closes seen / frozen / no-model / errors +
        # per-pair last status) and the frozen-table counts, so "কেন
        # প্রেডিকশন শূন্য" is always answerable from the UI.
        try:
            from core.otc_predict import predictor as _pred_runtime
            predictor = _pred_runtime.runtime_status()
        except Exception as exc:
            predictor = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            pred_table = _pred_tracker.prediction_table_stats()
        except Exception as exc:
            pred_table = {"error": f"{type(exc).__name__}: {exc}"}
        return {"daemon": st, "models": models,
                "candles": counts, "analytics": analytics,
                "predictor": predictor, "pred_table": pred_table,
                "generated_at": time.time()}

    return await asyncio.to_thread(_build)

@app.get("/api/prediction/models")
async def get_prediction_models():
    """Model registry state — which bundle version is allowed to predict."""
    import sqlite3 as _sql
    conn = _db._conn()
    try:
        rows = conn.execute(
            "SELECT name, version, scope, asset, trained_at, active "
            "FROM model_registry ORDER BY created_at DESC LIMIT 50").fetchall()
        return {"models": [dict(r) for r in rows]}
    finally:
        conn.close()

@app.post("/api/prediction/reload-models")
async def reload_prediction_models(request: Request):
    """Admin: force the predictor cache to re-check the registry now."""
    await _require_admin(request)
    from core.otc_predict import predictor as _pred
    _pred._cache["checked_at"] = 0.0
    bundle = await asyncio.to_thread(_pred._get_bundle)
    return {"reloaded": True,
            "model_version": bundle.version if bundle else None}

@app.get("/api/prediction-analytics")
async def get_prediction_analytics(days: Optional[int] = None):
    """PART 21 performance dashboard metrics (settled predictions only)."""
    from core.otc_predict import tracker as _pred_tracker
    data = await asyncio.to_thread(_pred_tracker.prediction_analytics, days)
    return data

@app.get("/api/prediction/{asset}")
async def get_prediction_card(asset: str):
    """Latest frozen T+1/T+2 predictions for a pair (PART 30 card payload).

    Reads the otc_predictions table — the same frozen rows the live engine
    writes at candle close, so a fresh page load sees exactly what was
    locked (never a re-computed/fabricated state).
    """
    from core.otc_predict import tracker as _pred_tracker
    from core.otc_predict.predictor import describe_status
    rows = await asyncio.to_thread(
        _pred_tracker.latest_predictions, asset, 12)
    status = await asyncio.to_thread(describe_status, asset)

    def _row_out(r):
        return {
            "horizon": r["horizon"],
            "target_time": r["target_time"],
            "prediction": r["prediction"],
            "probability": r["probability"],
            "tier": r["tier"], "score": r["score"],
            "emit": bool(r["emit"]),
            "signal_time": r["signal_time"],
            "model_version": r["model_version"],
            "actual_result": r["actual_result"],
            "win_loss": r["win_loss"],
            "locked": True,
        }

    # pick the most recent signal_time group (one candle-close snapshot)
    last_t = rows[0]["signal_time"] if rows else None
    group = [r for r in rows if r["signal_time"] == last_t] if last_t else []
    return {
        "endpoint": "/api/prediction",
        "asset": asset,
        "engine": status,
        "locked": True,
        "current": [_row_out(r) for r in sorted(group, key=lambda x: x["horizon"])],
        "history": [_row_out(r) for r in rows],
    }


@app.delete("/api/signals/{asset}/{period}/{ctime}")
async def delete_signal_endpoint(asset: str, period: int, ctime: int):
    """Delete a single signal by (asset, period, ctime)."""
    deleted = await asyncio.to_thread(_db.delete_signal, asset, period, ctime)
    if not deleted:
        raise HTTPException(status_code=404, detail="signal not found")
    return {"deleted": True, "asset": asset, "period": period, "ctime": ctime}

@app.post("/api/signals/clear")
async def clear_signals_endpoint(
    asset: Optional[str] = None,
    period: Optional[int] = None,
    before_ctime: Optional[int] = None,
):
    """Clear signals, optionally filtered by asset/period/before_ctime."""
    count = await asyncio.to_thread(
        _db.clear_signals, asset, period, before_ctime)
    return {"deleted_count": count, "filter": {"asset": asset, "period": period, "before_ctime": before_ctime}}

@app.get("/api/signals/count")
async def get_signals_count(
    asset: Optional[str] = None,
    period: Optional[int] = None,
    hours: Optional[int] = None,
):
    """Return total signal count, optionally filtered by asset/period/hours."""
    import time as _t
    q = "SELECT COUNT(*) as n FROM signal_log WHERE signal IN ('CALL','PUT')"
    params = []
    if asset:
        q += " AND asset=?"
        params.append(asset)
    if period is not None:
        q += " AND period=?"
        params.append(period)
    if hours is not None:
        cutoff = int(_t.time()) - hours * 3600
        q += " AND ts >= ?"
        params.append(cutoff)
    # Use read cursor
    import sqlite3 as _sql
    conn = _db._conn()
    try:
        row = conn.execute(q, params).fetchone()
        total = row["n"] if row else 0
    finally:
        conn.close()
    return {"count": total}

# ── WebSocket endpoint ───────────────────────────────────────────────────────

from core.constants import ALLOWED_PERIODS as _ALLOWED_PERIODS  # noqa: E402

def _ws_origin_allowed(ws: WebSocket) -> bool:
    """Check the WS Origin header against the module-level whitelist."""
    if not _ALLOWED_WS_ORIGINS:
        return True
    try:
        origin = (ws.headers.get("origin") or "").lower()
    except Exception:
        return False
    if not origin:
        # Browser always sends Origin on cross-origin WS; missing Origin
        # is suspicious → reject by default unless caller whitelisted "".
        return False
    # Allow exact matches plus scheme+host matches (ignore port).
    for allowed in _ALLOWED_WS_ORIGINS:
        if origin == allowed:
            return True
        # Allow http(s)://host on any port.
        if "://" in allowed:
            scheme, _, host = allowed.partition("://")
            if origin.startswith(f"{scheme}://{host}"):
                # match remainder is ":" + port
                rest = origin[len(f"{scheme}://{host}"):]
                if rest == "" or rest.startswith(":"):
                    return True
    _user_set_origins = bool(os.environ.get("ALLOWED_WS_ORIGINS", "").strip())
    if not _user_set_origins:
        # Extract host from origin for matching.
        try:
            from urllib.parse import urlparse
            host = urlparse(origin).hostname or ""
        except Exception:
            host = ""
        if host:
            # Allow any Railway-managed domain.
            if host.endswith(".up.railway.app"):
                return True
            if host.endswith(".railway.app"):
                return True
            # Allow any auto-detected Railway public domain's host.
            for auto in _auto_origins:
                try:
                    auto_host = urlparse(auto).hostname or ""
                    if auto_host and (host == auto_host or
                                       host.endswith("." + auto_host) or
                                       auto_host.endswith("." + host)):
                        return True
                except Exception as _e:
                    print(f"[silent-except] server.py:1231 {type(_e).__name__}: {_e}")
                    pass
    return False

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global cid_counter

    if not _ws_origin_allowed(ws):
        await ws.close(code=1008, reason="origin not allowed")
        return

    if len(clients) >= _MAX_WS_CLIENTS:
        try:
            await ws.close(code=1013, reason="server at max capacity")
        except Exception as _e:
            print(f"[silent-except] server.py:1253 {type(_e).__name__}: {_e}")
            pass
        return

    await ws.accept()
    cid_counter += 1
    cid = f"client-{cid_counter}"
    clients[cid] = ws
    print(f"[server] {cid} connected ({len(clients)} total)")

    ws_idle_timeout = _WS_IDLE_TIMEOUT

    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=ws_idle_timeout)
            except asyncio.TimeoutError:
                print(f"[server] {cid} idle timeout ({ws_idle_timeout}s) — closing")
                try:
                    await ws.close(code=1008, reason="idle timeout")
                except Exception as _e:
                    print(f"[silent-except] server.py:1284 {type(_e).__name__}: {_e}")
                    pass
                break
            if len(raw) > _MAX_WS_MSG_BYTES:
                try:
                    await ws.close(code=1009, reason="message too big")
                except Exception as _e:
                    print(f"[silent-except] server.py:1294 {type(_e).__name__}: {_e}")
                    pass
                print(f"[server] {cid} sent oversized message " f"({len(raw)} > {_MAX_WS_MSG_BYTES} bytes) — closing")
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                await ws.send_text(json.dumps({"type": "error", "error": "expected a JSON object"}))
                continue

            t = msg.get("type")

            if t == "subscribe":
                asset = msg.get("asset", "")
                if not isinstance(asset, str) or not asset or len(asset) > 32:
                    await ws.send_text(json.dumps({"type": "error", "error": "invalid or missing asset (max 32 chars)"}))
                    continue
                period_val = msg.get("period", 60)
                if not isinstance(period_val, int) or isinstance(period_val, bool):
                    await ws.send_text(json.dumps({"type": "error", "error": f"invalid period {period_val!r}; " f"must be an integer in {sorted(_ALLOWED_PERIODS)}"}))
                    continue
                period = period_val
                if period not in _ALLOWED_PERIODS:
                    await ws.send_text(json.dumps({"type": "error", "error": f"invalid period {period!r}; allowed: " f"{sorted(_ALLOWED_PERIODS)}"}))
                    continue
                category = (msg.get("category") or "").lower().strip()
                # FIX (ALLTIME-OTC-GATE-2026-09-07, CRITICAL): the frontend
                # always sends category=currentCategory and the All-Time OTC
                # page sends "alltime_otc" — which the gate below rejected,
                # so EVERY subscribe on that page was answered with an error
                # and ensure_stream() never ran. Result: the All-OTC page
                # never received a snapshot or a single tick. The engines
                # router already normalizes alltime_otc → otc
                # (engines/__init__.py predict: "if category == 'alltime_otc':
                # category = 'otc'"), so normalize here BEFORE validation.
                if category == "alltime_otc":
                    category = "otc"
                if category and category not in ("real", "otc"):
                    await ws.send_text(json.dumps({"type": "error", "error": f"invalid category {category!r}; " f"expected 'real' or 'otc'"}))
                    continue
                # If category is specified, validate it matches the asset.
                # 'otc' requires the asset to end with _otc.
                if category:
                    is_otc_asset = asset.endswith("_otc")
                    if category == "otc" and not is_otc_asset:
                        await ws.send_text(json.dumps({"type": "error", "error": f"category/asset mismatch: category={category!r} " f"but asset {asset!r} is not an OTC pair " f"(must end with '_otc')."}))
                        continue
                    if category == "real" and is_otc_asset:
                        await ws.send_text(json.dumps({"type": "error", "error": f"category/asset mismatch: category='real' " f"but asset {asset!r} is an OTC pair."}))
                        continue
                try:
                    result = await feed.ensure_stream(asset, period, cid=cid)
                except Exception as exc:
                    _logger.exception("ensure_stream failed for %s@%ss", asset, period)
                    await ws.send_text(json.dumps({"type": "error", "error": f"stream setup failed: {type(exc).__name__}"}))
                    continue
                await ws.send_text(json.dumps(result))

            elif t == "pairs":
                await ws.send_text(json.dumps(
                    {"type": "pairs", **feed.available_pairs()}))

            elif t == "status":
                await ws.send_text(json.dumps({"type": "status", "connected": feed._connected, "streams": feed.stream_status()}))

            elif t == "signals":
                asset = msg.get("asset", "")
                if not isinstance(asset, str) or not asset or len(asset) > 32:
                    await ws.send_text(json.dumps({"type": "error", "error": "invalid or missing asset (max 32 chars)"}))
                    continue
                period_val = msg.get("period", 60)
                if not isinstance(period_val, int) or isinstance(period_val, bool):
                    await ws.send_text(json.dumps({"type": "error", "error": f"invalid period {period_val!r}; " f"must be an integer in {sorted(_ALLOWED_PERIODS)}"}))
                    continue
                period = period_val
                if period not in _ALLOWED_PERIODS:
                    await ws.send_text(json.dumps({"type": "error", "error": f"invalid period {period!r}"}))
                    continue
                try:
                    req_limit = int(msg.get("limit", 100))
                except (TypeError, ValueError):
                    req_limit = 100
                req_limit = max(1, min(req_limit, 200))
                before_ctime = msg.get("before_ctime")
                if before_ctime is not None:
                    try:
                        before_ctime = int(before_ctime)
                    except (TypeError, ValueError):
                        await ws.send_text(json.dumps({"type": "error", "error": f"invalid before_ctime {before_ctime!r}; " f"must be an integer or null"}))
                        continue
                # FIX (ALL-PAIRS-HISTORY-2026-09-07): asset == "ALL" returns
                # the newest signals across every allowlisted pair so the
                # History tab can show "কোন সময়ে কোন পেয়ারে কী সিগন্যাল দিলো".
                if asset.upper() == "ALL":
                    sigs = await asyncio.to_thread(
                        _db.get_recent_signals_all, period, req_limit, before_ctime)
                else:
                    sigs = await asyncio.to_thread(
                        _db.get_recent_signals, asset, period, req_limit, before_ctime)
                await ws.send_text(json.dumps({"type": "signals", "signals": sigs, "asset": asset, "period": period, "before_ctime": before_ctime}))

    except WebSocketDisconnect:
        print(f"[server] {cid} disconnected")
    except Exception as e:
        _logger.exception("%s ws error", cid)
        print(f"[server] {cid} error: {e}")
    finally:
        clients.pop(cid, None)
        try:
            await feed.drop_interest(cid)
        except Exception:
            _logger.exception("drop_interest failed for %s", cid)

# ── Admin: prune non-allowlist pairs ──────────────────────────────────────────

@app.post("/api/admin/prune-pairs")
async def prune_non_allowlist_pairs(request: Request, dry_run: bool = False):
    """Delete DB rows for assets NOT in the 15-pair allowlist.
    FIX (PAIR-ALLOWLIST-2026-08-07 / A-14 #7): one-time migration to clean up
    historical rows for removed pairs (EURUSD_otc, USDCHF_otc, USDJPY_otc,
    USDARS_otc, USDBRL_otc, USDSGD_otc, USDCNH_otc, USDTHB_otc, USDRUB_otc,
    EURGBP_otc, GBPUSD_otc, USDCAD_otc, EURJPY_otc, GBPJPY_otc, EURAUD_otc).
    Set ?dry_run=true to preview counts without deleting.
    Requires X-Admin-Key header.
    """
    _check_admin_key(request.headers.get("X-Admin-Key"))
    results = await asyncio.to_thread(_db.prune_non_allowlist_assets, dry_run)
    return {
        "dry_run": dry_run,
        "allowlist_count": 15,
        "results": results,
    }


@app.get("/api/allowlist")
async def get_allowlist():
    """Return the 15-pair allowlist (11 OTC + 4 Real).
    NEW (PAIR-ALLOWLIST-2026-08-07): single endpoint that exposes the
    canonical pair list from core/constants. Frontend can use this instead
    of hardcoding the list.
    """
    from core.constants import (
        ALLOWED_PAIRS_OTC, ALLOWED_PAIRS_REAL, ALLOWED_PAIRS,
        ALLOWED_PAIRS_BY_CATEGORY,
    )
    return {
        "otc": list(ALLOWED_PAIRS_OTC),
        "real": list(ALLOWED_PAIRS_REAL),
        "all": sorted(ALLOWED_PAIRS),
        "by_category": {k: sorted(v) for k, v in ALLOWED_PAIRS_BY_CATEGORY.items()},
        "count": {"otc": len(ALLOWED_PAIRS_OTC), "real": len(ALLOWED_PAIRS_REAL), "total": len(ALLOWED_PAIRS)},
    }


@app.get("/api/share-signals")
async def share_signals():
    """Latest signal data for ALL pairs — for the Share Signal table.

    User requirement: 'একটি টেবিল সেকশন বানাতে হবে। যার নাম থাকবে শেয়ার
    সিগন্যাল। সেখানে এই ভাবে থাকবে। Pair name - otc or real - time-
    close candle data buyer Sellar - prediction signal call put'

    Returns a table row per pair:
    - pair: asset name
    - type: 'OTC' or 'Real'
    - time: last candle close time (HH:MM UTC)
    - buyer_pct: buyer percentage from microstructure
    - seller_pct: seller percentage
    - signal: CALL / PUT / NEUTRAL
    - confidence: 0-100
    - strength: WEAK / MEDIUM / STRONG
    - last_update: seconds ago
    """
    import time as _time
    from core.constants import ALLOWED_PAIRS

    now = _time.time()
    rows = []

    for asset in sorted(ALLOWED_PAIRS):
        pair_type = "OTC" if asset.endswith("_otc") else "Real"
        stream = getattr(feed, '_streams', {}).get((asset, 60))

        if not stream:
            rows.append({
                "pair": asset,
                "type": pair_type,
                "time": "—",
                "buyer_pct": None,
                "seller_pct": None,
                "signal": "—",
                "confidence": 0,
                "strength": "—",
                "last_update": None,
                "live": False,
            })
            continue

        # Last candle close time
        candles = getattr(stream, 'candles', [])
        last_candle = candles[-1] if candles else None
        if last_candle and last_candle.get("time"):
            ct = last_candle["time"]
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(int(ct), tz=timezone.utc)
            time_str = dt.strftime("%H:%M")
        else:
            time_str = "—"

        # Buyer/seller from last prediction's microstructure
        pred = getattr(stream, 'prediction', None) or {}
        micro = getattr(stream, '_last_micro', None) or {}
        buyer_pct = micro.get("buy_pct")
        seller_pct = micro.get("sell_pct")

        # Signal
        signal = pred.get("signal", "—")
        confidence = pred.get("confidence", 0)
        strength = pred.get("strength", "—")

        # Last update
        last_tick = getattr(stream, 'last_real_tick_wall', 0)
        last_update = round(now - last_tick, 0) if last_tick > 0 else None

        rows.append({
            "pair": asset,
            "type": pair_type,
            "time": time_str,
            "buyer_pct": round(buyer_pct, 1) if buyer_pct is not None else None,
            "seller_pct": round(seller_pct, 1) if seller_pct is not None else None,
            "signal": signal,
            "confidence": round(confidence, 1) if confidence else 0,
            "strength": strength,
            "last_update": last_update,
            "live": last_tick > 0 and (now - last_tick) < 120,
        })

    return {
        "total_pairs": len(rows),
        "live_pairs": sum(1 for r in rows if r["live"]),
        "timestamp": now,
        "rows": rows,
    }


@app.post("/api/share-signals/save")
async def share_signals_save(request: Request):
    """Save current Share Signal table snapshot to history.

    User requirement: 'তারপর এই গুলো হিস্টোরিতে গিয়ে সেভ হবে।'
    """
    try:
        # Get current snapshot
        snapshot = await share_signals()

        # Save to DB
        import json as _json
        import time as _time
        with _db._write_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS share_signal_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_json TEXT,
                    total_pairs INT,
                    live_pairs INT,
                    ts REAL
                )
            """)
            cur.execute("""
                INSERT INTO share_signal_history
                    (snapshot_json, total_pairs, live_pairs, ts)
                VALUES (?, ?, ?, ?)
            """, (
                _json.dumps(snapshot),
                snapshot["total_pairs"],
                snapshot["live_pairs"],
                _time.time(),
            ))

        return {
            "ok": True,
            "message": f"Saved snapshot with {snapshot['total_pairs']} pairs "
                       f"({snapshot['live_pairs']} live)",
            "timestamp": snapshot["timestamp"],
        }
    except Exception as e:
        _logger.exception("share signals save failed")
        return {"ok": False, "error": str(e)}


@app.get("/api/share-signals/history")
async def share_signals_history(limit: int = 50):
    """Get saved Share Signal snapshots from history."""
    try:
        with _db._read_cursor() as cur:
            cur.execute("""
                SELECT id, snapshot_json, total_pairs, live_pairs, ts
                FROM share_signal_history
                ORDER BY ts DESC
                LIMIT ?
            """, (limit,))
            rows = [dict(r) for r in cur.fetchall()]

        import json as _json
        from datetime import datetime, timezone
        results = []
        for r in rows:
            snapshot = _json.loads(r["snapshot_json"]) if r["snapshot_json"] else {}
            dt = datetime.fromtimestamp(r["ts"], tz=timezone.utc)
            results.append({
                "id": r["id"],
                "time": dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "total_pairs": r["total_pairs"],
                "live_pairs": r["live_pairs"],
                "rows": snapshot.get("rows", []),
            })

        return {"history": results, "count": len(results)}
    except Exception as e:
        _logger.exception("share signals history failed")
        return {"error": str(e)}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT") or "8000")
    is_railway = (
        os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("RAILWAY_SERVICE_ID")
        or os.environ.get("RAILWAY_ENVIRONMENT")
    )
    # Railway detection: disable auto browser open, force headless
    if is_railway:
        os.environ.setdefault("AUTO_OPEN_BROWSER", "0")
        os.environ.setdefault("HEADLESS", "1")
        print("[server] Railway environment detected — headless mode, no browser auto-open")
        host = "0.0.0.0"
    else:
        host = os.environ.get("HOST", "127.0.0.1")
    workers = int(os.environ.get("WEB_CONCURRENCY") or "1")
    uvicorn.run(app, host=host, port=port, log_level="info", workers=workers)
