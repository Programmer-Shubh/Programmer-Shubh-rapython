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
    stop_loss: float = 1500.0
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


@router.get("/chain/{symbol}")
def get_chain_auto(symbol: str):
    """Auto option chain without date - live NSE (Yahoo spot + NSE option live) or synthetic - no date input needed."""
    live = LiveMarketData()
    # try cached live first (2s TTL)
    data = live.get_live_chain_cached(symbol)
    if not data:
        data = live.fetch_live_option_chain(symbol)
    if data and data.get("rows"):
        return data
    return {"error": "No data - try again"}


@router.get("/chain/{symbol}/{date}/{expiry}")
def get_chain(symbol: str, date: str, expiry: str):
    bhav = BhavcopyModel()
    live = LiveMarketData()
    chain = bhav.get_option_chain(symbol, date, expiry)
    if not chain:
        # No bhavcopy for this date/expiry - fallback to live auto chain (no date needed, no network error)
        data = live.get_live_chain_cached(symbol)
        if not data:
            data = live.fetch_live_option_chain(symbol)
        if data and data.get("rows"):
            return {"symbol": symbol, "date": date, "expiry": data.get("timestamp") or expiry, "spot": data.get("spot"), "atm": data.get("atm"), "rows": data.get("rows"), "source": data.get("source")}
        return {"error": "No data - no bhavcopy for this date and live fetch failed"}
    spot = live.get_spot_price(symbol)
    # if DB spot missing, use live Yahoo spot (instant via cache or 2s)
    if spot <= 0:
        try:
            ld = live.get_live_spot(symbol)
            if ld and ld.get("spot"):
                spot = float(ld["spot"])
            else:
                ld2 = live.fetch_live_from_nse(symbol)
                if ld2 and ld2.get("spot"):
                    spot = float(ld2["spot"])
        except Exception:
            pass
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
    symbol = symbol.upper()
    # 1) Live chain: DB/bhavcopy -> synthetic (NSE spot + Black-Scholes, no NiftyTrader)
    data = live.get_live_chain_cached(symbol)
    if not data:
        data = live.fetch_live_option_chain(symbol)
    if data and data.get("rows"):
        return data
    return {"error": "Could not fetch live data. Try again later."}


@router.post("/place-trade")
def place_trade(req: TradeRequest):
    # Mandatory SL/Target validation
    if req.stop_loss is None or req.take_profit is None:
        return {"error": "Stop-Loss and Target are mandatory - cannot be blank (SELL requires SL to prevent unmanaged risk)"}
    if req.stop_loss <= 0 or req.take_profit <= 0:
        return {"error": "Stop-Loss and Target must be > 0 - mandatory fields"}
    # Data Validation: symbol, option_type, quantity, strike checks
    if not req.symbol or not str(req.symbol).strip():
        return {"error": "Symbol missing"}
    if req.option_type not in ("CE", "PE"):
        return {"error": "Option type must be CE or PE"}
    if req.quantity <= 0:
        return {"error": "Quantity must be > 0"}
    # Strike validation: reject 0, '01', missing, not aligned
    raw_strike = str(req.strike).strip() if req.strike is not None else ""
    if req.strike is None or req.strike <= 0:
        # Auto-select ATM if strike 0 or invalid (fixes BANKNIFTY CE 0 bug)
        try:
            live_tmp = LiveMarketData()
            spot_tmp = live_tmp.get_spot_price(req.symbol)
            if spot_tmp <= 0:
                ls = live_tmp.get_live_spot(req.symbol)
                spot_tmp = float(ls["spot"]) if ls and ls.get("spot") else 0
            step_tmp = get_strike_step(req.symbol)
            if spot_tmp > 0:
                req.strike = round(spot_tmp / step_tmp) * step_tmp
            else:
                return {"error": f"Invalid strike price {req.strike} (0) - no live spot to auto-select ATM"}
        except Exception as e:
            return {"error": f"Invalid strike price {req.strike}: {e}"}
    # Reject faulty leading zero like '01' (comes as 1.0)
    if raw_strike.startswith("0") and raw_strike not in ("0", "0.0") and not raw_strike.startswith("0."):
        return {"error": f"Faulty strike price '{raw_strike}' - remove leading zeros"}
    # Auto-align strike to nearest valid step (no error for any symbol)
    try:
        step = get_strike_step(req.symbol)
        if req.strike % step != 0:
            if abs((req.strike % step)) > 0.01 and abs(step - (req.strike % step)) > 0.01:
                req.strike = round(req.strike / step) * step
    except Exception:
        pass
    # Deduplication check before insert
    from core.models.trade_model import TradeModel as _TM
    _tm = _TM()
    dup = _tm.db.fetch_one("SELECT id FROM paper_trades WHERE symbol=? AND strike_price=? AND option_type=? AND transaction_type=? AND status='open' LIMIT 1", [req.symbol, req.strike, req.option_type, req.transaction_type])
    if dup:
        return {"error": f"Duplicate open position for {req.symbol} {req.strike} {req.option_type} {req.transaction_type} (ID {dup['id']}) - already open"}
    bhav = BhavcopyModel()
    live = LiveMarketData()
    chain = bhav.get_option_chain(req.symbol, req.date, req.expiry)
    ce_data = {r["strike_price"]: r for r in chain if r["option_type"] == "CE"}
    pe_data = {r["strike_price"]: r for r in chain if r["option_type"] == "PE"}
    chain_row = ce_data.get(req.strike) if req.option_type == "CE" else pe_data.get(req.strike)
    premium = float(chain_row.get("close_price", 0)) if chain_row else 0
    if premium <= 0:
        live_premium = live.get_option_ltp(req.symbol, req.strike, req.option_type)
        premium = live_premium if live_premium and live_premium > 0 else 0
    if premium <= 0:
        try:
            lc = live.fetch_live_option_chain(req.symbol)
            if lc and lc.get("rows"):
                for r in lc["rows"]:
                    if r.get("strike") == req.strike:
                        premium = float(r.get("ce_ltp", 0) if req.option_type == "CE" else r.get("pe_ltp", 0))
                        break
        except Exception:
            pass
    if premium <= 0:
        try:
            spot_price = live.get_spot_price(req.symbol)
            if spot_price <= 0:
                ld = live.get_live_spot(req.symbol)
                spot_price = float(ld["spot"]) if ld and ld.get("spot") else 0
            if spot_price > 0 and req.strike > 0:
                from utils.helpers import black_scholes
                import datetime
                try:
                    exp_dt = datetime.datetime.strptime(req.expiry, "%Y-%m-%d") if req.expiry else datetime.datetime.now() + datetime.timedelta(days=7)
                    dte = max(1, (exp_dt - datetime.datetime.now()).days)
                except Exception:
                    dte = 7
                premium = black_scholes(spot_price, req.strike, dte / 365.0, 0.20, req.option_type)
        except Exception:
            pass
    if premium <= 0:
        return {"error": f"No premium data for {req.symbol} {req.strike} {req.option_type}. Try refreshing option chain."}
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
