"""MCX commodity futures pricing via Yahoo国际 proxy + FX (COMEX/NYMEX).
MCX ka apna free archive nahi hai, isliye:
- BACKTEST/RESEARCH: proxy history (clearly labeled source='proxy').
- PAPER fills: proxy LTP (labeled; MCX COMEX ko tightly track karta hai).
- LIVE broker orders: Phase-2 (explicit block) - proxy par real paisa nahi.
Units: MCX quote units me convert (GOLD per-10g, SILVER per-kg, ...).
NICKEL/ZINC/ALU/LEAD/minis ka reliable Yahoo proxy nahi - wahan None
(INSUFFICIENT honesty, fantasy-fill kabhi nahi).
"""
import time as _time

# symbol -> (yahoo proxy, converter(px, fx) -> MCX quote units)
def _converters():
    return {
        # USD/bbl -> INR/bbl
        "CRUDEOIL": ("CL=F", lambda px, fx: px * fx),
        "CRUDEOILM": ("CL=F", lambda px, fx: px * fx),
        # USD/mmBtu -> INR/mmBtu
        "NATURALGAS": ("NG=F", lambda px, fx: px * fx),
        # USD/troy-oz -> INR/10g
        "GOLD": ("GC=F", lambda px, fx: px / 31.1035 * 10.0 * fx),
        "GOLDM": ("GC=F", lambda px, fx: px / 31.1035 * 10.0 * fx),
        # USD/troy-oz -> INR/kg
        "SILVER": ("SI=F", lambda px, fx: px / 31.1035 * 1000.0 * fx),
        "SILVERM": ("SI=F", lambda px, fx: px / 31.1035 * 1000.0 * fx),
        # USD/lb -> INR/kg
        "COPPER": ("HG=F", lambda px, fx: px * 2.20462 * fx),
    }

_FX_CACHE = {"v": 0.0, "ts": 0.0}
_FX_TTL = 6 * 3600
# Run-shared FX series (15-min TTL): ek run ke saare MCX symbols EK hi FX
# intraday + EK hi FX daily fetch share karte hain (pehle har symbol apna
# 2x Yahoo chain chalata tha - 3 symbols par wahi 140s hang ka kaaran).
_FXSER_CACHE = {}
_FXSER_TTL = 900

# Liquid MCX symbols warmed in background (paper/backtest bina network ke).
MCX_WARM_SYMBOLS = ["CRUDEOIL", "NATURALGAS", "GOLD", "GOLDM", "SILVER",
                    "SILVERM", "COPPER"]


def _ensure_mcx_table():
    """mcx_proxy_bars archive (proxy bars ka local godaam). Pehli successful
    fill ke baad backtest Yahoo-jitter proof: dobara wahi window secondon me,
    bina network, bina broker ke."""
    try:
        from core.models.database import Database as _DB
        db = _DB.get_instance()
        try:
            _pg = bool(db._is_postgres())
        except Exception:
            _pg = False
        if _pg:
            db.execute("CREATE TABLE IF NOT EXISTS mcx_proxy_bars (id SERIAL PRIMARY KEY,"
                       " symbol TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT '1d',"
                       " trade_date TEXT NOT NULL, bar_time TEXT NOT NULL DEFAULT '',"
                       " open_price DOUBLE PRECISION DEFAULT 0, high_price DOUBLE PRECISION DEFAULT 0,"
                       " low_price DOUBLE PRECISION DEFAULT 0, close_price DOUBLE PRECISION DEFAULT 0,"
                       " volume BIGINT DEFAULT 0, created_at TIMESTAMPTZ DEFAULT NOW())")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mcxbar_uq ON"
                       " mcx_proxy_bars(symbol, timeframe, trade_date, bar_time)")
        else:
            db.execute("CREATE TABLE IF NOT EXISTS mcx_proxy_bars (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                       " symbol TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT '1d',"
                       " trade_date TEXT NOT NULL, bar_time TEXT NOT NULL DEFAULT '',"
                       " open_price REAL DEFAULT 0, high_price REAL DEFAULT 0,"
                       " low_price REAL DEFAULT 0, close_price REAL DEFAULT 0,"
                       " volume INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')))")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mcxbar_uq ON"
                       " mcx_proxy_bars(symbol, timeframe, trade_date, bar_time)")
    except Exception:
        pass


def _wanted_days(start_date, end_date):
    """Requested range ke trading days (weekends nikal kar)."""
    try:
        import datetime as _dt
        _s = _dt.datetime.strptime(str(start_date)[:10], "%Y-%m-%d").date()
        _e = _dt.datetime.strptime(str(end_date)[:10], "%Y-%m-%d").date()
        out, _d = [], _s
        while _d <= _e:
            if _d.weekday() < 5:
                out.append(_d.strftime("%Y-%m-%d"))
            _d += _dt.timedelta(days=1)
        return out
    except Exception:
        return []


