"""Persistent intraday OHLC bar cache (multi-year).

Backtests used to re-download 5m/15m bars from Fyers/Yahoo on EVERY run
(seconds of network work). Bars for completed trading days never change,
so they are safe to persist in the main DB.

Honest by construction: cached bars are the REAL bars fetched earlier from
the primary sources (Fyers/Yahoo). Nothing is synthesized or filled.
If a symbol/date is absent from cache, we simply fetch it from the network.
"""
from __future__ import annotations
from core.models.database import Database


def _db():
    return Database.get_instance()


_INSERT = (
    "INSERT INTO intraday_bars(symbol, timeframe, trade_date, bar_time, "
    "open_price, high_price, low_price, close_price, volume) "
    "VALUES(?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT (symbol, timeframe, trade_date, bar_time) DO NOTHING"
)


def get_cached_bars(symbol: str, timeframe: str, start_date: str, end_date: str) -> list:
    """Return cached bars for [start_date, end_date] (list of dicts, IST sorted)."""
    try:
        db = _db()
        rows = db.fetch_all(
            "SELECT trade_date, bar_time, open_price, high_price, low_price, "
            "close_price, volume FROM intraday_bars "
            "WHERE symbol=? AND timeframe=? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date, bar_time",
            [str(symbol).upper(), str(timeframe or "5m").lower(),
             str(start_date)[:10], str(end_date)[:10]],
        )
    except Exception:
        return []
    out = []
    for r in rows or []:
        out.append({
            "symbol": str(symbol).upper(),
            "trade_date": str(r.get("trade_date") or "")[:10],
            "bar_time": str(r.get("bar_time") or "")[:5],
            "open_price": float(r.get("open_price") or 0),
            "high_price": float(r.get("high_price") or 0),
            "low_price": float(r.get("low_price") or 0),
            "close_price": float(r.get("close_price") or 0),
            "volume": int(r.get("volume") or 0),
        })
    return out


def store_bars(symbol: str, timeframe: str, bars: list) -> int:
    """Persist freshly-fetched bars. Idempotent (upsert by unique key)."""
    if not bars:
        return 0
    try:
        db = _db()
        rows = []
        for b in bars:
            rows.append((
                str(symbol).upper(),
                str(timeframe or "5m").lower(),
                str(b.get("trade_date") or "")[:10],
                str(b.get("bar_time") or "")[:5],
                float(b.get("open_price") or 0),
                float(b.get("high_price") or 0),
                float(b.get("low_price") or 0),
                float(b.get("close_price") or 0),
                int(float(b.get("volume") or 0)),
            ))
        # Chunked to stay well below any statement timeout
        CH = 400
        for a in range(0, len(rows), CH):
            db.executemany(_INSERT, rows[a:a + CH])
        return len(rows)
    except Exception:
        return 0
