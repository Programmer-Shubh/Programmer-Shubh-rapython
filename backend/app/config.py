from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "RaTrade — Institutional Quant Backtesting"
    BASE_DIR: Path = Path(__file__).resolve().parents[1]
    DATA_DIR: Path = Path(os.getenv("RATRADE_DATA_DIR", str(Path.home() / "ratrade_data")))
    LAKEHOUSE_DIR: Path = DATA_DIR / "lakehouse"
    UPLOAD_STAGING_DIR: Path = DATA_DIR / "staging"
    DB_PATH: Path = DATA_DIR / "ratrade.db"
    COMPRESSION: str = "zstd"
    ZSTD_LEVEL: int = 3
    TIMEZONE: str = "Asia/Kolkata"
    MARKET_OPEN: str = "09:15"
    MARKET_CLOSE: str = "15:30"
    INTRADAY_EXIT_TIME: str = "15:15"
    RECORDER_RING_CAPACITY: int = 20_000
    RECORDER_FLUSH_BATCH: int = 500
    RECORDER_FLUSH_INTERVAL_SEC: float = 3.0
    DEFAULT_SLIPPAGE_PCT: float = 0.05
    DEFAULT_LATENCY_MODE: str = "next_tick"  # next_tick | next_bar
    DEFAULT_LOT_SIZE: int = 50
    SEBI_DISCLAIMER: str = (
        "This platform is for educational and research purposes only. It is not a SEBI-registered "
        "investment adviser and nothing herein constitutes investment advice or a recommendation to "
        "buy, sell or hold any derivative or security. Derivatives trading involves substantial risk "
        "of loss and is not suitable for all investors. Past performance is not indicative of future "
        "results. Please consult a qualified, SEBI-registered financial advisor before taking any "
        "investment or trading decision."
    )

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()

for _directory in (settings.DATA_DIR, settings.LAKEHOUSE_DIR, settings.UPLOAD_STAGING_DIR):
    _directory.mkdir(parents=True, exist_ok=True)
