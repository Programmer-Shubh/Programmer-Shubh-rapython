from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import routes
from .config import settings
from .db import init_db
from .services.broker import broker_adapter
from .services.scheduler import NightlyCompactionScheduler
from .lakehouse.recorder import recorder

scheduler = NightlyCompactionScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes.router)

# Mount API root alias
app.include_router(routes.router, prefix="/api")


STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith("api") or full_path.startswith("docs") or full_path.startswith("openapi") or full_path.startswith("redoc"):
            return {"detail": "Not Found"}
        file_path = STATIC_DIR / full_path
        if full_path and file_path.is_file():
            return FileResponse(file_path)
        index = STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(index)
        return {"detail": "Not Found"}
else:
    @app.get("/")
    def root():
        return {"app": settings.APP_NAME, "status": "running", "docs": "/docs"}
