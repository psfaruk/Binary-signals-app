FROM python:3.11-slim

# ── System dependencies for Playwright Chromium ───────────────────────────
# These are required for headless Chrome to run on Linux servers.
RUN apt-get update && apt-get install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libxshmfence1 \
    fonts-liberation \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install Python dependencies ───────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Install Playwright Chromium browser ───────────────────────────────────
RUN playwright install chromium
RUN playwright install-deps chromium

# ── Copy application code ─────────────────────────────────────────────────
COPY . .

# ── Railway sets PORT env var; server.py reads it ─────────────────────────
EXPOSE 8000

# ── Disable auto browser open (no display on Railway) ─────────────────────
ENV AUTO_OPEN_BROWSER=0
ENV HEADLESS=1
ENV QX_USE_RAW_WS=1

CMD ["python", "server.py"]
