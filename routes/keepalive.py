"""Keep-alive: Render free sleeps after 15 min idle, killing auto-paper/SL
threads while the browser is closed. Two layers (either suffices):

1. SELF-PING (automatic, zero setup): the app pings its own public URL
   (RENDER_EXTERNAL_URL, or SELF_URL env) every 9 minutes, so the process
   never idles out. Starts on boot, no user action needed.
2. EXTERNAL cron (backup): GitHub keep-alive.yml / cron-job.org hitting
   /api/keepalive/ping wakes a sleeping service; then layer 1 keeps it up.
   (GitHub throttles schedules to a few runs/day - NOT sufficient alone.)

Endpoints: /api/keepalive/ping (wake), /api/keepalive/info (setup text).
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
        "ping_url": "/api/keepalive/ping (full: https://ratrade-iwi2.onrender.com/api/keepalive/ping)",
        "recommended_interval": "every 5 minutes",
        "provider": "https://cron-job.org (free)",
        "steps": [
            "1. Sign up free at cron-job.org",
            "2. Create cronjob titled 'RaTrade keep-alive'",
            "3. Address = https://ratrade-iwi2.onrender.com/api/keepalive/ping",
            "4. Schedule = every 5 minutes, then Save + Enable",
        ],
    }


async def self_ping_loop():
    """Backup self-ping: every 9 min (< Render 15-min idle sleep) so the
    process + auto-paper/SL threads stay alive with the browser closed.
    URL: SELF_URL env, else Render's own RENDER_EXTERNAL_URL (zero setup).
    GitHub crons alone are NOT enough (throttled to ~2-5 runs/day)."""
    import asyncio
    import os
    base = ((os.environ.get("SELF_URL") or "").strip()
            or (os.environ.get("RENDER_EXTERNAL_URL") or "").strip()).rstrip("/")
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
