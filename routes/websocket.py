"""Live market WebSockets - shared broadcasters (Render 512MB / 0.15 CPU).

Before this module was refactored every browser connection ran its own
fetch loop:

* ``/live``  -> one 8-worker ThreadPoolExecutor + 8 network calls every 2s
* ``/chain`` -> a full 3-query DB chain rebuild every 0.6s

With a few tabs open (or a proxy that reconnects) that alone could saturate
0.15 CPU and spike RAM. Now there is exactly ONE async broadcaster per data
stream for the whole process; connections are just subscribers. The shared
market-data executor (see ``live_market_data``) is bounded to 4 threads and
the live cache already has a 60s TTL, so most ticks cost no network at all.
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio
import json
import logging
import time

from core.services.live_market_data import LiveMarketData, _LIVE_CACHE
from core.models.bhavcopy_model import BhavcopyModel

router = APIRouter()
logger = logging.getLogger(__name__)

SPOT_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "HDFCBANK", "TCS", "INFY"]

# Hard cap: keep a misbehaving client / reconnect storm from eating memory.
MAX_CONNECTIONS = 20   # per stream
TICK_INTERVAL = 3      # seconds between broadcaster passes (was 2s per client)
HEARTBEAT_SEC = 8      # re-send unchanged snapshot this often (proxy keep-alive)
CHAIN_INTERVAL = 8     # seconds between chain passes (was 0.6s per client)
CHAIN_HEARTBEAT = 10   # keep chain sockets alive through the proxy too

# --- process-wide state ---------------------------------------------------
_connections: set = set()          # every accepted socket (for /stats)
_spot_subs: set = set()            # sockets subscribed to /live
_spot_task = None
_spot_prev: dict = {}
_spot_snapshot_text = None
_spot_snapshot_ts = 0.0

_chain_subs: dict = {}             # symbol -> set(WebSocket)
_chain_tasks: dict = {}            # symbol -> asyncio.Task
_chain_cache: dict = {}            # symbol -> (ts, payload_text)
_CHAIN_CACHE_TTL = 10


# --- helpers --------------------------------------------------------------

def _tick_from_data(sym: str, data: dict | None, prev_spot: float, now_ms: int) -> dict:
    spot = 0.0
    try:
        if data and data.get("spot"):
            spot = float(data["spot"])
    except Exception:
        spot = 0.0
    if spot <= 0:
        spot = float(prev_spot or 0)
    return {
        "spot": spot,
        "change": (data or {}).get("change", 0) or 0,
        "ts": now_ms,
        "source": (data or {}).get("source", "na"),
    }


def _cached_snapshot_text() -> str | None:
    """Instant first frame from the in-process cache (zero network/DB)."""
    now = time.time()
    now_ms = int(now * 1000)
    ticks = {}
    any_spot = False
    for sym in SPOT_SYMBOLS:
        entry = _LIVE_CACHE.get(sym)
        data = None
        if isinstance(entry, dict) and now - float(entry.get("ts") or 0) < 120:
            data = entry.get("data")
        tick = _tick_from_data(sym, data, 0, now_ms)
        if tick["spot"] > 0:
            any_spot = True
        ticks[sym] = tick
    if not any_spot:
        return None
    return json.dumps({"type": "tick", "ticks": ticks, "interval_ms": TICK_INTERVAL * 1000})


def _build_spot_frame(live: LiveMarketData, prev: dict):
    """Blocking part (runs in a worker thread): fetch batch + build frame."""
    try:
        batch = live.get_live_spots_parallel(SPOT_SYMBOLS, max_workers=4) or {}
    except Exception:
        batch = {}
    now_ms = int(time.time() * 1000)
    ticks = {}
    changed = False
    for sym in SPOT_SYMBOLS:
        data = batch.get(sym)
        if not data:
            entry = _LIVE_CACHE.get(sym)
            data = entry.get("data") if isinstance(entry, dict) else None
        tick = _tick_from_data(sym, data, prev.get(sym, 0), now_ms)
        ticks[sym] = tick
        if prev.get(sym) != tick["spot"]:
            changed = True
    return ticks, changed


async def _spot_broadcaster():
    """Single process-wide tick producer. Stops when nobody is listening.

    Sending is DECOUPLED from fetching. The data batch can take up to 25s on
    Render's free CPU (network quotes + shared 4-thread pool), and a proxy in
    front of the app drops a socket that sees no traffic for ~20-30s. So the
    fetch runs as a background thread task while this loop keeps sending the
    last snapshot every HEARTBEAT_SEC. That is what keeps the socket alive.
    """
    global _spot_task, _spot_snapshot_text, _spot_snapshot_ts
    live = LiveMarketData()
    prev = dict(_spot_prev)
    last_sent = 0.0
    last_fetch = 0.0
    fetch_task = None
    try:
        while _spot_subs:
            now = time.time()
            # Kick off one fetch at a time; never await it on this loop.
            if fetch_task is None and now - last_fetch >= TICK_INTERVAL:
                last_fetch = now
                fetch_task = asyncio.create_task(
                    asyncio.to_thread(_build_spot_frame, live, dict(prev))
                )

            payload = None
            if fetch_task is not None and fetch_task.done():
                try:
                    ticks, changed = fetch_task.result()
                except Exception:
                    ticks, changed = {}, False
                fetch_task = None
                if ticks:
                    prev = {k: v.get("spot", 0) for k, v in ticks.items()}
                    _spot_prev.clear()
                    _spot_prev.update(prev)
                    if changed:
                        payload = json.dumps({
                            "type": "tick", "ticks": ticks,
                            "interval_ms": TICK_INTERVAL * 1000,
                        })

            # Proxy keep-alive: repeat the last snapshot even while the next
            # fetch is still in flight.
            now = time.time()
            if payload is None and _spot_snapshot_text and now - last_sent >= HEARTBEAT_SEC:
                payload = _spot_snapshot_text

            if payload:
                _spot_snapshot_text = payload
                _spot_snapshot_ts = now
                last_sent = now
                dead = []
                for ws in list(_spot_subs):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    _spot_subs.discard(ws)
                    _connections.discard(ws)
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    except Exception as e:  # never let the broadcaster die silently
        logger.warning("[ws] spot broadcaster stopped: %s", e)
    finally:
        if fetch_task is not None:
            try:
                fetch_task.cancel()
            except Exception:
                pass
        _spot_task = None
        # If a connection arrived while this task was winding down, restart.
        if _spot_subs:
            try:
                _ensure_spot_task()
            except Exception:
                pass


def _ensure_spot_task():
    global _spot_task
    if not _spot_subs:
        return
    try:
        if _spot_task is None or _spot_task.done():
            _spot_task = asyncio.get_running_loop().create_task(_spot_broadcaster())
    except Exception:
        _spot_task = None


# --- chain helpers --------------------------------------------------------

def _build_chain_frame(sym: str, bhav: BhavcopyModel) -> str | None:
    """DB/live chain snapshot, 10s in-process cache shared by all clients."""
    now = time.time()
    hit = _chain_cache.get(sym)
    if hit and now - hit[0] < _CHAIN_CACHE_TTL:
        return hit[1]

    data = None
    try:
        data = LiveMarketData().fetch_live_option_chain(sym)
    except Exception:
        data = None

    if not (data and data.get("rows")):
        try:
            dates = bhav.get_dates(sym)
            if dates:
                expiries = bhav.get_expiries(sym, dates[0])
                if expiries:
                    chain = bhav.get_option_chain(sym, dates[0], expiries[0])
                    data = {
                        "symbol": sym,
                        "source": "db",
                        "rows": [
                            {
                                "strike": r["strike_price"],
                                "ce_ltp": r["close_price"] if r["option_type"] == "CE" else 0,
                                "pe_ltp": r["close_price"] if r["option_type"] == "PE" else 0,
                            }
                            for r in (chain or [])[:20]
                        ],
                    }
        except Exception:
            data = None

    if not (data and data.get("rows")):
        return None

    try:
        text = json.dumps(data)
    except Exception:
        return None
    _chain_cache[sym] = (now, text)
    # Bound the per-symbol snapshot cache (long-lived process, many symbols).
    if len(_chain_cache) > 30:
        try:
            _chain_cache.pop(next(iter(_chain_cache)))
        except Exception:
            pass
    return text


async def _chain_broadcaster(sym: str):
    bhav = BhavcopyModel()
    last_text = None
    last_sent = 0.0
    last_fetch = 0.0
    fetch_task = None
    try:
        while _chain_subs.get(sym):
            now = time.time()
            if fetch_task is None and now - last_fetch >= CHAIN_INTERVAL:
                last_fetch = now
                fetch_task = asyncio.create_task(
                    asyncio.to_thread(_build_chain_frame, sym, bhav)
                )
            text = None
            if fetch_task is not None and fetch_task.done():
                try:
                    text = fetch_task.result()
                except Exception:
                    text = None
                fetch_task = None
            now = time.time()
            # Same proxy keep-alive rule as the spot stream.
            if text is None and last_text and now - last_sent >= CHAIN_HEARTBEAT:
                text = last_text
            if text and (text != last_text or now - last_sent >= CHAIN_HEARTBEAT):
                dead = []
                for ws in list(_chain_subs.get(sym, ())):
                    try:
                        await ws.send_text(text)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    _chain_subs.get(sym, set()).discard(ws)
                    _connections.discard(ws)
                last_text = text
                last_sent = now
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("[ws] chain broadcaster %s stopped: %s", sym, e)
    finally:
        if fetch_task is not None:
            try:
                fetch_task.cancel()
            except Exception:
                pass
        _chain_tasks.pop(sym, None)
        if _chain_subs.get(sym):
            try:
                _ensure_chain_task(sym)
            except Exception:
                pass


def _ensure_chain_task(sym: str):
    if not _chain_subs.get(sym):
        return
    task = _chain_tasks.get(sym)
    if task is None or task.done():
        try:
            _chain_tasks[sym] = asyncio.get_running_loop().create_task(_chain_broadcaster(sym))
        except Exception:
            _chain_tasks.pop(sym, None)


# --- endpoints ------------------------------------------------------------

@router.websocket("/live")
async def ws_live(websocket: WebSocket):
    if len(_spot_subs) >= MAX_CONNECTIONS:
        try:
            await websocket.close(code=1013)  # try again later
        except Exception:
            pass
        return

    await websocket.accept()
    _spot_subs.add(websocket)
    _connections.add(websocket)
    try:
        # Instant frame from cache (only if fresh; otherwise the broadcaster
        # sends a real one within ~3s).
        snap = None
        if _spot_snapshot_text and time.time() - _spot_snapshot_ts < 15:
            snap = _spot_snapshot_text
        snap = snap or _cached_snapshot_text()
        if snap:
            try:
                await websocket.send_text(snap)
            except Exception:
                pass
        _ensure_spot_task()
        # The broadcaster owns sending; this handler only waits for the
        # client to go away (uvicorn's websocket ping detects dead peers).
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _spot_subs.discard(websocket)
        _connections.discard(websocket)


@router.websocket("/chain/{symbol}")
async def ws_chain(websocket: WebSocket, symbol: str):
    sym = (symbol or "").upper()
    if len(_chain_subs.get(sym, ())) >= MAX_CONNECTIONS:
        try:
            await websocket.close(code=1013)
        except Exception:
            pass
        return

    await websocket.accept()
    _chain_subs.setdefault(sym, set()).add(websocket)
    _connections.add(websocket)
    try:
        # First payload immediately (shared 10s cache makes this cheap).
        try:
            text = await asyncio.to_thread(_build_chain_frame, sym, BhavcopyModel())
            if text:
                await websocket.send_text(text)
        except Exception:
            pass
        _ensure_chain_task(sym)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        subs = _chain_subs.get(sym)
        if subs is not None:
            subs.discard(websocket)
            if not subs:
                _chain_subs.pop(sym, None)
        _connections.discard(websocket)


@router.get("/stats")
def ws_stats():
    return {
        "connections": len(_connections),
        "spot_subscribers": len(_spot_subs),
        "chain_symbols": list(_chain_subs.keys()),
        "interval_ms": TICK_INTERVAL * 1000,
        "chain_interval_ms": CHAIN_INTERVAL * 1000,
        "source": "shared broadcaster + bounded executor + change-only push",
        "latency": f"{TICK_INTERVAL}s",
    }
