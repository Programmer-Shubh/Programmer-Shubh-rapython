"""Real option-tick recorder: stores actual option LTP (1-min bars) into the
`option_ticks` table so backtests can price contracts from REAL recorded ticks
instead of synthetic Black-Scholes interpolation.

Data flow:
  Fyers WebSocket tick (fast_exit._on_message) -> record_tick(sym, strike, opt, ltp)
  -> option_ticks table -> backtest_engine._close_premium / entry premium
                           (when data_mode='1m_chain' or real ticks available)

Public API:
  record_tick(symbol, strike, option_type, ltp, expiry='', volume=0, source='fyers_ws')
  get_ticks(symbol, trade_date, strike, option_type, expiry='') -> list[dict]
  tick_bar(symbol, trade_date, bar_time, strike, option_type, expiry='') -> dict|None
  tick_count(symbol=None, trade_date=None) -> int
"""
import threading
import time as _time
from datetime import datetime, timedelta, timezone

_LOCK = threading.Lock()
_LAST_WRITE = {}   # (symbol, strike, option_type, bar_minute) -> epoch (throttle)
_MIN_WRITE_INTERVAL = 1.0   # 1 tick/minute par likho (1-min bars)

_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now():
    now = datetime.now(_IST)
    return now.strftime("%Y-%m-%d"), now.strftime("%H:%M")


# HARD GUARD (user rule 24-11, permanent): sirf REAL recorded ticks hi store
# ho sakte hain. Kabhi bhi fabricated/assumed time points (10:00 high,
# 14:00 low, interpolation) allow NAHI. Ye list aage se badhayi ja sakti hai
# par koi bhi 'synthetic'/'model'/'interpolated' source yahan kabhi na aaye.
_REAL_TICK_SOURCES = frozenset({"fyers_ws", "nse_bhavcopy"})


def _is_real_source(source: str) -> bool:
    return str(source or "").lower() in _REAL_TICK_SOURCES


def record_tick(symbol, strike, option_type, ltp, expiry="", volume=0,
                source="fyers_ws", force=False):
    """Persist ONE real option LTP into option_ticks (throttled to 1/min/strike).
    Safe to call from the WS callback thread - never raises.
    Fabricated/synthetic time points permanently blocked (see _REAL_TICK_SOURCES)."""
    try:
        if not _is_real_source(source):
            return False   # fabricated source -> kabhi store na ho
        strike = float(strike or 0)
        ltp = float(ltp or 0)
        symbol = str(symbol or "").upper()
        option_type = str(option_type or "").upper()
        if not symbol or strike <= 0 or ltp <= 0 or option_type not in ("CE", "PE"):
            return False
        date, hhmm = _ist_now()
        bar_time = f"{hhmm[:2]}:{hhmm[3:]}"   # real WS minute (09:17 -> "09:17")
        key = (symbol, strike, option_type, date, bar_time)
        with _LOCK:
            now = _time.time()
            if not force and now - _LAST_WRITE.get(key, 0) < _MIN_WRITE_INTERVAL:
                return False
            _LAST_WRITE[key] = now
            if len(_LAST_WRITE) > 5000:
                _LAST_WRITE.clear()
        from core.models.database import Database
        db = Database.get_instance()
        db.execute(
            "INSERT INTO option_ticks (symbol, trade_date, bar_time, expiry_date, "
            "strike_price, option_type, ltp, volume, source) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, trade_date, bar_time, strike_price, option_type, expiry_date) "
            "DO UPDATE SET ltp=excluded.ltp, volume=excluded.volume",
            [symbol, date, bar_time, str(expiry or ""), strike, option_type,
             ltp, int(volume or 0), str(source or "fyers_ws")])
        return True
    except Exception:
        return False


def get_ticks(symbol, trade_date, strike, option_type, expiry=""):
    """All recorded ticks for one contract on one date (chronological).
    Returns list of {'bar_time': 'HH:MM:00', 'ltp': float}."""
    try:
        from core.models.database import Database
        rows = Database.get_instance().fetch_all(
            "SELECT bar_time, ltp FROM option_ticks "
            "WHERE symbol=? AND trade_date=? AND strike_price=? AND option_type=? "
            "AND expiry_date=? ORDER BY bar_time",
            [str(symbol or "").upper(), str(trade_date)[:10], float(strike or 0),
             str(option_type or "").upper(), str(expiry or "")])
        return [{"bar_time": str(r.get("bar_time") or "")[:5],
                 "ltp": float(r.get("ltp") or 0)} for r in (rows or [])]
    except Exception:
        return []


