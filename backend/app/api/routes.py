from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..db import SessionLocal, get_db, init_db
from ..models import BacktestRun, DataCatalog, Strategy, Trade, UploadRecord
from ..schemas import (
    BacktestRunCreate,
    BacktestRunOut,
    CatalogEntry,
    JobProgress,
    RecorderStatus,
    StrategyCreate,
    StrategyOut,
    StrategyUpdate,
    TradeOut,
)
from ..backtest.engine import BacktestEngine
from ..services.broker import DummyBrokerAdapter
from ..lakehouse.recorder import AutoRecorder
from ..lakehouse.ingestion import ingest_file
from ..lakehouse.compaction import run_nightly_compaction

router = APIRouter()
executor = ThreadPoolExecutor(max_workers=4)

# In-memory job store (single process; swap for Redis in multi-node deployments)
JOBS: dict[str, dict[str, Any]] = {}

# Singletons
broker_adapter = DummyBrokerAdapter()
recorder = AutoRecorder()


# --------------------------------------------------------------------------- startup helpers
def _ensure_db():
    init_db()


# --------------------------------------------------------------------------- strategies
@router.post("/api/strategies", response_model=StrategyOut)
def create_strategy(payload: StrategyCreate, db: Session = Depends(get_db)):
    default_config = {
        "indicators": ["supertrend", "rsi", "adx", "vwap", "ema"],
        "strike_offset": "ATM",
        "expiry_type": "weekly",
        "option_side": "both",
        "sl_points": 20.0,
        "tp_points": 40.0,
        "trailing_sl_points": 0.0,
        "slippage_pct": settings.DEFAULT_SLIPPAGE_PCT,
        "latency": settings.DEFAULT_LATENCY_MODE,
        "lot_size": settings.DEFAULT_LOT_SIZE,
    }
    config = {**default_config, **payload.config}
    strategy = Strategy(name=payload.name, user_notes=payload.user_notes, config_json=config)
    db.add(strategy)
    db.commit()
    db.refresh(strategy)
    return strategy


@router.get("/api/strategies", response_model=list[StrategyOut])
def list_strategies(db: Session = Depends(get_db)):
    return db.query(Strategy).order_by(Strategy.id.desc()).all()


