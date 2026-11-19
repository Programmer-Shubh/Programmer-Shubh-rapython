# -*- coding: utf-8 -*-
"""Stock OHLC Sync — top F&O stocks daily bars into bhavcopy_data (SPOT rows).

WHY: Direct broker calls from cloud IPs are blocked (NSE blocks; Fyers 429s).
FyersHistoryData goes via the static-IP egress proxy, so this works on Render.
Bhavcopy seed currently only covers indices (NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY)
and a handful of stocks - so stock backtests returned INSUFFICIENT_DATA with
limited local data.

IMPORTS (daily bars, 2026-01-01 ... today):
   Top ~50 liquid F&O stocks (NSE EQ) - symbol as-is, option_type NULL
   (same shape jo bhavcopy_data me index rows ka hai), expiry_date NULL,
   strike NULL. Engine stock trades par spot OHLC use karta hai.

Idempotent (delete+insert per batch) + rate-limited (1s/call for Fyers).
Runs: manual trigger via POST /api/broker/stock-history-sync (background).
"""
from __future__ import annotations

import datetime as _dt
import json as _js
import time as _t
import threading

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _ist_today():
    return _dt.datetime.now(_IST).date()


# F&O universe: NSE F&O stocks (FNO_STOCKS) — 92 symbols × 1 year OHLC.
# Indices (NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY) already in bhavcopy_data via
# NSE option bhavcopy import — sync only imports stock SPOT rows.
try:
    from utils.helpers import FNO_STOCKS as _FNO
    TOP_STOCKS = sorted(set(_FNO))
except Exception:
    # Fallback: local list (same source of truth as Strategy Builder symbols list)
    TOP_STOCKS = [
        "ABB", "ADANIENT", "ALKEM", "AMBER", "APOLLOHOSP", "ASHOKLEY",
        "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJAJFINSV", "BAJAJHLDNG",
        "BAJFINANCE", "BEL", "BHARTIARTL", "BOSCHLTD", "BPCL", "BRITANNIA",
        "CDSL", "CIPLA", "COALINDIA", "COFORGE", "COLPAL", "CONCOR",
        "CROMPTON", "CUMMINSIND", "DABUR", "DIVISLAB", "DIXON", "DMART",
        "DRREDDY", "EICHERMOT", "FEDERALBNK", "HCLTECH", "HDFCBANK",
        "HDFCLIFE", "HINDPETRO", "HINDUNILVR", "ICICIBANK", "ICICIGI",
        "IDFCFIRSTB", "INFY", "IOC", "ITC", "KOTAKBANK", "LT", "M&M",
        "MARUTI", "NESTLEIND", "NTPC", "ONGC", "PNB", "POWERGRID",
        "RELIANCE", "SBILIFE", "SBIN", "SUNPHARMA", "TATACONSUM",
        "TATAMOTORS", "TATASTEEL", "TCS", "TECHM", "TITAN", "ULTRACEMCO",
        "UNIONBANK", "VEDL", "WIPRO",
    ]


def _db():
    from core.models.database import Database
    return Database.get_instance()


def _set_status(patch: dict):
    try:
        db = _db()
        _row = db.fetch_one(
            "SELECT setting_value FROM settings WHERE setting_key=?",
            ["stock_history_sync_status"])
        _cur = {}
        if _row and _row.get("setting_value"):
            try:
                _cur = _js.loads(_row["setting_value"]) or {}
            except Exception:
                _cur = {}
        _cur.update(patch)
        _payload = _js.dumps(_cur, default=str)
        db.execute(
            "INSERT INTO settings (setting_key, setting_value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT (setting_key) DO UPDATE SET setting_value=excluded.setting_value, "
            "updated_at=excluded.updated_at",
            ["stock_history_sync_status", _payload])
    except Exception:
        pass


def sync_status() -> dict:
    try:
        db = _db()
        row = db.fetch_one(
            "SELECT setting_value FROM settings WHERE setting_key=?",
            ["stock_history_sync_status"])
        if row and row.get("setting_value"):
            try:
                return _js.loads(row["setting_value"]) or {}
            except Exception:
                return {"state": "error", "error": "unparseable status"}
        return {"state": "never_run"}
    except Exception:
        return {"state": "unknown"}


_RUN_LOCK = threading.Lock()
_RUNNING = False


