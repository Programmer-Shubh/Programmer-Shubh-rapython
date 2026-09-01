import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from core.models.database import Database

os.environ.setdefault("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "ratrade.db"))


async def _start_background_refresh():
    from core.services.data_refresher import run_refresh_loop
    asyncio.get_running_loop().create_task(run_refresh_loop())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-fix lot sizes for existing trades (Aug 2026)
    try:
        from utils.helpers import get_lot_size
        _db = Database.get_instance()
        for sym in ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","ADANIENT","BAJFINANCE","RELIANCE","HDFCBANK","ICICIBANK","TCS","INFY","SBIN","KOTAKBANK","LT","M&M","MARUTI"]:
            correct = get_lot_size(sym)
            _db.execute("UPDATE paper_trades SET lot_size=? WHERE symbol=? AND lot_size!=?", [correct, sym, correct])
    except Exception:
        pass
    await _start_background_refresh()
    yield


app = FastAPI(title="RaTrade API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = Database.get_instance()
db.init_schema()

from routes import dashboard, option_chain, paper_trade, strategy_builder, scanner, broker, webhook, websocket, email_alerts, strategies

app.include_router(dashboard.router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(option_chain.router, prefix="/api/chain", tags=["Option Chain"])
app.include_router(paper_trade.router, prefix="/api/trades", tags=["Paper Trade"])
app.include_router(strategy_builder.router, prefix="/api/backtest", tags=["Strategy Builder"])
app.include_router(scanner.router, prefix="/api/scanner", tags=["Scanner"])
app.include_router(broker.router, prefix="/api/broker", tags=["Brokers"])
app.include_router(webhook.router, prefix="/api/webhook", tags=["Webhook"])
app.include_router(websocket.router, prefix="/api/ws", tags=["Live WebSocket"])
app.include_router(email_alerts.router, prefix="/api/email-alerts", tags=["Email Alerts"])
app.include_router(strategies.router, prefix="/api/strategies", tags=["Strategies"])


@app.get("/")
def root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.get("/health")
async def health():
    # Async instant - never block event loop, never touch DB for Render 5s timeout
    return {"status": "healthy", "timestamp": __import__("time").time()}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/lot-size/{symbol}")
def lot_size(symbol: str):
    from utils.helpers import get_lot_size
    return {"symbol": symbol.upper(), "lot_size": get_lot_size(symbol)}

@app.get("/ready")
async def ready():
    # Light readiness check with DB ping but bounded 1s
    try:
        import asyncio as _aio
        await _aio.wait_for(_aio.to_thread(lambda: Database.get_instance().fetch_one("SELECT 1 as ok")), timeout=1.0)
        return {"status": "ready"}
    except Exception:
        return {"status": "ready - degraded"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
