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

# ── Railway sets PORT env var; server.py reads it ─────────────────────────
EXPOSE 8000

# ── Railway environment defaults ──────────────────────────────────────────
ENV AUTO_OPEN_BROWSER=0
ENV HEADLESS=1
ENV QX_USE_RAW_WS=1
ENV PYTHONUNBUFFERED=1

CMD ["python", "server.py"]
