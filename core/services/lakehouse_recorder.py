"""Async ring-buffered live/paper option auto-recorder -> Parquet lakehouse.

Non-blocking by design: the WebSocket callback only does a bounded
``queue.Queue(maxlen=N).put_nowait``. A dedicated daemon worker batches,
de-duplicates and flushes real ticks into the zero-copy Parquet lakehouse, so
live/paper order execution never waits on disk I/O. Synthetic/BS prices are
never accepted (``option_lakehouse`` rejects them again at ingestion).
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from core.services.option_lakehouse import (
    OPTION_TICKS_ROOT,
    get_lakehouse_status,
    store_option_ticks,
)

_IST = timezone(timedelta(hours=5, minutes=30))
_RING_SIZE = 20_000
_FLUSH_SECS = 5.0
_FLUSH_ROWS = 500

_ring: "queue.Queue[dict]" = queue.Queue(maxsize=_RING_SIZE)
_state = {"active": True, "enqueued": 0, "flushed": 0, "dropped": 0,
          "last_flush": "", "last_error": ""}
_lock = threading.Lock()
_worker: threading.Thread | None = None


def _ist_now() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")


def enqueue_tick(symbol: str, ltp: float, *, volume: int = 0,
                 bid: float = 0.0, ask: float = 0.0, data_mode: str = "tick") -> bool:
    """Zero-latency WS hook. Returns False only if the ring is saturated."""
    if not _state["active"] or not symbol or float(ltp or 0) <= 0:
        return False
    row = {
        "timestamp": _ist_now(),
        "underlying": str(symbol).upper(),
        "ltp": float(ltp), "bid": float(bid or 0), "ask": float(ask or 0),
        "volume": int(volume or 0), "source": "fyers_ws", "data_mode": data_mode,
    }
    try:
        _ring.put_nowait(row)
        with _lock:
            _state["enqueued"] += 1
        _start_worker()
        return True
    except queue.Full:
        with _lock:
            _state["dropped"] += 1
        return False
    except Exception:
        return False


def _flush(rows: List[dict]) -> None:
    if not rows:
        return
    try:
        written = store_option_ticks(rows)
        with _lock:
            _state["flushed"] += int(written or 0)
            _state["last_flush"] = _ist_now()
            _state["last_error"] = ""
    except Exception as exc:  # recorder must never break the WS loop
        with _lock:
            _state["last_error"] = str(exc)[:200]


def _worker_loop() -> None:
    batch: List[dict] = []
    last = time.monotonic()
    while True:
        try:
            batch.append(_ring.get(timeout=0.5))
        except queue.Empty:
            pass
        now = time.monotonic()
        if batch and (len(batch) >= _FLUSH_ROWS or now - last >= _FLUSH_SECS):
            _flush(batch)
            batch = []
            last = now
        time.sleep(0.01)


def _start_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, name="lakehouse-recorder", daemon=True)
            _worker.start()


def set_active(active: bool) -> None:
    with _lock:
        _state["active"] = bool(active)


def get_recorder_status() -> Dict[str, object]:
    """Live auto-recording pill data: active/paused, latest date, symbols, size."""
    st = get_lakehouse_status()
    with _lock:
        snap = dict(_state)
    snap.update({
        "active": bool(snap.get("active")),
        "worker_alive": bool(_worker and _worker.is_alive()),
        "ring_pending": _ring.qsize(),
        "ring_size": _RING_SIZE,
        "latest_date": (st.get("date_range") or {}).get("end") or "",
        "tracked_symbols": st.get("symbols") or [],
        "total_size_mb": st.get("total_size_mb") or 0,
        "lakehouse_root": str(OPTION_TICKS_ROOT),
    })
    return snap
