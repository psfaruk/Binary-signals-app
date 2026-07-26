"""
Binary Signal App — WebSocket server.
Serves static frontend and bridges QuotexFeed to browser clients.
"""
import asyncio
import hmac
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
# FIX (DEEP-AUDIT-2026-07-26 / F-13-43): removed unused `HTMLResponse` import.
# FIX (DEEP-AUDIT-2026-07-26 / F-13-44): removed unused `StaticFiles` import
#   (it was shadowed by the starlette import below at the static-file mount).

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

# ════════════════════════════════════════════════════════════════════════════
# SIMULATION MODE DISABLED (2026-07-25)
# ════════════════════════════════════════════════════════════════════════════
# The app now ALWAYS runs on live Quotex data. sim_feed.py is no longer
# imported. If QX_TOKEN is missing, the server will still start (so the
# /api/set-token endpoint is reachable to provision a token at runtime),
# but the feed will be in a "no token" state and stream subscriptions will
# return an error to clients. This is intentional — a loud failure is far
# safer than a silent sim fallback that produces fake predictions.
#
# USE_SIM env var is now IGNORED (treated as 0 always). Setting it has no
# effect — sim mode is structurally disabled.
# ════════════════════════════════════════════════════════════════════════════

# ── Quotex backend selection ────────────────────────────────────────────────
# QX_USE_RAW_WS=0 (DEFAULT): vendored pyquotex with Firefox TLS cipher suite
#                             → bypasses Cloudflare without Playwright/curl_cffi
# QX_USE_RAW_WS=1:           raw WebSocket backend (quotex_ws.py)
#                             → lighter but Cloudflare blocks login on datacenter IPs
# FIX (DEEP-AUDIT-2026-07-26 / F-13-45): the `_HAS_PYQUOTEX` and `_USE_RAW_WS`
#   variables were computed at module load and NEVER referenced elsewhere in
#   server.py — dead code. The print statements below are kept so the deploy
#   log still shows which backend is in use.
if os.environ.get("QX_USE_RAW_WS", "0") == "1":
    print("[server] QX_USE_RAW_WS=1 — raw WebSocket backend "
          "(pyquotex optional)")
else:
    print("[server] QX_USE_RAW_WS=0 — vendored pyquotex with Firefox TLS "
          "(Cloudflare bypass)")

# Always import the real feed. sim_feed.py is no longer used.
from feed import QuotexFeed as _Feed
print("[server] ✅ using REAL Quotex feed (live data only — sim mode disabled)")

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
    print(f"[server] ⚠️  QX_EMAIL found but no QX_TOKEN — will try email/password")
    print("[server]    Note: email/password login is blocked by Cloudflare on Railway.")
    print("[server]    Set QX_TOKEN manually via Railway Variables or /api/set-token.")

feed = _Feed()
clients: dict[str, WebSocket] = {}   # cid -> ws
cid_counter = 0
# FIX (DEEP-AUDIT-2026-07-26 / F-13-36): cap total concurrent WS clients to
#   prevent unbounded FD growth. Override via `MAX_WS_CLIENTS` env var.
_MAX_WS_CLIENTS = int(os.environ.get("MAX_WS_CLIENTS", "200"))
# FIX (DEEP-AUDIT-2026-07-26 / F-13-37): module-level logger so WS errors
#   include a traceback instead of just `str(e)`.
_logger = logging.getLogger("server")
# FIX (DEEP-AUDIT-2026-07-26 / F-13-68): read WS idle timeout ONCE at module
#   load instead of parsing the env var on every new WS connection.
_WS_IDLE_TIMEOUT = float(os.environ.get("WS_IDLE_TIMEOUT", "300.0"))
# FIX (DEEP-AUDIT-2026-07-26 / F-13-7): cap incoming WS text frame size to
#   prevent a malicious client from triggering OOM via a 1GB JSON payload.
_MAX_WS_MSG_BYTES = int(os.environ.get("MAX_WS_MSG_BYTES", str(1 << 20)))
# FIX (DEEP-AUDIT-2026-07-26 / F-13-6): origin whitelist for WS connections.
#   Comma-separated list of allowed origins (default: localhost + 127.0.0.1
#   on any port). Empty string disables the check (NOT recommended).
_ALLOWED_WS_ORIGINS = [
    o.strip().lower() for o in os.environ.get(
        "ALLOWED_WS_ORIGINS",
        "http://localhost,http://127.0.0.1,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",") if o.strip()
]
# FIX (DEEP-AUDIT-2026-07-26 / F-13-64): patterns initialised once at startup;
#   `/api/patterns*` endpoints no longer need to re-init on every request.
_PATTERNS_INITIALIZED = False
# FIX (DEEP-AUDIT-2026-07-26 / F-13-2): per-endpoint asyncio locks so two
#   concurrent POSTs to the expensive recompute endpoints can't double-recompute.
_BRAIN_ANALYZE_LOCK = asyncio.Lock()
_PATTERNS_REFRESH_LOCK = asyncio.Lock()
_AUTO_TUNE_APPLY_LOCK = asyncio.Lock()


def _check_admin_key(provided: Optional[str]) -> None:
    """Constant-time admin-key comparison.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-26): previously admin-key checks used
    `provided != expected` which leaks key length/prefix via response timing.
    Now uses `hmac.compare_digest`. If ADMIN_KEY env var is unset, the
    endpoint stays open (backwards-compatible for personal deployments).
    """
    expected = os.environ.get("ADMIN_KEY", "").strip()
    if not expected:
        return
    if not provided:
        raise HTTPException(status_code=403, detail="forbidden")
    if not hmac.compare_digest(provided.strip(), expected):
        raise HTTPException(status_code=403, detail="forbidden")


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

    FIX (CANDLE-STUCK-FIX, 2026-07-23): when the real feed has fallen
    back to sim_delegate mode, the stream's interested_cids are stored
    in sim_delegate._streams, NOT feed._streams. The previous code only
    checked feed._streams, so all sim_feed broadcasts were dropped
    (target_cids=[] → skip). This was THE cause of 'signals come but
    candle stuck' — the sim feed was producing ticks but they never
    reached the browser. Now we check BOTH feed._streams AND
    sim_delegate._streams.
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

            # FIX (CANDLE-STUCK-FIX): if not found in real feed, check
            # sim_delegate. The sim feed's streams are stored separately.
            if not target_cids:
                sim = getattr(feed, '_sim_delegate', None)
                if sim is not None and hasattr(sim, '_streams'):
                    sim_stream = sim._streams.get(stream_key)
                    if sim_stream and sim_stream.interested_cids:
                        target_cids = list(sim_stream.interested_cids)
        # FIX (DEEP-AUDIT-2026-07-26 / F-13-27): the previous bare
        #   `except Exception:` swallowed every error AND defaulted to
        #   broadcast-all — which would leak private pair data to clients
        #   who never subscribed. Now we only swallow the narrow set of
        #   attribute/lookup errors that the stream-registry lookup can
        #   legitimately raise; any other exception propagates so it
        #   shows up in logs instead of silently broadcasting to all.
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
    # Remove dead clients
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-1-06): use BaseException so
    # asyncio.CancelledError (a BaseException since 3.8) also triggers
    # client removal. Otherwise dead clients accumulate and every future
    # broadcast wastes time sending to them.
    for cid, result in zip(cids, results):
        if isinstance(result, BaseException):
            clients.pop(cid, None)


