# RaTrade on Fly.io (free shared-1x, always-on). Python 3.11 to match runtime.txt.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/ratrade.db \
    MARKET_MODE=auto

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + small DB seed (7MB bhavcopy history = instant backtests).
COPY main.py ./
COPY core ./core
COPY routes ./routes
COPY scripts ./scripts
COPY static ./static
COPY utils ./utils
COPY data/ratrade.db ./data/ratrade.db

EXPOSE 8000

COPY entrypoint.sh ./
CMD ["sh", "entrypoint.sh"]
