#!/usr/bin/env bash
# Entry point for persistent-host deploys (VPS / fly.io / Render / …).

# VERCEL-DEPLOY (2026-11): Railway config removed (railway.json deleted —
# the Vercel deployment lives in vercel.json + api/). This script remains
# for any host with a long-running process, where the live Quotex feed can
# stream continuously. Geo note preserved below for whoever runs the
# minter role: Quotex geo-blocks the trade page by country, so mint the
# SSID from a non-blocked region and hand the token to the app.
#
# Two services can share this image and differ only by QX_SERVICE_ROLE:
#
#   (unset)  the signals app  — owns the volume + signals.db
#   minter   the token minter — no volume
set -euo pipefail

if [ "${QX_SERVICE_ROLE:-}" = "minter" ]; then
  echo "[run_service] role=minter — starting the Quotex token minter"
  exec python scripts/minter_service.py
fi

echo "[run_service] role=app — starting the signals server"
exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8080}"
