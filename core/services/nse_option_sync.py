# -*- coding: utf-8 -*-
"""Import REAL index-option premiums into bhavcopy_data.

WHY: Fyers /data/history does NOT serve expired option contracts at all
(error -300 'Please provide a valid symbol' for every contract - Fyers v3
limitation: REST history resolves symbols against the CURRENT master only).
Index bars fetch fine (indices never expire); option contracts do not.

The only free, reliable source of historical option-premium OHLC for
NSE indices is the official F&O bhavcopy (via `nsefin`, already a dependency).
For 2026-11-13: 5,109 index-option rows (all strikes, OHLC+OI+volume) for
NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY - way more than needed.

WHAT gets stored (keeps rows lean): only strikes within +/-ATM (configurable)
of the day-spot, ATM step per index (NIFTY 50, BANKNIFTY 100, FINNIFTY 50,
MIDCPNIFTY 25). Stored with symbol=<index>, option_type=CE|PE, expiry_date,
strike_price/ohlc/volume/oi - exactly the shape the engine's `_day_iv()` and
DAILY-fill paths (`_prem_source`) already read.

SEED: `data/option_premium_seed.csv.gz` (committed) - prebuilt deterministic
snapshot 2026-01-01..2026-11-17. Same rows `import_seed()` inserts at startup
so production gets real premiums even if NSE is unreachable from Render's IP.

Sync entry points (all idempotent delete+insert per batch):
  import_seed()              - fast: insert the committed snapshot
  sync_from_nse(start, end)  - fetch official bhavcopy per weekday (best-effort)
  sync_async()               - background runner for the endpoint / startup
  sync_status()              - progress dict for the status endpoint
"""
from __future__ import annotations

import csv
import gzip
import io
import os
import datetime as _dt
import json as _js
import threading

INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
STEP = {"NIFTY": 50.0, "BANKNIFTY": 100.0, "FINNIFTY": 50.0, "MIDCPNIFTY": 25.0}
_STRIKE_RADIUS = 3          # ATM +/- 3 steps (covers entry/exit strikes + ITM/OTM)
_SEED_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "option_premium_seed.csv.gz")

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
_RUN_LOCK = threading.Lock()
_RUNNING = False


def _db():
    from core.models.database import Database
    return Database.get_instance()


def _ist_today():
    return _dt.datetime.now(_IST).date()


def _set_status(patch: dict):
    try:
        db = _db()
        row = None
        try:
            row = db.fetch_one("SELECT setting_value FROM settings WHERE setting_key='nse_option_sync_status'")
        except Exception:
            row = None
        cur = {}
        if row and row.get("setting_value"):
            try:
                cur = _js.loads(row["setting_value"]) or {}
            except Exception:
                cur = {}
        cur.update(patch)
        cur["updated_at"] = _dt.datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")
        try:
            n = db.execute("UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP "
                           "WHERE setting_key='nse_option_sync_status'", [_js.dumps(cur)])
            if not n:
                db.execute("INSERT INTO settings (setting_key, setting_value, updated_at) "
                           "VALUES ('nse_option_sync_status', ?, CURRENT_TIMESTAMP)", [_js.dumps(cur)])
        except Exception:
            try:
                db.execute("INSERT OR REPLACE INTO settings (setting_key, setting_value, updated_at) "
                           "VALUES ('nse_option_sync_status', ?, CURRENT_TIMESTAMP)", [_js.dumps(cur)])
            except Exception:
                pass
    except Exception:
        pass


def sync_status() -> dict:
    try:
        row = _db().fetch_one("SELECT setting_value FROM settings WHERE setting_key='nse_option_sync_status'")
        if row and row.get("setting_value"):
            d = _js.loads(row["setting_value"]) or {}
            d["sync_running"] = _RUNNING
            # SELF-HEAL: Render restart ke baad DB me 'running' atak jata hai
            # (thread mri par finally likhne ka mauqa nahi milta). Agar 15 min
            # se zyada purana 'running' ho aur abhi koi thread na chal rahi
            # ho to 'interrupted' mark karo - UI aur resume dono sahi.
            if str(d.get("state")) == "running" and not _RUNNING:
                try:
                    ua = str(d.get("updated_at") or "")
                    _u = _dt.datetime.strptime(ua, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_IST)
                    if (_dt.datetime.now(_IST) - _u).total_seconds() > 15 * 60:
                        d["state"] = "interrupted"
                        d["note"] = "Process restart — sync atak gaya tha. Dobara dabao, resume karega."
                except Exception:
                    pass
            return d
    except Exception:
        pass
    return {"sync_running": _RUNNING}