# ── Lifespan handler (modern FastAPI — replaces deprecated on_event) ───────
# FIX (DEEP-AUDIT-2026-07-26 / F-13-101): `asynccontextmanager` was imported
#   mid-file after route definitions; now imported at the top of the module.


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup + shutdown lifecycle (replaces @app.on_event).

    FIX (DEEP-AUDIT-2026-07-26 / F-13-3): `_db.init()`, `init_brain()`, and
      `recompute_from_signal_log()` are all SYNCHRONOUS SQLite/Python calls
      that previously ran directly inside the async lifespan — blocking the
      event loop and risking the Railway healthcheck grace period. They
      now run inside `asyncio.to_thread(...)` so the loop keeps serving
      /healthz while the (potentially slow) init proceeds in a worker.
    """
    global _PATTERNS_INITIALIZED
    print("[server] lifespan: startup beginning")

    def _sync_init():
        _db.init()
        # Initialize brain tables
        from core.brain import init_brain
        init_brain()
        # Initialize time/session pattern tables (BACKTEST-2026-07-21)
        try:
            from core.time_patterns import init_patterns, recompute_from_signal_log
            init_patterns()
            # Recompute patterns from existing signal_log on startup — this
            # populates the time_session_patterns table so the blender can
            # apply adjustments from the very first prediction.
            summary = recompute_from_signal_log(min_samples=3)
            total_patterns = sum(sum(dims.values()) for dims in summary.values()) if summary else 0
            print(f"[server] patterns loaded: {total_patterns} entries across "
                  f"{len(summary)} pairs")
        except Exception as _e:
            print(f"[server] pattern init failed (non-fatal): {_e}")
        # FIX (DATA-FLOW-2026-07-22): initialize algorithm-change monitor.
        try:
            from core.algorithm_monitor import init_algorithm_monitor
            init_algorithm_monitor()
            print("[server] algorithm monitor initialized")
        except Exception as _e:
            print(f"[server] algorithm monitor init failed (non-fatal): {_e}")

    await asyncio.to_thread(_sync_init)
    _PATTERNS_INITIALIZED = True

    # Start feed in background task
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-1-07): add a done-callback so
    # if feed.run() crashes during startup (e.g. _db.init failure or
    # _auto_login_startup raising), the exception is logged instead of
    # silently dying. Without this the server starts but never receives
    # data and /api/status shows connected: False forever with no error.
    def _on_feed_done(task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            _logger.exception("feed task died", exc_info=exc)
            print(f"[server] FATAL: feed task died: "
                  f"{type(exc).__name__}: {exc}")
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
    await feed.shutdown()
    feed_task.cancel()
    try:
        await feed_task
    except asyncio.CancelledError:
        pass
    print("[server] lifespan: shutdown complete")


app = FastAPI(lifespan=lifespan)
static_dir = Path(__file__).parent / "static"

# FIX (P0-ISSUE-013, 2026-07-22): serve static files with no-store (stronger
# than no-cache). Safari's Back-Forward Cache (bfcache) and Chrome's disk
# cache can serve stale JS even with `no-cache, must-revalidate` — `no-store`
# forbids storing entirely. Combined with ETag revalidation, this guarantees
# the browser ALWAYS fetches the latest version from the server.
# This was the root cause of "বাকি গুল সুইচ করা যায় না" — old common.js
# (without _nextCategory) was served from bfcache, so the switch button
# had no click handler.
from starlette.staticfiles import StaticFiles as _StarletteStaticFiles
class _NoCacheStaticFiles(_StarletteStaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        # no-store: browser MUST NOT cache. Every load fetches from server.
        # no-cache: redundant with no-store but kept for HTTP/1.0 compat.
        # must-revalidate: redundant but explicit.
        resp.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
        resp.headers["Pragma"] = "no-cache"  # HTTP/1.0 legacy
        resp.headers["Expires"] = "0"        # HTTP/1.0 legacy
        return resp

app.mount("/static", _NoCacheStaticFiles(directory=str(static_dir)), name="static")


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def _auto_open_browser():
    """Open the default browser to the app URL after server starts.
    Runs in a background thread so it doesn't block the event loop.
    Waits ~5 seconds for the server to be ready before opening.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-49, F-13-50, F-13-51, F-13-52):
      Removed the local `import os as _os`, `import time as _time`, and
      `import webbrowser as _wb` — these modules are now imported once at
      the top of the module. Also broadened the AUTO_OPEN_BROWSER skip
      check to recognise `"false"`, `"no"`, and `"off"` (not just `"0"`).
    """
    # FIX (F-13-52): accept any common false-y value, not just "0".
    if os.environ.get("AUTO_OPEN_BROWSER", "1").lower() in ("0", "false", "no", "off"):
        return

    port = os.environ.get("PORT", "8000")
    url = f"http://localhost:{port}"

    def _open():
        # FIX (F-13-53): poll /healthz until it returns 200 (or give up
        # after ~10s) instead of a hard-coded 5s sleep — works on slow
        # machines and is faster on quick ones.
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
    """Railway healthcheck endpoint — returns 200 if the process is up.

    Note: this returns ok:true even if QX_TOKEN is not set, so Railway's
    healthcheck passes (the server must be up for /api/set-token to work).
    Use /api/token-status to check whether the app has live credentials.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-38): previously this returned
      `{"ok": True}` unconditionally — even if the feed task had crashed,
      the container stayed "healthy" while serving zero data. We now also
      expose `feed_connected` and `feed_task_alive` so Railway/ops can
      fail the healthcheck on a dead feed by inspecting those fields.
      The top-level `ok` flag stays True (backwards compat) so existing
      Railway healthcheck configs keep working.
    """
    feed_task = getattr(request.app.state, "feed_task", None)
    feed_task_alive = bool(feed_task is not None and not feed_task.done())
    return {
        "ok": True,
        "feed_connected": bool(getattr(feed, "_connected", False)),
        "feed_task_alive": feed_task_alive,
    }


@app.get("/api/token-status")
async def token_status():
    """Returns whether the app has live Quotex credentials.

    Frontend uses this to show a clear "No Token" warning banner when
    QX_TOKEN is missing, instead of silently failing on stream subscribe.

    SIM-MODE-DISABLED (2026-07-25): sim mode is permanently disabled, so
    the only way to get live data is to set QX_TOKEN. This endpoint exposes
    the token presence state (without leaking the token itself).
    """
    qx_token = os.environ.get("QX_TOKEN", "").strip()
    qx_email = os.environ.get("QX_EMAIL", "").strip()
    has_token = bool(qx_token)
    has_email = bool(qx_email)
    if has_token:
        preview = f"{qx_token[:8]}...{qx_token[-4:]}" if len(qx_token) > 12 else "(short)"
        status = "live_token"
        message = f"QX_TOKEN is set ({preview}) — app is on live data."
    elif has_email:
        status = "email_only"
        message = ("QX_EMAIL is set but no QX_TOKEN — will try email/password "
                   "login. Note: Cloudflare blocks this on Railway datacenter IPs. "
                   "Set QX_TOKEN manually via Railway Variables or /api/set-token.")
    else:
        status = "no_credentials"
        message = ("❌ No Quotex credentials — sim mode is permanently disabled. "
                   "Set QX_TOKEN via Railway Variables or visit "
                   "/api/set-token?token=YOUR_TOKEN to provision at runtime.")
    return {
        "status": status,
        "has_token": has_token,
        "has_email": has_email,
        "sim_mode_disabled": True,
        "message": message,
        "action": "set_token" if status == "no_credentials" else None,
    }


@app.get("/")
async def index():
    # FIX (DEEP-AUDIT-2026-07-26 / F-13-55): use the same `no-store`
    #   cache policy that the static-file mount uses, so the browser never
    #   serves a stale index.html whose <script src=> tags point at
    #   freshly-updated JS modules.
    return FileResponse(static_dir / "index.html",
                        headers={"Cache-Control": "no-store, no-cache, max-age=0, must-revalidate"})


# ── Token update endpoint (2026-07-23) ──────────────────────────────────────
# FIX (TOKEN-AUTO-UPDATE): user reported app falling back to sim mode when
# Quotex token expires. The auto-relogin via email/password is blocked by
# Cloudflare on Railway datacenter IPs. This endpoint lets the user update
# the token at RUNTIME — no Railway dashboard access needed, no restart.
# The user just opens a URL in their browser with the new token.
#
# Usage:
#   GET  /api/set-token?token=XXXX  (simple, from browser address bar)
#   POST /api/set-token             (with JSON body {"token": "XXXX"})
#
# Security: this endpoint is UNPROTECTED — anyone with the URL can set
# the token. For a personal app this is acceptable. For production with
# multiple users, add an auth header check.
@app.get("/api/set-token")
async def set_token_get(token: str, x_admin_key: Optional[str] = None):
    """Set Quotex token at runtime — no restart needed.

    Usage: open in browser:
    https://binary-signals-app-production.up.railway.app/api/set-token?token=YOUR_TOKEN

    FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-1-08): optional shared-secret
    admin-key check. If ADMIN_KEY env var is set, callers MUST pass the
    matching X-Admin-Key header (or query param) or get 403. If ADMIN_KEY
    is unset, the endpoint stays open (backwards-compatible for personal
    deployments).

    FIX (DEEP-AUDIT-2026-07-26 / F-13-26, F-13-42): replaced the
      `provided != expected` comparison with the constant-time
      `hmac.compare_digest` helper (`_check_admin_key`) and corrected
      the type annotation from `str = None` to `Optional[str] = None`
      so the generated OpenAPI schema is accurate.
    """
    _check_admin_key(x_admin_key)
    return await _apply_token(token)


@app.post("/api/set-token")
async def set_token_post(request: Request):
    """Set Quotex token via POST (for programmatic updates).

    Body: {"token": "YOUR_TOKEN"}

    FIX (DEEP-AUDIT-2026-07-26 / A-07-CRIT-1):
    The GET version of this endpoint checked the X-Admin-Key header
    (AUDIT-1-08 fix), but the POST version did NOT — anyone could
    overwrite the Quotex token via POST without authentication. This
    is a security hole for any deployment with ADMIN_KEY set. Now the
    POST version applies the same gate, accepting the key via either
    the X-Admin-Key header or the JSON body's `x_admin_key` field.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-26): the comparison now goes
      through the constant-time `_check_admin_key` helper instead of
      `provided != expected`.
    """
    try:
        body = await request.json()
        token = body.get("token", "").strip()
        body_admin_key = body.get("x_admin_key", "").strip() if isinstance(body, dict) else ""
    except Exception:
        return {"ok": False, "error": "invalid JSON body"}
    # FIX (A-07-CRIT-1 / F-13-26): apply ADMIN_KEY gate (constant-time).
    header_admin_key = (request.headers.get("X-Admin-Key") or "").strip()
    _check_admin_key(body_admin_key or header_admin_key)
    return await _apply_token(token)


async def _apply_token(token: str):
    """Apply a new Quotex token at runtime.

    Sets the token in os.environ and forces an immediate reconnect on
    the next feed loop tick.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-47, F-13-48, F-13-49):
      Removed the entire `sim_delegate` cleanup block — sim mode is
      permanently disabled (server.py:33-45), so the defensive cleanup
      was dead code that added ~50ms latency to every token update.
      Also removed the redundant `os.environ["USE_SIM"] = "0"` line
      (USE_SIM is ignored) and the local `import os as _os` /
      `import time as _time` aliases — both modules are now imported
      once at the top of the file.
    """
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-1-05): raise minimum length
    # from 10 to 50. Real Quotex SSID tokens are JWT-style (200+ chars).
    # A 10-char minimum let garbage like "aaaaaaaaaa" through, which then
    # wasted 30s on a doomed authorization attempt before being cleared.
    if not token or len(token) < 50:
        return {"ok": False, "error": "token too short "
                "(real Quotex tokens are 200+ chars)"}

    # Set the token in the environment so _connect() picks it up
    os.environ["QX_TOKEN"] = token

    # Reset the real feed's connection state so it retries immediately.
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-1-03): set _token_update_pending
    # so feed._connect() (if currently suspended in await client.connect())
    # discards the in-flight attempt with the OLD token and retries with the
    # new one. Without this, an in-flight connect succeeds with the old
    # token and overwrites _connected=True, silently losing the update.
    feed._connected = False
    feed._abandoned = False
    feed._reconnect_attempts = 0
    feed._last_error = None
    feed._last_error_time = 0
    # FIX (DEEP-AUDIT-2026-07-26 / F-13-29): setting an attribute on a
    #   plain Python object essentially never raises; the previous
    #   `try/except Exception: pass` was dead defensive code that would
    #   have masked any real error in a future feed subclass overriding
    #   __setattr__. If feed ever does override __setattr__ we want to
    #   know loudly, so the try/except is removed.
    feed._token_update_pending = True

    # Clear any stale idle_since timestamps on existing streams.
    # FIX (DEEP-AUDIT-2026-07-26 / F-13-28): use getattr-default + setattr
    #   so a future stream subclass without `idle_since` no longer raises
    #   AttributeError and aborts the token update halfway through.
    for s in getattr(feed, '_streams', {}).values():
        try:
            s.idle_since = None
        except (AttributeError, TypeError):
            pass

    # Save token to session.json so it persists across restarts
    # AUDIT-1-02 FIX: was calling save_session_json(token) with wrong arg count
    try:
        from quotex_ws import QuotexWSClient
        QuotexWSClient.save_token_only(token)
        print(f"[server] token saved to session.json ({token[:8]}...)")
    except Exception as e:
        print(f"[server] session.json save error: {e}")

    token_preview = token[:8] + "..." + token[-4:] if len(token) > 12 else token
    print(f"[server] ✅ token updated at runtime: {token_preview}")
    print(f"[server]    real feed will reconnect within 5s...")

    return {
        "ok": True,
        "message": f"Token set ({token_preview}). Real feed reconnecting...",
        "timestamp": time.time(),
        "sim_mode_disabled_permanently": True,
        "next_step": "Wait 5-10 seconds, then check /api/debug to confirm connected:true",
    }


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
    # FIX (DATA-FLOW-2026-07-22): alltime_otc category — no payout floor.
    if cat == "alltime_otc":
        return {
            "category": "alltime_otc",
            "pairs": all_pairs.get("alltime_otc_pairs", []),
            "payout_floor": 0,
        }
    raise HTTPException(
        status_code=404,
        detail=f"unknown category {category!r}; expected 'real', 'otc', or 'alltime_otc'")


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
async def debug_info(request: Request):
    """Diagnostic endpoint — shows connection state, stream status, errors.
    Visit /api/debug in browser to see why candles aren't coming.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-60): this endpoint exposes env-var
      presence, signal delays and payout floors — information leak. Now
      gated behind ADMIN_KEY (same shared-secret as /api/set-token) when
      that env var is set. Backwards-compatible: with no ADMIN_KEY set,
      the endpoint stays open for personal deployments.
    FIX (DEEP-AUDIT-2026-07-26 / F-13-49): removed the local
      `import time as _time` — `time` is now imported at module top.
    """
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
            "SIGNAL_DELAY_SEC": os.environ.get("SIGNAL_DELAY_SEC", "3.0"),
        },
    }
    # Stream details
    # FIX (CANDLE-STUCK-FIX, 2026-07-23): merge streams from BOTH the real
    # feed AND the sim delegate (if active). Previously when the real feed
    # fell back to sim mode, /api/debug showed streams: {} because it only
    # read feed._streams (real feed's empty dict). This made it impossible
    # to diagnose why candles weren't updating — the user saw 0 streams
    # even though the sim delegate had active streams producing ticks.
    def _collect_streams(feed_obj):
        out = {}
        if hasattr(feed_obj, '_streams'):
            for key, s in feed_obj._streams.items():
                out[f"{key[0]}@{key[1]}s"] = {
                    "candles_count": len(s.candles) if hasattr(s, 'candles') else 0,
                    "ticks_count": len(s.ticks) if hasattr(s, 'ticks') else 0,
                    "last_real_tick_wall": getattr(s, 'last_real_tick_wall', 0),
                    "always_on": getattr(s, 'always_on', False),
                    "interested_cids": list(getattr(s, 'interested_cids', set())),
                    "sub_started": getattr(s, 'sub_started', False),
                    "source": "real_feed",
                }
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
    Visit /api/stats in browser to see which modules are performing well.

    FIX (BUG-3, 2026-07-18): previously had a local MODULE_NAMES dict that
    was missing `trend_follow` (the Real engine's 6th module), silently
    undercounting Real-engine signals in the per-module report. Now uses
    the shared `core.stats.compute_module_stats()` which sources module
    names from `core.constants.MODULE_NAMES` — the single source of truth.

    FIX (BUG-I, 2026-07-20): also reports DB-adaptation status — which
    pairs have enough graded samples for adaptation, and what the current
    learned weights are.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-22, F-13-61, F-13-62, F-13-63):
      The adaptation loop previously called N synchronous DB queries per
      pair inside an async handler, blocking the event loop. The entire
      stats computation now runs in `asyncio.to_thread`. Also: dedup the
      asset list (an asset could appear in both OTC and Real configs);
      on exception, log the full traceback server-side and return a
      generic `adaptation_error: "internal error"` instead of leaking
      the raw Python exception string (which includes module paths and
      line numbers — information leak).
    """
    def _compute_stats_with_adaptation():
        from core.stats import compute_module_stats
        stats = compute_module_stats(_db.DB_PATH)
        try:
            from engines.otc.config import weight_adapter as _otc_adapter
            from engines.real.config import weight_adapter as _real_adapter
            adaptation_status = {}
            # FIX (F-13-62): dedup assets that may appear in both adapters.
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
            # FIX (F-13-61): log full exception server-side; return generic
            # message to client so module paths / line numbers aren't leaked.
            _logger.exception("adaptation status computation failed")
            stats["adaptation_error"] = "internal error"
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
    # FIX (DEEP-AUDIT-2026-07-26 / F-13-21): clamp `limit` to [1, 500] so
    #   `?limit=10000000` can't allocate unbounded memory.
    safe_limit = max(1, min(int(limit), 500))
    from core.brain import get_insights
    return {"insights": get_insights(active_only=True, limit=safe_limit)}


