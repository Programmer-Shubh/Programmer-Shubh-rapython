from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl

from .schema import arrow_table, SCHEMA_COLUMNS, write_parquet_atomic
from .partitioning import partition_dir, lakehouse_glob
from ..config import settings
from ..db import SessionLocal
from ..models import DataCatalog
from datetime import datetime, timezone


def _partitions_to_compact(db) -> list[tuple]:
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            SELECT underlying, data_mode, CAST(expiry AS DATE), CAST(trade_date AS DATE),
                   COUNT(*) AS file_count
            FROM read_parquet('{lakehouse_glob()}', hive_partitioning=1)
            GROUP BY underlying, data_mode, expiry, trade_date
            HAVING COUNT(*) > 1 OR COUNT(*) = 0
            """
        ).fetchall()
    finally:
        con.close()
    return rows


def compact_partition(db, underlying: str, data_mode: str, expiry, trade_date) -> dict:
    """Rewrite a partition into a single sorted ZSTD Parquet file (nightly compaction)."""
    pdir = partition_dir(underlying, data_mode, expiry, trade_date)
    files = sorted(pdir.glob("part-*.parquet"))
    if len(files) <= 1:
        return {"compacted": False, "reason": "nothing to compact"}

    frames = [pl.read_parquet(f) for f in files]
    merged = pl.concat(frames, how="diagonal_relaxed").unique(
        subset=["timestamp", "expiry", "strike", "option_type"], keep="last"
    ).sort("timestamp")

    for f in files:
        try:
            f.unlink()
        except OSError:
            pass

    out = pdir / f"part-compacted-{expiry}-{trade_date}.parquet"
    write_parquet_atomic(arrow_table(merged.select(SCHEMA_COLUMNS + ["trade_date", "ingested_at"])), out)

    size_bytes = sum(f.stat().st_size for f in pdir.glob("part-*.parquet"))
    cat = (
        db.query(DataCatalog)
        .filter_by(underlying=underlying, data_mode=data_mode, expiry=expiry, trade_date=trade_date)
        .first()
    )
    if cat:
        cat.size_bytes = size_bytes
        cat.row_count = len(merged)
        cat.last_compacted = datetime.now(timezone.utc)
        db.commit()
    return {"compacted": True, "rows": len(merged), "size_bytes": size_bytes}


def run_nightly_compaction() -> dict:
    """Compact every partition with multiple files. Called nightly (or on demand)."""
    db = SessionLocal()
    try:
        parts = _partitions_to_compact(db)
        results = [compact_partition(db, *p[:4]) for p in parts]
        return {"partitions_scanned": len(parts), "results": results}
    finally:
        db.close()
