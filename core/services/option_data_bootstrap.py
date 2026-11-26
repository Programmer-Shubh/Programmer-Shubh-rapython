"""One-click REAL option data bootstrap for offline backtests.

Downloads official NSE F&O bhavcopy (real traded OHLC) for the requested
dates, imports it into `bhavcopy_data`, and mirrors the real prints into the
Parquet lakehouse as recorded ticks (09:15 open, 15:15 close). No synthetic
or Black-Scholes premium is ever created; provenance stays `nse_bhavcopy`.
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

from core.services.option_lakehouse import store_option_ticks


def _dates(start_date: str, end_date: str, cap: int) -> List[str]:
    try:
        a = dt.date.fromisoformat(str(start_date)[:10])
        b = dt.date.fromisoformat(str(end_date)[:10])
    except Exception:
        b = dt.date.today()
        a = b - dt.timedelta(days=14)
    if b < a:
        a, b = b, a
    out, d = [], a
    while d <= b and len(out) < cap:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    # Newest first — user ke selected dates pehle critical hote hain.
    return list(reversed(out))


def _mirror_to_lakehouse(trade_date: str, symbols: Optional[List[str]]) -> int:
    from core.models.database import Database
    db = Database.get_instance()
    sql = ("SELECT symbol, trade_date, expiry_date, strike_price, option_type, "
           "open_price, high_price, low_price, close_price, oi, volume "
           "FROM bhavcopy_data WHERE trade_date=? AND option_type IN ('CE','PE')")
    params = [trade_date]
    if symbols:
        ph = ",".join("?" for _ in symbols)
        sql += f" AND symbol IN ({ph})"
        params.extend(symbols)
    rows = db.fetch_all(sql, params) or []
    ticks = []
    for r in rows:
        try:
            base = {
                "underlying": str(r.get("symbol") or "").upper(),
                "expiry": str(r.get("expiry_date") or "")[:10],
                "option_type": str(r.get("option_type") or "").upper(),
                "strike": float(r.get("strike_price") or 0),
                "oi": int(r.get("oi") or 0),
                "volume": int(r.get("volume") or 0),
                "data_mode": "tick",
                "source": "nse_bhavcopy",
            }
            op = float(r.get("open_price") or 0) or float(r.get("close_price") or 0)
            cl = float(r.get("close_price") or 0) or op
            if op <= 0 or cl <= 0:
                continue
            # 09:15 open print: intrabar high/low ko aaj ke future se seed nahi
            # karte (look-ahead). 15:15 close print alag real tick hai.
            ticks.append(dict(base, timestamp=f"{trade_date} 09:15:00", ltp=op,
                              open_price=op, high_price=op, low_price=op,
                              close_price=op))
            ticks.append(dict(base, timestamp=f"{trade_date} 15:15:00", ltp=cl,
                              open_price=cl, high_price=cl, low_price=cl,
                              close_price=cl))
        except Exception:
            continue
    return store_option_ticks(ticks)


def fetch_real_option_data(start_date: str = "", end_date: str = "",
                           symbols: Optional[List[str]] = None,
                           max_days: int = 12) -> Dict[str, object]:
    """Fetch + cache real NSE option prints. Idempotent: re-running is a no-op."""
    from core.services.nsefin_bhav import import_fno_bhavcopy
    dates = _dates(start_date, end_date, max(1, min(int(max_days or 12), 20)))
    syms = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    done, failed, cached = [], [], 0
    for day in dates:
        try:
            res = import_fno_bhavcopy(day)
            cached += _mirror_to_lakehouse(day, syms or None)
            done.append({"date": day, **res})
        except Exception as exc:
            failed.append({"date": day, "error": str(exc)[:180]})
    return {
        "success": bool(done),
        "dates_requested": dates,
        "days_imported": len(done),
        "ticks_cached": cached,
        "failed": failed,
        "detail": done,
        "message": (f"{len(done)} real NSE F&O din import, {cached} asli option print "
                    f"lakehouse me cache. Ab backtest offline chal sakta hai."
                    if done else "NSE real data abhi nahi mila — internet/broker check karo."),
    }
