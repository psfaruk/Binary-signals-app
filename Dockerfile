FROM python:3.11-slim

# ── Lightweight: no Playwright, no Chromium, no browser deps ───────────────
# This app is pure Python + WebSocket. Total RAM usage <50MB.
# Fits Railway free tier (512MB) easily with room to spare.

WORKDIR /app

# ── Install Python dependencies ───────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy application code ─────────────────────────────────────────────────
COPY . .

# ── Railway sets PORT env var dynamically — server.py reads it ────────────
# Don't hardcode EXPOSE — Railway injects its own PORT
ENV PYTHONUNBUFFERED=1
ENV AUTO_OPEN_BROWSER=0
ENV HEADLESS=1
ENV QX_USE_RAW_WS=1

# Use shell form so $PORT is interpolated at runtime (not build time)
CMD python server.py
