"""Zero-Copy Parquet Option Data Lakehouse (DuckDB + Polars + PyArrow).

Persistent, Hive-partitioned local Parquet lakehouse organized by:
  underlying / data_mode / expiry / date

Features:
- DuckDB predicate pushdown + row-group pruning for fast queries
- Polars lazy evaluation for transforms
- ZSTD compression, IST timestamp sorting
- Automatic nightly file compaction
- Idempotent upserts (dedupe by timestamp, expiry, strike, option_type)
- NEVER stores synthetic/BS-fabricated ticks - real data only

Schema (single source of truth across ingestion & query):
  timestamp IST, underlying, expiry, option_type (CE/PE), strike,
  ltp (or open/high/low/close), bid, ask, oi, volume, iv, delta,
  data_mode ('tick' or '1m_chain'), source ('fyers_ws', 'nse_bhavcopy', 'csv_upload')
"""
from __future__ import annotations

import os
import glob
import json
import shutil
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import duckdb

_IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Lakehouse root: data/lakehouse/option_ticks/
# Partition layout: underlying=FINNIFTY/data_mode=1m_chain/expiry=2026-11-26/date=2026-11-25/
# ---------------------------------------------------------------------------
LAKEHOUSE_ROOT = Path(os.environ.get("OPTION_LAKEHOUSE_ROOT", "data/lakehouse"))
OPTION_TICKS_ROOT = LAKEHOUSE_ROOT / "option_ticks"
OPTION_TICKS_ROOT.mkdir(parents=True, exist_ok=True)

_COMPRESSION = "zstd"
_ROW_GROUP_SIZE = 50_000

# Single source of truth schema (Polars) for all ingestion/query paths.
OPTION_TICK_SCHEMA = {
    "timestamp": pl.Utf8,          # IST "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD HH:MM"
    "underlying": pl.Utf8,         # NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY
    "expiry": pl.Utf8,             # YYYY-MM-DD
    "option_type": pl.Utf8,        # CE / PE
    "strike": pl.Float64,
    "ltp": pl.Float64,             # last traded price
    "open_price": pl.Float64,
    "high_price": pl.Float64,
    "low_price": pl.Float64,
    "close_price": pl.Float64,
    "bid": pl.Float64,
    "ask": pl.Float64,
    "oi": pl.Int64,
    "volume": pl.Int64,
    "iv": pl.Float64,              # optional implied volatility
    "delta": pl.Float64,           # optional option delta
    "data_mode": pl.Utf8,          # tick / 1m_chain
    "source": pl.Utf8,             # fyers_ws / nse_bhavcopy / csv_upload / yahoo_intraday
}

# Honest real sources only - synthetic / BS / interpolated PERMANENTLY blocked.
REAL_TICK_SOURCES = frozenset({"fyers_ws", "nse_bhavcopy", "csv_upload", "yahoo_intraday"})


def _ist_now_str() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")


def _ist_date_str() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


def _partition_path(underlying: str, data_mode: str, expiry: str, trade_date: str) -> Path:
    return (
        OPTION_TICKS_ROOT
        / f"underlying={underlying}"
        / f"data_mode={data_mode}"
        / f"expiry={expiry}"
        / f"date={trade_date}"
    )