def _store_bars(db, bars):
    """Delete-then-insert per batch (same idiom as BhavcopyModel.import_data)."""
    CH = 400
    n = 0
    rows = []
    for b in bars:
        rows.append((
            b["symbol"], b["trade_date"], None, None, None,
            round(float(b.get("open_price") or 0), 2),
            round(float(b.get("high_price") or 0), 2),
            round(float(b.get("low_price") or 0), 2),
            round(float(b.get("close_price") or 0), 2),
            int(float(b.get("volume") or 0)), int(float(b.get("oi") or 0)),
        ))
    for a in range(0, len(rows), CH):
        chunk = rows[a:a + CH]
        try:
            db.executemany(
                "DELETE FROM bhavcopy_data WHERE symbol=? AND trade_date=? "
                "AND COALESCE(expiry_date,'')='' AND COALESCE(strike_price,0)=0 "
                "AND COALESCE(option_type,'')=''",
                [(r[0], r[1]) for r in chunk])
        except Exception:
            pass
        try:
            db.executemany(
                "INSERT INTO bhavcopy_data (symbol, trade_date, expiry_date, strike_price, option_type,"
                " open_price, high_price, low_price, close_price, volume, oi)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                chunk)
            n += len(chunk)
        except Exception:
            pass
    return n


def sync_all(start_date: str = None, log=print):
    global _RUNNING
    with _RUN_LOCK:
        if _RUNNING:
            return {"started": False, "reason": "already_running"}
        _RUNNING = True
    if not start_date:
        start_date = (_ist_today() - _dt.timedelta(days=365)).strftime("%Y-%m-%d")
    _set_status({"state": "running",
                 "started_at": _ist_today().strftime("%Y-%m-%d %H:%M:%S"),
                 "stage": "stocks", "ok": [], "failed": [], "rows_stored": 0,
                 "symbols_total": len(TOP_STOCKS)})
    try:
        import time as _time
        end = _ist_today().strftime("%Y-%m-%d")
        from core.services.fyers_data import FyersHistoryData
        from core.services.contract_pricer import _fyers_auth
        app_id, token = _fyers_auth()
        if not app_id or not token:
            _set_status({"state": "error", "error": "Fyers creds missing",
                         "symbols_failed": TOP_STOCKS})
            return {"ok": [], "failed": TOP_STOCKS, "error": "Fyers not configured"}
        ok, failed, rows_stored, details = [], [], 0, {}

        for i, sym in enumerate(TOP_STOCKS):
            _time.sleep(1.0)  # Fyers 1 req/sec
            try:
                fh = FyersHistoryData(client_id=app_id, access_token=token)
                under = f"NSE:{sym}-EQ"
                resp = fh.history(under, start_date, end, "D")
                if not resp or resp.get("s") != "ok":
                    msg = str((resp or {}).get("message") or "no data")[:80]
                    failed.append(sym)
                    details[sym] = {"bars": 0, "error": msg}
                    _set_status({"stage": "stocks", "ok": list(ok), "failed": list(failed),
                                 "rows_stored": rows_stored, "details": details})
                    continue
                candles = resp.get("candles") or []
                bars = []
                for ts, o, h, l, c, v in candles:
                    try:
                        d = _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).astimezone(_IST)
                    except Exception:
                        continue
                    cc = float(c) if c else 0
                    if cc <= 0:
                        continue
                    bars.append({"symbol": sym, "trade_date": d.strftime("%Y-%m-%d"),
                                 "open_price": float(o) if o else cc,
                                 "high_price": float(h) if h else cc,
                                 "low_price": float(l) if l else cc,
                                 "close_price": cc, "volume": int(float(v or 0)), "oi": 0})
                n = _store_bars(_db(), bars)
                rows_stored += n
                ok.append(sym)
                details[sym] = {"bars": len(bars), "stored": n}
                _set_status({"stage": "stocks", "ok": list(ok), "failed": list(failed),
                             "rows_stored": rows_stored, "details": details})
                log(sym, len(bars), "bars")
            except Exception as e:
                failed.append(sym)
                details[sym] = {"bars": 0, "error": str(e)[:80]}
                _set_status({"stage": "stocks", "ok": list(ok), "failed": list(failed),
                             "rows_stored": rows_stored, "details": details})
        _set_status({"state": "done", "finished_at": _ist_today().strftime("%Y-%m-%d %H:%M:%S"),
                     "ok": list(ok), "failed": list(failed), "rows_stored": rows_stored,
                     "symbols_ok": len(ok), "symbols_failed": len(failed),
                     "details": details})
        return {"ok": ok, "failed": failed, "rows_stored": rows_stored}
    except Exception as e:
        _set_status({"state": "error", "error": str(e)[:200]})
        return {"ok": [], "failed": TOP_STOCKS, "error": str(e)[:200]}
    finally:
        with _RUN_LOCK:
            _RUNNING = False


def sync_async(start_date: str = None):
    global _RUNNING
    with _RUN_LOCK:
        if _RUNNING:
            return {"started": False, "reason": "already_running"}
        _RUNNING = True
    if not start_date:
        start_date = (_ist_today() - _dt.timedelta(days=365)).strftime("%Y-%m-%d")
    try:
        threading.Thread(target=sync_all, args=(start_date, print), daemon=True,
                         name="stock-history-sync").start()
        return {"started": True, "symbols_total": len(TOP_STOCKS),
                "message": "stocks bars sync started in background"}
    except Exception as e:
        with _RUN_LOCK:
            _RUNNING = False
        return {"started": False, "error": str(e)[:200]}