@router.get("/api/strategies/{strategy_id}", response_model=StrategyOut)
def get_strategy(strategy_id: int, db: Session = Depends(get_db)):
    strategy = db.query(Strategy).filter_by(id=strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy


@router.patch("/api/strategies/{strategy_id}", response_model=StrategyOut)
def update_strategy(strategy_id: int, payload: StrategyUpdate, db: Session = Depends(get_db)):
    strategy = db.query(Strategy).filter_by(id=strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if payload.name is not None:
        strategy.name = payload.name
    if payload.user_notes is not None:
        strategy.user_notes = payload.user_notes
    if payload.config is not None:
        strategy.config_json = {**strategy.config_json, **payload.config}
    db.commit()
    db.refresh(strategy)
    return strategy


@router.delete("/api/strategies/{strategy_id}")
def delete_strategy(strategy_id: int, db: Session = Depends(get_db)):
    strategy = db.query(Strategy).filter_by(id=strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    db.delete(strategy)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- backtesting
def _backtest_job(job_id: str, run_id: int, strategy_id: int, payload: BacktestRunCreate):
    db = SessionLocal()
    try:
        run = db.query(BacktestRun).filter_by(id=run_id).first()
        strategy = db.query(Strategy).filter_by(id=strategy_id).first()
        if not run or not strategy:
            JOBS[job_id] = {"status": "failed", "progress": 0.0, "message": "run/strategy missing", "result": None}
            return

        run.status = "running"
        run.started_at = datetime.now()
        db.commit()

        engine = BacktestEngine(db)
        is_start = payload.is_start or (date.today() - __import__("datetime").timedelta(days=30))
        is_end = payload.is_end or date.today()
        oos_start = payload.oos_start
        oos_end = payload.oos_end

        JOBS[job_id] = {"status": "running", "progress": 10.0, "message": "loading cached option data", "result": None}

        result = engine.run(
            strategy.config_json,
            payload.data_mode,
            is_start,
            is_end,
            lot_size=strategy.config_json.get("lot_size", settings.DEFAULT_LOT_SIZE),
            walk_forward=payload.use_walk_forward and bool(oos_start and oos_end),
        )

        JOBS[job_id] = {"status": "running", "progress": 70.0, "message": "persisting trades", "result": None}

        data_source = result["data_source"]
        run.status = "completed"
        run.finished_at = datetime.now()
        run.data_source = data_source
        run.data_mode = payload.data_mode
        run.metrics_json = {
            **result["metrics"],
            "walk_forward": result.get("walk_forward"),
            "is_metrics": result.get("is_metrics"),
            "oos_metrics": result.get("oos_metrics"),
        }
        run.summary_json = {
            "strategy": strategy.name,
            "data_mode": payload.data_mode,
            "data_source": data_source,
            "date_range": f"{is_start} to {is_end}",
            "signals_evaluated": result.get("signals_evaluated", 0),
            "trades_executed": len(result["trades"]),
            "net_pnl": result["metrics"].get("net_pnl", 0.0),
            "win_rate": result["metrics"].get("win_rate", 0.0),
            "profit_factor": result["metrics"].get("profit_factor", 0.0),
            "expectancy": result["metrics"].get("expectancy", 0.0),
            "sharpe": result["metrics"].get("sharpe", 0.0),
            "calmar": result["metrics"].get("calmar", 0.0),
            "max_drawdown": result["metrics"].get("max_drawdown", 0.0),
            "run_timestamp": run.started_at.isoformat(),
        }
        db.add_all(
            Trade(
                run_id=run.id,
                entry_date=t["entry_date"],
                entry_time=t["entry_time"],
                exit_date=t["exit_date"],
                exit_time=t["exit_time"],
                symbol=t["symbol"],
                trade_type=t["trade_type"],
                strike=t["strike"],
                expiry=t["expiry"],
                entry_price=t["entry_price"],
                exit_price=t["exit_price"],
                status=t["status"],
                pnl=t["pnl"],
                mae=t["mae"],
                mfe=t["mfe"],
                exit_reason=t.get("exit_reason"),
                is_oos=t.get("is_oos", False),
            )
            for t in result["trades"]
        )
        db.commit()

        JOBS[job_id] = {
            "status": "completed",
            "progress": 100.0,
            "message": "backtest complete",
            "result": {"run_id": run.id, **result},
        }
    except Exception as exc:  # noqa: BLE001
        try:
            run = db.query(BacktestRun).filter_by(id=run_id).first()
            if run:
                run.status = "failed"
                run.error_message = str(exc)
                run.finished_at = datetime.now()
                db.commit()
        except Exception:
            pass
        JOBS[job_id] = {"status": "failed", "progress": 0.0, "message": str(exc), "result": None}
    finally:
        db.close()


@router.post("/api/backtest/run")
def run_backtest(payload: BacktestRunCreate, background: BackgroundTasks):
    db = SessionLocal()
    try:
        strategy = db.query(Strategy).filter_by(id=payload.strategy_id).first()
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategy not found")
        run = BacktestRun(
            strategy_id=payload.strategy_id,
            status="queued",
            data_mode=payload.data_mode,
            data_source="TICK DATA" if payload.data_mode == "tick" else "1M OPTION CHAIN",
            use_walk_forward=payload.use_walk_forward,
            is_start=payload.is_start,
            is_end=payload.is_end,
            oos_start=payload.oos_start,
            oos_end=payload.oos_end,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        job_id = str(uuid.uuid4())
        JOBS[job_id] = {"status": "queued", "progress": 0.0, "message": "queued", "result": None}
        background.add_task(_backtest_job, job_id, run.id, payload.strategy_id, payload)
        return {"job_id": job_id, "run_id": run.id, "status": "queued"}
    finally:
        db.close()


@router.get("/api/backtest/jobs/{job_id}", response_model=JobProgress)
def job_progress(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobProgress(job_id=job_id, **job)


@router.get("/api/backtest/jobs/{job_id}/stream")
async def job_stream(job_id: str):
    async def event_generator():
        while True:
            job = JOBS.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                break
            yield f"data: {json.dumps({'job_id': job_id, **job})}\n\n"
            if job["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(1.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/api/backtest/runs", response_model=list[BacktestRunOut])
def list_runs(strategy_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(BacktestRun)
    if strategy_id:
        q = q.filter_by(strategy_id=strategy_id)
    return q.order_by(BacktestRun.started_at.desc()).all()


@router.get("/api/backtest/runs/{run_id}", response_model=BacktestRunOut)
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.query(BacktestRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/api/backtest/runs/{run_id}/trades", response_model=list[TradeOut])
def get_trades(run_id: int, db: Session = Depends(get_db)):
    return db.query(Trade).filter_by(run_id=run_id).order_by(Trade.entry_date, Trade.entry_time).all()


@router.get("/api/strategies/{strategy_id}/latest-run", response_model=BacktestRunOut | None)
def latest_run(strategy_id: int, db: Session = Depends(get_db)):
    """Latest saved backtest record — single source of truth for all BT pills / badges."""
    return (
        db.query(BacktestRun)
        .filter_by(strategy_id=strategy_id)
        .order_by(BacktestRun.started_at.desc())
        .first()
    )


@router.get("/api/strategies/{strategy_id}/latest-trades", response_model=list[TradeOut])
def latest_trades(strategy_id: int, db: Session = Depends(get_db)):
    """Trade history from the latest saved run of a strategy."""
    run = (
        db.query(BacktestRun)
        .filter_by(strategy_id=strategy_id)
        .order_by(BacktestRun.started_at.desc())
        .first()
    )
    if not run:
        return []
    return db.query(Trade).filter_by(run_id=run.id).order_by(Trade.entry_date, Trade.entry_time).all()


# ------------------------------------------------------------------- lakehouse
@router.get("/api/lakehouse/catalog", response_model=list[CatalogEntry])
def lakehouse_catalog(db: Session = Depends(get_db)):
    """Stored Option Data Library — cached symbols, date ranges, completeness."""
    rows = db.query(DataCatalog).order_by(DataCatalog.underlying, DataCatalog.trade_date.desc()).all()
    return [
        CatalogEntry(
            underlying=r.underlying,
            data_mode=r.data_mode,
            expiry=r.expiry,
            trade_date=r.trade_date,
            row_count=r.row_count,
            min_timestamp=r.min_timestamp,
            max_timestamp=r.max_timestamp,
            size_bytes=r.size_bytes,
        )
        for r in rows
    ]


@router.get("/api/lakehouse/storage-stats")
def storage_stats(db: Session = Depends(get_db)):
    total = db.query(DataCatalog).with_entities(
        __import__("sqlalchemy").func.sum(DataCatalog.size_bytes),
        __import__("sqlalchemy").func.sum(DataCatalog.row_count),
    ).first()
    return {
        "cached_storage_bytes": total[0] or 0,
        "cached_storage_mb": round((total[0] or 0) / (1024 * 1024), 2),
        "total_cached_rows": total[1] or 0,
        "lakehouse_dir": str(settings.LAKEHOUSE_DIR),
    }


@router.post("/api/lakehouse/upload")
def lakehouse_upload(background: BackgroundTasks, file: UploadFile = File(...)):
    """Idempotent drag-and-drop CSV/Parquet upload — ingested once by content hash."""
    staging = settings.UPLOAD_STAGING_DIR
    staging.mkdir(parents=True, exist_ok=True)
    dest = staging / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    result = ingest_file(str(dest), file.filename)
    return result


@router.post("/api/lakehouse/sync-historical")
def sync_historical(underlying: str, from_date: str, to_date: str, data_mode: str = "tick"):
    """Optional incremental historical sync — only when broker is connected."""
    if broker_adapter.connected:
        return {"status": "not_implemented", "message": "wire a real BrokerAdapter for live sync"}
    return {"status": "offline", "message": "broker disconnected; historical sync requires a connected broker"}


# ------------------------------------------------------------------ recorder
@router.get("/api/recorder/status", response_model=RecorderStatus)
def recorder_status():
    total_bytes = 0
    for root, _dirs, files in os.walk(settings.LAKEHOUSE_DIR):
        for f in files:
            try:
                total_bytes += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    status = recorder.status(total_bytes / (1024 * 1024))
    return RecorderStatus(
        running=status["running"],
        buffer_size=status["buffer_size"],
        latest_recorded_date=status["latest_recorded_date"],
        tracked_symbols=status["tracked_symbols"],
        cached_storage_mb=status["cached_storage_mb"],
        mode=status["mode"],
    )


@router.post("/api/recorder/toggle")
def recorder_toggle():
    import asyncio as _asyncio

    if recorder._running:
        _asyncio.get_event_loop().create_task(recorder.stop())
    else:
        _asyncio.get_event_loop().create_task(recorder.start())
    return {"running": recorder._running}


@router.post("/api/recorder/flush")
def recorder_flush():
    import asyncio as _asyncio

    loop = _asyncio.get_event_loop()
    n = loop.create_task(recorder.flush())
    return {"flushed": "scheduled"}


# ------------------------------------------------------------------- trading
TRADING_MODE = {"mode": "paper"}  # paper | live


@router.get("/api/trading/mode")
def trading_mode():
    return TRADING_MODE


@router.post("/api/trading/mode")
def trading_mode_set(mode: str):
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
    TRADING_MODE["mode"] = mode
    return TRADING_MODE


@router.post("/api/trading/orders")
def place_order(symbol: str, side: str, qty: int, price: float | None = None):
    return run_in_threadpool(broker_adapter.place_order, symbol, side, qty, price)


# ------------------------------------------------------------------- system
@router.get("/api/health")
def health():
    return {"status": "ok", "app": settings.APP_NAME, "lakehouse": str(settings.LAKEHOUSE_DIR)}


@router.post("/api/demo/seed")
def demo_seed(force: bool = False):
    """Seed demo option data + a sample backtest run (for Render Free ephemeral storage)."""
    db = SessionLocal()
    try:
        existing = db.query(Strategy).filter_by(name="SuperTrend NIFTY Option Strategy").first()
        if existing and not force:
            return {"status": "already_seeded", "strategy_id": existing.id}
        # Import and run the seed logic inline to avoid path issues on Render
        import random
        from datetime import date, datetime, time, timedelta, timezone
        import polars as pl
        from ..lakehouse.schema import arrow_table, SCHEMA_COLUMNS, write_parquet_atomic, normalize_option_frame
        from ..lakehouse.ingestion import _refresh_catalog
        from ..lakehouse.partitioning import partition_dir
        from ..backtest.engine import BacktestEngine

        random.seed(42)
        UNDERLYING = "NIFTY"
        SPOT = 23750.0
        STRIKES = list(range(23600, 23901, 50))
        EXPIRIES = [date(2026, 11, 19), date(2026, 11, 26)]
        TRADING_DAYS = [date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20)]

        def ist(ts): return ts.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))

        rows = []
        for td in TRADING_DAYS:
            t = datetime.combine(td, time(9, 15)); end = datetime.combine(td, time(15, 30)); px = SPOT
            while t <= end:
                px += random.uniform(-3, 3)
                rows.append({"timestamp": ist(t), "underlying": UNDERLYING, "data_mode": "tick", "expiry": EXPIRIES[0], "option_type": "UNDERLYING", "strike": 0.0, "ltp": round(px,2), "bid": round(px-1,2), "ask": round(px+1,2), "oi": 0, "volume": random.randint(50,500), "trade_date": td, "source":"demo"})
                t += timedelta(seconds=5)
            days_to_expiry_base = max((EXPIRIES[0]-td).days, 0)
            for expiry in EXPIRIES:
                for strike in STRIKES:
                    for opt in ("CE","PE"):
                        intrinsic = max(SPOT-strike,0) if opt=="CE" else max(strike-SPOT,0)
                        base = intrinsic + max(40-days_to_expiry_base*8,4) + random.uniform(-2,2)
                        t = datetime.combine(td, time(9,15)); end = datetime.combine(td, time(15,30)); px2 = max(base,1.0)
                        while t <= end:
                            px2 = max(px2 + random.uniform(-0.6,0.6), 0.5)
                            illiquid = random.random() < 0.05
                            bid = round(max(px2-0.7,0.2),2) if not illiquid else None
                            ask = round(px2+0.7,2) if not illiquid else None
                            rows.append({"timestamp": ist(t), "underlying": UNDERLYING, "data_mode":"tick", "expiry": expiry, "option_type": opt, "strike": float(strike), "ltp": round(px2,2), "bid": bid, "ask": ask, "oi": random.randint(100,5000), "volume": 0 if illiquid else random.randint(1,200), "trade_date": td, "source":"demo"})
                            t += timedelta(seconds=30)

        df = pl.DataFrame(rows)
        total = 0
        for keys, group in df.group_by(["underlying","data_mode","expiry","trade_date"], maintain_order=True):
            norm = normalize_option_frame(group)
            for _k, g in norm.group_by(["underlying","data_mode","expiry","trade_date"], maintain_order=True):
                pdir = partition_dir(g["underlying"][0], g["data_mode"][0], g["expiry"][0], g["trade_date"][0])
                pdir.mkdir(parents=True, exist_ok=True)
                out = pdir / f"part-seed-{total}.parquet"
                write_parquet_atomic(arrow_table(g.select(SCHEMA_COLUMNS + ["trade_date","ingested_at"])), out)
                total += len(g)

        _refresh_catalog(db)
        from ..models import UploadRecord
        db.add(UploadRecord(filename="demo_option_ticks.parquet", file_hash=f"demo-seed-render-{force}", file_size=0, rows_ingested=total, status="ingested"))
        strategy = Strategy(name="SuperTrend NIFTY Option Strategy", user_notes="Demo strategy seeded for offline backtesting. User-editable notes.", config_json={
            "indicators":["supertrend","rsi","adx","vwap","ema"], "strike_offset":"ATM", "expiry_type":"weekly", "option_side":"both",
            "sl_points":15.0, "tp_points":35.0, "trailing_sl_points":7.0, "slippage_pct":0.05, "latency":"next_tick", "lot_size":50
        })
        db.add(strategy); db.commit(); db.refresh(strategy)

        engine = BacktestEngine(db, UNDERLYING)
        result = engine.run(strategy.config_json, "tick", date(2026,11,18), date(2026,11,20), lot_size=50, walk_forward=True)
        run = BacktestRun(strategy_id=strategy.id, status="completed", data_mode="tick", data_source="TICK DATA", use_walk_forward=True,
            is_start=date(2026,11,18), is_end=date(2026,11,19), oos_start=date(2026,11,20), oos_end=date(2026,11,20),
            metrics_json={**result["metrics"], "walk_forward": result.get("walk_forward"), "is_metrics": result.get("is_metrics"), "oos_metrics": result.get("oos_metrics")},
            summary_json={"strategy": strategy.name, "data_mode":"tick","data_source":"TICK DATA","date_range":"2026-11-18 to 2026-11-20 (walk-forward: IS 18-19 / OOS 20)", "signals_evaluated": result.get("signals_evaluated",0), "trades_executed": len(result["trades"]), "net_pnl": result["metrics"].get("net_pnl",0.0), "win_rate": result["metrics"].get("win_rate",0.0), "profit_factor": result["metrics"].get("profit_factor",0.0), "expectancy": result["metrics"].get("expectancy",0.0), "sharpe": result["metrics"].get("sharpe",0.0), "calmar": result["metrics"].get("calmar",0.0), "max_drawdown": result["metrics"].get("max_drawdown",0.0), "run_timestamp": datetime.now(timezone.utc).isoformat()},
            started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc))
        db.add(run); db.commit(); db.refresh(run)
        db.add_all([Trade(run_id=run.id, entry_date=t["entry_date"], entry_time=t["entry_time"], exit_date=t["exit_date"], exit_time=t["exit_time"], symbol=t["symbol"], trade_type=t["trade_type"], strike=t["strike"], expiry=t["expiry"], entry_price=t["entry_price"], exit_price=t["exit_price"], status=t["status"], pnl=t["pnl"], mae=t["mae"], mfe=t["mfe"], exit_reason=t.get("exit_reason"), is_oos=t.get("is_oos", False)) for t in result["trades"]])
        db.commit()
        return {"status":"seeded","rows_cached": total, "trades": len(result["trades"]), "strategy_id": strategy.id, "run_id": run.id}
    finally:
        db.close()


@router.post("/api/compact-now")
def compact_now():
    return run_nightly_compaction()
