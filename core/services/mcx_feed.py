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