def read_mcx_archive(symbol, timeframe, start_date, end_date):
    """Archive me poori range covered ho to bars (warna [])."""
    try:
        _ensure_mcx_table()
        from core.models.database import Database as _DB
        s = str(symbol or "").strip().upper()
        tf = str(timeframe or "1d")
        rows = _DB.get_instance().fetch_all(
            "SELECT trade_date, bar_time, open_price, high_price, low_price,"
            " close_price, volume FROM mcx_proxy_bars"
            " WHERE symbol=? AND timeframe=? AND trade_date BETWEEN ? AND ?"
            " ORDER BY trade_date, bar_time",
            [s, tf, str(start_date)[:10], str(end_date)[:10]]) or []
        if not rows:
            return []
        _want = set(_wanted_days(start_date, end_date))
        _have = {str(r.get("trade_date", ""))[:10] for r in rows}
        if _want and not _want.issubset(_have):
            return []
        out = []
        for r in rows:
            try:
                out.append({"symbol": s, "trade_date": str(r.get("trade_date", ""))[:10],
                            "bar_time": str(r.get("bar_time", "") or ""),
                            "open_price": float(r.get("open_price") or 0),
                            "high_price": float(r.get("high_price") or 0),
                            "low_price": float(r.get("low_price") or 0),
                            "close_price": float(r.get("close_price") or 0),
                            "volume": int(r.get("volume") or 0), "oi": 0,
                            "source": "proxy-archive"})
            except Exception:
                continue
        return [b for b in out if b["close_price"] > 0]
    except Exception:
        return []


def store_mcx_bars(symbol, timeframe, bars):
    """Proxy bars archive karo (deduped). Returns stored count."""
    try:
        if not bars:
            return 0
        _ensure_mcx_table()
        from core.models.database import Database as _DB
        db = _DB.get_instance()
        s = str(symbol or "").strip().upper()
        tf = str(timeframe or "1d")
        n = 0
        for b in bars or []:
            try:
                _d = str(b.get("trade_date", ""))[:10]
                _t = str(b.get("bar_time", "") or "")
                _c = float(b.get("close_price") or 0)
                if not _d or _c <= 0:
                    continue
                try:
                    db.execute("DELETE FROM mcx_proxy_bars WHERE symbol=? AND timeframe=?"
                               " AND trade_date=? AND bar_time=?", [s, tf, _d, _t])
                except Exception:
                    pass
                db.execute("INSERT INTO mcx_proxy_bars (symbol, timeframe, trade_date, bar_time,"
                           " open_price, high_price, low_price, close_price, volume)"
                           " VALUES (?,?,?,?,?,?,?,?,?)",
                           [s, tf, _d, _t,
                            float(b.get("open_price") or _c),
                            float(b.get("high_price") or _c),
                            float(b.get("low_price") or _c), _c,
                            int(b.get("volume") or 0)])
                n += 1
            except Exception:
                continue
        return n
    except Exception:
        return 0


def warm_mcx_archive(symbols=None, intraday_days: int = 5, daily_days: int = 120):
    """Background warmer: liquid symbols ka daily(120d) + intraday(5d) proxy
    archive me bharo. Ek baar bharne ke baad backtest bina network ke chalta
    hai (bina broker, bina Yahoo-jitter). Returns {symbol: bars}."""
    out = {}
    try:
        import datetime as _dt
        _e = _dt.date.today().strftime("%Y-%m-%d")
        _s = (_dt.date.today() - _dt.timedelta(days=int(daily_days or 120))).strftime("%Y-%m-%d")
        for _sym in (symbols or MCX_WARM_SYMBOLS):
            try:
                _db, _src = fetch_mcx_history(_sym, _s, _e)
                if _db:
                    store_mcx_bars(_sym, "1d", _db)
                _ib = fetch_mcx_intraday(_sym, "15m", days=int(intraday_days or 5))
                if _ib:
                    store_mcx_bars(_sym, "15m", _ib)
                out[_sym] = len(_db or []) + len(_ib or [])
            except Exception:
                continue
    except Exception:
        pass
    return out


