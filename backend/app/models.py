from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Strategy(Base):
    """User-configured strategy definition (single source of truth)."""

    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    user_notes = Column(Text, default="")
    config_json = Column(JSON, nullable=False)  # indicators, strike offset, expiry, SL/TP, data mode, lot size
    is_user_configured = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    runs = relationship("BacktestRun", back_populates="strategy", cascade="all, delete-orphan")

    @property
    def config(self):
        return self.config_json


class BacktestRun(Base):
    """One saved backtest run — single source of truth for all BT pills / badges."""

    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False, index=True)
    status = Column(String(32), default="queued", nullable=False)  # queued | running | completed | failed
    data_source = Column(String(32), nullable=False)  # TICK DATA | 1M OPTION CHAIN
    data_mode = Column(String(16), nullable=False)  # tick | 1m_chain
    use_walk_forward = Column(Boolean, default=False, nullable=False)
    is_start = Column(Date, nullable=True)
    is_end = Column(Date, nullable=True)
    oos_start = Column(Date, nullable=True)
    oos_end = Column(Date, nullable=True)
    metrics_json = Column(JSON, default={}, nullable=False)        # MAE/MFE, expectancy, PF, Sharpe, Calmar, MDD, ...
    summary_json = Column(JSON, default={}, nullable=False)        # structured backtest summary block
    started_at = Column(DateTime, default=utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    strategy = relationship("Strategy", back_populates="runs")
    trades = relationship("Trade", back_populates="run", cascade="all, delete-orphan")

    @property
    def created_at(self):
        return self.started_at

    @property
    def metrics(self):
        return self.metrics_json

    @property
    def summary(self):
        return self.summary_json

    __table_args__ = (Index("ix_backtest_runs_strategy_started", "strategy_id", "started_at"),)


class Trade(Base):
    """Individual backtest trade row (Trade History source of truth)."""

    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=False, index=True)
    entry_date = Column(Date, nullable=False)
    entry_time = Column(String(8), nullable=False)
    exit_date = Column(Date, nullable=False)
    exit_time = Column(String(8), nullable=False)
    symbol = Column(String(40), nullable=False)
    trade_type = Column(String(4), nullable=False)  # CE / PE
    strike = Column(Float, nullable=False)
    expiry = Column(Date, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    status = Column(String(32), nullable=False)  # SL hit / TP hit / Trailing SL / Intraday Exit / Open
    pnl = Column(Float, nullable=False)            # option points * lot size (rupees)
    mae = Column(Float, default=0.0, nullable=False)   # max adverse excursion (option points)
    mfe = Column(Float, default=0.0, nullable=False)   # max favorable excursion (option points)
    exit_reason = Column(String(64), nullable=True)
    is_oos = Column(Boolean, default=False, nullable=False)  # walk-forward out-of-sample flag

    run = relationship("BacktestRun", back_populates="trades")


class UploadRecord(Base):
    """Idempotent upload registry — a file is ingested exactly once (by content hash)."""

    __tablename__ = "upload_records"

    id = Column(Integer, primary_key=True, index=True)
    file_hash = Column(String(64), unique=True, index=True, nullable=False)
    filename = Column(String(500), nullable=False)
    file_size = Column(Integer, default=0, nullable=False)
    rows_ingested = Column(Integer, default=0, nullable=False)
    status = Column(String(32), default="ingested", nullable=False)  # ingested | skipped_duplicate | failed
    created_at = Column(DateTime, default=utcnow, nullable=False)


class DataCatalog(Base):
    """Parquet lakehouse catalogue for the Stored Option Data Library."""

    __tablename__ = "data_catalog"

    id = Column(Integer, primary_key=True, index=True)
    underlying = Column(String(40), nullable=False, index=True)
    data_mode = Column(String(16), nullable=False, index=True)  # tick | 1m_chain
    expiry = Column(Date, nullable=False, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    row_count = Column(Integer, default=0, nullable=False)
    min_timestamp = Column(DateTime, nullable=True)
    max_timestamp = Column(DateTime, nullable=True)
    file_path = Column(Text, nullable=True)
    size_bytes = Column(Integer, default=0, nullable=False)
    last_compacted = Column(DateTime, nullable=True)

    __table_args__ = (Index("ix_data_catalog_lookup", "underlying", "data_mode", "expiry", "trade_date"),)
