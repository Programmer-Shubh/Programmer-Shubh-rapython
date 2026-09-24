import time
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from core.models.bhavcopy_model import BhavcopyModel
from core.models.trade_model import TradeModel
from core.services.live_market_data import LiveMarketData
from core.services.transaction_costs import TransactionCosts
from utils.helpers import get_lot_size, get_strike_step, align_strike_price, black_scholes, model_premium

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
    trade_type: str = "intraday"


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
                                  "ce_ltp": r.get("ce_ltp", 0), "ce_oi": r.get("ce_oi", 0), "ce_vol": r.get("ce_vol", 0), "ce_iv": r.get("ce_iv", 0), "ce_oi_change": r.get("ce_oi_change", 0),
                                  "pe_ltp": r.get("pe_ltp", 0), "pe_oi": r.get("pe_oi", 0), "pe_vol": r.get("pe_vol", 0), "pe_iv": r.get("pe_iv", 0), "pe_oi_change": r.get("pe_oi_change", 0)})
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
    # 2) Get spot price: live (NSE/Stooq/Google, no Yahoo) -> DB
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
    # Last resort: stale DB chain (any recent date) instead of an error, so the
    # page never shows "No chain data available" when history exists
    try:
        _stale = bhav.get_dates(symbol)
        for _d in (_stale or [])[:3]:
            _exps = bhav.get_expiries(symbol, _d)
            if not _exps:
                continue
            _ch = bhav.get_option_chain(symbol, _d, _exps[0])
            if _ch:
                _ce = {}
                _pe = {}
                for r in _ch:
                    item = {"strike": r["strike_price"], "ltp": r["close_price"], "oi": r.get("oi", 0), "vol": r.get("volume", 0)}
                    if r["option_type"] == "CE":
                        _ce[r["strike_price"]] = item
                    else:
                        _pe[r["strike_price"]] = item
                _all = sorted(set(list(_ce.keys()) + list(_pe.keys())))
                _rows = [{"strike": s, "distance": 0, "ce_ltp": _ce.get(s, {}).get("ltp", 0), "ce_oi": 0, "ce_vol": 0, "ce_iv": 0, "pe_ltp": _pe.get(s, {}).get("ltp", 0), "pe_oi": 0, "pe_vol": 0, "pe_iv": 0} for s in _all]
                if _rows:
                    return {"symbol": symbol, "spot": spot, "atm": atm, "rows": _rows, "source": "db-stale", "note": f"Stale chain {_d} (live spot unavailable)"}
    except Exception:
        pass
    return {"error": "No chain data available"}