def _fx_series(iv="15m", days=5):
    """Shared (fx-intraday-rows, fx-daily-rows): TTL cache hit par 0 network."""
    import time as _tt
    try:
        _key = (str(iv or "15m"), int(days or 5))
    except Exception:
        _key = ("15m", 5)
    try:
        _hit = _FXSER_CACHE.get(_key)
        if _hit and _tt.time() - _hit[0] < _FXSER_TTL:
            return _hit[1], _hit[2]
    except Exception:
        pass
    _fxrows, _fxdaily = [], []
    try:
        from core.services.free_data import fetch_yahoo_intraday as _fyi
        _fxrows = _fyi("USDINR=X", _key[0], days=_key[1]) or []
    except Exception:
        _fxrows = []
    try:
        from core.services.free_data import fetch_yahoo_daily as _fyd
        import datetime as _dtx
        _e = _dtx.date.today().strftime("%Y-%m-%d")
        _s = (_dtx.date.today() - _dtx.timedelta(days=max(10, _key[1] + 5))).strftime("%Y-%m-%d")
        _fxdaily = _fyd("USDINR=X", _s, _e) or []
    except Exception:
        _fxdaily = []
    try:
        _FXSER_CACHE[_key] = (_tt.time(), _fxrows, _fxdaily)
        if len(_FXSER_CACHE) > 6:
            _FXSER_CACHE.pop(next(iter(_FXSER_CACHE)))
    except Exception:
        pass
    return _fxrows, _fxdaily


def _usdinr():
    """USDINR (Yahoo) with 6h cache. Returns 0.0 on failure."""
    try:
        now = _time.time()
        if _FX_CACHE["v"] > 0 and now - _FX_CACHE["ts"] < _FX_TTL:
            return float(_FX_CACHE["v"])
        from core.services.free_data import _yahoo_fallback_quote as _q
        d = _q("USDINR=X", timeout=8) or {}
        fx = float(d.get("spot") or 0)
        if fx > 0:
            _FX_CACHE["v"] = fx
            _FX_CACHE["ts"] = now
            return fx
    except Exception:
        pass
    return 0.0


def _proxy_quote(ticker, timeout=8):
    try:
        from core.services.free_data import _yahoo_fallback_quote as _q
        d = _q(ticker, timeout=timeout) or {}
        px = float(d.get("spot") or 0)
        return px if px > 0 else 0.0
    except Exception:
        return 0.0


def get_mcx_ltp(symbol: str, timeout: float = 8):
    """MCX futures LTP (proxy-derived, INR quote units). None = no quote."""
    try:
        s = str(symbol or "").strip().upper()
        conv = _converters().get(s)
        if not conv:
            return None
        ticker, fn = conv
        px = _proxy_quote(ticker, timeout=timeout)
        fx = _usdinr()
        if px <= 0 or fx <= 0:
            return None
        v = float(fn(px, fx))
        return round(v, 2) if v > 0 else None
    except Exception:
        return None


def fetch_mcx_intraday(symbol: str, interval: str = "15m", days: int = 5):
    """Proxy INTRADAY bars (COMEX/NYMEX via Yahoo + FX, MCX quote units, IST
    bar_time). Backtest 15m/live/paper sab yahi use karte hain - generic
    '{SYM}.NS' Yahoo kabhi nahi (wahi SILVER→₹221 fantasy ka source tha)."""
    try:
        from utils.helpers import is_mcx as _ismcx
        if not _ismcx(symbol):
            return []
        s = str(symbol or "").strip().upper()
        conv = _converters().get(s)
        if not conv:
            return []
        ticker, fn = conv
        iv = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m"}.get(
            str(interval or "15m"), "15m")
        # Archive-first: pichhli successful fill maujood ho to bina network.
        try:
            import datetime as _dtx
            _ddays = min(int(days or 5), 60)
            _ee = _dtx.date.today().strftime("%Y-%m-%d")
            _ss = (_dtx.date.today() - _dtx.timedelta(days=_ddays + 5)).strftime("%Y-%m-%d")
            _cached = read_mcx_archive(s, iv, _ss, _ee)
            if _cached and len(_cached) >= 20:
                return _cached
        except Exception:
            pass
        try:
            from core.services.free_data import fetch_yahoo_intraday as _fyi
            rows = _fyi(ticker, iv, days=min(int(days or 5), 60)) or []
        except Exception:
            rows = []
        if not rows:
            return []
        # Shared FX (run bhar me 1 baar): har symbol apna chain nahi chalata.
        try:
            fxrows, _fxdaily_shared = _fx_series(iv, min(int(days or 5), 60))
        except Exception:
            fxrows, _fxdaily_shared = [], []
        try:
            _fxm = {}
            for r in fxrows or []:
                try:
                    _fxm[(str(r.get("trade_date", ""))[:10],
                          str(r.get("bar_time", "") or "")[:5])] = float(
                        r.get("close_price") or 0)
                except Exception:
                    continue
            _fxd = {}
            for (d, _t), _v in _fxm.items():
                if _v > 0 and (d not in _fxd):
                    _fxd[d] = _v
        except Exception:
            _fxm, _fxd = {}, {}
        # FX fallback chain: bar-tick > day-close > shared daily-seed >
        # cached LTP. Shared seed (run me 1 baar) isliye taaki intraday-FX
        # ke ek gap par poori series skip na ho (wahi GOLDM/SILVER
        # INSUFFICIENT ka kaaran) - aur har symbol dobara fetch na kare.
        try:
            for _fr in _fxdaily_shared or []:
                try:
                    _dd = str(_fr.get("trade_date", ""))[:10]
                    _vv = float(_fr.get("close_price") or 0)
                    if _dd and _vv > 0 and _dd not in _fxd:
                        _fxd[_dd] = _vv
                except Exception:
                    continue
        except Exception:
            pass
        _fall = _usdinr() or 0.0
        if not _fall:
            try:
                _fall = float((_fxd or {}).get(max(_fxd.keys()))) if _fxd else 0.0
            except Exception:
                _fall = 0.0
        out = []
        _last_fx = 0.0
        for r in rows:
            try:
                _d = str(r.get("trade_date", ""))[:10]
                _bt = str(r.get("bar_time", "") or "")[:5]
                if not _d or not _bt:
                    continue
                _fx = float(_fxm.get((_d, _bt)) or 0) or float(_fxd.get(_d) or 0) or _fall
                if _fx > 0:
                    _last_fx = _fx
                else:
                    _fx = _last_fx  # forward-fill: thoda stale FX, par bar bachta hai
                if _fx <= 0:
                    continue
                _o = float(fn(float(r.get("open_price") or 0), _fx) or 0)
                _h = float(fn(float(r.get("high_price") or 0), _fx) or 0)
                _l = float(fn(float(r.get("low_price") or 0), _fx) or 0)
                _c = float(fn(float(r.get("close_price") or 0), _fx) or 0)
                if _c <= 0:
                    continue
                out.append({"symbol": s, "trade_date": _d, "bar_time": _bt,
                            "open_price": round(_o, 2), "high_price": round(_h, 2),
                            "low_price": round(_l, 2), "close_price": round(_c, 2),
                            "volume": int(r.get("volume") or 0), "oi": 0,
                            "source": "proxy"})
            except Exception:
                continue
        out.sort(key=lambda b: (b["trade_date"], b["bar_time"]))
        try:
            if out:
                store_mcx_bars(s, iv, out)
        except Exception:
            pass
        return out
    except Exception:
        return []


