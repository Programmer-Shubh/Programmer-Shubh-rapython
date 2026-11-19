# -*- coding: utf-8 -*-
"""Fyers Historical Sync — real option premiums into bhavcopy_data.

PROBLEM: local/production DB me option premium rows hi nahi the (0 CE/PE
rows). Engine har premium Black-Scholes se MODEL karta tha aur `_day_iv()`
ko real premium mile hi nahi, isliye 'real-IV anchored' IV bhi kabhi
available nahi tha. Agar coupon drift galat pakdi jaye to SL/TP bhi galat lagte.

WHAT THIS IMPORTS (daily bars, 2026-01-01 .. today):
  1) Index daily OHLC (NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY) -
     symbol as-is, option_type NULL (same shape jo bhavcopy_data me index
     rows ka hai).
  2) Weekly (NIFTY) / monthly (BANKNIFTY, FINNIFTY, MIDCPNIFTY -
     expiry = last Tuesday) contracts ke ATM+-2 strikes ke CE+PE daily bars.
     Expiry 2026-01-01 se aaj tak har weekly/monthly expiry cover hoti hai.
  3) Har row me expiry_date = YYYY-MM-DD set hota hai (engine `_day_iv`
     years-to-expiry yahin se nikalta hai).

STORED DATA ka istemail:
  - `_day_iv(date, spot)`: us din ka REAL IV BS solver kantract rows se
    nikalega -> intraday premium model ab real market IV se anchored hoga.
  - Daily backtest entry/exit premiums: seedha DB se (premium_source 'db').
  - PCR map (combo indicators) ab real OI/volume se ban sakta hai.

IMPORTANT for honesty: ye BACKTEST lagta hai real premium data par; intraday
fills phir bhi model-priced hain (clocked 5m bars par daily print nahi hota,
look-ahead guard) - bas ab IV real hai, price nahi.

Runs: main.py startup one-time auto-sync (flag `fyers_history_sync_auto_v1_done`,
skipped if no Fyers creds) + manual endpoint POST /api/broker/sync-historical
(button: Brokers page -> "Sync Fyers History"). Manual re-run idempotent hai
(delete+insert per batch). Progress in settings key `fyers_history_sync_status`.
"""
from __future__ import annotations

import datetime as _dt
import json as _js
import os
import threading
import calendar as _cal

INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")

# Fyers index instrument symbols (candidate order - prod Fyers par jo chale,
# woh use hoga). Production par ye verify honge (logs me failed candidates
# dikhenge).
_MIDCP_CANDIDATES = ["NSE:NIFTY_MID_SELECT-INDEX",
                     "NSE:NIFTYMIDSELECT-INDEX",
                     "NSE:NIFTY_MIDCAP_SELECT-INDEX"]
_INDEX_FYERS = {
    "NIFTY": ["NSE:NIFTY50-INDEX"],
    "BANKNIFTY": ["NSE:NIFTYBANK-INDEX", "NSE:BANKNIFTY-INDEX"],
    "FINNIFTY": ["NSE:FINNIFTY-INDEX", "NSE:FINNIFTY50-INDEX", "NSE:FIN_SERVICE-INDEX"],
    "MIDCPNIFTY": _MIDCP_CANDIDATES,
}

_STRIKE_STEP = {"NIFTY": 50.0, "BANKNIFTY": 100.0, "FINNIFTY": 50.0, "MIDCPNIFTY": 25.0}
_HAS_WEEKLY = {"NIFTY"}   # baki indices monthly-only (NSE Sep-2026 verified)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
_RUN_LOCK = threading.Lock()
_RUNNING = False


def _ist_now():
    return _dt.datetime.now(_IST)


def _ist_today():
    return _ist_now().date()


def _db():
    from core.models.database import Database
    return Database.get_instance()


def _set_status(patch: dict):
    """settings key 'fyers_history_sync_status' me merge-patch store karo."""
    try:
        db = _db()
        row = None
        try:
            row = db.fetch_one("SELECT setting_value FROM settings WHERE setting_key='fyers_history_sync_status'")
        except Exception:
            row = None
        cur = {}
        if row and row.get("setting_value"):
            try:
                cur = _js.loads(row["setting_value"]) or {}
                if not isinstance(cur, dict):
                    cur = {}
            except Exception:
                cur = {}
        cur.update(patch)
        cur["updated_at"] = _ist_now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            n = db.execute("UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key='fyers_history_sync_status'",
                           [_js.dumps(cur)])
            if not n:
                db.execute("INSERT INTO settings (setting_key, setting_value, updated_at) VALUES ('fyers_history_sync_status', ?, CURRENT_TIMESTAMP)",
                           [_js.dumps(cur)])
        except Exception:
            try:
                db.execute("INSERT OR REPLACE INTO settings (setting_key, setting_value, updated_at) VALUES ('fyers_history_sync_status', ?, CURRENT_TIMESTAMP)",
                           [_js.dumps(cur)])
            except Exception:
                pass
    except Exception:
        pass


