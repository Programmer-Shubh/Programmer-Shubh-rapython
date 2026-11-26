"""Bundled REAL NSE F&O bhavcopy seed for ephemeral/free-tier databases.

Render's free tier wipes SQLite on every deploy/restart. Without seed data a
fresh process has no option prints, so backtests can only return honest zeros
until a slow NSE download finishes. This module loads a compact, REAL
(downloaded from NSE) parquet into bhavcopy_data once, so any backtest works
instantly and offline.

No synthetic pricing is involved - every row is an official NSE F&O bhavcopy
print, identical to what nsefin imports. Re-running is a no-op.
"""
from __future__ import annotations

import os
import threading

_LOCK = threading.Lock()
_DONE = {"ran": False, "rows": 0, "seeded": False, "reason": ""}


def seed_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "data", "seed", "nse_fno_seed.parquet")


def _count_rows() -> int:
    try:
        from core.models.database import Database
        r = Database.get_instance().fetch_one("SELECT COUNT(*) c FROM bhavcopy_data")
        return int((r or {}).get("c") or 0)
    except Exception:
        return 10 ** 9  # don't seed on unknown errors


def seed_if_needed(force: bool = False) -> dict:
    """Load the bundled real NSE seed into bhavcopy_data if the table is empty.

    Returns a small status dict. Idempotent: repeated calls are instant no-ops.
    """
    global _DONE
    with _LOCK:
        if _DONE["ran"] and not force:
            return dict(_DONE)
        _DONE["ran"] = True
        path = seed_path()
        if not os.path.exists(path):
            _DONE["reason"] = "no seed file"
            return dict(_DONE)
        if (not force) and _count_rows() >= 100000:
            _DONE["seeded"] = False
            _DONE["reason"] = "db already populated"
            return dict(_DONE)
        try:
            import polars as pl
            from core.models.database import Database
            db = Database.get_instance()
            df = pl.read_parquet(path)
            # Fast path: drop the whole seeded date range in one shot, then bulk
            # insert without per-row DELETE (import_data deletes per-key which is
            # O(n) and very slow for 240k rows). Idempotent via the range delete.
            try:
                dmin = str(df["trade_date"].min())
                dmax = str(df["trade_date"].max())
                db.execute("DELETE FROM bhavcopy_data WHERE trade_date BETWEEN ? AND ?",
                           [dmin, dmax])
            except Exception:
                pass
            cols = ["symbol", "trade_date", "expiry_date", "strike_price", "option_type",
                    "open_price", "high_price", "low_price", "close_price", "volume", "oi"]
            rows = df.select(cols).rows()
            CH = 8000
            done = 0
            for i in range(0, len(rows), CH):
                db.executemany(
                    "INSERT INTO bhavcopy_data (symbol, trade_date, expiry_date, "
                    "strike_price, option_type, open_price, high_price, low_price, "
                    "close_price, volume, oi) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [tuple(r) for r in rows[i:i + CH]],
                )
                done += len(rows[i:i + CH])
            _DONE["rows"] = done
            _DONE["seeded"] = True
            _DONE["reason"] = f"seeded {done} real NSE rows"
            try:
                print(f"[seed_data] {done} real NSE F&O rows loaded from bundle", flush=True)
            except Exception:
                pass
            return dict(_DONE)
        except Exception as exc:
            _DONE["reason"] = str(exc)[:180]
            try:
                print("[seed_data] failed:", _DONE["reason"], flush=True)
            except Exception:
                pass
            return dict(_DONE)


def seed_in_background() -> None:
    """Fire-and-forget: seed the DB without blocking startup or /health."""
    def _worker():
        try:
            seed_if_needed()
        except Exception:
            pass
    t = threading.Thread(target=_worker, name="seed-data", daemon=True)
    t.start()
    return t
