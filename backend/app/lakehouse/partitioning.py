from __future__ import annotations

from pathlib import Path

from ..config import settings


def partition_dir(underlying: str, data_mode: str, expiry, trade_date) -> Path:
    """Hive-style partition dir: lakehouse/underlying=X/data_mode=Y/expiry=Z/date=D"""
    return (
        settings.LAKEHOUSE_DIR
        / f"underlying={underlying}"
        / f"data_mode={data_mode}"
        / f"expiry={expiry}"
        / f"date={trade_date}"
    )


def lakehouse_glob() -> str:
    return str(settings.LAKEHOUSE_DIR / "**" / "*.parquet")