def _store_rows(rows, chunk: int = 250):
    """Single multi-VALUES INSERT per ~250 rows (Render PG pooler friendly).
    PEHLE yahan har call par 'DELETE FROM bhavcopy_data WHERE option_type CE/PE'
    tha - matlab har din ke fetch par PURA option data delete ho jata aur sirf
    aaj ka bachta tha. Ye HATAYA (user rule 24-11): ab koi delete nahi, sirf
    per-day upsert. DB unique index par ON CONFLICT DO NOTHING se re-import
    idempotent rehta hai (duplicate nahi), purana data safe rehta hai."""
    if not rows:
        return 0
    db = _db()
    n = 0
    # Koi DELETE nahi - ye function sirf naye din ke rows insert karta hai.
    cols = ("symbol, trade_date, expiry_date, strike_price, option_type,"
            " open_price, high_price, low_price, close_price, volume, oi")
    for a in range(0, len(rows), chunk):
        batch = rows[a:a + chunk]
        tup = []
        params = []
        for r in batch:
            tup.append("(?,?,?,?,?,?,?,?,?,?,?)")
            params += [r["symbol"], r["trade_date"], r["expiry_date"],
                       float(r["strike_price"]), r["option_type"],
                       round(float(r["open_price"]), 2), round(float(r["high_price"]), 2),
                       round(float(r["low_price"]), 2), round(float(r["close_price"]), 2),
                       int(float(r["volume"])), int(float(r["oi"]))]
        # Idempotent: pehle se maujood (symbol,date,expiry,strike,type) par
        # DO NOTHING - dobara sync/chunk retry par duplicate nahi ban-te.
        q = (f"INSERT INTO bhavcopy_data ({cols}) VALUES {','.join(tup)} "
             "ON CONFLICT(symbol, trade_date, COALESCE(expiry_date,''), "
             "COALESCE(strike_price,0), COALESCE(option_type,'')) DO NOTHING")
        try:
            db.execute(q, params)
            n += len(batch)
        except Exception as e:
            try:
                db.execute("INSERT INTO settings (setting_key, setting_value, updated_at) "
                           "VALUES ('nse_option_sync_errors', ?, CURRENT_TIMESTAMP) "
                           "ON CONFLICT(setting_key) DO UPDATE SET setting_value=?, updated_at=CURRENT_TIMESTAMP",
                           [f"batch_{a}: {str(e)[:200]}", f"batch_{a}: {str(e)[:200]}"])
            except Exception:
                pass
    return n


def _parse_rows(df, trade_date):
    """Vectorised ATM±3-step filter for the 4 indices. Much faster than
    per-row Python (43k IDO rows/day → <200ms)."""
    if df is None or len(df) == 0:
        return []
    try:
        d = df
        sym = d["symbol"].astype(str).str.strip().str.upper()
        mask = sym.isin(INDICES)
        d = d[mask]
        if len(d) == 0:
            return []
        sym = d["symbol"].astype(str).str.strip().str.upper()
        cat = d["category"].astype(str).str.upper()
        right = d["right"].astype(str).str.upper()
        strike = d["strike"].astype(float)
        spot = d["spot"].astype(float)
        mask2 = (cat.isin(["IDO", "OPTIDX", "OPTSTK", "STO"])
                 & right.isin(["CE", "PE"])
                 & (strike > 0) & (spot > 0))
        d = d[mask2]
        if len(d) == 0:
            return []
        sym = d["symbol"].astype(str).str.strip().str.upper()
        right = d["right"].astype(str).str.upper()
        strike = d["strike"].astype(float)
        spot = d["spot"].astype(float)
        step = sym.map(STEP).fillna(50.0)
        atm = (spot / step).round() * step
        lo, hi = atm - _STRIKE_RADIUS * step, atm + _STRIKE_RADIUS * step
        d = d[(strike >= lo) & (strike <= hi)]
        if len(d) == 0:
            return []
        r = []
        for _, rr in d.iterrows():
            try:
                cp = float(rr.get("close") or 0)
                if cp <= 0:
                    continue
                r.append({
                    "symbol": str(rr["symbol"]).strip().upper(),
                    "trade_date": str(trade_date)[:10],
                    "expiry_date": str(rr.get("expiry") or "")[:10],
                    "strike_price": float(rr["strike"]),
                    "option_type": str(rr["right"]).upper(),
                    "open_price": float(rr.get("open") or 0),
                    "high_price": float(rr.get("high") or 0),
                    "low_price": float(rr.get("low") or 0),
                    "close_price": cp,
                    "volume": int(float(rr.get("volume") or 0)),
                    "oi": int(float(rr.get("oi") or 0)),
                })
            except Exception:
                continue
        return r
    except Exception:
        return []