def tick_bar(symbol, trade_date, bar_time, strike, option_type, expiry=""):
    """LTP of a specific 1-min bar (engine premium lookup). None if no tick."""
    try:
        from core.models.database import Database
        r = Database.get_instance().fetch_one(
            "SELECT ltp FROM option_ticks "
            "WHERE symbol=? AND trade_date=? AND bar_time LIKE ? "
            "AND strike_price=? AND option_type=? AND expiry_date=? "
            "ORDER BY bar_time DESC LIMIT 1",
            [str(symbol or "").upper(), str(trade_date)[:10],
             str(bar_time)[:5] + "%", float(strike or 0),
             str(option_type or "").upper(), str(expiry or "")])
        return float(r["ltp"]) if r and r.get("ltp") else None
    except Exception:
        return None


def tick_count(symbol=None, trade_date=None):
    """How many real ticks stored (diagnostics / UI)."""
    try:
        from core.models.database import Database
        where, params = [], []
        if symbol:
            where.append("symbol=?")
            params.append(str(symbol).upper())
        if trade_date:
            where.append("trade_date=?")
            params.append(str(trade_date)[:10])
        q = "SELECT COUNT(*) as c FROM option_ticks" + (
            (" WHERE " + " AND ".join(where)) if where else "")
        r = Database.get_instance().fetch_one(q, params)
        return int(r["c"]) if r else 0
    except Exception:
        return 0