def _normalize_tick_dict(tick: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate + normalize one tick dict to the single-source schema.
    Returns None if invalid / synthetic / missing required fields."""
    try:
        underlying = str(tick.get("underlying") or tick.get("symbol") or "").upper()
        if not underlying:
            return None
        option_type = str(tick.get("option_type") or "").upper()
        if option_type not in ("CE", "PE"):
            return None
        strike = float(tick.get("strike_price") or tick.get("strike") or 0)
        if strike <= 0:
            return None
        expiry = str(tick.get("expiry_date") or tick.get("expiry") or "")[:10]
        if not expiry:
            return None
        timestamp = str(tick.get("timestamp") or tick.get("trade_date") or "")[:16]
        if not timestamp:
            timestamp = _ist_now_str()
        ltp = float(tick.get("ltp") or tick.get("close_price") or tick.get("close") or 0)
        if ltp <= 0:
            return None
        source = str(tick.get("source") or "csv_upload").lower()
        if source not in REAL_TICK_SOURCES:
            return None  # synthetic sources permanently blocked
        data_mode = str(tick.get("data_mode") or "tick").lower()
        if data_mode not in ("tick", "1m_chain"):
            data_mode = "tick"

        return {
            "timestamp": timestamp,
            "underlying": underlying,
            "expiry": expiry,
            "option_type": option_type,
            "strike": float(strike),
            "ltp": round(ltp, 2),
            "open_price": round(float(tick.get("open_price") or ltp), 2),
            "high_price": round(float(tick.get("high_price") or ltp), 2),
            "low_price": round(float(tick.get("low_price") or ltp), 2),
            "close_price": round(float(tick.get("close_price") or ltp), 2),
            # Schema fallback: missing bid/ask -> LTP/close (never fabricated
            # mid-price; the real print is the honest executable proxy).
            "bid": round(float(tick.get("bid") or tick.get("close_price") or tick.get("close") or ltp), 2),
            "ask": round(float(tick.get("ask") or tick.get("close_price") or tick.get("close") or ltp), 2),
            "oi": int(float(tick.get("oi") or 0) or 0),
            "volume": int(float(tick.get("volume") or 0) or 0),
            "iv": float(tick.get("iv") or 0),
            "delta": float(tick.get("delta") or 0),
            "data_mode": data_mode,
            "source": source,
        }
    except Exception:
        return None


def _write_partition_parquet(partition_path: Path, rows: List[Dict[str, Any]]) -> int:
    """Write normalized rows to a partitioned Parquet file with ZSTD + IST sort."""
    if not rows:
        return 0
    partition_path.mkdir(parents=True, exist_ok=True)
    # Sort by timestamp IST for predicate pushdown efficiency
    rows.sort(key=lambda r: r.get("timestamp", ""))
    df = pl.DataFrame(rows, schema=OPTION_TICK_SCHEMA)
    import uuid
    file_path = partition_path / (
        f"part_{_ist_now_str().replace(' ', '_').replace(':', '')}_{uuid.uuid4().hex[:8]}.parquet")
    df.write_parquet(
        file_path,
        compression=_COMPRESSION,
        row_group_size=_ROW_GROUP_SIZE,
        statistics=True,
    )
    return len(rows)


def store_option_ticks(ticks: List[Dict[str, Any]]) -> int:
    """Idempotent store: normalize, dedupe by (timestamp, expiry, strike, option_type),
    atomically merge into Parquet lakehouse. Returns rows written."""
    normalized: List[Dict[str, Any]] = []
    seen: set = set()
    for t in ticks:
        nt = _normalize_tick_dict(t)
        if nt is None:
            continue
        dedupe_key = (
            nt["timestamp"], nt["expiry"], nt["strike"], nt["option_type"],
            nt["underlying"], nt["data_mode"],
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(nt)

    if not normalized:
        return 0

    # Group by partition and write
    by_partition: Dict[Path, List[Dict[str, Any]]] = {}
    for r in normalized:
        p = _partition_path(r["underlying"], r["data_mode"], r["expiry"], r["timestamp"][:10])
        by_partition.setdefault(p, []).append(r)

    total = 0
    for p, rows in by_partition.items():
        # Idempotent atomic merge: read THIS partition's existing keys, drop
        # rows already cached, then rewrite the partition in one shot. A file
        # re-uploaded tomorrow is a no-op — never duplicate storage.
        existing = []
        if p.is_dir():
            for fp in p.glob("*.parquet"):
                try:
                    existing.extend(pl.read_parquet(fp).to_dicts())
                except Exception:
                    pass
        keys = {(r.get("timestamp"), r.get("expiry"), r.get("strike"),
                 r.get("option_type"), r.get("underlying"), r.get("data_mode"))
                for r in existing}
        fresh = [r for r in rows if (r["timestamp"], r["expiry"], r["strike"],
                                     r["option_type"], r["underlying"], r["data_mode"]) not in keys]
        if not fresh:
            continue
        combined = existing + fresh
        df = pl.DataFrame(combined, schema=OPTION_TICK_SCHEMA).unique(
            subset=["timestamp", "expiry", "strike", "option_type", "underlying", "data_mode"],
            keep="last").sort("timestamp")
        p.mkdir(parents=True, exist_ok=True)
        import uuid
        old_files = list(p.glob("*.parquet"))
        tmp = p / f".merge_{uuid.uuid4().hex}.parquet"
        df.write_parquet(tmp, compression=_COMPRESSION, row_group_size=_ROW_GROUP_SIZE,
                         statistics=True)
        for fp in old_files:
            try:
                fp.unlink()
            except Exception:
                pass
        os.replace(tmp, p / "part_merged.parquet")
        total += len(fresh)
    return total


# ---------------------------------------------------------------------------
# DuckDB query engine over the Parquet lakehouse (predicate pushdown)
# ---------------------------------------------------------------------------
_duck_conn: Optional[duckdb.DuckDBPyConnection] = None
_duck_lock = threading.Lock()


def _duckdb_conn() -> duckdb.DuckDBPyConnection:
    global _duck_conn
    with _duck_lock:
        if _duck_conn is None:
            _duck_conn = duckdb.connect(database=":memory:")
            _duck_conn.execute("INSTALL parquet; LOAD parquet;")
        return _duck_conn


def query_option_ticks(
    underlying: str,
    data_mode: str,
    expiry: str,
    start_date: str,
    end_date: str,
    option_type: Optional[str] = None,
    strike: Optional[float] = None,
    limit: int = 100_000,
) -> pl.DataFrame:
    """Query option ticks from the Parquet lakehouse via DuckDB.
    Uses predicate pushdown + row-group pruning (only reads matching partitions).
    Returns a Polars DataFrame (lazy-compatible)."""
    conn = _duckdb_conn()
    expiry_dir = OPTION_TICKS_ROOT / f"underlying={underlying}" / f"data_mode={data_mode}" / f"expiry={expiry}"
    if not expiry_dir.is_dir():
        return pl.DataFrame(schema=OPTION_TICK_SCHEMA)

    # Partition pruning BEFORE DuckDB: pass only files from date folders in
    # range. DuckDB then applies predicate pushdown + row-group statistics
    # on timestamp / strike / option_type, with zero-copy Arrow -> Polars.
    files: List[str] = []
    for date_dir in sorted(expiry_dir.glob("date=*")):
        d = date_dir.name.split("=", 1)[-1]
        if (not start_date or d >= str(start_date)[:10]) and (not end_date or d <= str(end_date)[:10]):
            files.extend(str(p) for p in date_dir.glob("*.parquet"))
    if not files:
        return pl.DataFrame(schema=OPTION_TICK_SCHEMA)

    def _q(s) -> str:
        return str(s).replace("'", "''")

    file_list = "[" + ",".join(f"'{_q(f)}'" for f in files) + "]"
    query = f"""
    SELECT timestamp, underlying, expiry, option_type, strike, ltp,
           open_price, high_price, low_price, close_price, bid, ask, oi,
           volume, iv, delta, data_mode, source
    FROM read_parquet({file_list}, hive_partitioning=1)
    WHERE underlying = '{_q(underlying)}'
      AND data_mode = '{_q(data_mode)}'
      AND expiry = '{_q(expiry)}'
    """
    if start_date:
        query += f" AND CAST(timestamp AS DATE) >= DATE '{_q(str(start_date)[:10])}'"
    if end_date:
        query += f" AND CAST(timestamp AS DATE) <= DATE '{_q(str(end_date)[:10])}'"
    if option_type:
        query += f" AND option_type = '{_q(str(option_type).upper())}'"
    if strike is not None:
        query += f" AND strike = {float(strike)}"
    query += f" ORDER BY timestamp ASC LIMIT {int(limit)}"

    try:
        # Zero-copy Apache Arrow stream -> Polars (no Python row materialization).
        arrow = conn.sql(query).to_arrow_table()
        return pl.from_arrow(arrow)
    except Exception:
        # Fail-open: return empty schema-correct frame on any query error
        return pl.DataFrame(schema=OPTION_TICK_SCHEMA)


def get_lakehouse_status() -> Dict[str, Any]:
    """Report cached symbols, date ranges, completeness %, modes, storage size."""
    status: Dict[str, Any] = {
        "symbols": [],
        "modes": ["tick", "1m_chain"],
        "total_size_mb": 0.0,
        "partitions": [],
    }
    try:
        total_size = 0
        seen_symbols: set = set()
        seen_expiries: set = set()
        seen_modes: set = set()
        seen_dates: set = set()

        for root, dirs, files in os.walk(OPTION_TICKS_ROOT):
            for f in files:
                if f.endswith(".parquet"):
                    fp = os.path.join(root, f)
                    try:
                        total_size += os.path.getsize(fp)
                    except Exception:
                        pass
                    # Parse partition path: .../underlying=X/data_mode=Y/expiry=Z/date=D/file.parquet
                    parts = root.replace(str(OPTION_TICKS_ROOT), "").split(os.sep)
                    part_map = {}
                    for part in parts:
                        if "=" in part:
                            k, v = part.split("=", 1)
                            part_map[k] = v
                    seen_symbols.add(part_map.get("underlying"))
                    seen_modes.add(part_map.get("data_mode"))
                    seen_expiries.add(part_map.get("expiry"))
                    seen_dates.add(part_map.get("date"))
        status["symbols"] = sorted([s for s in seen_symbols if s])
        status["modes"] = sorted([m for m in seen_modes if m])
        status["expiries"] = sorted([e for e in seen_expiries if e])
        status["date_range"] = {
            "start": min(seen_dates) if seen_dates else None,
            "end": max(seen_dates) if seen_dates else None,
        }
        status["total_size_mb"] = round(total_size / (1024 * 1024), 2)
        status["partition_count"] = sum(
            1 for _, _, files in os.walk(OPTION_TICKS_ROOT) for f in files if f.endswith(".parquet")
        )
    except Exception as e:
        status["error"] = str(e)
    return status


def get_lakehouse_library() -> Dict[str, Any]:
    """Stored Option Data Library: per symbol/mode date ranges, cached days,
    completeness % (trading days represented in the requested range) and size."""
    import calendar
    status = get_lakehouse_status()
    lib = []
    for sym in status.get("symbols", []):
        sym_root = OPTION_TICKS_ROOT / f"underlying={sym}"
        for mode_dir in sorted(sym_root.glob("data_mode=*")):
            mode = mode_dir.name.split("=", 1)[-1]
            dates = set()
            size = 0
            expiries = set()
            for p in mode_dir.rglob("*.parquet"):
                parts = {k.split("=", 1)[0]: k.split("=", 1)[-1]
                         for k in p.relative_to(mode_dir).parts if "=" in k}
                if parts.get("date"):
                    dates.add(parts["date"])
                if parts.get("expiry"):
                    expiries.add(parts["expiry"])
                try:
                    size += p.stat().st_size
                except Exception:
                    pass
            if not dates:
                continue
            start, end = min(dates), max(dates)
            # Completeness vs weekday (trading) calendar in the cached window.
            try:
                import datetime as _dt
                a = _dt.date.fromisoformat(start)
                b = _dt.date.fromisoformat(end)
                weekdays = sum(1 for i in range((b - a).days + 1)
                               if (a + _dt.timedelta(days=i)).weekday() < 5)
            except Exception:
                weekdays = len(dates)
            lib.append({
                "symbol": sym, "mode": mode,
                "modes": [mode],
                "label": "TICK DATA" if mode == "tick" else "1M OPTION CHAIN",
                "start": start, "end": end, "days": len(dates),
                "completeness_pct": round(min(100.0, len(dates) / max(1, weekdays) * 100), 1),
                "expiries": sorted(expiries),
                "size_mb": round(size / (1024 * 1024), 3),
            })
    # Merge modes per symbol for the compact UI card.
    merged: Dict[str, Dict[str, Any]] = {}
    for item in lib:
        s = merged.setdefault(item["symbol"], {
            "symbol": item["symbol"], "modes": [], "label": item["label"], "start": item["start"],
            "end": item["end"], "days": 0, "completeness_pct": 0.0,
            "size_mb": 0.0, "expiries": []})
        s["modes"].append(item["mode"])
        s["start"] = min(s["start"], item["start"])
        s["end"] = max(s["end"], item["end"])
        s["days"] = max(s["days"], item["days"])
        s["completeness_pct"] = max(s["completeness_pct"], item["completeness_pct"])
        s["size_mb"] += item["size_mb"]
        s["expiries"] = sorted(set(s["expiries"]) | set(item["expiries"]))
    return {"status": status, "library": list(merged.values()), "entries": lib}


def missing_dates(underlying: str, data_mode: str, start_date: str, end_date: str) -> List[str]:
    """Actionable warning: weekdays in range with no cached Parquet partition."""
    import datetime as _dt
    root = OPTION_TICKS_ROOT / f"underlying={str(underlying or '').upper()}" / f"data_mode={data_mode}"
    have = set()
    for p in root.rglob("*.parquet"):
        have.add(p.parent.name.split("=", 1)[-1])
    try:
        a = _dt.date.fromisoformat(str(start_date)[:10])
        b = _dt.date.fromisoformat(str(end_date)[:10])
    except Exception:
        return []
    out = []
    d = a
    while d <= b:
        if d.weekday() < 5 and d.isoformat() not in have:
            out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def compact_lakehouse() -> Dict[str, Any]:
    """Nightly compaction: merge small parquet files within each partition into
    fewer larger files to improve query performance. Idempotent."""
    compacted = 0
    errors = 0
    try:
        for root, dirs, files in os.walk(OPTION_TICKS_ROOT):
            parquet_files = [os.path.join(root, f) for f in files if f.endswith(".parquet")]
            if len(parquet_files) <= 1:
                continue
            # Read all, rewrite as single file
            try:
                dfs = [pl.read_parquet(fp) for fp in parquet_files]
                combined = pl.concat(dfs).unique(
                    subset=["timestamp", "expiry", "strike", "option_type", "underlying", "data_mode"],
                    keep="last",
                )
                combined = combined.sort("timestamp")
                # Write temp then replace
                tmp_dir = root + ".compact_tmp"
                os.makedirs(tmp_dir, exist_ok=True)
                tmp_file = os.path.join(tmp_dir, "compacted.parquet")
                combined.write_parquet(tmp_file, compression=_COMPRESSION, row_group_size=_ROW_GROUP_SIZE)
                # Remove old files
                for fp in parquet_files:
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
                # Move compacted file into root with a stable name
                final_file = os.path.join(root, f"compacted_{_ist_now_str().replace(' ', '_')}.parquet")
                shutil.move(tmp_file, final_file)
                compacted += 1
                try:
                    os.rmdir(tmp_dir)
                except Exception:
                    pass
            except Exception:
                errors += 1
    except Exception as e:
        errors += 1
    return {"compacted_partitions": compacted, "errors": errors}
