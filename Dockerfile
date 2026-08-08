FROM python:3.12-slim

WORKDIR /app

# System deps for aiosqlite/motor TLS etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# database.db lives at this path so it survives container restarts -
# mount a Railway Volume at /app/data in your Railway project settings
# (Settings → Volumes → Add Volume → mount path "/app/data"). Docker's
# VOLUME instruction isn't supported by Railway's builder, so the mount
# is configured there instead of here.
ENV DATABASE_PATH=/app/data/database.db

EXPOSE 8000

# Basic container-level healthcheck against the FastAPI /health endpoint
# that main.py now actually starts (previously unwired).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
    urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4) or sys.exit(0)" || exit 1

CMD ["python", "main.py"]
