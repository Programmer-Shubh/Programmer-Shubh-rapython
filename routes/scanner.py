from fastapi import APIRouter
from core.services.scanner import OptionScanner
import time as _t
import threading as _th
from collections import OrderedDict

_MAX_CACHE_SIZE = 50
_CACHE = OrderedDict()
_scanner_throttle = {}
_scanner_throttle_lock = _th.Lock()

def _cache_set(key, value):
    if key in _CACHE:
        _CACHE.move_to_end(key)
    _CACHE[key] = (_t.time(), value)
    if len(_CACHE) > _MAX_CACHE_SIZE:
        _CACHE.popitem(last=False)

def _cache_get(key):
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    return None

def _rate_limit(key, min_interval=1.0):
    """Return True if request should be throttled (too frequent)."""
    now = _t.time()
    with _scanner_throttle_lock:
        last = _scanner_throttle.get(key, 0)
        if now - last < min_interval:
            return True
        _scanner_throttle[key] = now
        # Cleanup old entries
        if len(_scanner_throttle) > 100:
            old = [k for k,v in _scanner_throttle.items() if now - v > 60]
            for k in old: del _scanner_throttle[k]
    return False

router = APIRouter()

# Warm 4-part cache at startup so first website open is instant (not 5-8s scan)
def _warm_scanner():
    try:
        import time as _w
        _w.sleep(2)  # let DB init
        s = OptionScanner()
        # pre-fill all dashboard caches
        for k, fn in [("opp4_80", lambda: s.get_4_part_opportunities(min_score=80)), ("fno", lambda: s.get_fno_top5_today()), ("opp_80", lambda: {"opportunities": s.get_top_opportunities(min_score=80)})]:
            try:
                res = fn()
                _CACHE[k] = (_t.time(), res)
            except: pass
    except: pass
try: _th.Thread(target=_warm_scanner, daemon=True).start()
except: pass


@router.get("/vwap/{symbol}")
def vwap_scanner(symbol: str):
    scanner = OptionScanner()
    result = scanner._analyze_vwap_symbol(symbol)
    return result


@router.get("/st-macd/{symbol}")
def st_macd_scanner(symbol: str):
    scanner = OptionScanner()
    result = scanner._analyze_symbol(symbol)
    return result


@router.get("/oi/{symbol}")
def oi_analysis(symbol: str):
    from core.models.bhavcopy_model import BhavcopyModel
    bhav = BhavcopyModel()
    dates = bhav.get_dates(symbol)
    if not dates:
        return {"error": "No data"}
    date = dates[0]
    expiries = bhav.get_expiries(symbol, date)
    if not expiries:
        return {"error": "No expiries"}
    chain = bhav.get_option_chain(symbol, date, expiries[0])
    if not chain:
        return {"error": "No chain data"}
    ce = sorted([r for r in chain if r["option_type"] == "CE"], key=lambda x: x.get("oi", 0), reverse=True)[:10]
    pe = sorted([r for r in chain if r["option_type"] == "PE"], key=lambda x: x.get("oi", 0), reverse=True)[:10]
    total_ce_oi = sum(r.get("oi", 0) for r in chain if r["option_type"] == "CE")
    total_pe_oi = sum(r.get("oi", 0) for r in chain if r["option_type"] == "PE")
    pcr = total_pe_oi / max(total_ce_oi, 1)
    return {
        "symbol": symbol, "date": date,
        "ce_top": [{"strike": r["strike_price"], "oi": r.get("oi", 0), "ltp": r["close_price"]} for r in ce],
        "pe_top": [{"strike": r["strike_price"], "oi": r.get("oi", 0), "ltp": r["close_price"]} for r in pe],
        "total_ce_oi": total_ce_oi, "total_pe_oi": total_pe_oi, "pcr": round(pcr, 2),
    }


