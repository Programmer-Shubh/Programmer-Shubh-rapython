from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Optional, Dict
from core.models.trade_model import TradeModel
from core.services.transaction_costs import TransactionCosts
from core.services.historical_replay import HistoricalReplayEngine
from core.services.backtest_engine import BacktestEngine
from utils.helpers import get_lot_size, format_currency

router = APIRouter()


class PlaceTradeRequest(BaseModel):
    symbol: str
    option_type: str
    strike: float
    transaction_type: str
    quantity: int = 1
    entry_price: float
    stop_loss: float = 1500.0
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
    positions = trade_model.get_open_positions_with_pnl(auto_exit=False)
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
                "lot_size": get_lot_size(t["trade"]["symbol"]) if t["trade"].get("lot_size",50)==50 and get_lot_size(t["trade"]["symbol"])!=50 else t["trade"].get("lot_size", 50),
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
    # Premium sanity: option premium should not be ~spot price (EICHERMOT bug ₹3128) - must be <20% of spot
    try:
        from core.services.live_market_data import LiveMarketData as _LMD2
        _sp = _LMD2().get_spot_price(req.symbol) if ' _spot' not in dir() else _spot
        if _sp and _sp>0 and req.entry_price > _sp*0.25:
            # allow deep ITM CE where premium ~ spot-strike, but cap at spot*0.25 or intrinsic+500
            intrinsic = max(0, _sp - req.strike) if req.option_type=="CE" else max(0, req.strike - _sp)
            if req.entry_price > intrinsic + 600:
                return {"error": f"Premium ₹{req.entry_price} too high vs spot ₹{_sp:.0f} (likely spot sent as premium). Correct premium ~₹{intrinsic+80:.0f}. Use option LTP, not spot."}
    except Exception: pass
    # Deep ITM/OTM guard: ATM ±5 only
    try:
        from core.services.live_market_data import LiveMarketData as _LMD
        from utils.helpers import get_strike_step
        _lm = _LMD(); _spot = _lm.get_spot_price(req.symbol) or 0
        if _spot<=0:
            try: _s=_lm.get_live_spot(req.symbol); _spot=float(_s["spot"]) if _s and _s.get("spot") else 0
            except: pass
        _step=get_strike_step(req.symbol); _atm=round(_spot/_step)*_step if _spot>0 else 0
        if _atm>0 and abs(req.strike-_atm)>_step*4:
            return {"error": f"Deep {'ITM' if req.strike<_atm else 'OTM'} blocked: strike {req.strike} far from ATM {_atm} (ATM±4 only, spot {_spot:.0f})"}
        if _spot>0 and abs(req.strike-_spot)/_spot>0.04:
            return {"error": f"Deep ITM/OTM blocked: >4% from spot {_spot:.2f} (ATM±4 only)"}
    except Exception: pass
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
                "lot_size": get_lot_size(t["symbol"]) if t.get("lot_size",50)==50 and get_lot_size(t["symbol"])!=50 else t.get("lot_size", 50),
                "quantity": t.get("quantity", 1),
                "total_qty": t.get("quantity", 1) * (get_lot_size(t["symbol"]) if t.get("lot_size",50)==50 and get_lot_size(t["symbol"])!=50 else t.get("lot_size", 50)),
                "transaction_type": t.get("transaction_type",""),
                "calc": f"({t.get('exit_price',0):.2f}-{t['entry_price']:.2f})×{t.get('lot_size',50)}" if t.get("transaction_type")=="BUY" else f"({t['entry_price']:.2f}-{t.get('exit_price',0):.2f})×{t.get('lot_size',50)}",
                "analysis": f"{t.get('transaction_type','')} {t['option_type']} {'profit' if ((t.get('exit_price',0)-t['entry_price'])>0 if t.get('transaction_type')=='BUY' else (t['entry_price']-t.get('exit_price',0))>0) else 'loss'}: ₹{t['entry_price']:.2f}→₹{t.get('exit_price',0):.2f} × Lot {t.get('lot_size',50)} = Gross ₹{((t.get('exit_price',0)-t['entry_price']) if t.get('transaction_type')=='BUY' else (t['entry_price']-t.get('exit_price',0)))*t.get('quantity',1)*t.get('lot_size',50):,.2f} - Costs = Net {format_currency(t['pnl'])}",
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


