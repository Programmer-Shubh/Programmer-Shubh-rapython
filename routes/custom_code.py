"""API for the Custom Python Strategy Lab (web code editor + sandboxed backtest)."""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from core.services.custom_code_runner import (
    run_custom_backtest, TEMPLATE_CODE, ALLOWED_MODULES,
    MAX_CODE_CHARS, EXEC_TIMEOUT_SEC,
)
from core.services.historical_fetcher import fetch_historical

router = APIRouter()


class CustomCodeRequest(BaseModel):
    code: str = ""
    symbol: str = "NIFTY"
    start_date: str = "2026-08-01"
    end_date: str = "2026-08-20"
    initial_capital: float = 100000.0


@router.get("/template")
def get_template():
    return {"template": TEMPLATE_CODE}


@router.get("/limits")
def get_limits():
    return {
        "allowed_libraries": sorted(ALLOWED_MODULES),
        "max_code_chars": MAX_CODE_CHARS,
        "timeout_seconds": EXEC_TIMEOUT_SEC,
        "data_columns": ["date", "open", "high", "low", "close", "volume"],
        "contract": "Define run(data) returning {'trades': [{'entry_date','exit_date','entry_price','exit_price','qty'}]}, or a backtrader Strategy named CustomStrategy.",
    }


@router.post("/run")
def run_custom(req: CustomCodeRequest):
    try:
        symbol = (req.symbol or "NIFTY").upper()
        capital = float(req.initial_capital or 100000.0)
        if capital <= 0:
            capital = 100000.0
        historical = fetch_historical(symbol, req.start_date, req.end_date, allow_synthetic=True)
        if not historical or len(historical) < 5:
            # Same synthetic fallback as the main backtest route (fetch_historical
            # forces allow_synthetic=False internally).
            try:
                from routes.strategy_builder import _generate_synthetic_fallback
                historical = _generate_synthetic_fallback(symbol, req.start_date, req.end_date)
            except Exception:
                pass
        if not historical or len(historical) < 5:
            return {"error": f"No historical data for {symbol} in {req.start_date} to {req.end_date}."}
        if len(historical) > 500:
            historical = historical[-500:]
        result = run_custom_backtest(req.code or "", historical, symbol, capital)
        if not result.get("success"):
            return {"error": result.get("error", "Backtest failed")}
        return {
            "success": True,
            "symbol": symbol,
            "metrics": result["metrics"],
            "trades": result["trades"],
            "trade_list": result["trades"],
            "equity_curve": result.get("equity_curve", []),
            "logs": result.get("logs", ""),
            "bars": result.get("bars", 0),
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": f"Internal error: {str(e)}"}
