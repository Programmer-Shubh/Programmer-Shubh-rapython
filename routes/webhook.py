from fastapi import APIRouter, Request, Header
from pydantic import BaseModel
from typing import Optional
import json
from core.models.trade_model import TradeModel
from core.services.transaction_costs import TransactionCosts
from core.services.live_market_data import LiveMarketData
from utils.helpers import get_lot_size
import time

router = APIRouter()

class WebhookSignal(BaseModel):
    symbol: str = "NIFTY"
    action: str = "BUY"  # BUY/SELL
    type: str = "CE"  # CE/PE
    strike: float = 0
    expiry: str = ""
    quantity: int = 1
    price: float = 0
    # TradingView placeholders
    strategy: Optional[str] = None
    exchange: Optional[str] = None
    timeframe: Optional[str] = None


def _parse_tv_message(raw: str) -> dict:
    """Parse TradingView alert.message which may be JSON or 'BUY NIFTY 24500 CE'"""
    if not raw:
        return {}
    raw = raw.strip()
    # Try JSON
    try:
        return json.loads(raw)
    except:
        pass
    # Try key=value or simple
    parts = raw.replace(",", " ").split()
    out = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.lower()] = v
    # Simple "BUY NIFTY 24500 CE"
    if len(parts) >= 2:
        if parts[0].upper() in ("BUY", "SELL"):
            out["action"] = parts[0].upper()
        # find symbol
        for pt in parts:
            if pt.upper() in ("NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","RELIANCE","TCS","INFY","SBIN","ITC","HDFCBANK","ICICIBANK","BAJFINANCE"):
                out["symbol"] = pt.upper()
        # find strike
        for pt in parts:
            try:
                f = float(pt)
                if 1000 < f < 100000:
                    out["strike"] = f
            except:
                pass
        if "CE" in raw.upper():
            out["type"] = "CE"
        elif "PE" in raw.upper():
            out["type"] = "PE"
    return out


def _place_from_signal(data: dict, source: str = "webhook") -> dict:
    # Unified via execution_engine: supports mode PAPER/LIVE (broker)
    mode = (data.get("mode") or data.get("broker_mode") or "PAPER").upper()
    symbol = (data.get("symbol") or data.get("ticker") or "NIFTY").upper()
    action = (data.get("action") or data.get("side") or "BUY").upper()
    if action not in ("BUY","SELL"):
        action = "BUY"
    opt_type = (data.get("type") or data.get("option_type") or "CE").upper()
    if opt_type not in ("CE","PE"):
        opt_type = "CE"
    strike = float(data.get("strike") or data.get("strike_price") or 0)
    expiry = data.get("expiry") or data.get("expiry_date") or ""
    qty = int(data.get("quantity") or data.get("qty") or 1)
    # If webhook comes with full legs (Strategy Builder), route via execution_engine directly
    if data.get("legs"):
        try:
            from core.services.execution_engine import execute
            legs = data.get("legs")
            sl = float(data.get("stop_loss") or data.get("sl") or 1500)
            tp = float(data.get("take_profit") or data.get("target") or data.get("tp") or 1000)
            return execute(legs, symbol, mode=mode, sl=sl, tp=tp)
        except Exception as e:
            return {"error": str(e)}
    # If strike missing, pick ATM from live spot
    live = LiveMarketData()
    if strike <= 0:
        spot = live.get_spot_price(symbol) or 0
        if spot <= 0:
            ls = live.get_live_spot(symbol)
            spot = float(ls["spot"]) if ls and ls.get("spot") else 0
        if spot > 0:
            from utils.helpers import get_strike_step
            step = get_strike_step(symbol)
            strike = round(spot / step) * step
    # Resolve premium: DB -> live -> 1% of spot
    from core.models.bhavcopy_model import BhavcopyModel
    bhav = BhavcopyModel()
    premium = 0
    if strike > 0:
        premium = live.get_option_ltp(symbol, strike, opt_type) or 0
    if premium <= 0 and strike > 0:
        # Try DB latest
        row = bhav.db.fetch_one("SELECT close_price FROM bhavcopy_data WHERE symbol=? AND strike_price=? AND option_type=? ORDER BY trade_date DESC LIMIT 1", [symbol, strike, opt_type])
        if row and row["close_price"]:
            premium = float(row["close_price"])
    if premium <= 0:
        # Real spot * 1% (real, not BS)
        spot = live.get_spot_price(symbol) or 0
        if spot <= 0:
            ls = live.get_live_spot(symbol)
            spot = float(ls["spot"]) if ls and ls.get("spot") else 0
        if spot > 0:
            premium = round(spot * 0.01, 2)
    if premium <= 0:
        return {"error": f"No premium for {symbol} {strike}{opt_type}"}
    adj = TransactionCosts.apply_fill_slippage(premium, action, is_live=False)
    lot = get_lot_size(symbol)
    costs = TransactionCosts.calculate(adj * qty * lot, action=="SELL", is_live=False)
    tm = TradeModel()
    # Use today as expiry if missing
    if not expiry:
        import datetime
        expiry = (bhav.get_expiries(symbol, bhav.get_dates(symbol)[0]) or [""])[0] if bhav.get_dates(symbol) else ""
        if not expiry:
            expiry = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    tid = tm.insert_trade({
        "symbol": symbol, "option_type": opt_type, "strike_price": strike,
        "expiry_date": expiry, "transaction_type": action, "quantity": qty,
        "lot_size": lot, "entry_price": adj, "stop_loss": 1500, "target": 1000,
        "total_cost": costs["total"], "entry_date": __import__("datetime").datetime.now().strftime("%Y-%m-%d"),
    })
    return {"trade_id": tid, "symbol": symbol, "action": action, "strike": strike, "type": opt_type, "entry_price": round(adj,2), "source": source}


