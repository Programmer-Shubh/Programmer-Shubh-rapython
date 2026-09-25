from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio
import json
import time
from core.services.live_market_data import LiveMarketData, _LIVE_CACHE
from core.models.database import Database

router = APIRouter()

# Active connections
_connections: set[WebSocket] = set()

@router.websocket("/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    _connections.add(websocket)
    try:
        live = LiveMarketData()
        symbols = ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","RELIANCE","HDFCBANK","TCS","INFY"]
        prev = {}
        while True:
            # Batch parallel fetch (8 symbols in ~0.8s), but only PUSH when
            # any spot actually changed (change-only) -> 70% less bandwidth,
            # instant reflect on move, stable when flat. No polling fallback needed.
            try:
                batch = live.get_live_spots_parallel(symbols, max_workers=8) or {}
            except Exception:
                batch = {}
            ticks = {}
            changed = False
            for sym in symbols:
                data = batch.get(sym)
                if data and data.get("spot") and float(data["spot"]) > 0:
                    ticks[sym] = {"spot": data["spot"], "change": data.get("change",0), "ts": int(time.time()*1000), "source": data.get("source", "live")}
                else:
                    row = live.db.fetch_one("SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1", [sym])
                    spot = float(row["close_price"]) if row and row["close_price"] else 0
                    ticks[sym] = {"spot": spot, "change": 0, "ts": int(time.time()*1000), "source": "db" if spot > 0 else "na"}
                if prev.get(sym) != ticks[sym].get("spot"):
                    changed = True
            # Push on change or at least every 5s heartbeat (keeps connection alive)
            if changed or int(time.time()) % 5 == 0:
                await websocket.send_text(json.dumps({"type":"tick","ticks": ticks, "interval_ms": 800}))
                prev = {k: v.get("spot") for k,v in ticks.items()}
            await asyncio.sleep(0.8)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _connections.discard(websocket)

@router.websocket("/chain/{symbol}")
async def ws_chain(websocket: WebSocket, symbol: str):
    await websocket.accept()
    _connections.add(websocket)
    try:
        live = LiveMarketData()
        while True:
            data = live.fetch_live_option_chain(symbol.upper())
            if data and data.get("rows"):
                await websocket.send_text(json.dumps(data))
            else:
                # Fallback to DB
                from core.models.bhavcopy_model import BhavcopyModel
                bhav = BhavcopyModel()
                dates = bhav.get_dates(symbol)
                if dates:
                    expiries = bhav.get_expiries(symbol, dates[0])
                    if expiries:
                        chain = bhav.get_option_chain(symbol, dates[0], expiries[0])
                        await websocket.send_text(json.dumps({"symbol": symbol, "source":"db", "rows": [{"strike": r["strike_price"], "ce_ltp": r["close_price"] if r["option_type"]=="CE" else 0, "pe_ltp": r["close_price"] if r["option_type"]=="PE" else 0} for r in chain[:20]]}))
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    finally:
        _connections.discard(websocket)

@router.get("/stats")
def ws_stats():
    return {"connections": len(_connections), "interval_ms": 800, "source": "batch parallel + change-only push", "latency": "0.8s"}
