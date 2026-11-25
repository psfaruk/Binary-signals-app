# =============================================================================
# Dockerfile — RAILWAY / any long-running host (2026-11-26)
# =============================================================================
# WHY RAILWAY (not Vercel serverless) — architecture truth:
#   * The app holds a PERSISTENT Quotex WebSocket (quotex_ws.py) plus
#     per-candle timers and an in-memory stream registry. Serverless
#     functions (Vercel) freeze/kill between requests — the feed dies.
#     A long-running Railway worker is the correct platform.
#   * SQLite (db.py) needs a persistent Volume: db.py auto-detects
#     /app/data and /data mounts. Mount the Railway Volume at /app/data
#     or the 2026-09-09 incident (14k+ signal rows wiped on redeploy)
#     repeats.
#   * run_service.sh already honors $PORT and the QX_SERVICE_ROLE=minter
#     second-service pattern (token minting from a Quotex-served region).
#
# Root by design: Railway mounts Volumes root-owned; a non-root user
# silently loses DB writes and falls back to the ephemeral container FS
# (the exact data-loss failure db.py warns about). Railway containers
# are isolated anyway; the app's own Phase-J hardening handles auth.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

# ca-certificates: Quotex/Binance TLS; curl: container-level debugging.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --no-install-suggests \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (layer cache).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code (.dockerignore excludes .git / .env / *.db / logs).
COPY . .

# Volume mount point — db.py auto-detects this and keeps signals.db here.
RUN mkdir -p /app/data

EXPOSE 8080

# Container-level liveness (Railway also probes /healthz via railway.toml).
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# QX_SERVICE_ROLE=minter (second service) starts the token minter instead.
CMD ["bash", "run_service.sh"]
