"""api/index.py — Vercel serverless entry for the Binary Signals App.

VERCEL-DEPLOY (2026-11). Vercel routes EVERY path through this single ASGI
function (see vercel.json → routes). The full FastAPI app from server.py is
exposed with two serverless adaptations:

  1. QX_DISABLE_FEED=1 — the persistent Quotex feed task, the backup
     scheduler and the Supabase mirror thread are NOT started. Serverless
     functions have no long-running process, so the live signal engine
     runs only in bounded bursts inside api/cron/refresh.py (driven by
     Vercel Cron). The UI's WebSocket connect fails silently and its
     built-in 10-second REST polling takes over.

  2. A boot middleware guarantees the one-time static init (DB schema,
     gates, brain tables) even when the platform's ASGI adapter does not
     deliver the FastAPI lifespan protocol — and pulls the durable signal
     history back from Supabase so the read endpoints have data.

Ephemeral storage note: every serverless instance starts with an empty
/tmp. SQLite there is a per-instance cache. SUPABASE_URL +
SUPABASE_ANON_KEY (Supabase project URL + anon/publishable key) make the
app durable — see DEPLOYMENT_VERCEL.md for the one-time Supabase setup.
"""
import asyncio
import os
import sys

# ── Serverless guards — MUST be set before `import server` ─────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("QX_DISABLE_FEED", "1")       # no feed/threads here
os.environ.setdefault("DB_PATH", "/tmp/signals.db")  # ephemeral per-instance
os.environ.setdefault("AUTO_OPEN_BROWSER", "0")      # no desktop on serverless
os.environ.setdefault("QX_SKIP_DOTENV", "1")         # env comes from platform
os.environ.setdefault("QX_ROOT", "/tmp/plybit_cache")

from server import app  # noqa: E402  — full FastAPI app (static UI + REST)

from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402

# ── Serverless boot ────────────────────────────────────────────────────────
# Runs ONCE per instance: identical init to the real server boot, plus the
# Supabase history pull. If the platform adapter already delivered the
# FastAPI lifespan (detected via app.state._lifespan_ran), the init part is
# skipped — that lifespan path executes the same _boot_static_init().
_BOOT_LOCK = asyncio.Lock()
_BOOTED = False


async def _ensure_boot() -> None:
    global _BOOTED
    if _BOOTED:
        return
    async with _BOOT_LOCK:
        if _BOOTED:
            return
        if not getattr(app.state, "_lifespan_ran", False):
            import server as _server
            await asyncio.to_thread(_server._boot_static_init)
        # Best-effort durable history pull. Bounded so the first request is
        # never blocked for long — the UI polls again in 10 s anyway.
        try:
            import core.supabase_pull as _pull
            await asyncio.wait_for(
                asyncio.to_thread(_pull.pull, True), timeout=12.0)
        except Exception as _exc:
            print(f"[vercel] supabase history pull skipped "
                  f"(non-fatal): {type(_exc).__name__}: {_exc}")
        _BOOTED = True


class _ServerlessBootMiddleware(BaseHTTPMiddleware):
    """Guarantees _ensure_boot() ran before the first request is served."""

    async def dispatch(self, request: Request, call_next):
        await _ensure_boot()
        return await call_next(request)


app.add_middleware(_ServerlessBootMiddleware)
