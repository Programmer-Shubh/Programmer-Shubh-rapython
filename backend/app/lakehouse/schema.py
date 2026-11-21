from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from ..config import settings

# ---------------------------------------------------------------------------
# Unified option & Greeks schema (single source of truth for every path)
# ---------------------------------------------------------------------------
UNIFIED_OPTION_SCHEMA = pa.schema(
    [
        ("timestamp", pa.timestamp("ns", tz="Asia/Kolkata")),
        ("underlying", pa.string()),
        ("data_mode", pa.string()),          # "tick" | "1m_chain"
        ("expiry", pa.date32()),
        ("option_type", pa.string()),        # "CE" | "PE" | "UNDERLYING"
        ("strike", pa.float64()),
        ("ltp", pa.float64()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("bid", pa.float64()),
        ("ask", pa.float64()),
        ("oi", pa.int64()),
        ("volume", pa.int64()),
        ("iv", pa.float64()),
        ("delta", pa.float64()),
        ("source", pa.string()),
    ]
)

SCHEMA_COLUMNS = [f.name for f in UNIFIED_OPTION_SCHEMA]

REQUIRED_COLUMNS = {"timestamp", "underlying", "expiry", "option_type", "strike", "ltp"}

OPTION_TYPES = {"CE", "PE"}
UNDERLYING_TYPES = {"UNDERLYING", "IDX", "INDEX", "FUT", "FUTURE"}

COLUMN_ALIASES: dict[str, list[str]] = {
    "timestamp": ["timestamp", "dt", "datetime", "time", "ts", "date_time", "trade_timestamp"],
    "data_mode": ["data_mode", "mode", "datamode"],
    "underlying": ["underlying", "symbol", "underlier", "index", "instrument"],
    "expiry": ["expiry", "expiration", "exp", "expiry_date"],
    "option_type": ["option_type", "type", "optiontype", "instrument_type", "opt_type"],
    "strike": ["strike", "strk"],
    "ltp": ["ltp", "last_price", "last", "close_price", "price", "traded_price"],
    "open": ["open"],
    "high": ["high"],
    "low": ["low"],
    "close": ["close"],
    "bid": ["bid", "bid_price"],
    "ask": ["ask", "ask_price"],
    "oi": ["oi", "open_interest", "openinterest"],
    "volume": ["volume", "vol", "qty", "trade_quantity"],
    "iv": ["iv", "implied_volatility", "implied_vol", "iv_iv"],
    "delta": ["delta"],
    "source": ["source", "feed", "provider"],
}

_TIMESTAMP_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S%.f",
    "%Y-%m-%d %H:%M:%S.%f",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d",
]


def _resolve_columns(columns: Iterable[str]) -> dict[str, str]:
    lower = {c.lower(): c for c in columns}
    mapping: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lower:
                mapping[canonical] = lower[alias]
                break
    return mapping


def _parse_ts_expr(col: pl.Expr) -> pl.Expr:
    """Parse a timestamp column, assuming IST when naive."""
    return (
        col.cast(pl.Utf8)
        .str.to_datetime(format="%Y-%m-%d %H:%M:%%.f", strict=False)
        .alias("_parsed")
    )