@app.get("/api/brain/learning")
async def brain_learning(asset: Optional[str] = None, limit: int = 100):
    """Get learned weights per pair per module."""
    # FIX (DEEP-AUDIT-2026-07-26 / F-13-21, F-13-41): clamp `limit` to
    #   [1, 500] and correct `asset` type annotation from `str = None` to
    #   `Optional[str] = None` so the generated OpenAPI schema is accurate.
    safe_limit = max(1, min(int(limit), 500))
    from core.brain import get_learning
    return {"learning": get_learning(asset=asset, limit=safe_limit)}


@app.get("/api/brain/analyze")
async def brain_analyze(request: Request):
    """Trigger brain analysis manually.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-2): added ADMIN_KEY gate (same as
      /api/set-token) and an `asyncio.Lock` so two concurrent calls can't
      double-recompute. Endpoint signature & HTTP method unchanged for
      clients without ADMIN_KEY — backwards-compatible. (A-07 noted these
      endpoints should ideally be POST mutations, but the original route
      was GET and changing it would break existing clients — left as-is.)
    """
    _check_admin_key(request.headers.get("X-Admin-Key"))
    from core.brain import analyze_and_learn
    async with _BRAIN_ANALYZE_LOCK:
        await asyncio.to_thread(analyze_and_learn)
    return {"status": "analysis complete"}


