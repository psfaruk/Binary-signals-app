"""Python startup hook for the production persistence bridge.

Railway launches the app with the repository on sys.path, so Python loads
sitecustomize automatically during normal interpreter startup. The sync
worker waits before its first read, allowing server.py to finish SQLite
schema initialization first.
"""

try:
    import supabase_sync
    supabase_sync.start()
except Exception as exc:
    # Never prevent the application from starting because an optional
    # persistence bridge is unavailable.
    print(f"[sitecustomize] Supabase sync bootstrap skipped: {exc}")