def sync_from_nse(start_date: str = "2026-01-01", end_date: str | None = None,
                  log=print):
    """Fetch official bhavcopy per weekday; store index-option rows within
    +/-3 steps of ATM. Best-effort (a missing/blocked day is skipped)."""
    global _RUNNING
    with _RUN_LOCK:
        if _RUNNING:
            return {"started": False, "reason": "already_running"}
        _RUNNING = True
    s = _dt.datetime.strptime(start_date[:10], "%Y-%m-%d").date()
    e = (_dt.datetime.strptime(end_date[:10], "%Y-%m-%d").date() if end_date
         else _ist_today())
    # RESUME (user rule 24-11): pichhle atke hue sync ne jis date tak kaam
    # kiya tha, wahan se shuru karo (har baar 01-01 se nahi). Status mein
    # 'current_date' save hota hai - next run usse aage se chalega.
    try:
        _prev = sync_status()
        _resume = str(_prev.get("current_date") or "")[:10]
        # 'paused' (4-min deadline) aur 'interrupted' (restart) dono se resume
        # karo - pehle sirf 'running' par resume hota tha, deadline ke baad
        # dobara dabane par 01-01 se shuru ho jata tha (barbaad fetch).
        if _resume and str(_prev.get("state")) in ("running", "paused", "interrupted") and _resume > str(s):
            s = _dt.datetime.strptime(_resume, "%Y-%m-%d").date() + _dt.timedelta(days=1)
    except Exception:
        pass
    # Overall deadline 4 min: 225 din × ~3s = ~11 min tha -> Render thread
    # mar jata tha par _RUNNING true reh jata tha (state 'running' atka).
    import time as _ptime
    _deadline = _ptime.time() + 240
    _set_status({"state": "running", "started_at": _dt.datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S"),
                 "stage": "nse_fetch", "days_done": 0, "rows_stored": 0,
                 "days_failed": 0, "current_date": ""})
    d = s
    days_done = days_failed = rows_stored = 0
    _interrupted = False
    try:
        from core.services.nsefin_bhav import fetch_fno_bhavcopy
    except Exception as e:
        _set_status({"state": "error", "error": f"nsefin import: {e}"})
        _RUNNING = False
        return {"started": False, "error": str(e)}
    try:
      while d <= e:
        if _ptime.time() > _deadline:
            _interrupted = True
            break
        if d.weekday() >= 5:
            d += _dt.timedelta(days=1)
            continue
        td = d.strftime("%Y-%m-%d")
        try:
            df = fetch_fno_bhavcopy(td)
            if df is None or len(df) == 0:
                days_failed += 1
            else:
                rows = _parse_rows(df, td)
                if rows:
                    rows_stored += _store_rows(rows)
                days_done += 1
        except Exception as e:
            days_failed += 1
            try:
                log("[nse-opt]", td, "FAILED:", str(e)[:80])
            except Exception:
                pass
        d += _dt.timedelta(days=1)
        # Har din status save karo taaki resume/+ UI progress sahi rahe.
        _set_status({"stage": "nse_fetch", "days_done": days_done,
                     "days_failed": days_failed,
                     "rows_stored": rows_stored, "current_date": td})
    finally:
        # Lock HAMESHA release karo (crash/timeout par bhi atka na rahe).
        _RUNNING = False
    if _interrupted:
        _set_status({"state": "paused", "stage": "nse_fetch",
                     "note": "4-min deadline — agle run yahin se resume hoga",
                     "days_done": days_done, "days_failed": days_failed,
                     "rows_stored": rows_stored, "current_date": td})
        return {"started": True, "paused": True, "days_done": days_done,
                "days_failed": days_failed, "rows_stored": rows_stored}
    _set_status({"state": "done", "stage": "done", "days_done": days_done,
                 "days_failed": days_failed, "rows_stored": rows_stored,
                 "finished_at": _dt.datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")})
    try:
        db = _db()
        db.execute("INSERT INTO settings (setting_key, setting_value, updated_at) "
                   "VALUES ('nse_option_sync_done','1',CURRENT_TIMESTAMP) "
                   "ON CONFLICT(setting_key) DO UPDATE SET setting_value='1', updated_at=CURRENT_TIMESTAMP")
    except Exception:
        try:
            db = _db()
            db.execute("INSERT OR REPLACE INTO settings (setting_key, setting_value, updated_at) "
                       "VALUES ('nse_option_sync_done','1',CURRENT_TIMESTAMP)")
        except Exception:
            pass
    try:
        log(f"[nse-opt] DONE days_ok={days_done} days_skip={days_failed} rows={rows_stored}")
    except Exception:
        pass
    _RUNNING = False
    return {"started": True, "days_done": days_done, "days_failed": days_failed,
            "rows_stored": rows_stored}


def import_seed(path: str | None = None, log=print):
    """Insert the committed seed snapshot (data/option_premium_seed.csv.gz).
    Fast (~2-5s for ~15k rows). Idempotent."""
    p = path or _SEED_PATH
    if not os.path.exists(p):
        _set_status({"state": "error", "error": f"seed not found: {p}"})
        return {"started": False, "error": "seed file missing", "path": p}
    global _RUNNING
    with _RUN_LOCK:
        if _RUNNING:
            return {"started": False, "reason": "already_running"}
        _RUNNING = True
    _set_status({"state": "running", "stage": "seed_import",
                 "started_at": _dt.datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S"),
                 "rows_stored": 0})
    rows = []
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append({
                    "symbol": str(r.get("symbol") or "").upper(),
                    "trade_date": str(r.get("trade_date") or "")[:10],
                    "expiry_date": str(r.get("expiry_date") or "")[:10],
                    "strike_price": float(r.get("strike_price") or 0),
                    "option_type": str(r.get("option_type") or "").upper(),
                    "open_price": float(r.get("open_price") or 0),
                    "high_price": float(r.get("high_price") or 0),
                    "low_price": float(r.get("low_price") or 0),
                    "close_price": float(r.get("close_price") or 0),
                    "volume": int(float(r.get("volume") or 0)),
                    "oi": int(float(r.get("oi") or 0)),
                })
    except Exception as e:
        _set_status({"state": "error", "error": f"seed read: {e}"})
        _RUNNING = False
        return {"started": False, "error": str(e)}
    n = _store_rows(rows)
    _set_status({"state": "done", "stage": "seed_done", "rows_stored": n,
                 "seed_file": p,
                 "finished_at": _dt.datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")})
    try:
        log("[nse-opt] seed import DONE rows=%d", n)
    except Exception:
        pass
    _RUNNING = False
    return {"started": True, "rows_stored": n, "source": "seed"}


def sync_async(start_date: str = "2026-01-01"):
    """Background: seed (guaranteed) first, then best-effort NSE refresh."""
    global _RUNNING
    with _RUN_LOCK:
        if _RUNNING:
            return {"started": False, "reason": "already_running"}
    t = threading.Thread(target=_worker, kwargs={"start_date": start_date}, daemon=True)
    t.start()
    return {"started": True}


def _worker(start_date):
    r = import_seed()
    sync_from_nse(start_date, None)


def auto_seed_if_empty(log=print):
    """Startup one-time (flag-guarded) import of the committed seed snapshot.
    Merge import (idempotent delete+insert per row) so a partial/recent-only
    bhavcopy on the host is topped up with the full 2026 history."""
    try:
        db = _db()
        row = db.fetch_one("SELECT setting_value FROM settings WHERE setting_key='nse_option_seed_done'")
        if row and str(row.get("setting_value") or "") == "1":
            log("[nse-opt] seed already imported - skip")
            return {"skipped": True}
    except Exception:
        pass
    log("[nse-opt] importing seed snapshot (merge)")
    r = import_seed(log=log)
    try:
        db = _db()
        if int(r.get("rows_stored") or 0) > 0:
            db.execute("INSERT INTO settings (setting_key, setting_value, updated_at) "
                       "VALUES ('nse_option_seed_done','1',CURRENT_TIMESTAMP) "
                       "ON CONFLICT(setting_key) DO UPDATE SET setting_value='1', updated_at=CURRENT_TIMESTAMP")
    except Exception:
        try:
            db = _db()
            db.execute("INSERT OR REPLACE INTO settings (setting_key, setting_value, updated_at) "
                       "VALUES ('nse_option_seed_done','1',CURRENT_TIMESTAMP)")
        except Exception:
            pass
    return r
