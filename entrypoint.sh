#!/bin/sh
# First boot: seed the persistent volume DB from the image (7MB bhavcopy
# history). Never overwrites an existing volume DB (trades/settings safe).
if [ ! -f /data/ratrade.db ] && [ -f /app/data/ratrade.db ]; then
  mkdir -p /data
  cp /app/data/ratrade.db /data/ratrade.db
  echo "[entrypoint] seeded /data/ratrade.db from image"
fi
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
