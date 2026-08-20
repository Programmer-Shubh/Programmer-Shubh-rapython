from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Optional
from core.models.bhavcopy_model import BhavcopyModel
from core.services.backtest_engine import BacktestEngine
from utils.helpers import format_currency

router = APIRouter()


class BacktestRequest(BaseModel):
    symbol: str = "BANKNIFTY"
    start_date: str = "2024-08-01"
    end_date: str = "2025-01-31"
    indicators: list = []
    entry_conditions: list = []
    exit_conditions: list = []
    legs: list = []
    advanced: dict = {}
    risk: dict = {}


@router.post("/run")
def run_backtest(req: BacktestRequest):
    bhav = BhavcopyModel()
    historical = bhav.get_by_symbol(req.symbol, req.start_date, req.end_date, False)
    if not historical:
        return {"error": f"No data for {req.symbol} between {req.start_date} and {req.end_date}"}
    if len(historical) > 120:
        historical = historical[-120:]
    engine = BacktestEngine()
    result = engine.run(
        historical, req.symbol, req.start_date, req.end_date,
        req.indicators, req.entry_conditions, req.exit_conditions,
        req.legs, req.advanced, req.risk,
    )
    if not result.get("success"):
        return {"error": result.get("error", "Backtest failed")}
    m = result["metrics"]
    return {
        "success": True,
        "engine": result.get("engine", "engine"),
        "metrics": {
            "initial_capital": m["initial_capital"],
            "final_capital": m["final_capital"],
            "total_return": m["total_return"],
            "total_return_pct": m["total_return_pct"],
            "win_rate": m["win_rate"],
            "max_drawdown": m["max_drawdown"],
            "profit_factor": m["profit_factor"],
            "sharpe_ratio": m["sharpe_ratio"],
            "total_trades": m["total_trades"],
            "winning_trades": m["winning_trades"],
            "losing_trades": m["losing_trades"],
            "avg_win": m["avg_win"],
            "avg_loss": m["avg_loss"],
            "total_brokerage": m["total_brokerage"],
        },
        "equity_curve": m.get("equity_curve", []),
        "monthly_pnl": m.get("monthly_pnl", {}),
        "trade_list": m.get("trade_list", []),
    }