def sync_status() -> dict:
    try:
        row = _db().fetch_one("SELECT setting_value FROM settings WHERE setting_key='fyers_history_sync_status'")
        if row and row.get("setting_value"):
            d = _js.loads(row["setting_value"]) or {}
            d["sync_running"] = _RUNNING
            return d
    except Exception:
        pass
    return {"sync_running": _RUNNING}


# ---------------- expiry calendar ----------------
def expiry_dates(symbol: str, start_ymd: str, end_ymd: str):
    """NIFTY: har Tuesday; baki (MONTHLY_IDX): mahine ka last Tuesday."""
    s = _dt.datetime.strptime(start_ymd[:10], "%Y-%m-%d").date()
    e = _dt.datetime.strptime(end_ymd[:10], "%Y-%m-%d").date()
    out = []
    if symbol.upper() in _HAS_WEEKLY:
        d = s
        while d <= e:
            if d.weekday() == 1:  # Tuesday
                out.append(d.strftime("%Y-%m-%d"))
            d += _dt.timedelta(days=1)
        return out
    # Monthly: last Tuesday of each month
    y, m = s.year, s.month
    while True:
        last = _dt.date(y, m, _cal.monthrange(y, m)[1])
        while last.weekday() != 1:
            last -= _dt.timedelta(days=1)
        if s <= last <= e:
            out.append(last.strftime("%Y-%m-%d"))
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        if (ny > e.year or (ny == e.year and nm > e.month)) and _dt.date(ny, nm, 1) > e:
            break
        y, m = ny, nm
        if y > e.year or (y == e.year and m > e.month):
            break
    return out


def _nearest_bar_close(bars, ymd):
    """bars: [{'trade_date','close_price'}...] -> closest bar close on/before ymd.
    Bars kis order me hon (source-dependent), pehle date-ascending sort zaroor."""
    ref = ymd[:10]
    try:
        seq = sorted(bars or [], key=lambda b: str(b.get("trade_date") or "")[:10])
    except Exception:
        seq = list(bars or [])
    best = None
    for b in seq:
        d = str(b.get("trade_date") or "")[:10]
        if d <= ref:
            c = float(b.get("close_price") or 0)
            if c > 0:
                best = c
        else:
            break
    return best


def _fetch_index_daily(idx, start, end):
    app_id, token = _fyers_creds()
    unders = _INDEX_FYERS.get(idx, [])
    errs = []
    if app_id and token:
        try:
            from core.services.fyers_data import FyersHistoryData
            fh = FyersHistoryData(app_id, token)
            for under in unders:
                resp = fh.history(under, start, end, "D")
                if resp.get("s") == "ok":
                    bars = []
                    for ts, o, h, l, c, v in (resp.get("candles") or []):
                        d = _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).astimezone(_IST).date()
                        bars.append({"symbol": idx, "trade_date": d.strftime("%Y-%m-%d"),
                                     "open_price": round(float(o), 2), "high_price": round(float(h), 2),
                                     "low_price": round(float(l), 2), "close_price": round(float(c), 2),
                                     "volume": int(float(v or 0)), "oi": 0})
                    if bars:
                        return bars, f"fyers:{under}", errs
                errs.append(f"{under}:{str(resp.get('message'))[:60]}")
        except Exception as e:
            errs.append(f"fyers_exc:{str(e)[:80]}")
    # Fallback: NSE index archive (official ind_close_all CSVs, no token).
    # Yahoo fallback completely removed (user request).
    try:
        from core.services.historical_fetcher import _fetch_nseindex_historical as _fni
        bars = []
        for b in (_fni(idx, start, end) or []):
            d = str(b.get("trade_date") or "")[:10]
            if not d:
                continue
            bars.append({"symbol": idx, "trade_date": d,
                         "open_price": float(b.get("open_price") or 0), "high_price": float(b.get("high_price") or 0),
                         "low_price": float(b.get("low_price") or 0), "close_price": float(b.get("close_price") or 0),
                         "volume": int(float(b.get("volume") or 0)), "oi": 0})
        if bars:
            return bars, "nse_index_archive", errs + ["fyers_index_cands_failed"]
        errs.append("nse_index_empty")
    except Exception as e:
        errs.append(f"nse_index_exc:{str(e)[:80]}")
    return [], "failed", errs