# ── Pattern endpoints (BACKTEST-2026-07-21) ────────────────────────────────

def _ensure_patterns_init():
    """Lazily init patterns once at module level.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-64): `/api/patterns` and `/api/patterns/{asset}`
      both used to call `init_patterns()` defensively on every request. Now
      a module-level flag (`_PATTERNS_INITIALIZED`, set in lifespan) tracks
      whether startup init succeeded; if not yet set, we re-attempt here so
      the endpoints still work after a failed startup (backwards compat).
    """
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


@app.post("/api/patterns/refresh")
async def patterns_refresh(request: Request):
    """Recompute ALL patterns from signal_log. Call after backtest or manually.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-2, F-13-65): ADMIN_KEY gate +
      `asyncio.Lock` so concurrent refresh calls can't double-recompute.
      `recompute_from_signal_log` now called with `min_samples=3` for
      consistency with the startup call at lifespan.
    """
    _check_admin_key(request.headers.get("X-Admin-Key"))
    _ensure_patterns_init()
    from core.time_patterns import recompute_from_signal_log
    async with _PATTERNS_REFRESH_LOCK:
        # FIX (F-13-65): match startup's `min_samples=3` so the refresh
        # doesn't produce a different pattern set than the boot-time recompute.
        summary = await asyncio.to_thread(recompute_from_signal_log, 3)
    return {"status": "refreshed", "summary": summary}