@router.post("/place-trade")
def place_trade(req: TradeRequest):
    try:
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
        # Global strike alignment: snap to nearest valid step (e.g. Cipla 123 -> 120)
        try:
            aligned = align_strike_price(req.symbol, req.strike)
            if abs(aligned - req.strike) > 0.01:
                req.strike = aligned
        except Exception:
            pass
        # ATM-distance guard: relaxed to 12% and chain-aware (fixes false 60% error for KOTAKBANK/BANKNIFTY)
        # If the exact strike exists in the visible chain (DB or model), allow it even if dev >12% — stale price, not stale strike.
        try:
            _live0 = LiveMarketData()
            _spot0 = _live0.get_spot_price(req.symbol) or 0
            if _spot0 > 0:
                # chain_has_strike = already fetched chain contains this strike?
                _chain_has = False
                try:
                    if 'chain' in locals() and chain:
                        _chain_has = any(float(r.get("strike_price",0))==float(req.strike) for r in chain)
                except: pass
                if not _chain_has:
                    try:
                        bhav2 = BhavcopyModel()
                        _chk = bhav2.get_option_chain(req.symbol, req.date, req.expiry) if req.date and req.expiry else []
                        if _chk and any(float(r.get("strike_price",0))==float(req.strike) for r in _chk):
                            _chain_has = True
                    except: pass
                _step0 = get_strike_step(req.symbol)
                _atm0 = round(_spot0 / _step0) * _step0
                _dev = abs(float(req.strike) - _atm0) / _spot0 if _spot0 else 0
                # Block far ITM/OTM orders: allow ATM ±10 steps or ±6% (saved
                # robots store stale strikes — e.g. CIPLA 1180 vs ATM 1380 at
                # trade time). For saved-robot replays auto-snap instead of error.
                _allow = max(10 * _step0, 0.06 * _spot0)
                if _dev * _spot0 > _allow and not _chain_has:
                    # Snap stale strike to ATM for saved-robot/paper replays instead of hard error
                    try:
                        # keep direction (OTM/ITM) if possible
                        _orig = float(req.strike)
                        _snapped = _atm0
                        # try to preserve OTM vs ATM intent (if original was below ATM for PE / above for CE small drift, keep near ATM)
                        req.strike = float(_snapped)
                    except Exception:
                        return {"error": f"Strike {req.strike} ATM {_atm0} se bahut door hai (spot {_spot0:,.2f}) - ATM ke 2-3 strike upar-neeche chunho"}
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
        # If still no premium, instant model premium via LIVE spot (same as
        # chain's model_rows - live first, then DB - so entry == chain)
        if premium <= 0:
            try:
                _spot_g = live.get_spot_price(req.symbol) or 0
                if _spot_g <= 0:
                    try:
                        _ls = live.get_live_spot(req.symbol)
                        _spot_g = float(_ls["spot"]) if _ls and _ls.get("spot") else 0
                    except: pass
                # Fallback to DB spot only if live unavailable (same order as chain)
                if _spot_g <= 0:
                    try:
                        _dr = bhav.db.fetch_one("SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1", [req.symbol])
                        _spot_g = float(_dr["close_price"]) if _dr and _dr["close_price"] else 0
                    except: pass
                if _spot_g > 0 and req.strike > 0:
                    try:
                        expiry_days = 7
                        # Far expiry (2027-03-30) from stale suggestion would inflate
                        # premium to 3362 vs chain 471 — force weekly for paper
                        try:
                            if req.expiry and "-" in str(req.expiry):
                                import datetime as _ed
                                ed = _ed.datetime.strptime(str(req.expiry)[:10], "%Y-%m-%d")
                                diff = (ed - _ed.datetime.now()).days
                                if diff > 30:
                                    expiry_days = 7
                                elif "monthly" in str(req.expiry).lower():
                                    expiry_days = 28
                        except Exception:
                            pass
                        premium = model_premium(_spot_g, req.strike, expiry_days, req.option_type, symbol=req.symbol)
                    except Exception:
                        premium = max(round(_spot_g * 0.015, 2), 1.5)
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
            # Stale-DB guard: DB close like 1.50 for ATM is unrealistic (shows 1.50 vs scanner 105) -> ALWAYS recompute via SAME model_premium as scanner (fixes NIFTY 105 vs 1.50 mismatch for all symbols)
            # Use DB/historical spot FIRST (same as scanner's _suggest_option), then live — ensures dashboard 25000 vs order 25000 match, not 23376
            if premium is not None and premium < 10:
                try:
                    spot_chk = 0
                    # Prefer DB/historical spot (scanner uses this when live cache miss, shows 25000)
                    try:
                        _latest = bhav.db.fetch_one("SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1", [req.symbol])
                        spot_chk = float(_latest['close_price']) if _latest and _latest['close_price'] else 0
                    except: pass
                    if spot_chk <= 0:
                        try:
                            spot_chk = live.get_spot_price(req.symbol)
                        except Exception:
                            pass
                    if spot_chk <= 0:
                        try:
                            ls = live.get_live_spot(req.symbol)
                            spot_chk = float(ls["spot"]) if ls and ls.get("spot") else 0
                        except Exception:
                            spot_chk = 0
                    if spot_chk > 0:
                        expiry_days = 7
                        if req.expiry and "monthly" in req.expiry.lower():
                            expiry_days = 28
                        elif req.expiry and "-" in req.expiry:
                            try:
                                import datetime as _dt2
                                exp_d2 = _dt2.datetime.strptime(req.expiry, "%Y-%m-%d")
                                expiry_days = max(2, min(45, (exp_d2 - _dt2.datetime.now()).days))
                            except: pass
                        # Use same model as scanner — ensures 105 vs 1.50 mismatch never happens for any symbol
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
            "trade_type": req.trade_type,
        })
        return {"trade_id": trade_id, "entry_price": round(adj_premium, 2), "costs": costs}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": f"Order failed: {str(e)[:250]}"}


