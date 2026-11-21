from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from ..config import settings


class BrokerAdapter(ABC):
    """Broker abstraction. Live/Paper adapters (Zerodha, Dhan, ...) subclass this."""

    @abstractmethod
    async def connect(self) -> bool:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    async def subscribe_options(self, symbols: list[str], on_tick: Callable[[dict], None]) -> None:
        """Stream live option ticks / 1-min chain snapshots via on_tick callback."""
        ...

    @abstractmethod
    async def get_historical_options(
        self, underlying: str, from_date, to_date, data_mode: str
    ) -> list[dict]:
        """Incremental historical sync (called by the sync button when broker connected)."""
        ...

    @abstractmethod
    async def place_order(self, symbol: str, side: str, qty: int, price: float | None = None) -> dict:
        ...


class DummyBrokerAdapter(BrokerAdapter):
    """
    Offline / development adapter. Returns no market data — RaTrade's backtester
    runs 100% from the local Parquet lakehouse, so a broker connection is never
    required for backtesting or paper trading.
    """

    def __init__(self) -> None:
        self.connected = False

    async def connect(self) -> bool:
        self.connected = False
        return False

    async def disconnect(self) -> None:
        self.connected = False

    async def subscribe_options(self, symbols: list[str], on_tick) -> None:
        # No live feed in offline mode; the recorder stays paused.
        return

    async def get_historical_options(self, underlying, from_date, to_date, data_mode: str) -> list[dict]:
        return []

    async def place_order(self, symbol: str, side: str, qty: int, price: float | None = None) -> dict:
        return {"status": "simulated", "symbol": symbol, "side": side, "qty": qty, "price": price}


broker_adapter = DummyBrokerAdapter()
