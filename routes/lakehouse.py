"""Option Data Lakehouse API (DuckDB + Polars + PyArrow).

Endpoints:
  GET  /api/lakehouse/status          - cached symbols, modes, date range, size
  GET  /api/lakehouse/ticks           - query real option ticks from lakehouse
  POST  /api/lakehouse/upload         - idempotent CSV/Parquet upload -> lakehouse
  POST  /api/lakehouse/compact        - nightly compaction trigger
  GET  /api/lakehouse/backtest        - offline backtest from lakehouse data
"""
from __future__ import annotations

import io
import os
import tempfile
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from core.services.option_lakehouse import (
    OPTION_TICKS_ROOT,
    REAL_TICK_SOURCES,
    compact_lakehouse,
    get_lakehouse_library,
    get_lakehouse_status,
    missing_dates,
    query_option_ticks,
    store_option_ticks,
)
from core.services.lakehouse_recorder import get_recorder_status, set_active

router = APIRouter(prefix="/api/lakehouse", tags=["Option Data Lakehouse"])


class TickQuery(BaseModel):
    underlying: str = "FINNIFTY"
    data_mode: str = "1m_chain"
    expiry: str = ""
    start_date: str = ""
    end_date: str = ""
    option_type: Optional[str] = None
    strike: Optional[float] = None
    limit: int = 10000


class TickUploadResponse(BaseModel):
    success: bool
    rows_written: int
    message: str
    duplicates_skipped: int = 0


@router.get("/status")
async def lakehouse_status():
    """Lakehouse health: symbols, modes, date range, storage size."""
    return get_lakehouse_status()


@router.get("/library")
async def lakehouse_library():
    """Stored Option Data Library: symbols, modes, date ranges, completeness."""
    return get_lakehouse_library()


@router.get("/missing-dates")
async def lakehouse_missing_dates(
    symbol: str = "", data_mode: str = "1m_chain",
    start_date: str = "", end_date: str = "",
):
    """Actionable missing-date warning for the requested offline backtest window."""
    miss = missing_dates(symbol, data_mode, start_date, end_date)
    return {"symbol": symbol.upper(), "data_mode": data_mode,
            "missing_dates": miss, "count": len(miss),
            "message": (f"{len(miss)} dates missing — sync/upload ya chhoti range chuno."
                        if miss else "Requested dates cached — offline backtest ready.")}


@router.get("/auto-record/status")
async def lakehouse_auto_record_status():
    """Live auto-recording pill: active/paused, latest date, symbols, size."""
    return get_recorder_status()


@router.post("/auto-record/toggle")
async def lakehouse_auto_record_toggle(active: bool = True):
    set_active(active)
    return {"success": True, **get_recorder_status()}


@router.post("/fetch-real")
async def lakehouse_fetch_real(
    start_date: str = "", end_date: str = "",
    symbols: str = "", max_days: int = 12,
):
    """One-click real NSE F&O data fetch + lakehouse cache (offline backtest ke liye)."""
    from starlette.concurrency import run_in_threadpool
    from core.services.option_data_bootstrap import fetch_real_option_data
    syms = [s for s in str(symbols or "").replace(" ", ",").split(",") if s]
    try:
        return await run_in_threadpool(
            fetch_real_option_data, start_date, end_date, syms, max_days)
    except Exception as e:
        return {"success": False, "message": f"Fetch failed: {e}"[:300]}


