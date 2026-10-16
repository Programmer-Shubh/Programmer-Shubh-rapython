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
        try:
            from core.services.free_data import fetch_yahoo_intraday as _fyi
            rows = _fyi(ticker, iv, days=min(int(days or 5), 60)) or []
        except Exception:
            rows = []
        if not rows:
            return []
        try:
            from core.services.free_data import fetch_yahoo_intraday as _fyi2
            fxrows = _fyi2("USDINR=X", iv, days=min(int(days or 5), 60)) or []
        except Exception:
            fxrows = []
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
        # FX fallback chain: bar-tick > day-close > daily-history seed >
        # cached LTP. Daily seed isliye taaki intraday-FX ke ek gap par
        # poori series skip na ho (wahi GOLDM/SILVER INSUFFICIENT ka kaaran).
        try:
            from core.services.free_data import fetch_yahoo_daily as _fyd
            _fxh = _fyd("USDINR=X", (rows[0].get("trade_date", "") if rows else "")[:10] or "2020-01-01",
                        (rows[-1].get("trade_date", "") if rows else "")[:10] or "2030-01-01") or []
            for _fr in _fxh:
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
        return out
    except Exception:
        return []


def fetch_mcx_history(symbol: str, start_date: str, end_date: str):
    """Proxy OHLC history converted to MCX quote units.
    Returns (bars, source) - bars like bhavcopy rows with trade_date/open/
    high/low/close/volume; source 'proxy'. ([], 'none') when unavailable."""
    try:
        s = str(symbol or "").strip().upper()
        conv = _converters().get(s)
        if not conv:
            return [], "none"
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
        return out, ("proxy" if out else "none")
    except Exception:
        return [], "none"
