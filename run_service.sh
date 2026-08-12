#!/usr/bin/env bash
# Entry point for every Railway service built from this repo.
#
# Two services share this image and differ only by QX_SERVICE_ROLE:
#
#   (unset)  the signals app  — us-west, owns the volume + signals.db
#   minter   the token minter — southeast-asia, no volume
#
# The split exists because Quotex geo-blocks by country: from us-west the
# trade page literally returns "Quotex is currently not available in your
# region. (United States)", so the app cannot mint its own SSID there. The
# WebSocket is not geo-blocked, so the app keeps streaming from us-west with
# a token the minter hands it. See scripts/minter_service.py.
set -euo pipefail

if [ "${QX_SERVICE_ROLE:-}" = "minter" ]; then
  echo "[run_service] role=minter — starting the Quotex token minter"
  exec python scripts/minter_service.py
fi

echo "[run_service] role=app — starting the signals server"
exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8080}"