class HistoricalReplayRequest(BaseModel):
    symbol: str = "NIFTY"
    start_date: str = "2026-08-01"
    end_date: str = "2026-08-20"
    indicators: List[Dict] = []
    entry_conditions: List[Dict] = []
    exit_conditions: List[Dict] = []
    legs: List[Dict] = []
    advanced: Dict = {}
    risk: Dict = {}


@router.post("/historical-replay")
def run_historical_replay(req: HistoricalReplayRequest):
    """Run paper trade on historical data with EXACT backtest logic.
    This ensures paper trade results match backtest results exactly."""
    try:
        engine = HistoricalReplayEngine()
        engine.configure(
            symbol=req.symbol,
            start_date=req.start_date,
            end_date=req.end_date,
            ind_list=req.indicators,
            entry_conditions=req.entry_conditions,
            exit_conditions=req.exit_conditions,
            legs=req.legs,
            advanced_options=req.advanced,
            risk_management=req.risk,
        )
        result = engine.run_replay()
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}


@router.post("/backtest-compare")
def backtest_compare(req: HistoricalReplayRequest):
    """Run both backtest and historical replay, return both for comparison."""
    try:
        # Run backtest
        bt_engine = BacktestEngine(is_live=False)
        historical = bt_engine._load_historical(req.symbol, req.start_date, req.end_date)
        bt_result = bt_engine.run(
            historical=historical,
            symbol=req.symbol,
            start_date=req.start_date,
            end_date=req.end_date,
            ind_list=req.indicators,
            entry_conditions=req.entry_conditions,
            exit_conditions=req.exit_conditions,
            legs=req.legs,
            advanced_options=req.advanced,
            risk_management=req.risk,
            is_live=False,
        )
        
        # Run historical replay (exact same logic)
        replay_engine = HistoricalReplayEngine()
        replay_engine.configure(
            symbol=req.symbol,
            start_date=req.start_date,
            end_date=req.end_date,
            ind_list=req.indicators,
            entry_conditions=req.entry_conditions,
            exit_conditions=req.exit_conditions,
            legs=req.legs,
            advanced_options=req.advanced,
            risk_management=req.risk,
        )
        replay_result = replay_engine.run_replay()
        
        # Compare
        bt_metrics = bt_result.get("metrics", {})
        replay_metrics = replay_result.get("metrics", {})
        
        comparison = {
            "backtest": bt_metrics,
            "historical_replay": replay_metrics,
            "match": {
                "total_return_pct": round(bt_metrics.get("total_return_pct", 0) - replay_metrics.get("total_return_pct", 0), 4),
                "total_trades": bt_metrics.get("total_trades", 0) - replay_metrics.get("total_trades", 0),
                "win_rate": round(bt_metrics.get("win_rate", 0) - replay_metrics.get("win_rate", 0), 4),
                "max_drawdown": round(bt_metrics.get("max_drawdown", 0) - replay_metrics.get("max_drawdown", 0), 4),
                "sharpe_ratio": round(bt_metrics.get("sharpe_ratio", 0) - replay_metrics.get("sharpe_ratio", 0), 4),
            },
            "exact_match": (
                abs(bt_metrics.get("total_return_pct", 0) - replay_metrics.get("total_return_pct", 0)) < 0.01 and
                bt_metrics.get("total_trades", 0) == replay_metrics.get("total_trades", 0) and
                abs(bt_metrics.get("win_rate", 0) - replay_metrics.get("win_rate", 0)) < 0.01
            )
        }
        
        return comparison
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}
