from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Iterable

import polars as pl

from .schema import normalize_option_frame, SCHEMA_COLUMNS, write_parquet_atomic, arrow_table
from .partitioning import partition_dir, lakehouse_glob
from ..config import settings
from ..db import SessionLocal
from ..models import UploadRecord, DataCatalog
from datetime import datetime, timezone


def file_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _merge_write_group(group: pl.DataFrame) -> int:
    """
    Idempotently merge one partition group into the lakehouse:
    read existing files for the same partition, concat, deduplicate by
    (timestamp, expiry, strike, option_type), sort, rewrite atomically.
    Returns number of rows written.
    """
    keys = ["timestamp", "expiry", "strike", "option_type"]
    underlying = group["underlying"][0]
    data_mode = group["data_mode"][0]
    expiry = group["expiry"][0]
    trade_date = group["trade_date"][0]
    pdir = partition_dir(underlying, data_mode, expiry, trade_date)
    pdir.mkdir(parents=True, exist_ok=True)

    frames = [group]
    existing_files = sorted(pdir.glob("part-*.parquet"))
    for f in existing_files:
        try:
            frames.append(pl.read_parquet(f))
        except Exception:
            continue

    merged = pl.concat(frames, how="diagonal_relaxed").unique(subset=keys, keep="last").sort("timestamp")
    total_rows = len(merged)

    # Remove old files, write single compacted file for this partition batch
    for f in existing_files:
        try:
            f.unlink()
        except OSError:
            pass

    out = pdir / f"part-{uuid.uuid4().hex}.parquet"
    write_parquet_atomic(arrow_table(merged.select(SCHEMA_COLUMNS + ["trade_date", "ingested_at"])), out)
    return total_rows


def ingest_dataframe(df: pl.DataFrame, filename: str, db=None) -> dict:
    """Normalize + idempotently merge a DataFrame into the lakehouse."""
    own = db is None
    if own:
        db = SessionLocal()
    try:
        normalized = normalize_option_frame(df)
        total = 0
        for _keys, group in normalized.group_by(
            ["underlying", "data_mode", "expiry", "trade_date"], maintain_order=True
        ):
            total += _merge_write_group(group)

        _refresh_catalog(db)
        record = UploadRecord(
            filename=filename, file_size=0, rows_ingested=total, status="ingested"
        )
        # file hash is set by the caller for file-based uploads
        db.add(record)
        db.commit()
        db.refresh(record)
        return {"status": "ingested", "rows_ingested": total, "upload_id": record.id}
    finally:
        if own:
            db.close()


def _refresh_catalog(db) -> None:
    """Rebuild lightweight catalogue entries from the lakehouse filesystem."""
    import duckdb

    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            SELECT underlying, data_mode, CAST(expiry AS DATE) AS expiry,
                   CAST(trade_date AS DATE) AS trade_date,
                   COUNT(*) AS row_count,
                   MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts,
                   SUM(size_bytes) AS size_bytes
            FROM (
                SELECT underlying, data_mode, expiry, trade_date, timestamp,
                       1 AS size_bytes
                FROM read_parquet('{lakehouse_glob()}', hive_partitioning=1)
            )
            GROUP BY underlying, data_mode, expiry, trade_date
            """
        ).fetchall()
    finally:
        con.close()

    # Replace catalogue wholesale (cheap for local dev scale)
    db.query(DataCatalog).delete()
    for underlying, data_mode, expiry, trade_date, row_count, min_ts, max_ts, _ in rows:
        pdir = partition_dir(underlying, data_mode, expiry, trade_date)
        size_bytes = sum(f.stat().st_size for f in pdir.glob("part-*.parquet")) if pdir.exists() else 0
        db.add(
            DataCatalog(
                underlying=underlying,
                data_mode=data_mode,
                expiry=expiry,
                trade_date=trade_date,
                row_count=row_count or 0,
                min_timestamp=min_ts,
                max_timestamp=max_ts,
                file_path=str(pdir),
                size_bytes=size_bytes,
                last_compacted=datetime.now(timezone.utc),
            )
        )
    db.commit()


def ingest_file(file_path: str, filename: str | None = None) -> dict:
    """Public entry: validate hash (idempotency), read, normalize, merge."""
    filename = filename or Path(file_path).name
    h = file_hash(file_path)

    db = SessionLocal()
    try:
        existing = db.query(UploadRecord).filter_by(file_hash=h).first()
        if existing:
            return {
                "status": "skipped_duplicate",
                "file_hash": h,
                "filename": existing.filename,
                "rows_ingested": existing.rows_ingested,
                "message": "File already ingested — no re-upload required.",
            }

        ext = Path(file_path).suffix.lower()
        if ext == ".parquet":
            lf = pl.scan_parquet(file_path)
        elif ext == ".csv":
            lf = pl.scan_csv(file_path, infer_schema_length=50_000, ignore_errors=True)
        else:
            raise ValueError(f"Unsupported file extension: {ext}")

        df = lf.collect()
        result = ingest_dataframe(df, filename, db=db)

        record = db.query(UploadRecord).filter_by(filename=filename).order_by(UploadRecord.id.desc()).first()
        if record:
            record.file_hash = h
            record.file_size = os.path.getsize(file_path)
            db.commit()

        return {
            "status": result["status"],
            "file_hash": h,
            "filename": filename,
            "rows_ingested": result["rows_ingested"],
            "message": "Ingested into lakehouse." if result["status"] == "ingested" else "Skipped (duplicate).",
        }
    finally:
        db.close()
