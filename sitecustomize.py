"""Python startup hook for the production persistence bridge.

Railway launches the app with the repository on sys.path, so Python loads
sitecustomize automatically during normal interpreter startup. The sync
worker waits before its first read, allowing server.py to finish SQLite
schema initialization first.
"""

try:
    import os
    # VERCEL-DEPLOY (2026-11): on the serverless API function
    # (QX_DISABLE_FEED=1, api/index.py) there is no persistent process,
    # so the mirror thread can never make progress — skip it there. The
    # burst worker (api/cron/refresh.py) leaves this flag unset.
    if os.environ.get("QX_DISABLE_FEED", "0") == "1":
        print("[sitecustomize] QX_DISABLE_FEED=1 — Supabase mirror thread "
              "skipped (serverless API function)")
    else:
        import supabase_sync
        supabase_sync.start()
except Exception as exc:
    # Never prevent the application from starting because an optional
    # persistence bridge is unavailable.
    print(f"[sitecustomize] Supabase sync bootstrap skipped: {exc}")
