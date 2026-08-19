from fastapi import APIRouter
from core.models.bhavcopy_model import BhavcopyModel
from core.services.live_market_data import LiveMarketData
from core.services.indicator_engine import IndicatorEngine
from utils.helpers import get_strike_step

router = APIRouter()


@router.get("/vwap/{symbol}")
def vwap_scanner(symbol: str):
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
    live = LiveMarketData()
    spot = live.get_spot_price(symbol)
    step = get_strike_step(symbol)
    atm = round(spot / step) * step if spot > 0 else 0
    results = []
    ce_data = [r for r in chain if r["option_type"] == "CE"]
    for r in sorted(ce_data, key=lambda x: x["strike_price"]):
        strike = r["strike_price"]
        if strike < atm:
            continue
        oi = r.get("oi", 0)
        vol = r.get("volume", 0)
        signal = "NEUTRAL"
        if vol > 1000 and oi > 5000:
            signal = "BULLISH" if strike > atm else "BEARISH"
        elif vol > 500:
            signal = "WATCH"
        results.append({"strike": strike, "ltp": r.get("close_price", 0), "oi": oi, "volume": vol, "signal": signal, "distance": int(strike - atm)})
    return {"symbol": symbol, "date": date, "spot": spot, "results": results}


@router.get("/oi/{symbol}")
def oi_analysis(symbol: str):
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