@router.get("/greeks/{symbol}")
def option_greeks(symbol: str, date: str = "", expiry: str = ""):
    """nsefin Greeks (delta/gamma/theta/vega/IV) on latest DB chain snapshot.
    Optional ?date=YYYY-MM-DD&expiry=YYYY-MM-DD."""
    try:
        from core.models.database import Database
        import pandas as pd
        sym = (symbol or "").upper()
        db = Database.get_instance()
        if not date:
            r = db.fetch_one("SELECT MAX(trade_date) d FROM bhavcopy_data WHERE symbol=? AND option_type IN ('CE','PE')", [sym])
            date = (r["d"] if r and r["d"] else "") or ""
        if not date:
            return {"error": f"No chain data for {sym}. Run /api/backtest/seed first."}
        if not expiry:
            r = db.fetch_one("SELECT expiry_date e, COUNT(*) c FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND option_type IN ('CE','PE') GROUP BY expiry_date ORDER BY c DESC LIMIT 1", [sym, date])
            expiry = (r["e"] if r and r["e"] else "") or ""
        rows = db.fetch_all("SELECT strike_price, option_type, close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND expiry_date=? AND option_type IN ('CE','PE')", [sym, date, expiry])
        ce = {float(r["strike_price"]): float(r["close_price"] or 0) for r in rows if r["option_type"] == "CE"}
        pe = {float(r["strike_price"]): float(r["close_price"] or 0) for r in rows if r["option_type"] == "PE"}
        strikes = sorted(set(ce) & set(pe))
        if len(strikes) < 3:
            return {"error": f"Need 3+ strikes with CE+PE on {sym} {date} (found {len(strikes)})"}
        spot_r = db.fetch_one("SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND option_type IS NULL", [sym, date])
        spot = float(spot_r["close_price"]) if spot_r and spot_r["close_price"] else 0.0
        import datetime as _dt
        exp_fmt = _dt.datetime.strptime(expiry, "%Y-%m-%d").strftime("%d-%b-%Y")
        df = pd.DataFrame({"strike": strikes, "ce_ltp": [ce[s] for s in strikes],
                           "pe_ltp": [pe[s] for s in strikes], "spot_price": spot, "expiry": exp_fmt})
        from nsefin import get_nse_instance
        g = get_nse_instance().compute_greek(df, strike_diff=get_strike_step(sym))
        keep = {"strike", "ce_ltp", "pe_ltp", "spot_price", "dte"}
        out = []
        for _, r2 in g.iterrows():
            d2 = dict(r2)
            row = {}
            for k, v in d2.items():
                kl = str(k).lower()
                if k in keep or any(x in kl for x in ("delta", "gamma", "theta", "vega", "iv")):
                    try:
                        row[k] = round(float(v), 4)
                    except Exception:
                        row[k] = str(v)
            out.append(row)
        atm = round(spot / get_strike_step(sym)) * get_strike_step(sym) if spot > 0 else 0
        return {"symbol": sym, "date": date, "expiry": expiry, "spot": spot, "atm": atm,
                "count": len(out), "greeks": out[:121]}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e)[:300]}
