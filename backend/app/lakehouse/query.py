from __future__ import annotations

from datetime import date, datetime
from typing import Any

import duckdb
import polars as pl

from .partitioning import lakehouse_glob
from ..config import settings

def _get_con():
    return duckdb.connect()


def _base_sql() -> str:
    return f"""
        SELECT
            timestamp, underlying, data_mode, expiry, option_type, strike,
            ltp, open, high, low, close, bid, ask, oi, volume, iv, delta,
            source
        FROM read_parquet('{lakehouse_glob()}', hive_partitioning=1)
    """


def query_options(
    underlying: str | None = None,
    data_mode: str | None = None,
    expiry: date | None = None,
    option_type: str | None = None,
    strike: float | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
) -> pl.DataFrame:
    """Predicate-pushdown query over the Parquet lakehouse via DuckDB."""
    conditions = ["1=1"]
    params: list[Any] = []

    if underlying is not None:
        conditions.append("underlying = ?")
        params.append(underlying)
    if data_mode is not None:
        conditions.append("data_mode = ?")
        params.append(data_mode)
    if expiry is not None:
        conditions.append("expiry = ?")
        params.append(expiry)
    if option_type is not None:
        conditions.append("option_type = ?")
        params.append(option_type)
    if strike is not None:
        conditions.append("strike = ?")
        params.append(strike)
    if start is not None:
        conditions.append("timestamp >= ?")
        params.append(start)
    if end is not None:
        conditions.append("timestamp <= ?")
        params.append(end)

    sql = _base_sql() + " WHERE " + " AND ".join(conditions) + " ORDER BY timestamp"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    con = _get_con()
    try:
        pdf = con.execute(sql, params).fetchdf()
    finally:
        con.close()
    if pdf.empty:
        return pl.DataFrame()
    return pl.from_pandas(pdf)


def scan_lazy(
    underlying: str | None = None,
    data_mode: str | None = None,
    expiry: date | None = None,
) -> pl.LazyFrame:
    """Lazy Polars scan over the lakehouse with hive-partition pruning."""
    lf = pl.scan_parquet(lakehouse_glob(), hive_partitioning=True)
    if underlying is not None:
        lf = lf.filter(pl.col("underlying") == underlying)
    if data_mode is not None:
        lf = lf.filter(pl.col("data_mode") == data_mode)
    if expiry is not None:
        lf = lf.filter(pl.col("expiry") == expiry)
    return lf


def get_contract_series(
    underlying: str,
    data_mode: str,
    expiry: date,
    option_type: str,
    strike: float,
    start_ts: datetime,
    end_ts: datetime,
) -> pl.DataFrame:
    """Fetch one option contract's real premium series (tick or 1-min chain)."""
    return query_options(
        underlying=underlying,
        data_mode=data_mode,
        expiry=expiry,
        option_type=option_type,
        strike=strike,
        start=start_ts,
        end=end_ts,
    )


def get_underlying_series(
    underlying: str,
    data_mode: str,
    start_ts: datetime,
    end_ts: datetime,
) -> pl.DataFrame:
    """Fetch underlying index quote rows (option_type='UNDERLYING') for indicator math."""
    return query_options(
        underlying=underlying,
        data_mode=data_mode,
        option_type="UNDERLYING",
        start=start_ts,
        end=end_ts,
    )