def parse_fyers_option(sym):
    """Parse a Fyers option symbol -> (underlying, strike, CE/PE, expiry_ymd).
    Examples: NIFTY25DEC24000CE, BANKNIFTY26NOV55000PE, NIFTY26NOV24CE.
    Returns None for non-option/index-equity symbols."""
    try:
        s = str(sym or "").upper().replace("-", "").replace(" ", "")
        if not s.endswith("CE") and not s.endswith("PE"):
            return None
        opt = s[-2:]
        body = s[:-2]
        # Strike = trailing digits (before CE/PE). Expiry = 2-digit YY + MMM before strike.
        import re
        m = re.search(r"(\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC))(\d+)$", body)
        if not m:
            return None
        exp_token = m.group(1)      # e.g. 26NOV
        strike = float(m.group(2))
        underlying = body[:m.start()]
        if not underlying or strike <= 0:
            return None
        yy = 2000 + int(exp_token[:2])
        mon = {m_: i + 1 for i, m_ in enumerate(
            ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
        exp_ymd = f"{yy:04d}-{mon.get(exp_token[2:], 1):02d}-01"
        return underlying, strike, opt, exp_ymd
    except Exception:
        return None


def record_from_ws(sym, ltp, volume=0):
    """Hook for fast_exit WS tick: parse option symbol and store its real LTP.
    Non-option symbols are ignored. Never raises."""
    try:
        p = parse_fyers_option(sym)
        if not p:
            return False
        underlying, strike, opt, exp_ymd = p
        return record_tick(underlying, strike, opt, ltp,
                           expiry=exp_ymd, volume=volume, source="fyers_ws")
    except Exception:
        return False


def purge_fabricated_ticks() -> int:
    """One-time cleanup: koi bhi purana fabricated tick (synthetic source ya
    10:00/14:00 jaise assumed high/low time points) hamesha ke liye hatao.
    Real sources (fyers_ws, nse_bhavcopy) kabhi nahi hote. Returns deleted."""
    try:
        from core.models.database import Database
        db = Database.get_instance()
        # Sirf non-real sources delete karo (real source kabhi fabricate nahi hota).
        r = db.fetch_one("SELECT COUNT(*) as c FROM option_ticks "
                         "WHERE LOWER(COALESCE(source,'')) NOT IN ('fyers_ws','nse_bhavcopy')")
        n0 = int(r["c"]) if r else 0
        if n0:
            db.execute("DELETE FROM option_ticks "
                       "WHERE LOWER(COALESCE(source,'')) NOT IN ('fyers_ws','nse_bhavcopy')")
        return n0
    except Exception:
        return 0


def backfill_from_bhavcopy(days: int = 90, batch: int = 4000) -> int:
    """Seed `option_ticks` from REAL NSE bhavcopy OHLC saved in `bhavcopy_data`.

    Best honest approach (user rule 24-11): har (date, contract) ke liye DO
    real ticks store karo - 09:15 = asli OPEN, 15:15 = asli CLOSE. High/low
    ko koi time stamp NAHI dete (asli time pata nahi - fabricate-na) - wo
    bhavcopy_data mein pehle se hi SL/TP range ke liye maujood hain.

    Isse intraday backtest ko real open→close price move milta hai (synthetic
    nahi), broker offline par bhi. Idempotent (upsert). Returns rows written."""
    from datetime import datetime, timedelta
    try:
        from core.models.database import Database
        db = Database.get_instance()
        since = (datetime.now() - timedelta(days=int(days or 90))).strftime("%Y-%m-%d")
        # FASTEST PATH (24-11): dono real ticks EK server-side INSERT..SELECT se.
        # Pehle Python me rows fetch karke har tick alag/executemany se likha —
        # Supabase par 400s timeout bhi cross kar gaya (sirf 3192 ticks likhe
        # the). Ab DB khud 2 statements me sab karta hai (seconds).
        head = ("INSERT INTO option_ticks (symbol, trade_date, bar_time, expiry_date, "
                "strike_price, option_type, ltp, volume, source) ")
        where = (" FROM bhavcopy_data WHERE option_type IN ('CE','PE') AND trade_date>=? "
                 "AND close_price>0 AND strike_price>0 ")
        tail = (" ON CONFLICT(symbol, trade_date, bar_time, strike_price, option_type, expiry_date) "
                "DO UPDATE SET ltp=excluded.ltp, volume=excluded.volume")
        ok = True
        for bar_time, px in (("09:15", "COALESCE(open_price, close_price)"),
                             ("15:15", "close_price")):
            q = (head + "SELECT UPPER(symbol), trade_date, ?, COALESCE(expiry_date,''), "
                 "strike_price, UPPER(option_type), " + px +
                 ", COALESCE(volume,0), 'nse_bhavcopy'" + where
                 + "AND " + px + ">0" + tail)
            try:
                db.execute(q, [bar_time, since])
            except Exception:
                ok = False
                break
        if ok:
            try:
                r = db.fetch_one("SELECT COUNT(*) c FROM option_ticks WHERE source='nse_bhavcopy' AND trade_date>=?", [since])
                return int((r or {}).get("c") or 0)
            except Exception:
                return 1
    except Exception:
        pass
    # FALLBACK (purana Python path) - agar SQL variant kisi DB par na chale.
    try:
        from core.models.database import Database
        db = Database.get_instance()
        rows = db.fetch_all(
            "SELECT symbol, trade_date, expiry_date, strike_price, option_type, "
            "open_price, close_price, volume FROM bhavcopy_data "
            "WHERE option_type IN ('CE','PE') AND trade_date>=? "
            "AND close_price>0 ORDER BY trade_date, symbol", [since])
        q = ("INSERT INTO option_ticks (symbol, trade_date, bar_time, expiry_date, "
             "strike_price, option_type, ltp, volume, source) "
             "VALUES (?,?,?,?,?,?,?,?,?) "
             "ON CONFLICT(symbol, trade_date, bar_time, strike_price, option_type, expiry_date) "
             "DO UPDATE SET ltp=excluded.ltp, volume=excluded.volume")
        out = []
        for r in (rows or []):
            try:
                sym = str(r.get("symbol") or "").upper()
                strike = float(r.get("strike_price") or 0)
                opt = str(r.get("option_type") or "").upper()
                close = float(r.get("close_price") or 0)
                opn = float(r.get("open_price") or 0)
                if not sym or strike <= 0 or opt not in ("CE", "PE") or close <= 0:
                    continue
                date = str(r.get("trade_date"))[:10]
                exp = str(r.get("expiry_date") or "")
                vol = int(r.get("volume") or 0)
                for bar_time, price in (("09:15", opn or close), ("15:15", close)):
                    if price > 0:
                        out.append((sym, date, bar_time, exp, strike, opt,
                                    price, vol, "nse_bhavcopy"))
            except Exception:
                continue
        for i in range(0, len(out), batch):
            try:
                db.executemany(q, out[i:i + batch])
            except Exception:
                for r0 in out[i:i + batch]:
                    try:
                        db.execute(q, list(r0))
                    except Exception:
                        continue
        return len(out)
    except Exception:
        return 0
