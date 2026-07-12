FROM python:3.11-slim

# ── System packages: Playwright Chromium deps + build tools ────────────────
# install-deps needs apt-get to work, so install system deps FIRST, then
# let playwright install-deps add anything missing.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install Python dependencies ───────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Install Playwright Chromium browser + its system deps ──────────────────
# Order matters: install-deps uses apt-get to install system libraries
# that Chromium needs. Then `playwright install chromium` downloads the
# browser binary itself.
RUN playwright install-deps chromium \
    && playwright install chromium

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