# ── Algorithm-change endpoints (DATA-FLOW-2026-07-22) ──────────────────────

@app.get("/api/strategies")
async def get_strategies():
    """List all available trading strategies."""
    from core.algorithm_strategy import get_all_strategies
    return {"strategies": get_all_strategies()}


@app.get("/api/current-strategy")
async def get_current_strategy(asset: Optional[str] = None):
    """Get the current trading strategy for one or all assets.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-41): correct `asset` type annotation
      from `str = None` to `Optional[str] = None` so the generated OpenAPI
      schema marks the param as optional instead of required.
    """
    from core.algorithm_strategy import get_asset_strategy_summary
    return get_asset_strategy_summary(asset)


@app.get("/api/auto-tune")
async def auto_tune_report():
    """Show current module win rates and auto-tuned weights."""
    from core.auto_tune import get_tuning_report
    return get_tuning_report()


@app.post("/api/auto-tune/apply")
async def auto_tune_apply(request: Request):
    """Manually trigger auto-tune weight recalculation.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-2): ADMIN_KEY gate + `asyncio.Lock`
      so two concurrent apply calls can't double-write weights.
    """
    _check_admin_key(request.headers.get("X-Admin-Key"))
    from core.auto_tune import apply_tuned_weights_to_engines
    async with _AUTO_TUNE_APPLY_LOCK:
        result = await asyncio.to_thread(apply_tuned_weights_to_engines)
    return {"status": "applied", "weights": result}


