# Official Playwright Python image: bundled Chromium + all OS deps preinstalled.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

# Install Python deps first (better layer caching), then ensure the Chromium build
# matching the pip-installed Playwright version is present.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

# App code
COPY . .

# Screenshots live here and are served at /shots; keep it writable.
RUN mkdir -p /app/shots

ENV HOST=0.0.0.0 \
    PORT=8080 \
    HEADLESS=true \
    NO_SANDBOX=true \
    PREFER_CHROME=false \
    MAX_CONCURRENCY=10 \
    NAV_TIMEOUT_MS=45000 \
    SHOTS_DIR=/app/shots \
    SHOTS_TTL_HOURS=24

EXPOSE 8080

CMD ["python", "server.py"]