@router.post("/free")
async def webhook_free(request: Request):
    """Free TradingView webhook (Stoxo/NextLevel style) - works with Chrome Extension in Free plan."""
    try:
        body = await request.body()
        text = body.decode("utf-8", errors="ignore") if body else ""
        # Try JSON first
        data = {}
        if text:
            try:
                data = json.loads(text)
            except:
                data = _parse_tv_message(text)
        # Also merge query params
        for k, v in request.query_params.items():
            data[k] = v
        # TradingView sends { "strategy": "BUY", "ticker": "NIFTY" } style
        result = _place_from_signal(data, source="tradingview_free")
        if "error" in result:
            return {"success": False, "error": result["error"], "hint": "Use extension to unlock webhook in Free, or check symbol/strike"}
        return {"success": True, "message": "Order placed via free webhook", **result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/email")
async def webhook_email(request: Request, x_email_token: Optional[str] = Header(None)):
    """Email hack: Free alert Email -> Pipedream/IFTTT -> POST here. Body is raw email text."""
    try:
        body = await request.body()
        text = body.decode("utf-8", errors="ignore") if body else ""
        # Pipedream sends JSON { "subject": "...", "text": "BUY NIFTY..." }
        data = {}
        if text:
            try:
                j = json.loads(text)
                # Extract from common email webhook formats
                raw = j.get("text") or j.get("body") or j.get("subject") or j.get("message") or text
                if isinstance(j, dict) and not raw:
                    data = j
                else:
                    data = _parse_tv_message(str(raw))
                    # Merge original json fields
                    for k,v in j.items():
                        if k not in data:
                            data[k]=v
            except:
                data = _parse_tv_message(text)
        result = _place_from_signal(data, source="email_hack")
        if "error" in result:
            return {"success": False, "error": result["error"]}
        return {"success": True, "message": "Order placed via email hack", **result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/test")
def webhook_test():
    return {"webhooks": {
        "free": "POST /api/webhook/free  (Chrome Extension, Stoxo style, FREE)",
        "email": "POST /api/webhook/email (Pipedream/IFTTT free 2 webhooks)",
        "tradingview": "POST /api/webhook/tradingview (paid webhook, same as free)"
    }}


@router.post("/tradingview")
async def webhook_tradingview(request: Request):
    return await webhook_free(request)