@app.get("/api/algorithm-changes")
async def algorithm_changes(hours: int = 24, limit: int = 100):
    """Recent algorithm changes across all pairs (default: last 24h).

    FIX (DEEP-AUDIT-2026-07-26 / F-13-21, F-13-66): clamp `hours` to
      [1, 24*30] and `limit` to [1, 500] so negative hours / huge limits
      can't allocate unbounded memory.
    """
    safe_hours = max(1, min(int(hours), 24 * 30))
    safe_limit = max(1, min(int(limit), 500))
    from core.algorithm_monitor import get_recent_changes, get_change_summary
    changes = get_recent_changes(asset=None, hours=safe_hours, limit=safe_limit)
    summary = get_change_summary(asset=None, hours=safe_hours)
    return {"changes": changes, "summary": summary}


@app.get("/api/algorithm-changes/{asset}")
async def algorithm_changes_for_asset(asset: str, hours: int = 24, limit: int = 50):
    """Recent algorithm changes for one pair.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-21, F-13-66): same input clamping as
      the all-assets endpoint above.
    """
    safe_hours = max(1, min(int(hours), 24 * 30))
    safe_limit = max(1, min(int(limit), 500))
    from core.algorithm_monitor import get_recent_changes, get_current_state
    changes = get_recent_changes(asset=asset, hours=safe_hours, limit=safe_limit)
    state = get_current_state(asset)
    return {"asset": asset, "changes": changes, "current_state": state}