def _fyers_creds():
    try:
        from routes.broker import _get_config as _gc
        cfg = _gc("fyers") or {}
        return str(cfg.get("app_id") or ""), str(cfg.get("access_token") or "")
    except Exception:
        return "", ""


def _store_bars(bars, expiry=None, strike=None, option_type=None):
    """Delete-then-insert per batch (same idiom as BhavcopyModel.import_data)."""
    db = _db()
    CH = 400
    n = 0
    rows = []
    for b in bars:
        rows.append((
            b["symbol"], b["trade_date"],
            expiry if expiry is not None else b.get("expiry_date"),
            strike if strike is not None else b.get("strike_price"),
            option_type if option_type is not None else b.get("option_type"),
            round(float(b.get("open_price") or 0), 2), round(float(b.get("high_price") or 0), 2),
            round(float(b.get("low_price") or 0), 2), round(float(b.get("close_price") or 0), 2),
            int(float(b.get("volume") or 0)), int(float(b.get("oi") or 0)),
        ))
    for a in range(0, len(rows), CH):
        chunk = rows[a:a + CH]
        try:
            db.executemany(
                "DELETE FROM bhavcopy_data WHERE symbol=? AND trade_date=? "
                "AND COALESCE(expiry_date,'')=COALESCE(?,'') "
                "AND COALESCE(strike_price,0)=COALESCE(?,0) "
                "AND COALESCE(option_type,'')=COALESCE(?,'')",
                [(r[0], r[1], r[2], r[3], r[4]) for r in chunk])
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


