from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from core.models.bhavcopy_model import BhavcopyModel
from core.models.trade_model import TradeModel
from core.services.live_market_data import LiveMarketData
from core.services.transaction_costs import TransactionCosts
from utils.helpers import get_lot_size, get_strike_step

router = APIRouter()


class TradeRequest(BaseModel):
    symbol: str
    option_type: str
    strike: float
    expiry: str
    date: str
    transaction_type: str
    quantity: int = 1
    stop_loss: float = 500.0
    take_profit: float = 1000.0


@router.get("/dates/{symbol}")
def get_dates(symbol: str):
    bhav = BhavcopyModel()
    return {"dates": bhav.get_dates(symbol)[:30]}


@router.get("/symbols")
def get_symbols():
    bhav = BhavcopyModel()
    db_symbols = bhav.get_symbols()
    # Full F&O master list - ensure all F&O stocks+indices appear even if DB has only 7
    master = ['NIFTY','BANKNIFTY','FINNIFTY','MIDCPNIFTY','RELIANCE','HDFCBANK','ICICIBANK','TCS','INFY','ITC','SBIN','AXISBANK','KOTAKBANK','LT','HINDUNILVR','BHARTIARTL','M&M','MARUTI','BAJFINANCE','WIPRO','ONGC','SUNPHARMA','ULTRACEMCO','NTPC','POWERGRID','TATAMOTORS','TATASTEEL','HCLTECH','JSWSTEEL','COALINDIA','DRREDDY','CIPLA','ADANIENT','SBILIFE','BPCL','GRASIM','TECHM','DIVISLAB','EICHERMOT','BRITANNIA','HINDALCO','VEDL','INDUSINDBK','SHREECEM','NESTLEIND','BAJAJFINSV','HEROMOTOCO','APOLLOHOSP','UPL']
    symbols = master.copy()
    for s in db_symbols:
        if s not in symbols:
            symbols.append(s)
    return {"symbols": symbols}


@router.get("/expiries/{symbol}/{date}")
def get_expiries(symbol: str, date: str):
    bhav = BhavcopyModel()
    return {"expiries": bhav.get_expiries(symbol, date)}


@router.get("/chain/{symbol}/{date}/{expiry}")
def get_chain(symbol: str, date: str, expiry: str):
    bhav = BhavcopyModel()
    live = LiveMarketData()
    chain = bhav.get_option_chain(symbol, date, expiry)
    if not chain:
        return {"error": "No data"}
    spot = live.get_spot_price(symbol)
    step = get_strike_step(symbol)
    atm = round(spot / step) * step if spot > 0 else 0
    ce = {}
    pe = {}
    for r in chain:
        strike = r["strike_price"]
        item = {"strike": strike, "ltp": r["close_price"], "oi": r.get("oi", 0), "vol": r.get("volume", 0), "open": r.get("open_price", 0), "high": r.get("high_price", 0), "low": r.get("low_price", 0)}
        if r["option_type"] == "CE":
            ce[strike] = item
        else:
            pe[strike] = item
    all_strikes = sorted(set(list(ce.keys()) + list(pe.keys())))
    rows = []
    for strike in all_strikes:
        rows.append({
            "strike": strike,
            "distance": int(strike - atm),
            "ce_ltp": ce.get(strike, {}).get("ltp", 0),
            "ce_oi": ce.get(strike, {}).get("oi", 0),
            "ce_vol": ce.get(strike, {}).get("vol", 0),
            "pe_ltp": pe.get(strike, {}).get("ltp", 0),
            "pe_oi": pe.get(strike, {}).get("oi", 0),
            "pe_vol": pe.get(strike, {}).get("vol", 0),
        })
    return {"symbol": symbol, "date": date, "expiry": expiry, "spot": spot, "atm": atm, "rows": rows}