@app.get("/api/signals/{asset}/{period}")
async def get_signals(asset: str, period: int, limit: int = 100, before_ctime: Optional[int] = None):
    # FIX (AUDIT-CRITICAL #002, 2026-07-21): default limit raised from 50
    # to 100 to match the frontend's HISTORY_MAX=100. Also accepts
    # `before_ctime` for pagination (the frontend's "Load more" button
    # uses this to fetch older signals on demand).
    # FIX (DEEP-AUDIT-2026-07-26 / F-13-23, F-13-40): wrap the synchronous
    #   SQLite call in `asyncio.to_thread` so a 500-row cold-cache read
    #   doesn't block every other HTTP request. Also correct
    #   `before_ctime` type annotation from `int = None` to
    #   `Optional[int] = None` so the OpenAPI schema is accurate.
    safe_limit = max(1, min(int(limit), 500))
    signals = await asyncio.to_thread(
        _db.get_recent_signals, asset, period, safe_limit, before_ctime)
    return {"signals": signals}


@app.get("/api/signals/{asset}/{period}/{ctime}")
async def get_signal_detail(asset: str, period: int, ctime: int):
    """Return full detail for a single signal (win/loss reason, regime, etc.).

    FIX (DEEP-AUDIT-2026-07-26 / F-13-24): wrap the synchronous SQLite call
      in `asyncio.to_thread` to match the WS-side fix from AUDIT-CORE #7.
    """
    detail = await asyncio.to_thread(_db.get_signal_detail, asset, period, ctime)
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
# FIX (DEEP-AUDIT-2026-07-26 / F-13-67): moved this import to the top
#   of the module with the other top-level imports — having it here
#   after route definitions was unusual and meant an import failure
#   would surface only after partial app setup.
from core.constants import ALLOWED_PERIODS as _ALLOWED_PERIODS  # noqa: E402


