FROM python:3.11.9-slim

WORKDIR /app

# System deps for building numpy/pandas if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure data folder exists (will be overwritten by volume if mounted)
RUN mkdir -p data

# Koyeb sets PORT env, default 8000 for local
ENV PORT=8000
EXPOSE 8000

# Health check uses /health
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('PORT','8000') + '/health')" || exit 1

CMD uvicorn main:app --host 0.0.0.0 --port $PORT
