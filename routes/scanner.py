from fastapi import APIRouter
from core.services.scanner import OptionScanner
import time as _t
_CACHE = {}
router = APIRouter()


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
def scan_all():
    try:
        scanner = OptionScanner()
        st_result = scanner.scan()
        vwap_result = scanner.scan_vwap()
        return {"st_macd": st_result, "vwap": vwap_result}
    except Exception as e:
        return {"st_macd": {"bullish": [], "bearish": [], "total_scanned": 0, "error": str(e)[:200]}, "vwap": {"long": [], "short": [], "total_scanned": 0}}


@router.get("/fno-top5")
def fno_top5():
    k="fno"; now=_t.time()
    if k in _CACHE and now-_CACHE[k][0] < 60:
        return _CACHE[k][1]
    try:
        scanner = OptionScanner()
        res=scanner.get_fno_top5_today()
        _CACHE[k]=(now,res)
        return res
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"date": __import__("datetime").datetime.now().strftime("%Y-%m-%d"), "bullish": [], "bearish": [], "total_scanned": 0, "error": str(e)[:300]}


@router.get("/opportunities")
def top_opportunities():
    k="opp"; now=_t.time()
    if k in _CACHE and now-_CACHE[k][0] < 60:
        return _CACHE[k][1]
    try:
        scanner = OptionScanner()
        res={"opportunities": scanner.get_top_opportunities()}
        _CACHE[k]=(now,res)
        return res
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"opportunities": [], "error": str(e)[:300]}