def _ws_origin_allowed(ws: WebSocket) -> bool:
    """Check the WS Origin header against the module-level whitelist.

    FIX (DEEP-AUDIT-2026-07-26 / F-13-6): previously any website could
      open a WS to `/ws` (CSRF attack surface). Now the Origin header is
      validated against `_ALLOWED_WS_ORIGINS` (configured via the
      `ALLOWED_WS_ORIGINS` env var, default localhost+127.0.0.1). If
      the list is empty the check is skipped (NOT recommended).
    """
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
    return False


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global cid_counter

    # FIX (DEEP-AUDIT-2026-07-26 / F-13-6): reject cross-origin WS
    #   connections BEFORE accepting them, with WS code 1008 (policy
    #   violation).
    if not _ws_origin_allowed(ws):
        await ws.close(code=1008, reason="origin not allowed")
        return

    # FIX (DEEP-AUDIT-2026-07-26 / F-13-36): cap total concurrent WS
    #   clients so a botnet can't exhaust file descriptors. Reject with
    #   code 1013 (try again later) when the cap is hit.
    if len(clients) >= _MAX_WS_CLIENTS:
        try:
            await ws.close(code=1013, reason="server at max capacity")
        except Exception:
            pass
        return

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
    # FIX (DEEP-AUDIT-2026-07-26 / F-13-68): `_WS_IDLE_TIMEOUT` is now
    #   read once at module load instead of being re-parsed on every
    #   new connection.
    ws_idle_timeout = _WS_IDLE_TIMEOUT

    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=ws_idle_timeout)
            except asyncio.TimeoutError:
                print(f"[server] {cid} idle timeout ({ws_idle_timeout}s) — closing")
                try:
                    await ws.close(code=1008, reason="idle timeout")
                except Exception:
                    pass
                break
            # FIX (DEEP-AUDIT-2026-07-26 / F-13-7): enforce a max message
            #   size so a malicious client can't OOM the server with a
            #   1GB text frame. Reject with WS code 1009 (message too big)
            #   and skip processing.
            if len(raw) > _MAX_WS_MSG_BYTES:
                try:
                    await ws.close(code=1009, reason="message too big")
                except Exception:
                    pass
                print(f"[server] {cid} sent oversized message "
                      f"({len(raw)} > {_MAX_WS_MSG_BYTES} bytes) — closing")
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # FIX (DEEP-AUDIT-2026-07-26 / F-13-9): a JSON array or
            #   scalar would later crash `msg.get("type")` with
            #   AttributeError. Reject non-dict payloads explicitly.
            if not isinstance(msg, dict):
                await ws.send_text(json.dumps({
                    "type": "error",
                    "error": "expected a JSON object",
                }))
                continue

            t = msg.get("type")

            if t == "subscribe":
                asset = msg.get("asset", "")
                # FIX (DEEP-AUDIT-2026-07-26 / F-13-10): cap asset length
                #   to prevent 1MB asset names bloating the stream
                #   registry / SQLite params.
                if not isinstance(asset, str) or not asset or len(asset) > 32:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "error": "invalid or missing asset (max 32 chars)",
                    }))
                    continue
                # Validate period up-front. Reject anything not in the
                # whitelist with a clear error instead of letting the feed
                # create a bogus stream.
                # FIX (DEEP-AUDIT-2026-07-26 / F-13-11): reject non-integer
                #   `period` (e.g. 60.9 or "60s") explicitly instead of
                #   silently truncating / falling back to a default.
                period_val = msg.get("period", 60)
                if not isinstance(period_val, int) or isinstance(period_val, bool):
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "error": f"invalid period {period_val!r}; "
                                 f"must be an integer in {sorted(_ALLOWED_PERIODS)}",
                    }))
                    continue
                period = period_val
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
                # FIX (DATA-FLOW-2026-07-22): accept 'alltime_otc' as a 3rd
                # category. It routes to the OTC engine (asset ends with _otc).
                if category and category not in ("real", "otc", "alltime_otc"):
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "error": f"invalid category {category!r}; "
                                 f"expected 'real', 'otc', or 'alltime_otc'",
                    }))
                    continue
                # If category is specified, validate it matches the asset.
                # 'alltime_otc' and 'otc' both require asset to end with _otc.
                if category:
                    is_otc_asset = asset.endswith("_otc")
                    if category in ("otc", "alltime_otc") and not is_otc_asset:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "error": f"category/asset mismatch: category={category!r} "
                                     f"but asset {asset!r} is not an OTC pair "
                                     f"(must end with '_otc').",
                        }))
                        continue
                    if category == "real" and is_otc_asset:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "error": f"category/asset mismatch: category='real' "
                                     f"but asset {asset!r} is an OTC pair.",
                        }))
                        continue
                # FIX (DEEP-AUDIT-2026-07-26 / F-13-12): wrap ensure_stream
                #   in try/except so a failure sends a structured error
                #   back to the client instead of killing the connection.
                try:
                    result = await feed.ensure_stream(asset, period, cid=cid)
                except Exception as exc:
                    _logger.exception("ensure_stream failed for %s@%ss", asset, period)
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "error": f"stream setup failed: {type(exc).__name__}",
                    }))
                    continue
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
                # FIX (DEEP-AUDIT-2026-07-26 / F-13-10): same asset length
                #   cap as the subscribe branch.
                if not isinstance(asset, str) or not asset or len(asset) > 32:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "error": "invalid or missing asset (max 32 chars)",
                    }))
                    continue
                # FIX (DEEP-AUDIT-2026-07-26 / F-13-11, F-13-102): reject
                #   non-integer `period` instead of silently falling back
                #   to 60 (the old behavior hid client bugs and could
                #   route to the wrong stream).
                period_val = msg.get("period", 60)
                if not isinstance(period_val, int) or isinstance(period_val, bool):
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "error": f"invalid period {period_val!r}; "
                                 f"must be an integer in {sorted(_ALLOWED_PERIODS)}",
                    }))
                    continue
                period = period_val
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
                # FIX (AUDIT-CRITICAL #002, 2026-07-21): limit raised from
                # 50 to 100 to match the frontend's HISTORY_MAX. Also
                # supports `before_ctime` for pagination and a `limit`
                # field from the message (capped at 200).
                try:
                    req_limit = int(msg.get("limit", 100))
                except (TypeError, ValueError):
                    req_limit = 100
                req_limit = max(1, min(req_limit, 200))
                before_ctime = msg.get("before_ctime")
                # FIX (DEEP-AUDIT-2026-07-26 / F-13-13): if `before_ctime`
                #   is present but unparseable, send an error instead of
                #   silently coercing to None (which broke pagination
                #   cursors in the frontend).
                if before_ctime is not None:
                    try:
                        before_ctime = int(before_ctime)
                    except (TypeError, ValueError):
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "error": f"invalid before_ctime {before_ctime!r}; "
                                     f"must be an integer or null",
                        }))
                        continue
                sigs = await asyncio.to_thread(
                    _db.get_recent_signals, asset, period, req_limit, before_ctime)
                await ws.send_text(json.dumps({
                    "type": "signals",
                    "signals": sigs,
                    "asset": asset,
                    "period": period,
                    "before_ctime": before_ctime,
                }))

    except WebSocketDisconnect:
        print(f"[server] {cid} disconnected")
    except Exception as e:
        # FIX (DEEP-AUDIT-2026-07-26 / F-13-37): log full traceback via
        #   the module logger instead of just `str(e)` — debugging
        #   production WS issues was near-impossible without the stack.
        _logger.exception("%s ws error", cid)
        print(f"[server] {cid} error: {e}")
    finally:
        clients.pop(cid, None)
        # FIX (DEEP-AUDIT-2026-07-26 / F-13-14): wrap drop_interest in
        #   defensive try/except so a raised cleanup error doesn't
        #   swallow the original exception or leak the cid registration.
        try:
            await feed.drop_interest(cid)
        except Exception:
            _logger.exception("drop_interest failed for %s", cid)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    # FIX (DEEP-AUDIT-2026-07-26 / F-13-69): use `os.environ.get("PORT") or "8000"`
    #   so an empty-string PORT env var doesn't crash with `int("")`.
    port = int(os.environ.get("PORT") or "8000")
    # FIX (DEEP-AUDIT-2026-07-26 / F-13-70): also recognise the official
    #   Railway detection env var `RAILWAY_ENVIRONMENT`.
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
        # FIX (DEEP-AUDIT-2026-07-26 / F-13-71): on a local dev machine,
        #   bind to 127.0.0.1 by default so the app isn't exposed to the
        #   LAN. Override with `HOST=0.0.0.0` if you really want LAN access.
        host = os.environ.get("HOST", "127.0.0.1")
    # FIX (DEEP-AUDIT-2026-07-26 / F-13-72): allow `WEB_CONCURRENCY` to
    #   spin up multiple uvicorn workers (note: requires feed to be
    #   per-worker or shared via Redis pubsub — left as an ops decision).
    workers = int(os.environ.get("WEB_CONCURRENCY") or "1")
    uvicorn.run(app, host=host, port=port, log_level="info", workers=workers)