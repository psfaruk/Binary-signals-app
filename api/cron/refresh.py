"""api/cron/refresh.py — Vercel Cron burst worker for the Binary Signals App.

VERCEL-DEPLOY (2026-11). A serverless platform cannot keep the Quotex
WebSocket feed alive, so the LIVE signal engine runs in bounded bursts:

    1. pull durable history  (Supabase → /tmp SQLite — re-arms the
       history-gate / pair-health / pattern learning with every past row)
    2. run the REAL feed      (feed.run with the server's broadcast) for
       QX_CRON_BUDGET seconds — signals are generated, graded and logged
       exactly like on a persistent host
    3. mirror back            (supabase_sync.sync_once, also every 60 s so
       a hard wall-clock kill still keeps most of the burst)

Every invocation starts with a fresh /tmp, so Supabase is the ONLY durable
memory. Without SUPABASE_URL/KEY the burst still runs and returns counts,
but nothing survives the invocation.

Environment (Vercel → Settings → Environment Variables):
    QX_TOKEN            Quotex SSID token (required; expires ~daily)
    QX_CRON_BUDGET      seconds inside the burst (default 240, max 540 —
                        keep below the function's maxDuration minus ~30 s)
    CRON_SECRET         shared secret (?secret=… or Bearer) — set it!
    SUPABASE_URL        https://<project>.supabase.co
    SUPABASE_ANON_KEY   anon/publishable key (tables per DEPLOYMENT_VERCEL.md)

Vercel Cron invokes this path on schedule (vercel.json → crons; the
x-vercel-cron: 1 header is accepted when CRON_SECRET is set). Manual run:

    curl "https://<app>.vercel.app/api/cron/refresh?secret=<CRON_SECRET>"
"""
import asyncio
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Ephemeral per-invocation database (separate file from the API function's
# /tmp cache — this instance owns a live feed for the whole burst).
os.environ.setdefault("DB_PATH", "/tmp/cron_signals.db")
os.environ.setdefault("AUTO_OPEN_BROWSER", "0")
os.environ.setdefault("QX_SKIP_DOTENV", "1")
os.environ.setdefault("QX_ROOT", "/tmp/plybit_cache")
# NOTE: QX_DISABLE_FEED is deliberately NOT set — the feed is the point.

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

# Importing server boots every real module (db, feed, engines) and gives us
# the production singletons: _server.feed + _server.broadcast.
import server as _server  # noqa: E402
import db as _db  # noqa: E402

app = FastAPI()


def _authorized(request: Request) -> bool:
    """CRON_SECRET gates the endpoint. With no secret configured the
    endpoint is open (documented risk — anyone can trigger a burst);
    always set CRON_SECRET in production."""
    secret = os.environ.get("CRON_SECRET", "").strip()
    if not secret:
        return True
    if request.query_params.get("secret", "").strip() == secret:
        return True
    auth = request.headers.get("authorization", "").strip()
    if auth == f"Bearer {secret}":
        return True
    # Header sent by the Vercel Cron scheduler itself.
    return request.headers.get("x-vercel-cron", "").strip() == "1"


def _sync_once() -> dict:
    import supabase_sync
    if not supabase_sync.enabled():
        return {}
    return supabase_sync.sync_once()


@app.get("/api/cron/refresh")
async def refresh(request: Request):
    t0 = time.time()
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        budget = int(os.environ.get("QX_CRON_BUDGET", "240"))
    except ValueError:
        budget = 240
    budget = max(30, min(budget, 540))

    if not os.environ.get("QX_TOKEN", "").strip():
        return JSONResponse({
            "error": "QX_TOKEN not set",
            "hint": ("Set the Quotex SSID env var (Vercel → Settings → "
                     "Environment Variables). Tokens expire roughly daily — "
                     "update the env var to re-deploy with a fresh token."),
        }, status_code=503)

    # ── 1. one-time static init (same tables/gates as the real server) ─────
    await asyncio.to_thread(_server._boot_static_init)

    # ── 2. durable history read-back so the gates start smart ─────────────
    pulled: dict = {}
    try:
        import core.supabase_pull as _pull
        pulled = await asyncio.wait_for(
            asyncio.to_thread(_pull.pull, True), timeout=15.0)
    except Exception as _exc:
        print(f"[cron] history pull skipped (non-fatal): "
              f"{type(_exc).__name__}: {_exc}")

    # ── 3. periodic Supabase push (survives hard wall-clock kills) ────────
    async def _pusher() -> None:
        while True:
            await asyncio.sleep(60.0)
            try:
                await asyncio.to_thread(_sync_once)
            except Exception as _exc:
                print(f"[cron] periodic sync failed (non-fatal): {_exc}")

    pusher = asyncio.create_task(_pusher())

    # ── 4. the burst: run the REAL feed for the budget window ─────────────
    print(f"[cron] burst started — budget {budget}s, "
          f"db={_db.DB_PATH}")
    feed_task = asyncio.create_task(_server.feed.run(_server.broadcast))
    try:
        await asyncio.sleep(budget)
    except asyncio.CancelledError:
        pass
    try:
        await asyncio.wait_for(_server.feed.shutdown(), timeout=20.0)
    except Exception as _exc:
        print(f"[cron] feed shutdown issue (non-fatal): {_exc}")
    feed_task.cancel()
    try:
        await feed_task
    except BaseException as _exc:  # noqa: BLE001 — expected CancelledError
        print(f"[cron] feed task ended: {type(_exc).__name__}")
    pusher.cancel()

    # ── 5. final mirror to the durable store ──────────────────────────────
    synced: dict = {}
    try:
        synced = await asyncio.to_thread(_sync_once)
    except Exception as _exc:
        print(f"[cron] final sync failed: {_exc}")

    return JSONResponse({
        "ok": True,
        "budget_sec": budget,
        "wall_sec": round(time.time() - t0, 1),
        "history_pulled": pulled,
        "synced_to_supabase": synced,
        "supabase_enabled": bool(os.environ.get("SUPABASE_URL", "").strip()),
        "note": ("Burst complete. Signals now live in Supabase — the app "
                 "UI reads them via its REST polling."),
    })
