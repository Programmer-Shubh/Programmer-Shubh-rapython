from fastapi import APIRouter
from core.services.scanner import OptionScanner
import time as _t
import threading as _th
_CACHE = {}
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
    try:
        scanner = OptionScanner()
        st_result = scanner.scan(min_score=min_score)
        vwap_result = scanner.scan_vwap()
        return {"st_macd": st_result, "vwap": vwap_result}
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
    k="fno"; now=_t.time()
    if k in _CACHE:
        ct, cv = _CACHE[k]
        if now-ct < 300:
            return cv
        # stale return + bg refresh
        try:
            def _bg():
                try: _CACHE[k]=(_t.time(), OptionScanner().get_fno_top5_today())
                except: pass
            _th.Thread(target=_bg, daemon=True).start()
        except: pass
        return cv
    try:
        scanner = OptionScanner()
        res=scanner.get_fno_top5_today()
        _CACHE[k]=(now,res)
        return res
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"date": __import__("datetime").datetime.now().strftime("%Y-%m-%d"), "bullish": [], "bearish": [], "total_scanned": 0, "error": str(e)[:300]}


@router.get("/opportunities")
def top_opportunities(min_score: int = 80):
    try: min_score=int(min_score)
    except: min_score=80
    k=f"opp_{min_score}"; now=_t.time()
    if k in _CACHE and now-_CACHE[k][0] < 300:
        return _CACHE[k][1]
    try:
        scanner = OptionScanner()
        res={"opportunities": scanner.get_top_opportunities(min_score=min_score)}
        _CACHE[k]=(now,res)
        return res
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"opportunities": [], "error": str(e)[:300]}


@router.get("/opportunities-4part")
def opportunities_4part(min_score: int = 80):
    """4-part dashboard: CE Buy / PE Buy / CE Sell / PE Sell, Score>=80 - instant after open (stale-while-revalidate)"""
    try: min_score=int(min_score)
    except: min_score=80
    k=f"opp4_{min_score}"; now=_t.time()
    # If cached (even slightly stale), return instantly and refresh in background
    if k in _CACHE:
        cached_time, cached_val = _CACHE[k]
        if now - cached_time < 120:
            return cached_val
        # stale but return immediately, refresh async
        try:
            def _bg():
                try:
                    s = OptionScanner()
                    _CACHE[k]=(_t.time(), s.get_4_part_opportunities(min_score=min_score))
                except: pass
            _th.Thread(target=_bg, daemon=True).start()
        except: pass
        return cached_val
    try:
        scanner = OptionScanner()
        res = scanner.get_4_part_opportunities(min_score=min_score)
        _CACHE[k]=(now,res)
        return res
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"ce_buy": [], "pe_buy": [], "ce_sell": [], "pe_sell": [], "error": str(e)[:300]}