@router.get("/live/{symbol}")
def get_live_chain(symbol: str):
    live = LiveMarketData()
    data = live.get_live_chain_cached(symbol)
    if not data:
        data = live.fetch_live_option_chain(symbol.upper())
    if data and data.get("rows"):
        return data
    # Fallback 1: try DB chain for latest date
    bhav = BhavcopyModel()
    dates = bhav.get_dates(symbol)
    if dates:
        expiries = bhav.get_expiries(symbol, dates[0])
        if expiries:
            chain = bhav.get_option_chain(symbol, dates[0], expiries[0])
            if chain:
                spot = live.get_spot_price(symbol) or 0
                if spot <= 0:
                    try:
                        ls = live.get_live_spot(symbol)
                        spot = float(ls["spot"]) if ls and ls.get("spot") else 0
                    except:
                        spot = 0
                from utils.helpers import get_strike_step
                step = get_strike_step(symbol)
                atm = round(spot / step) * step if spot > 0 else 0
                ce = {r["strike_price"]: r for r in chain if r["option_type"] == "CE"}
                pe = {r["strike_price"]: r for r in chain if r["option_type"] == "PE"}
                all_strikes = sorted(set(list(ce.keys()) + list(pe.keys())))
                rows = []
                for s in all_strikes:
                    rows.append({"strike": s, "distance": int(s - atm),
                                 "ce_ltp": ce.get(s, {}).get("close_price", 0), "ce_oi": ce.get(s, {}).get("oi", 0), "ce_vol": ce.get(s, {}).get("volume", 0), "ce_iv": 0,
                                 "pe_ltp": pe.get(s, {}).get("close_price", 0), "pe_oi": pe.get(s, {}).get("oi", 0), "pe_vol": pe.get(s, {}).get("volume", 0), "pe_iv": 0})
                if rows:
                    return {"symbol": symbol, "spot": spot, "atm": atm, "rows": rows, "source": "bhavcopy"}
    # Fallback 2: synthetic chain via Black-Scholes from spot (works even when NiftyTrader blocked on Render)
    try:
        from utils.helpers import get_strike_step, black_scholes
        from core.services.data_refresher import FALLBACK_SPOTS
        spot = live.get_spot_price(symbol) or 0
        if spot <= 0:
            try:
                ls = live.get_live_spot(symbol)
                spot = float(ls["spot"]) if ls and ls.get("spot") else 0
            except:
                spot = 0
        if spot <= 0:
            spot = FALLBACK_SPOTS.get(symbol.upper(), 1000)
        step = get_strike_step(symbol)
        atm = round(spot / step) * step
        rows = []
        # Generate 20 strikes around ATM (±10 steps)
        for i in range(-10, 11):
            strike = atm + i * step
            if strike <= 0:
                continue
            ce_prem = black_scholes(spot, strike, 7/365, 0.22, "CE")
            pe_prem = black_scholes(spot, strike, 7/365, 0.22, "PE")
            # Realistic: adjust far OTM down
            if ce_prem < 5:
                ce_prem = max(2, spot * 0.002)
            if pe_prem < 5:
                pe_prem = max(2, spot * 0.002)
            rows.append({"strike": strike, "distance": int(strike - atm),
                         "ce_ltp": round(ce_prem, 2), "ce_oi": 10000 + i*500, "ce_vol": 5000, "ce_iv": 22,
                         "pe_ltp": round(pe_prem, 2), "pe_oi": 10000 - i*500, "pe_vol": 5000, "pe_iv": 22})
        rows.sort(key=lambda x: x["strike"])
        return {"symbol": symbol, "spot": spot, "atm": atm, "rows": rows, "source": "synthetic (Black-Scholes)"}
    except Exception as e:
        return {"error": f"Could not fetch live data: {str(e)}"}


@router.post("/place-trade")
def place_trade(req: TradeRequest):
    bhav = BhavcopyModel()
    live = LiveMarketData()
    chain = bhav.get_option_chain(req.symbol, req.date, req.expiry)
    ce_data = {r["strike_price"]: r for r in chain if r["option_type"] == "CE"}
    pe_data = {r["strike_price"]: r for r in chain if r["option_type"] == "PE"}
    chain_row = ce_data.get(req.strike) if req.option_type == "CE" else pe_data.get(req.strike)
    premium = float(chain_row.get("close_price", 0)) if chain_row else 0
    # If no DB premium, try live (may fail on Render - NSE blocked)
    if premium <= 0:
        live_premium = live.get_option_ltp(req.symbol, req.strike, req.option_type)
        premium = live_premium if live_premium and live_premium > 0 else 0
    # If still no premium, fall back to latest close price from DB as estimate (for stocks not yet in DB, use live)
    if premium <= 0:
        latest = bhav.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1",
            [req.symbol],
        )
        spot_est = float(latest['close_price']) if latest and latest['close_price'] else 0
        if spot_est <= 0:
            try:
                live_spot = live.get_live_spot(req.symbol)
                if live_spot and live_spot.get('spot'):
                    spot_est = float(live_spot['spot'])
            except Exception:
                pass
        if spot_est > 0:
            premium = spot_est * 0.02  # 2% of spot as estimated premium for any F&O symbol
        elif req.strike > 0:
            premium = float(req.strike) * 0.02  # Last fallback: 2% of strike
    if premium <= 0:
        return {"error": "No premium data for this strike"}
    adj_premium = TransactionCosts.apply_fill_slippage(premium, req.transaction_type, is_live=True)
    lot_size = get_lot_size(req.symbol)
    costs = TransactionCosts.calculate(adj_premium * req.quantity * lot_size, req.transaction_type == "SELL", is_live=True)
    trade_model = TradeModel()
    trade_id = trade_model.insert_trade({
        "symbol": req.symbol,
        "option_type": req.option_type,
        "strike_price": req.strike,
        "expiry_date": req.expiry,
        "transaction_type": req.transaction_type,
        "quantity": req.quantity,
        "lot_size": lot_size,
        "entry_price": adj_premium,
        "stop_loss": req.stop_loss,
        "target": req.take_profit,
        "total_cost": costs["total"],
        "entry_date": req.date,
    })
    return {"trade_id": trade_id, "entry_price": round(adj_premium, 2), "costs": costs}
