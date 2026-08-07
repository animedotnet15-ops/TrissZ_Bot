FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY . .

# SQLite database lives here — mount a volume on this path in production
# (Railway/Render both support persistent volumes) or it resets on redeploy.
ENV DATABASE_PATH=/app/data/bot.db
RUN mkdir -p /app/data

CMD ["python", "main.py"]
