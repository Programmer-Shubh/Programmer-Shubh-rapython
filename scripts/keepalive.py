"""Render Free sleep workaround - ping /health every 10 min.
Run locally (python scripts/keepalive.py) or as GitHub Action. No server change needed."""
import time, urllib.request, os
URL = os.getenv("KEEPALIVE_URL", "https://ratrade.onrender.com/health")
INTERVAL = 600  # 10 min < Render 15 min sleep
while True:
    try:
        with urllib.request.urlopen(URL, timeout=10) as r:
            print(f"[{time.strftime('%H:%M:%S')}] {r.status} {r.read()[:80]}")
    except Exception as e:
        print(f"ping fail: {e}")
    time.sleep(INTERVAL)