def normalize_option_frame(frame) -> pl.DataFrame:
    """
    Validate & normalize any option CSV/Parquet/tick frame into the unified schema.

    Rules:
      * timestamp parsed & normalized to Asia/Kolkata (naive -> IST, aware -> converted)
      * expiry normalized to a date
      * option_type upper-cased & mapped (CALL->CE, PUT->PE, UNDERLYING/IDX->UNDERLYING)
      * ltp fallback to close and vice versa
      * bid/ask fallback: missing -> ltp (zero-spread); single missing -> ltp
      * deduplicated by (timestamp, expiry, strike, option_type), keep last
      * sorted by timestamp
    """
    if isinstance(frame, pl.LazyFrame):
        frame = frame.collect()
    if not isinstance(frame, pl.DataFrame):
        frame = pl.DataFrame(frame)

    colmap = _resolve_columns(frame.columns)
    missing = [c for c in REQUIRED_COLUMNS if c not in colmap]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    df = frame.select([pl.col(colmap[c]).alias(c) for c in colmap.keys()])

    for name in SCHEMA_COLUMNS:
        if name not in df.columns:
            df = df.with_columns(pl.lit(None).cast(pl.Float64).alias(name) if name in ("oi", "volume") else pl.lit(None).alias(name))

    # --- timestamp: IST normalization (handles tz-aware / naive / string) ---
    ts_dtype = df.schema["timestamp"]
    base = pl.col("timestamp")
    if isinstance(ts_dtype, pl.Datetime):
        if ts_dtype.time_zone is not None:
            ts = base.dt.convert_time_zone("Asia/Kolkata")
        else:
            ts = base.dt.replace_time_zone("Asia/Kolkata")
    else:
        ts = base.cast(pl.Utf8, strict=False).str.to_datetime(time_zone="Asia/Kolkata")
    df = df.with_columns(ts.alias("timestamp"))

    # --- expiry: date normalization ---
    df = df.with_columns(
        pl.coalesce(
            pl.col("expiry").cast(pl.Date, strict=False),
            pl.col("expiry").cast(pl.Utf8, strict=False).str.to_date("%d-%m-%Y", strict=False),
            pl.col("expiry").cast(pl.Utf8, strict=False).str.to_date("%d/%m/%Y", strict=False),
            pl.col("expiry").cast(pl.Utf8, strict=False).str.to_date("%Y/%m/%d", strict=False),
        ).alias("expiry")
    )

    # --- option type normalization ---
    df = df.with_columns(
        pl.col("option_type").cast(pl.Utf8, strict=False).str.to_uppercase().str.strip_chars().alias("option_type")
    )
    df = df.with_columns(
        pl.when(pl.col("option_type").is_in(["CALL"]))
        .then(pl.lit("CE"))
        .when(pl.col("option_type").is_in(["PUT"]))
        .then(pl.lit("PE"))
        .when(pl.col("option_type").is_in(list(UNDERLYING_TYPES)))
        .then(pl.lit("UNDERLYING"))
        .otherwise(pl.col("option_type"))
        .alias("option_type")
    )

    # --- numeric casting ---
    numeric_cols = ["strike", "ltp", "open", "high", "low", "close", "bid", "ask", "iv", "delta"]
    for c in numeric_cols:
        df = df.with_columns(pl.col(c).cast(pl.Float64, strict=False).alias(c))
    df = df.with_columns(
        pl.col("oi").cast(pl.Int64, strict=False).alias("oi"),
        pl.col("volume").cast(pl.Int64, strict=False).alias("volume"),
    )

    # --- fallback chain ---
    df = df.with_columns(
        pl.coalesce(pl.col("ltp"), pl.col("close")).alias("ltp")
    )
    df = df.with_columns(
        pl.coalesce(pl.col("close"), pl.col("ltp")).alias("close")
    )
    df = df.with_columns(
        pl.coalesce(pl.col("bid"), pl.col("ltp")).alias("bid"),
        pl.coalesce(pl.col("ask"), pl.col("ltp")).alias("ask"),
    )
    df = df.with_columns(pl.col("oi").fill_null(0).alias("oi"), pl.col("volume").fill_null(0).alias("volume"))

    # --- dedupe & sort ---
    df = df.unique(subset=["timestamp", "expiry", "strike", "option_type"], keep="last")
    df = df.sort("timestamp")

    # --- trade_date partition column ---
    df = df.with_columns(pl.col("timestamp").dt.date().alias("trade_date"))

    out_cols = SCHEMA_COLUMNS + ["trade_date", "ingested_at"]
    df = df.with_columns(pl.lit(datetime.now().astimezone().isoformat()).alias("ingested_at"))
    return df.select(out_cols)


def arrow_table(df: pl.DataFrame):
    """Convert to a PyArrow table cast to the unified schema (zero-copy friendly).

    Non-schema columns (e.g. trade_date, ingested_at) are preserved as-is.
    """
    table = df.to_arrow()
    unified_names = [f.name for f in UNIFIED_OPTION_SCHEMA]
    to_cast = [c for c in unified_names if c in table.column_names]
    casted = table.select(to_cast).cast(UNIFIED_OPTION_SCHEMA)
    for c in table.column_names:
        if c not in unified_names:
            casted = casted.append_column(c, table.column(c))
    return casted


def write_parquet_atomic(table, path) -> None:
    """Write a Parquet file atomically (temp file + os.replace), ZSTD compressed."""
    import os
    import uuid

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".part-{uuid.uuid4().hex}.parquet.tmp"
    pq.write_table(
        table,
        tmp,
        compression="zstd",
        compression_level=settings.ZSTD_LEVEL,
        write_statistics=True,
        use_dictionary=True,
    )
    os.replace(tmp, path)
