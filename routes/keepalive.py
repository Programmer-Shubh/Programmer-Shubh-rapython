"""Keep-alive endpoints so a free cron-job.org account can ping the site
and stop Render's free tier from sleeping.

Setup (2 minutes, free):
1. Create account at https://cron-job.org (free plan = 50 cronjobs).
2. Create cronjob -> Title "RaTrade keep-alive" -> Address:
      https://ratrade.onrender.com/api/keepalive/ping
   -> every 5 minutes.
3. Save + enable. Render stays awake while pings arrive.

If SELF_URL env var is set (e.g. https://ratrade.onrender.com), the app also
self-pings every 9 minutes as a backup.
"""
import time
from fastapi import APIRouter

router = APIRouter()
_STARTED = time.time()


@router.get("/ping")
def ping():
    return {"status": "ok", "uptime_sec": round(time.time() - _STARTED, 1),
            "timestamp": time.time()}


@router.get("/info")
def info():
    return {
        "ping_url": "/api/keepalive/ping (full: https://ratrade.onrender.com/api/keepalive/ping)",
        "recommended_interval": "every 5 minutes",
        "provider": "https://cron-job.org (free)",
        "steps": [
            "1. Sign up free at cron-job.org",
            "2. Create cronjob titled 'RaTrade keep-alive'",
            "3. Address = https://ratrade.onrender.com/api/keepalive/ping",
            "4. Schedule = every 5 minutes, then Save + Enable",
        ],
    }


async def self_ping_loop():
    """Backup self-ping when SELF_URL is configured. Runs forever in background."""
    import asyncio
    import os
    base = (os.environ.get("SELF_URL") or "").rstrip("/")
    if not base:
        return
    try:
        import urllib.request
    except Exception:
        return
    import asyncio as _aio

    def _hit():
        try:
            urllib.request.urlopen(base + "/api/keepalive/ping", timeout=10).read(64)
        except Exception:
            pass

    while True:
        try:
            await _aio.sleep(9 * 60)
            await _aio.to_thread(_hit)
        except Exception:
            await _aio.sleep(60)