def fetch_mcx_history(symbol: str, start_date: str, end_date: str):
    """Proxy OHLC history converted to MCX quote units.
    Returns (bars, source) - bars like bhavcopy rows with trade_date/open/
    high/low/close/volume; source 'proxy' (live) ya 'proxy-archive' (local).
    ([], 'none') when unavailable."""
    try:
        s = str(symbol or "").strip().upper()
        conv = _converters().get(s)
        if not conv:
            return [], "none"
        try:
            _cached = read_mcx_archive(s, "1d", start_date, end_date)
            if _cached and len(_cached) >= 5:
                return _cached, "proxy-archive"
        except Exception:
            pass
        ticker, fn = conv
        from core.services.free_data import fetch_yahoo_daily as _yd
        rows = _yd(ticker, start_date, end_date) or []
        fxrows = _yd("USDINR=X", start_date, end_date) or []
        if not rows:
            return [], "none"
        try:
            _fxm = {}
            for r in fxrows or []:
                try:
                    _fxm[str(r.get("trade_date", ""))[:10]] = float(
                        r.get("close_price") or r.get("close") or 0)
                except Exception:
                    continue
            _last_fx = 0.0
            for _d in sorted(_fxm):
                if _fxm[_d] > 0:
                    _last_fx = _fxm[_d]
        except Exception:
            _fxm, _last_fx = {}, 0.0
        if not _last_fx:
            _last_fx = _usdinr()
        if not _last_fx:
            return [], "none"
        out = []
        for r in rows:
            try:
                _d = str(r.get("trade_date", ""))[:10]
                if not _d:
                    continue
                _fx = float(_fxm.get(_d) or 0) or _last_fx
                _o = float(fn(float(r.get("open_price") or r.get("open") or 0), _fx) or 0)
                _h = float(fn(float(r.get("high_price") or r.get("high") or 0), _fx) or 0)
                _l = float(fn(float(r.get("low_price") or r.get("low") or 0), _fx) or 0)
                _c = float(fn(float(r.get("close_price") or r.get("close") or 0), _fx) or 0)
                if _c <= 0:
                    continue
                out.append({"symbol": s, "trade_date": _d,
                            "open_price": round(_o, 2), "high_price": round(_h, 2),
                            "low_price": round(_l, 2), "close_price": round(_c, 2),
                            "volume": int(r.get("volume") or 0), "oi": 0,
                            "source": "proxy"})
            except Exception:
                continue
        out.sort(key=lambda b: b["trade_date"])
        try:
            if out:
                store_mcx_bars(s, "1d", out)
        except Exception:
            pass
        return out, ("proxy" if out else "none")
    except Exception:
        return [], "none"
