from fastapi import APIRouter
from pydantic import BaseModel
from core.models.trade_model import TradeModel
from core.services.transaction_costs import TransactionCosts
from utils.helpers import get_lot_size, format_currency

router = APIRouter()


class PlaceTradeRequest(BaseModel):
    symbol: str
    option_type: str
    strike: float
    transaction_type: str
    quantity: int = 1
    entry_price: float
    stop_loss: float = 500.0
    take_profit: float = 1000.0
    date: str = ""


class CloseTradeRequest(BaseModel):
    trade_id: int
    exit_price: float
    date: str


class UpdateSLTPRequest(BaseModel):
    trade_id: int
    stop_loss: float
    target: float


class TradeModeRequest(BaseModel):
    trade_id: int
    trade_mode: str


@router.get("/open")
def get_open_trades():
    trade_model = TradeModel()
    positions = trade_model.get_open_positions_with_pnl()
    total_pnl = sum(p["unrealized_pnl"] for p in positions)
    return {
        "count": len(positions),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_formatted": format_currency(total_pnl),
        "positions": [
            {
                "id": t["trade"]["id"],
                "symbol": t["trade"]["symbol"],
                "option_type": t["trade"]["option_type"],
                "strike": t["trade"]["strike_price"],
                "transaction_type": t["trade"]["transaction_type"],
                "quantity": t["trade"]["quantity"],
                "lot_size": t["trade"].get("lot_size", 50),
                "entry_price": t["trade"]["entry_price"],
                "current_price": t["current_price"],
                "pnl": t["unrealized_pnl"],
                "sl": t["trade"]["stop_loss"],
                "tp": t["trade"]["target"],
                "trade_mode": t["trade"].get("trade_mode", "paper"),
            }
            for t in positions
        ],
    }


@router.post("/place")
def place_trade(req: PlaceTradeRequest):
    if req.entry_price <= 0 or req.strike <= 0:
        return {"error": "Enter valid strike and premium"}
    adj_premium = TransactionCosts.apply_fill_slippage(req.entry_price, req.transaction_type)
    lot_size = get_lot_size(req.symbol)
    costs = TransactionCosts.calculate(adj_premium * req.quantity * lot_size, req.transaction_type == "SELL")
    trade_model = TradeModel()
    tid = trade_model.insert_trade({
        "symbol": req.symbol,
        "option_type": req.option_type,
        "strike_price": req.strike,
        "transaction_type": req.transaction_type,
        "quantity": req.quantity,
        "lot_size": lot_size,
        "entry_price": adj_premium,
        "stop_loss": req.stop_loss,
        "target": req.take_profit,
        "total_cost": costs["total"],
        "entry_date": req.date,
    })
    return {"trade_id": tid, "entry_price": round(adj_premium, 2), "costs": costs}


@router.post("/close")
def close_trade(req: CloseTradeRequest):
    trade_model = TradeModel()
    trade_model.close_trade(req.trade_id, req.exit_price, req.date)
    return {"status": "closed", "trade_id": req.trade_id}


@router.post("/update-sltp")
def update_sltp(req: UpdateSLTPRequest):
    trade_model = TradeModel()
    trade_model.update_management(req.trade_id, req.stop_loss, req.target, "OFF")
    return {"status": "updated", "trade_id": req.trade_id}


@router.delete("/{trade_id}")
def delete_trade(trade_id: int):
    trade_model = TradeModel()
    trade_model.delete_trade(trade_id)
    return {"status": "deleted", "trade_id": trade_id}


@router.post("/mode")
def set_trade_mode(req: TradeModeRequest):
    trade_model = TradeModel()
    trade_model.set_trade_mode(req.trade_id, req.trade_mode)
    return {"status": "mode_updated", "trade_id": req.trade_id, "trade_mode": req.trade_mode}


@router.get("/history")
def get_history():
    trade_model = TradeModel()
    closed = trade_model.get_closed_trades()
    total_pnl = sum(t["pnl"] for t in closed)
    return {
        "count": len(closed),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_formatted": format_currency(total_pnl),
        "trades": [
            {
                "id": t["id"],
                "date": t["entry_date"],
                "symbol": t["symbol"],
                "option_type": t["option_type"],
                "strike": t["strike_price"],
                "entry": t["entry_price"],
                "exit": t.get("exit_price", 0),
                "pnl": t["pnl"],
                "pnl_formatted": format_currency(t["pnl"]),
            }
            for t in closed[:30]
        ],
    }
