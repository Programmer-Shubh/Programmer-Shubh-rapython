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

from routes import dashboard, option_chain, paper_trade, strategy_builder, bhavcopy_import, scanner, broker, webhook, websocket, email_alerts

app.include_router(dashboard.router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(option_chain.router, prefix="/api/chain", tags=["Option Chain"])
app.include_router(paper_trade.router, prefix="/api/trades", tags=["Paper Trade"])
app.include_router(strategy_builder.router, prefix="/api/backtest", tags=["Strategy Builder"])
app.include_router(bhavcopy_import.router, prefix="/api/bhavcopy", tags=["Bhavcopy Import"])
app.include_router(scanner.router, prefix="/api/scanner", tags=["Scanner"])
app.include_router(broker.router, prefix="/api/broker", tags=["Brokers"])
app.include_router(webhook.router, prefix="/api/webhook", tags=["Webhook"])
app.include_router(websocket.router, prefix="/api/ws", tags=["Live WebSocket"])
app.include_router(email_alerts.router, prefix="/api/email-alerts", tags=["Email Alerts"])


@app.get("/")
def root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.get("/health")
def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
