from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime
from typing import Callable

import polars as pl

from .schema import normalize_option_frame
from .partitioning import partition_dir
from .ingestion import _merge_write_group, _refresh_catalog
from ..config import settings
from ..db import SessionLocal
from ..models import DataCatalog


class AutoRecorder:
    """
    Async ring-buffered live / paper auto-recorder.

    Incoming ticks (live or paper) are dropped into an in-memory ring buffer
    through `offer()` — O(1), lock-free-ish, never blocks the order-execution
    path. A background flush loop drains the ring, deduplicates, normalizes to
    the unified schema, and atomically flushes into the daily Parquet lakehouse
    partitions without touching the broker/order latency path.
    """

    def __init__(self) -> None:
        self._buffer: deque = deque(maxlen=settings.RECORDER_RING_CAPACITY)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._running = False
        self.stats = {"offered": 0, "flushed": 0, "dropped": 0}
        self.tracked_symbols: set[str] = set()
        self.latest_recorded_date: date | None = None
        self.on_tick: Callable[[dict], None] | None = None  # optional fan-out to UI WS

    async def offer(self, tick: dict) -> None:
        """Called by the broker feed handler. Never awaits slow IO."""
        normalized = {
            "timestamp": tick.get("timestamp"),
            "underlying": tick.get("underlying", ""),
            "data_mode": tick.get("data_mode", "tick"),
            "expiry": tick.get("expiry"),
            "option_type": tick.get("option_type", "UNDERLYING"),
            "strike": tick.get("strike", 0),
            "ltp": tick.get("ltp"),
            "bid": tick.get("bid"),
            "ask": tick.get("ask"),
            "oi": tick.get("oi"),
            "volume": tick.get("volume"),
            "source": tick.get("source", "live"),
        }
        async with self._lock:
            self._buffer.append(normalized)
        self.stats["offered"] += 1
        if normalized["underlying"]:
            self.tracked_symbols.add(normalized["underlying"])
        ts = normalized["timestamp"]
        if ts:
            try:
                self.latest_recorded_date = (
                    ts.date() if hasattr(ts, "date") else datetime.fromisoformat(str(ts)).date()
                )
            except Exception:
                pass
        if self.on_tick:
            try:
                self.on_tick(tick)
            except Exception:
                pass

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(settings.RECORDER_FLUSH_INTERVAL_SEC)
            await self.flush()

    async def flush(self) -> int:
        """Drain the ring buffer and persist to today's lakehouse partitions."""
        async with self._lock:
            batch = [
                self._buffer.popleft()
                for _ in range(min(len(self._buffer), settings.RECORDER_FLUSH_BATCH))
            ]
        if not batch:
            return 0

        df = pl.DataFrame(batch)
        try:
            normalized = normalize_option_frame(df)
        except Exception:
            self.stats["dropped"] += len(batch)
            return 0

        db = SessionLocal()
        try:
            total = 0
            for _keys, group in normalized.group_by(
                ["underlying", "data_mode", "expiry", "trade_date"], maintain_order=True
            ):
                total += _merge_write_group(group)
            _refresh_catalog(db)
            self.stats["flushed"] += total
            return total
        finally:
            db.close()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.flush()  # final drain

    def status(self, storage_mb: float = 0.0) -> dict:
        return {
            "running": self._running,
            "buffer_size": len(self._buffer),
            "latest_recorded_date": self.latest_recorded_date,
            "tracked_symbols": sorted(self.tracked_symbols),
            "cached_storage_mb": round(storage_mb, 2),
            "mode": "live" if self._running else "paused",
            "stats": self.stats,
        }


recorder = AutoRecorder()

