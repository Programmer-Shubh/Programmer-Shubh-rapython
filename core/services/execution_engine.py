"""Unified execution: Paper -> Dhan/Fyers/Angel. Single router for Strategy/Webhook/Live."""
from typing import Dict, List
from core.services.live_market_data import LiveMarketData
from utils.helpers import get_lot_size, get_strike_step
from core.services.transaction_costs import TransactionCosts

def execute(legs: List[Dict], symbol: str, mode: str = "PAPER", sl: float = 1500, tp: float = 3000) -> Dict:
    """legs: [{option_type, transaction, lots, strike_selection, otm_distance}]"""
    mode = mode.upper()
    live = LiveMarketData()
    spot = live.get_spot_price(symbol) or 0
    if spot <= 0:
        sd = live.get_live_spot(symbol)
        spot = float(sd["spot"]) if sd and sd.get("spot") else 0
    if mode == "PAPER":
        from core.models.trade_model import TradeModel
        # place first leg as paper trade (multi-leg handled via Backtest spread later)
        leg = legs[0] if legs else {"option_type":"CE","transaction":"buy","lots":1}
        step = get_strike_step(symbol)
        atm = round(spot/step)*step if spot and step else 0
        otm = int(leg.get("otm_distance",1))
        sel = leg.get("strike_selection","atm")
        if sel == "otm": strike = atm + (otm*step if leg["option_type"]=="CE" else -otm*step)
        elif sel == "itm": strike = atm + (-otm*step if leg["option_type"]=="CE" else otm*step)
        else: strike = atm
        # premium via nse_client or Black-Scholes fallback handled in trade route
        from core.models.bhavcopy_model import BhavcopyModel
        bhav = BhavcopyModel()
        # reuse paper_trade route logic via direct DB insert with costs
        from routes.option_chain import TradeRequest
        # fallback premium: Black-Scholes
        premium = 0
        try:
            from utils.helpers import black_scholes
            premium = black_scholes(spot, strike, 7/365, 0.22, leg["option_type"])
        except Exception:
            premium = spot*0.02
        adj = TransactionCosts.apply_fill_slippage(premium, leg["transaction"].upper(), is_live=False)
        lot = get_lot_size(symbol)
        costs = TransactionCosts.calculate(adj * int(leg.get("lots",1)) * lot, leg["transaction"].lower()=="sell", is_live=False)
        tm = TradeModel()
        tid = tm.insert_trade({"symbol":symbol,"option_type":leg["option_type"],"strike_price":strike,"expiry_date":"","transaction_type":leg["transaction"].upper(),"quantity":int(leg.get("lots",1)),"lot_size":lot,"entry_price":adj,"stop_loss":sl,"target":tp,"total_cost":costs["total"],"entry_date":__import__("datetime").date.today().strftime("%Y-%m-%d")})
        return {"mode":"PAPER","trade_id":tid,"strike":strike,"premium":adj}
    # Live: route to broker
    broker_map = {"DHAN":"dhan","FYERS":"fyers","ANGEL":"angel"}
    broker = broker_map.get(mode, "dhan")
    try:
        if broker == "dhan":
            from core.services.broker_dhan import place_order as dhan_place
            return dhan_place(symbol, legs, sl, tp)
        elif broker == "fyers":
            from core.services.broker_fyers import place_order as fyers_place
            return fyers_place(symbol, legs, sl, tp)
        else:
            return {"error": f"Live broker {mode} not configured"}
    except Exception as e:
        return {"error": str(e)}