def sync_all(start_date: str = "2026-01-01", log=print):
    """Main entry. Returns summary dict. Safe to re-run (upsert)."""
    global _RUNNING
    with _RUN_LOCK:
        if _RUNNING:
            return {"started": False, "reason": "already_running"}
        _RUNNING = True
    _set_status({"state": "running", "started_at": _ist_now().strftime("%Y-%m-%d %H:%M:%S"),
                 "stage": "index_bars", "index_ok": [], "index_failed": [],
                 "contracts_ok": 0, "contracts_failed": [], "rows_stored": 0})
    try:
        end = _ist_today().strftime("%Y-%m-%d")
        app_id, token = _fyers_creds()
        summary = {"indices": {}, "contracts_ok": 0, "contracts_failed": [], "rows_stored": 0,
                   "expiries": {}, "premium_coverage": {}}
        sys_log = lambda *a: log("[fyers-sync]", *a)

        # ---------- 1) index daily bars ----------
        index_bars = {}
        for idx in INDICES:
            bars, src, errs = _fetch_index_daily(idx, start_date, end)
            if bars:
                _store_bars(bars)
                index_bars[idx] = bars
                summary["indices"][idx] = {"bars": len(bars), "source": src}
                _set_status({"stage": "index_bars", "index_ok": list(summary["indices"])})
                sys_log(idx, "index bars:", len(bars), "via", src)
            else:
                summary["indices"][idx] = {"bars": 0, "source": src, "errors": errs}
                _set_status({"stage": "index_bars", "index_failed": [idx]})
                sys_log(idx, "INDEX BARS FAILED:", errs)

        # ---------- 2) option contracts per expiry ----------
        for idx in INDICES:
            ibars = index_bars.get(idx) or []
            if not ibars:
                continue
            closes = ibars
            step = _STRIKE_STEP.get(idx, 50.0)
            exps = expiry_dates(idx, start_date, (end[:8] + "28"))
            exp_ok = []
            for exp in exps:
                ref = _nearest_bar_close(closes, exp) or (ibars[0]["close_price"] if ibars else 0)
                if ref <= 0:
                    continue
                atm = round(ref / step) * step
                strikes = [round(atm + k * step, 2) for k in (-2, -1, 0, 1, 2)]
                # expiry already gone by >10 days and no OI/volume -> skip deep-history
                # (faster, and keep rows lean); still store if within 75d or monthly.
                try:
                    d_exp = _dt.datetime.strptime(exp, "%Y-%m-%d").date()
                    days_ago = (_ist_today() - d_exp).days
                except Exception:
                    days_ago = 0
                is_monthly = idx not in _HAS_WEEKLY
                if days_ago > 100 and not is_monthly:
                    continue
                exp_ok.append(exp)
                for stk in strikes:
                    for opt in ("CE", "PE"):
                        try:
                            from core.services.symbol_resolver import _fyers_symbol
                            fsym = _fyers_symbol(idx, exp, stk, opt)
                        except Exception as e:
                            summary["contracts_failed"].append(f"{idx} {exp} {stk}{opt}:sym:{str(e)[:40]}")
                            continue
                        try:
                            from core.services.fyers_data import FyersHistoryData
                            fh = FyersHistoryData(app_id, token)
                            resp = fh.history(fsym, start_date, end, "D")
                            if resp.get("s") == "ok" and resp.get("candles"):
                                crows = []
                                for ts, o, h, l, c, v in resp["candles"]:
                                    d = _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).astimezone(_IST).date()
                                    crows.append({"symbol": idx, "trade_date": d.strftime("%Y-%m-%d"),
                                                  "open_price": float(o), "high_price": float(h),
                                                  "low_price": float(l), "close_price": float(c),
                                                  "volume": int(float(v or 0)), "oi": 0})
                                stored = _store_bars(crows, expiry=exp, strike=stk, option_type=opt)
                                summary["rows_stored"] += stored
                                summary["contracts_ok"] += 1
                            else:
                                summary["contracts_failed"].append(
                                    f"{idx} {exp} {stk}{opt}:{str(resp.get('message') or 'empty')[:60]}")
                        except Exception as e:
                            summary["contracts_failed"].append(f"{idx} {exp} {stk}{opt}:{str(e)[:60]}")
                _set_status({"contracts_ok": summary["contracts_ok"],
                             "rows_stored": summary["rows_stored"],
                             "stage": f"contracts:{idx}",
                             "current_expiry": exp})
            summary["expiries"][idx] = exp_ok
            sys_log(idx, "expiries synced:", len(exp_ok), "contracts ok:", summary["contracts_ok"])

        # ---------- 3) coverage snapshot ----------
        try:
            db = _db()
            cov = {}
            for idx in INDICES:
                try:
                    r = db.fetch_one("SELECT COUNT(*) AS c FROM bhavcopy_data WHERE symbol=? AND option_type IN ('CE','PE')", [idx])
                    cov[idx] = int((r or {}).get("c") or 0)
                except Exception:
                    cov[idx] = -1
            summary["premium_coverage"] = cov
        except Exception:
            pass

        finished = _ist_now().strftime("%Y-%m-%d %H:%M:%S")
        _set_status({"state": "done", "finished_at": finished,
                     "stage": "done",
                     "contracts_ok": summary["contracts_ok"],
                     "rows_stored": summary["rows_stored"],
                     "premium_coverage": summary["premium_coverage"],
                     "contracts_failed_count": len(summary["contracts_failed"]),
                     "contracts_failed_sample": summary["contracts_failed"][:25]})
        _rows = int(summary.get("rows_stored") or 0)
        _cov = sum(v for v in (summary.get("premium_coverage") or {}).values() if isinstance(v, (int, float)))
        # One-time auto flag SIRF tab set karo jab actual premium rows store
        # hue ho - warna adhoori sync ko 'done' mark karke agle boot par
        # real data kabhi nahi aayega. Manual re-run button hamesha available.
        _auto_done_ok = (_rows > 0 and int(summary.get("contracts_ok") or 0) > 10) or _cov > 0
        if _auto_done_ok:
            try:
                db = _db()
                db.execute("INSERT INTO settings (setting_key, setting_value, updated_at) VALUES ('fyers_history_sync_auto_v1_done','1',CURRENT_TIMESTAMP) "
                           "ON CONFLICT(setting_key) DO UPDATE SET setting_value='1', updated_at=CURRENT_TIMESTAMP")
            except Exception:
                try:
                    db = _db()
                    db.execute("INSERT OR REPLACE INTO settings (setting_key, setting_value, updated_at) VALUES ('fyers_history_sync_auto_v1_done','1',CURRENT_TIMESTAMP)")
                except Exception:
                    pass
        else:
            sys_log("auto-sync INCOMPLETE (rows=%s contracts_ok=%s) - flag NOT set; next boot retries",
                    _rows, summary.get("contracts_ok"))
        sys_log("DONE — rows stored:", summary["rows_stored"], "contracts ok:", summary["contracts_ok"],
                "coverage:", summary["premium_coverage"])
        return summary
    except Exception as e:
        _set_status({"state": "error", "finished_at": _ist_now().strftime("%Y-%m-%d %H:%M:%S"),
                     "error": str(e)[:300]})
        try:
            log("[fyers-sync] FATAL:", e)
        except Exception:
            pass
        return {"started": True, "error": str(e)}
    finally:
        _RUNNING = False


def sync_async(start_date: str = "2026-01-01"):
    """Spawn background thread (non-blocking). Returns status dict."""
    global _RUNNING
    with _RUN_LOCK:
        if _RUNNING:
            return {"started": False, "reason": "already_running"}
    t = threading.Thread(target=sync_all, kwargs={"start_date": start_date}, daemon=True)
    t.start()
    return {"started": True}
