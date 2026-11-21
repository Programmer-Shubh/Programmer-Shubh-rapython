from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class StrategyBase(BaseModel):
    name: str
    user_notes: str = ""
    config: dict[str, Any]


class StrategyCreate(StrategyBase):
    pass


class StrategyUpdate(BaseModel):
    name: str | None = None
    user_notes: str | None = None
    config: dict[str, Any] | None = None


class StrategyOut(StrategyBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_user_configured: bool = True
    created_at: datetime
    updated_at: datetime


class BacktestRunCreate(BaseModel):
    strategy_id: int
    data_mode: str  # tick | 1m_chain
    use_walk_forward: bool = False
    is_start: date | None = None
    is_end: date | None = None
    oos_start: date | None = None
    oos_end: date | None = None


class BacktestRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: int
    status: str
    data_source: str
    data_mode: str
    metrics: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    created_at: datetime


class TradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entry_date: date
    entry_time: str
    exit_date: date
    exit_time: str
    symbol: str
    trade_type: str
    strike: float
    expiry: date
    entry_price: float
    exit_price: float
    status: str
    pnl: float
    mae: float
    mfe: float
    exit_reason: str | None = None
    is_oos: bool = False


class CatalogEntry(BaseModel):
    underlying: str
    data_mode: str
    expiry: date
    trade_date: date
    row_count: int
    min_timestamp: datetime | None
    max_timestamp: datetime | None
    size_bytes: int


class JobProgress(BaseModel):
    job_id: str
    status: str  # queued | running | completed | failed
    progress: float = 0.0
    message: str = ""
    result: dict[str, Any] | None = None


class RecorderStatus(BaseModel):
    running: bool
    buffer_size: int
    latest_recorded_date: date | None
    tracked_symbols: list[str]
    cached_storage_mb: float
    mode: str | None = None