@router.post("/sync-incremental")
async def lakehouse_incremental_sync(days: int = 5):
    """Optional incremental historical sync (broker connected only).

    Idempotent: rows flow through the same normalized dedup/merge path.
    """
    try:
        from core.services.nse_option_sync import sync_from_nse
        from datetime import datetime, timedelta, timezone
        _ist = timezone(timedelta(hours=5, minutes=30))
        _end = datetime.now(_ist).strftime("%Y-%m-%d")
        _start = (datetime.now(_ist) - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
        res = sync_from_nse(start_date=_start, end_date=_end)
        return {"success": True, "message": "Incremental sync complete — dedup merge applied",
                "result": str(res)[:300]}
    except Exception as e:
        return {"success": False, "message": f"Broker/NSE sync unavailable: {e}"[:300]}


@router.post("/ticks")
async def lakehouse_ticks(q: TickQuery):
    """Query real option ticks from the Parquet lakehouse via DuckDB."""
    try:
        df = query_option_ticks(
            underlying=q.underlying,
            data_mode=q.data_mode,
            expiry=q.expiry,
            start_date=q.start_date,
            end_date=q.end_date,
            option_type=q.option_type,
            strike=q.strike,
            limit=q.limit,
        )
        return {
            "success": True,
            "rows": len(df) if df is not None else 0,
            "data": df.to_dicts() if df is not None and len(df) > 0 else [],
            "source": "parquet_lakehouse",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload", response_model=TickUploadResponse)
async def lakehouse_upload(
    file: UploadFile = File(...),
    data_mode: str = Form("tick"),
    source: str = Form("csv_upload"),
    symbol: Optional[str] = Form(None),
):
    """Idempotent CSV/Parquet upload -> merge into Parquet lakehouse.
    Deduplicates by (timestamp, expiry, strike, option_type) so users never
    need to re-upload the same files."""
    if source not in REAL_TICK_SOURCES:
        raise HTTPException(status_code=400, detail=f"source '{source}' not allowed (synthetic blocked)")

    if data_mode not in ("tick", "1m_chain"):
        raise HTTPException(status_code=400, detail="data_mode must be 'tick' or '1m_chain'")

    try:
        contents = await file.read()
        filename = file.filename or "upload"

        # Detect format
        is_parquet = filename.lower().endswith(".parquet")
        is_csv = filename.lower().endswith(".csv")

        if not is_parquet and not is_csv:
            raise HTTPException(status_code=400, detail="Only .csv or .parquet files allowed")

        import polars as pl

        if is_parquet:
            df = pl.read_parquet(io.BytesIO(contents))
        else:
            df = pl.read_csv(io.BytesIO(contents), infer_schema_length=10000)

        # Normalize columns to our schema
        col_map = {
            "symbol": "underlying",
            "tradingsymbol": "underlying",
            "expiry_date": "expiry",
            "option": "option_type",
            "opt_type": "option_type",
            "ltp": "ltp",
            "last_price": "ltp",
            "close": "close_price",
            "close_price": "close_price",
            "open": "open_price",
            "high": "high_price",
            "low": "low_price",
            "bid_price": "bid",
            "ask_price": "ask",
            "open_interest": "oi",
            "trade_date": "timestamp",
            "timestamp": "timestamp",
        }
        rename = {c: col_map.get(c.lower(), c.lower()) for c in df.columns}
        df = df.rename(rename)

        # Ensure required columns exist with defaults
        for col, dtype in {
            "timestamp": pl.Utf8,
            "underlying": pl.Utf8,
            "expiry": pl.Utf8,
            "option_type": pl.Utf8,
            "strike": pl.Float64,
            "ltp": pl.Float64,
            "open_price": pl.Float64,
            "high_price": pl.Float64,
            "low_price": pl.Float64,
            "close_price": pl.Float64,
            "bid": pl.Float64,
            "ask": pl.Float64,
            "oi": pl.Int64,
            "volume": pl.Int64,
            "iv": pl.Float64,
            "delta": pl.Float64,
            "data_mode": pl.Utf8,
            "source": pl.Utf8,
        }.items():
            if col not in df.columns:
                if dtype == pl.Int64:
                    df = df.with_columns(pl.lit(0).cast(pl.Int64).alias(col))
                elif dtype == pl.Float64:
                    df = df.with_columns(pl.lit(0.0).cast(pl.Float64).alias(col))
                else:
                    df = df.with_columns(pl.lit("").cast(pl.Utf8).alias(col))

        # Apply overrides from form
        if symbol:
            df = df.with_columns(pl.lit(symbol.upper()).alias("underlying"))
        df = df.with_columns(pl.lit(data_mode).alias("data_mode"))
        df = df.with_columns(pl.lit(source).alias("source"))

        rows = df.to_dicts()
        before = len(rows)
        written = store_option_ticks(rows)

        return TickUploadResponse(
            success=True,
            rows_written=written,
            message=f"Uploaded {before} rows, wrote {written} new (dedup applied)",
            duplicates_skipped=max(0, before - written),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@router.post("/compact")
async def lakehouse_compact():
    """Trigger nightly compaction: merge small parquet files per partition."""
    try:
        result = compact_lakehouse()
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/backtest")
async def lakehouse_backtest(
    underlying: str = "FINNIFTY",
    data_mode: str = "1m_chain",
    expiry: str = "",
    start_date: str = "",
    end_date: str = "",
    option_type: Optional[str] = None,
    strike: Optional[float] = None,
):
    """Offline backtest summary from lakehouse data (no broker needed)."""
    try:
        df = query_option_ticks(
            underlying=underlying,
            data_mode=data_mode,
            expiry=expiry,
            start_date=start_date,
            end_date=end_date,
            option_type=option_type,
            strike=strike,
            limit=500_000,
        )
        if df is None or len(df) == 0:
            return {
                "success": False,
                "message": "No cached data for the requested range. Upload data first.",
                "trades": 0,
            }

        # Simple equity simulation: buy at first tick of day, sell at last tick
        # (placeholder for full engine integration - real backtest uses strategy_builder)
        df = df.sort("timestamp")
        first = df[0]
        last = df[-1]
        entry = float(first.get("ltp", 0) or first.get("close_price", 0) or 0)
        exit_px = float(last.get("ltp", 0) or last.get("close_price", 0) or 0)

        return {
            "success": True,
            "source": "parquet_lakehouse",
            "underlying": underlying,
            "data_mode": data_mode,
            "start_date": start_date,
            "end_date": end_date,
            "bars": len(df),
            "entry_price": entry,
            "exit_price": exit_px,
            "gross_points": round(exit_px - entry, 2),
            "message": "Offline lakehouse backtest summary (use /api/backtest/run for full engine)",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
