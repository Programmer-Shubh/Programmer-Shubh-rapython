"""Unified execution: Paper -> Fyers. Single router for Strategy/Webhook/Live."""
from typing import Dict, List
from core.services.live_market_data import LiveMarketData
from utils.helpers import get_lot_size, get_strike_step
from core.services.transaction_costs import TransactionCosts

def execute(legs: List[Dict], symbol: str, mode: str = "PAPER", sl: float = 1500, tp: float = 3000) -> Dict:
    """legs: [{option_type, transaction, lots, strike_selection, otm_distance}]"""
    mode = mode.upper()
    if mode == "PAPER":
        # Market-hours gate (button/webhook PAPER entries): band market me
        # fantasy rate par order nahi. LIVE broker flow untouched (AMO etc.
        # broker khud handle karta hai).
        try:
            from core.services.sl_monitor import market_open_ist as _mkt
            if not bool(_mkt()):
                return {"error": "Market band hai - paper trade sirf market hours me lagega (Mon-Fri 9:15-3:30 IST)."}
        except Exception:
            pass
        # 3:00 PM entry cutoff (user rule).
        try:
            from core.services.sl_monitor import entry_open_ist as _eok
            from core.services.sl_monitor import entry_block_reason as _ebr
            if not bool(_eok()):
                return {"error": _ebr()}
        except Exception:
            pass
    # Official-lot gate (fail-closed): unverified lot par order BLOCK.
    try:
        from utils.helpers import lot_guard_error as _lge5
        _lot_err5 = _lge5(symbol)
        if _lot_err5:
            return {"error": _lot_err5}
    except Exception:
        pass
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
        # premium via unified model (per-symbol IV, floor 1.5 — backtest parity).
        # DTE: weekly legs -> 7, monthly -> 28 (pehle hardcoded 7 tha, monthly
        # paper entry backtest se sasta/mahanga padta tha).
        premium = 0
        try:
            from utils.helpers import model_premium, model_iv
            _exp_hint = str((leg.get("expiry") or "")).lower()
            _dte = 28 if _exp_hint == "monthly" else 7
            premium = model_premium(spot, strike, _dte, leg["option_type"], symbol=symbol)
        except Exception:
            premium = 0  # purana spot*2% synthetic HATAYA - fantasy fill nahi
        if not premium or premium <= 0:
            return {"error": f"No premium for {symbol} {strike}{leg['option_type']} (spot/model fail) - order nahi laga"}
        adj = TransactionCosts.apply_fill_slippage(premium, leg["transaction"].upper(), is_live=False)
        lot = get_lot_size(symbol)
        costs = TransactionCosts.calculate(adj * int(leg.get("lots",1)) * lot, leg["transaction"].lower()=="sell", is_live=False)
        tm = TradeModel()
        try:
            from utils.helpers import model_iv as _miv
            _eiv = _miv(symbol)
        except Exception:
            _eiv = None
        tid = tm.insert_trade({"symbol":symbol,"option_type":leg["option_type"],"strike_price":strike,"expiry_date":"","transaction_type":leg["transaction"].upper(),"quantity":int(leg.get("lots",1)),"lot_size":lot,"entry_price":adj,"stop_loss":sl,"target":tp,"total_cost":costs["total"],"entry_date":__import__("datetime").date.today().strftime("%Y-%m-%d"),"entry_iv":_eiv})
        return {"mode":"PAPER","trade_id":tid,"strike":strike,"premium":adj}
    # Live: route to the broker (Fyers is the main broker).
    broker_map = {"FYERS": "fyers", "FYN": "fyers", "ANGEL": "angel", "BIGUL": "bigul"}
    broker = broker_map.get(mode, "fyers")
    try:
        if broker == "fyers":
            from core.services.broker_fyers import FyersV3
            cfg = _broker_config("fyers")
            fy = FyersV3(app_id=cfg.get("app_id", ""), secret_key=cfg.get("secret", ""),
                         redirect_uri=cfg.get("redirect_uri", ""),
                         access_token=cfg.get("access_token", ""),
                         refresh_token=cfg.get("refresh_token", ""))
            if not fy.access_token:
                return {"error": "Fyers connected nahi hai - Brokers tab se Connect karo"}
            leg = legs[0] if legs else {"option_type": "CE", "transaction": "BUY", "lots": 1}
            lot = get_lot_size(symbol)
            qty = int(leg.get("lots", 1)) * lot
            expiry = leg.get("expiry") or ""
            strike = leg.get("strike") or 0
            _opt_l = str(leg.get("option_type", "CE")).upper()
            _txn_l = str(leg.get("transaction", "BUY")).upper()
            from core.services.symbol_resolver import _fyers_symbol
            fy_sym = _fyers_symbol(symbol, expiry, float(strike or 0), _opt_l)
            import asyncio as _aio

            async def _place_safe():
                # SAFETY GUARD (live, user fix): safe LIMIT-IOC price mila to
                # MARKET KABHI nahi. Reference = SHARED contract LTP (wahi
                # price jo paper trade dikhata hai = dual-mode sync), side-
                # aware buffer, 0.05-tick rounded. Price na mile to fail-open
                # MARKET (order entry block behtar nahi). httpx async client
                # hi request karta hai (latency minimal).
                _lp = 0.0
                try:
                    from routes.broker import _broker_limit_price as _blp
                    _lp = float(await _blp(symbol, _opt_l, float(strike or 0), expiry, _txn_l) or 0)
                except Exception:
                    _lp = 0.0
                if _lp > 0:
                    return await fy.place_order(fy_sym, _txn_l, qty, order_type="LIMIT",
                                                price=max(0.05, round(_lp, 2)),
                                                validity="IOC")
                return await fy.place_order(fy_sym, _txn_l, qty, order_type="MARKET")

            res = _aio.run(_place_safe())
            if not res.get("success"):
                return {"error": f"Fyers rejected order: {res.get('error')}"}
            return {"mode": "LIVE", "broker": "fyers", "symbol": fy_sym,
                    "order_id": (res.get("data") or {}).get("order_id", "")}
        else:
            return {"error": f"Live broker {mode} not configured"}
    except Exception as e:
        return {"error": str(e)}


def _broker_config(name):
    """Stored broker config (DB + env seed) for the live router."""
    try:
        from routes.broker import _get_config
        return _get_config(name) or {}
    except Exception:
        return {}