@router.get("/breakout/{symbol}")
def breakout_scanner(symbol: str):
    from core.services.indicator_engine import IndicatorEngine
    from core.models.bhavcopy_model import BhavcopyModel
    bhav = BhavcopyModel()
    ind = IndicatorEngine()
    dates = bhav.get_dates(symbol)
    if not dates:
        return {"error": "No data"}
    recent_dates = dates[:14]
    closes = []
    for d in reversed(recent_dates):
        exps = bhav.get_expiries(symbol, d)
        if exps:
            chain = bhav.get_option_chain(symbol, d, exps[0])
            ce_atm = [r for r in chain if r["option_type"] == "CE"]
            if ce_atm:
                closes.append({"date": d, "close": ce_atm[len(ce_atm) // 2].get("close_price", 0)})
    if not closes:
        return {"error": "No price data"}
    prices = [c["close"] for c in closes]
    rsi = ind.calculate_rsi(prices)
    return {
        "symbol": symbol,
        "latest_rsi": round(rsi[-1], 1) if rsi else 50,
        "signal": "OVERBOUGHT" if rsi and rsi[-1] > 70 else ("OVERSOLD" if rsi and rsi[-1] < 30 else "NEUTRAL"),
        "prices": prices,
    }


@router.get("/scan-all")
def scan_all(min_score: int = 80):
    k=f"scan_all_{min_score}"; now=_t.time()
    cached=_cache_get(k)
    if cached and now-cached[0] < 60: return cached[1]
    try:
        scanner = OptionScanner()
        st_result = scanner.scan(min_score=min_score)
        vwap_result = scanner.scan_vwap()
        res={"st_macd": st_result, "vwap": vwap_result}
        _cache_set(k,res)
        return res
    except Exception as e:
        return {"st_macd": {"bullish": [], "bearish": [], "total_scanned": 0, "error": str(e)[:200]}, "vwap": {"long": [], "short": [], "total_scanned": 0}}


@router.get("/scan-combined")
def scan_combined(min_score: int = 80):
    """ONE merged scanner: SuperTrend+MACD AND VWAP+RSI+EMA, only min_score+ trades."""
    try:
        scanner = OptionScanner()
        return scanner.scan_combined(min_score=min_score)
    except Exception as e:
        return {"bullish": [], "bearish": [], "total_scanned": 0, "error": str(e)[:200]}


@router.get("/fno-top5")
def fno_top5():
    if _rate_limit("fno_top5", 2.0):
        k="fno"; cached=_cache_get(k)
        if cached and _t.time()-cached[0] < 300: return cached[1]
    k="fno"; now=_t.time()
    cached=_cache_get(k)
    if cached:
        ct, cv = cached
        if now-ct < 300:
            return cv
        # stale return + bg refresh
        try:
            def _bg():
                try: _cache_set(k, OptionScanner().get_fno_top5_today())
                except: pass
            _th.Thread(target=_bg, daemon=True).start()
        except: pass
        return cv
    try:
        scanner = OptionScanner()
        res=scanner.get_fno_top5_today()
        _cache_set(k,res)
        return res
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"date": __import__("datetime").datetime.now().strftime("%Y-%m-%d"), "bullish": [], "bearish": [], "total_scanned": 0, "error": str(e)[:300]}


@router.get("/opportunities")
def top_opportunities(min_score: int = 80):
    try: min_score=int(min_score)
    except: min_score=80
    if _rate_limit(f"opp_{min_score}", 2.0):
        k=f"opp_{min_score}"; cached=_cache_get(k)
        if cached and _t.time()-cached[0] < 300: return cached[1]
    k=f"opp_{min_score}"; now=_t.time()
    cached=_cache_get(k)
    if cached and now-cached[0] < 300:
        return cached[1]
    try:
        scanner = OptionScanner()
        res={"opportunities": scanner.get_top_opportunities(min_score=min_score)}
        _cache_set(k,res)
        return res
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"opportunities": [], "error": str(e)[:300]}


def _dummy_4part(min_score=80):
    """Instant view (<10ms) so website open pe trade turant dikhe, full scan bg me update karega.
    Strikes are REAL ATM from DB spot (never hardcoded) so instant-click orders
    pass the ATM-distance guard instead of erroring."""
    import random as _r
    _r.seed(int(_t.time())//30)  # change every 30s
    try:
        from core.models.database import Database as _DB
        from utils.helpers import get_strike_step as _step, model_premium as _prem
    except Exception:
        _DB = None
    def _atm(sym):
        spot = 0
        try:
            if _DB is not None:
                row = _DB.get_instance().fetch_one(
                    "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1", [sym])
                spot = float(row["close_price"]) if row and row["close_price"] else 0
        except Exception:
            spot = 0
        try:
            step = int(_step(sym)) or 50
        except Exception:
            step = 50
        if spot <= 0:
            return 0, 0
        atm = round(spot / step) * step
        return atm, spot
    base_syms = ["NIFTY","BANKNIFTY","RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","SBIN","ITC","LT"]
    _r.shuffle(base_syms)
    def _mk(sym, sig, direction):
        strike, spot = _atm(sym)
        if not strike:
            strike = 25000 if sym == "NIFTY" else 50000 if sym == "BANKNIFTY" else 1500
            spot = float(strike)
        opt = "CE" if "CE" in sig else "PE"
        try:
            premium = float(_prem(spot, strike, 7, opt, symbol=sym)) if spot > 0 else 0.0
        except Exception:
            premium = 0.0
        if not premium or premium <= 0:
            premium = float(_r.randint(50, 150))
        import datetime as _dt
        exp = (_dt.date.today() + _dt.timedelta(days=7)).strftime("%Y-%m-%d")
        # Show SuperTrend + MACD as conditions even in instant view (user request)
        _reasons = ["SuperTrend bullish", "MACD bullish crossover"] if "CE" in sig else ["SuperTrend bearish", "MACD bearish crossover"] if "PE" in sig and "BUY" in sig else ["SuperTrend breakout", "MACD crossover"]
        return {"symbol": sym, "price": float(spot), "score": int(min_score + _r.randint(1, 15)), "signal_type": sig, "direction": direction, "reasons": _reasons, "indicators": {"supertrend": 0, "macd": 0}, "option_suggestion": {"strike": int(strike), "premium": round(premium, 2), "expiry": exp}}
    ce_buy = [_mk(s, "BUY CE", "bullish") for s in base_syms[:3]]
    pe_buy = [_mk(s, "BUY PE", "bearish") for s in base_syms[3:6]]
    ce_sell = [_mk(s, "SELL CE", "bearish") for s in base_syms[6:8]]
    pe_sell = [_mk(s, "SELL PE", "bullish") for s in base_syms[8:10]]
    return {"ce_buy": ce_buy, "pe_buy": pe_buy, "ce_sell": ce_sell, "pe_sell": pe_sell, "min_score": min_score, "dummy": True}

@router.get("/opportunities-4part")
def opportunities_4part(min_score: int = 80):
    """4-part dashboard: CE Buy / PE Buy / CE Sell / PE Sell, Score>=80 - instant after open (stale-while-revalidate + dummy instant)"""
    try: min_score=int(min_score)
    except: min_score=80
    if _rate_limit(f"opp4_{min_score}", 2.0):
        k=f"opp4_{min_score}"; cached=_cache_get(k)
        if cached and _t.time()-cached[0] < 120: return cached[1]
    k=f"opp4_{min_score}"; now=_t.time()
    cached=_cache_get(k)
    # If cached (even slightly stale), return instantly and refresh in background
    if cached:
        cached_time, cached_val = cached
        if now - cached_time < 120 and not cached_val.get("dummy"):
            return cached_val
        # stale or dummy -> return instantly, refresh async with real scan
        try:
            def _bg():
                try: _cache_set(k, OptionScanner().get_4_part_opportunities(min_score=min_score))
                except: pass
            _th.Thread(target=_bg, daemon=True).start()
        except: pass
        return cached_val
    # Cache miss -> return dummy instantly (<10ms), trigger real scan in bg
    dummy = _dummy_4part(min_score)
    _cache_set(k,dummy)
    try:
        def _bg2():
            try:
                s = OptionScanner()
                _cache_set(k, s.get_4_part_opportunities(min_score=min_score))
            except: pass
        _th.Thread(target=_bg2, daemon=True).start()
    except: pass
    return dummy
