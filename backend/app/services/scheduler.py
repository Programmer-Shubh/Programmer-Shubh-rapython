from __future__ import annotations

import asyncio
from datetime import time

from ..lakehouse.compaction import run_nightly_compaction
from ..config import settings


class NightlyCompactionScheduler:
    """Runs lakehouse compaction once daily (default 02:00 IST)."""

    def __init__(self, run_at: time = time(2, 0)) -> None:
        self.run_at = run_at
        self._task: asyncio.Task | None = None
        self._running = False

    async def _loop(self) -> None:
        while self._running:
            now = __import__("datetime").datetime.now()
            target = now.replace(hour=self.run_at.hour, minute=self.run_at.minute, second=0, microsecond=0)
            if now > target:
                target = target + __import__("datetime").timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                run_nightly_compaction()
            except Exception:  # pragma: no cover - scheduler must not die
                pass

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
