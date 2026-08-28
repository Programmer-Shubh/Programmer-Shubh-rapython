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
                "entry_date": t["trade"].get("entry_date", ""),
            }
            for t in positions
        ],
    }


@router.post("/place")
def place_trade(req: PlaceTradeRequest):
    # Mandatory Risk Management: SL/Target required (especially for option selling)
    if req.stop_loss is None or req.take_profit is None:
        return {"error": "Stop-Loss and Target are mandatory - cannot be blank"}
    if req.stop_loss <= 0 or req.take_profit <= 0:
        return {"error": "Stop-Loss and Target must be > 0 (mandatory for risk management, especially for SELL)"}
    # Data Validation: strike, quantity, symbol checks
    trade_model = TradeModel()
    v_err = trade_model.validate_trade_data({"symbol": req.symbol, "option_type": req.option_type, "strike_price": req.strike, "quantity": req.quantity, "entry_price": req.entry_price})
    if v_err:
        return {"error": v_err}
    # Prevent faulty strike '01' etc already handled, also reject strike 0
    if req.entry_price <= 0 or req.strike <= 0:
        return {"error": "Enter valid strike (>0) and premium (>0) - Strike 0 invalid, use ATM"}
    # Deduplication: if identical open position exists, block duplicate
    existing = trade_model.db.fetch_one("SELECT id FROM paper_trades WHERE symbol=? AND strike_price=? AND option_type=? AND transaction_type=? AND status='open' LIMIT 1", [req.symbol, req.strike, req.option_type, req.transaction_type])
    if existing:
        return {"error": f"Duplicate open position exists for {req.symbol} {req.strike} {req.option_type} {req.transaction_type} (ID {existing['id']}) - close or delete existing first"}
    adj_premium = TransactionCosts.apply_fill_slippage(req.entry_price, req.transaction_type, is_live=True)
    lot_size = get_lot_size(req.symbol)
    costs = TransactionCosts.calculate(adj_premium * req.quantity * lot_size, req.transaction_type == "SELL", is_live=True)
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


@router.post("/deduplicate")
def deduplicate_trades():
    trade_model = TradeModel()
    dup_count = trade_model.deduplicate_open_trades()
    # Clean faulty strikes: 0, 01, <100 etc and missing values
    faulty = trade_model.db.fetch_all("SELECT id, symbol, strike_price FROM paper_trades WHERE status='open' AND (strike_price <= 100 OR strike_price IS NULL OR strike_price = '' OR symbol IS NULL OR symbol='' OR option_type IS NULL OR option_type='')")
    faulty_ids = [r["id"] for r in faulty]
    for fid in faulty_ids:
        trade_model.db.execute("DELETE FROM paper_trades WHERE id=?", [fid])
    # Clean specific user reported duplicates: ensure clean accurate format
    # Return summary
    return {"status": "cleaned", "duplicates_removed": dup_count, "faulty_removed": len(faulty_ids), "faulty_ids": faulty_ids}


@router.get("/clean-preview")
def clean_preview():
    trade_model = TradeModel()
    open_trades = trade_model.get_open_trades()
    # Group by key to find duplicates
    groups = {}
    for t in open_trades:
        k = f"{t['symbol']}_{t['strike_price']}_{t['option_type']}_{t['transaction_type']}"
        groups.setdefault(k, []).append(t)
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    faulty = [t for t in open_trades if not t["symbol"] or not t["option_type"] or t["strike_price"] is None or float(t["strike_price"] or 0) <= 100]
    return {"duplicates": dups, "faulty": faulty, "total_open": len(open_trades)}


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
                "entry_date": t["entry_date"],
                "exit_date": t.get("exit_date", ""),
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


@router.post("/clean-history")
def clean_history():
    trade_model = TradeModel()
    result = trade_model.clean_and_fix_history()
    return result


@router.get("/risk-analysis")
def risk_analysis():
    trade_model = TradeModel()
    return trade_model.get_risk_analysis()


@router.get("/nifty-fix")
def nifty_fix():
    trade_model = TradeModel()
    # Trigger NIFTY incomplete fix via clean
    result = trade_model.clean_and_fix_history()
    return {"nifty": "fixed" if any("NIFTY" in i for i in result.get("issues", [])) else "ok", "details": result}
