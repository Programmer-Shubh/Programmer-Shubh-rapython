import time
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from core.models.bhavcopy_model import BhavcopyModel
from core.models.trade_model import TradeModel
from core.services.live_market_data import LiveMarketData
from core.services.transaction_costs import TransactionCosts
from utils.helpers import get_lot_size, get_strike_step, black_scholes, model_premium

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
    spot = live.get_spot_price(symbol)
    step = get_strike_step(symbol)
    atm = round(spot / step) * step if spot > 0 else 0
    ce = {}
    pe = {}
    if chain:
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
            "ce_vol": ce.get(strike, {}).get("volume", 0),
            "pe_ltp": pe.get(strike, {}).get("ltp", 0),
            "pe_oi": pe.get(strike, {}).get("oi", 0),
            "pe_vol": pe.get(strike, {}).get("volume", 0),
        })
    return {"symbol": symbol, "date": date, "expiry": expiry, "spot": spot, "atm": atm, "rows": rows}


@router.get("/live/{symbol}")
def get_live_chain(symbol: str):
    live = LiveMarketData()
    sym = symbol.upper()
    # 0) Real NSE chain when NOT on cloud (local/home IP - exact NSE match).
    # On Render/cloud NSE blocks the IP, so this is skipped (fast, no timeouts).
    try:
        from core.services.nse_client import is_cloud as _is_cloud, nse_fetch_option_chain_v3
        if not _is_cloud():
            nse = nse_fetch_option_chain_v3(sym, timeout=6)
            if nse and nse.get("rows"):
                _atm = nse.get("atm") or 0
                _rows = []
                for r in nse["rows"]:
                    _rows.append({"strike": r["strike"], "distance": int(r["strike"] - _atm) if _atm else 0,
                                  "ce_ltp": r.get("ce_ltp", 0), "ce_oi": r.get("ce_oi", 0), "ce_vol": r.get("ce_vol", 0), "ce_iv": r.get("ce_iv", 0),
                                  "pe_ltp": r.get("pe_ltp", 0), "pe_oi": r.get("pe_oi", 0), "pe_vol": r.get("pe_vol", 0), "pe_iv": r.get("pe_iv", 0)})
                return {"symbol": sym, "spot": nse.get("spot", 0), "atm": _atm, "rows": _rows,
                        "source": "nse", "expiry": nse.get("expiry", ""), "pcr": nse.get("pcr"),
                        "timestamp": nse.get("timestamp", "")}
    except Exception:
        pass
    # 1) DB chain ONLY if fresh (trade_date == today IST). A stale EOD chain
    # shows old LTPs (e.g. POWERGRID 370 CE 3.75) while orders price off live
    # spot (270 PE 5.91) -> chain vs entry mismatch on every symbol. Stale DB
    # falls through to the model chain below, which always matches order entry.
    bhav = BhavcopyModel()
    dates = bhav.get_dates(symbol)
    chain = None
    try:
        import datetime as _dt
        _today = (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
    except Exception:
        _today = ""
    if dates and dates[0] == _today:
        expiries = bhav.get_expiries(symbol, dates[0])
        if expiries:
            chain = bhav.get_option_chain(symbol, dates[0], expiries[0])
    # 2) Get spot price: live (Yahoo-first on cloud) -> DB
    spot = live.get_spot_price(symbol)
    step = get_strike_step(symbol)
    atm = round(spot / step) * step if spot > 0 else 0
    ce = {}
    pe = {}
    if chain:
        for r in chain:
            strike = r["strike_price"]
            item = {"strike": strike, "ltp": r["close_price"], "oi": r.get("oi", 0), "vol": r.get("volume", 0), "open": r.get("open_price", 0), "high": r.get("high_price", 0), "low": r.get("low_price", 0)}
            if r["option_type"] == "CE":
                ce[strike] = item
            else:
                pe[strike] = item
    all_strikes = sorted(set(list(ce.keys()) + list(pe.keys())))
    rows = []
    for s in all_strikes:
        rows.append({"strike": s, "distance": int(s - atm),
                     "ce_ltp": ce.get(s, {}).get("ltp", 0), "ce_oi": ce.get(s, {}).get("oi", 0), "ce_vol": ce.get(s, {}).get("volume", 0), "ce_iv": 0,
                     "pe_ltp": pe.get(s, {}).get("ltp", 0), "pe_oi": pe.get(s, {}).get("oi", 0), "pe_vol": pe.get(s, {}).get("volume", 0), "pe_iv": 0})
    if rows:
        return {"symbol": symbol, "spot": spot, "atm": atm, "rows": rows, "source": "db"}
    # 3) Fallback: ATM-centered model chain (same model_premium as order entry,
    # so chain premium == trade entry; no random IVs). NSE itself is blocked on cloud.
    from utils.helpers import model_premium
    model_rows = []
    if spot > 0 and atm > 0:
        for i in range(-7, 8):
            strike = atm + i * step
            if strike <= 0:
                continue
            ce_price = model_premium(spot, strike, 7, "CE", symbol=sym)
            pe_price = model_premium(spot, strike, 7, "PE", symbol=sym)
            model_rows.append({"strike": strike, "distance": int(strike - atm),
                               "ce_ltp": ce_price, "ce_oi": 0, "ce_vol": 0, "ce_iv": 0,
                               "pe_ltp": pe_price, "pe_oi": 0, "pe_vol": 0, "pe_iv": 0})
    if model_rows:
        return {"symbol": symbol, "spot": spot, "atm": atm, "rows": model_rows, "source": "model", "note": "NSE blocked on cloud - model chain (same rate as order entry)"}
    return {"error": "No chain data available"}


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
    # Validate strike step alignment
    try:
        step = get_strike_step(req.symbol)
        if req.strike % step != 0:
            if abs((req.strike % step)) > 0.01 and abs(step - (req.strike % step)) > 0.01:
                return {"error": f"Strike {req.strike} not aligned to step {step} for {req.symbol}"}
    except Exception:
        pass
    # ATM-distance guard: trades must land near live ATM (stale scanner/chain data
    # produces garbage strikes like SENSEX 4900 vs spot 76826). Fail open when
    # live spot is unavailable.
    try:
        _live0 = LiveMarketData()
        _spot0 = _live0.get_spot_price(req.symbol) or 0
        if _spot0 > 0:
            _step0 = get_strike_step(req.symbol)
            _atm0 = round(_spot0 / _step0) * _step0
            _dev = abs(float(req.strike) - _atm0) / _spot0
            if _dev > 0.05:
                return {"error": f"Strike {req.strike} is {_dev*100:.1f}% away from live ATM {_atm0} (spot {_spot0:,.2f}) - stale data? Refresh chain/scanner and retry near ATM"}
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
    # Historical-date honesty: if the order date is NOT today and the DB has no
    # premium for this strike/date, NEVER fall back to live pricing (that mixes
    # a stale chain view with a live rate, e.g. chain 370@3.75 vs entry 5.91).
    # Fail with a clear message so the rate always matches the visible chain.
    try:
        import datetime as _dt
        _ist = (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
    except Exception:
        _ist = ""
    if premium <= 0 and req.date and _ist and str(req.date) < _ist:
        return {"error": f"No {req.option_type} {req.strike} premium on {req.date} for {req.symbol} - pick a strike visible in that date's chain (live rate not applied to past dates)"}
    # If no DB premium, try live (may fail on Render - NSE blocked)
    if premium <= 0:
        live_premium = live.get_option_ltp(req.symbol, req.strike, req.option_type)
        premium = live_premium if live_premium and live_premium > 0 else 0
    # If still no premium, try Google Finance real premium (no synthetic 2%)
    if premium <= 0:
        # Try Google Finance real option quote as last real source
        try:
            import requests
            # Google Finance option quote: try NSE option page
            headers = {"User-Agent": "Mozilla/5.0"}
            g_url = f"https://www.google.com/finance/quote/{req.symbol}:NSE"
            gr = requests.get(g_url, headers=headers, timeout=8)
            if gr.status_code == 200 and "data-last-price" in gr.text:
                import re
                m = re.search(r'data-last-price="([^"]+)"', gr.text)
                if m:
                    spot_g = float(m.group(1).replace(",", ""))
                    if spot_g > 0 and req.strike > 0:
                        # Unified model premium (IV 25%, floor 1.5) - same as open positions
                        try:
                            expiry_days = 7
                            if req.expiry and "monthly" in req.expiry.lower():
                                expiry_days = 28
                            premium = model_premium(spot_g, req.strike, expiry_days, req.option_type, symbol=req.symbol)
                        except Exception:
                            premium = max(round(spot_g * 0.015, 2), 1.5)
        except Exception:
            pass
    if premium <= 0:
        # Try DB spot as last real check (no synthetic 2% of strike)
        try:
            latest = bhav.db.fetch_one(
                "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1",
                [req.symbol],
            )
            spot_est = float(latest['close_price']) if latest and latest['close_price'] else 0
        except:
            spot_est = 0
        if spot_est <= 0:
            try:
                live_spot = live.get_live_spot(req.symbol)
                if live_spot and live_spot.get('spot'):
                    spot_est = float(live_spot['spot'])
            except Exception:
                pass
        if spot_est > 0 and req.strike > 0:
            # Unified model premium (IV 25%, floor 1.5) - same as open positions
            try:
                expiry_days = 7
                if req.expiry and "monthly" in req.expiry.lower():
                    expiry_days = 28
                # Try parse expiry date if it's YYYY-MM-DD
                elif req.expiry and "-" in req.expiry:
                    try:
                        import datetime as _dt
                        exp_d = _dt.datetime.strptime(req.expiry, "%Y-%m-%d")
                        today = _dt.datetime.now()
                        diff = (exp_d - today).days
                        if diff > 0:
                            expiry_days = min(45, max(2, diff))
                    except Exception:
                        pass
                premium = model_premium(spot_est, req.strike, expiry_days, req.option_type, symbol=req.symbol)
            except Exception:
                premium = max(round(spot_est * 0.015, 2), 1.5)
        if premium <= 0:
            return {"error": "No premium data for this strike"}
        # Stale-DB guard: DB close like 1.0 for ATM is unrealistic -> recompute with
        # live spot via the SAME unified model (floor 1.5, never inflated to 5).
        # Real DB premiums are otherwise used as-is.
        if premium < 10:
            try:
                spot_chk = 0
                # Yahoo first (most reliable free)
                try:
                    from core.services.free_data import fetch_yahoo_spot
                    spot_chk = fetch_yahoo_spot(req.symbol)
                except Exception:
                    pass
                if spot_chk <= 0:
                    spot_chk = live.get_spot_price(req.symbol)
                if spot_chk <= 0:
                    try:
                        ls = live.get_live_spot(req.symbol)
                        spot_chk = float(ls["spot"]) if ls and ls.get("spot") else 0
                    except Exception:
                        spot_chk = 0
                step_chk = get_strike_step(req.symbol)
                atm_chk = round(spot_chk / step_chk) * step_chk if spot_chk > 0 else 0
                if atm_chk > 0 and abs(req.strike - atm_chk) <= step_chk * 4:
                    expiry_days = 7
                    if req.expiry and "monthly" in req.expiry.lower():
                        expiry_days = 28
                    premium = model_premium(spot_chk, req.strike, expiry_days, req.option_type, symbol=req.symbol)
            except Exception:
                pass
    adj_premium = TransactionCosts.apply_fill_slippage(premium, req.transaction_type, is_live=True)
    lot_size = get_lot_size(req.symbol)
    costs = TransactionCosts.calculate(adj_premium * req.quantity * lot_size, req.transaction_type == "SELL", is_live=True)
    trade_model = TradeModel()
    from utils.helpers import model_iv as _model_iv
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
        "entry_iv": _model_iv(req.symbol),
    })
    return {"trade_id": trade_id, "entry_price": round(adj_premium, 2), "costs": costs}
