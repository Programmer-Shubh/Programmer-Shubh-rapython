import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.cost_model import CostModel

BARS_PER_YEAR = {
    "1d": 252,
    "4h": 252 * 6,
    "1h": 252 * 24,
    "15m": 252 * 24 * 4,
    "5m": 252 * 24 * 12,
}


def calculate_metrics(trades: List[Dict],
                      bar_frequency: str = "1h",
                      cost_model: Optional[CostModel] = None) -> Dict:
    if not trades:
        return {
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "win_rate": 0, "profit_factor": 0, "total_pnl": 0,
            "avg_rr": 0, "max_drawdown": 0, "avg_hold_bars": 0,
            "false_breakout_pct": 0, "sharpe_ratio": 0,
            "status": "no_trades"
        }

    if cost_model is not None:
        trades = cost_model.apply(trades)

    df = pd.DataFrame(trades)
    total = len(df)
    winners = df[df["pnl"] > 0]
    losers = df[df["pnl"] < 0]
    win_count = len(winners)
    loss_count = len(losers)

    win_rate = win_count / total * 100 if total > 0 else 0
    total_pnl = df["pnl"].sum()

    gross_profit = winners["pnl"].sum() if not winners.empty else 0
    gross_loss = abs(losers["pnl"].sum()) if not losers.empty else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)

    avg_win = winners["pnl"].mean() if not winners.empty else 0
    avg_loss = abs(losers["pnl"].mean()) if not losers.empty else 0
    avg_rr = avg_win / avg_loss if avg_loss > 0 else 0

    cum_pnl = df["pnl"].cumsum().values
    rolling_peak = np.maximum.accumulate(cum_pnl)
    dd = cum_pnl - rolling_peak
    max_dd = dd.min()

    avg_hold = df["bars_held"].mean() if "bars_held" in df.columns else 0
    false_breakouts = len(df[df["exit_reason"] == "STOP_LOSS"]) if "exit_reason" in df.columns else 0
    false_pct = false_breakouts / total * 100 if total > 0 else 0

    returns = df["pnl"].values
    sharpe = _annualized_sharpe(returns, bar_frequency, avg_hold)
    sortino = _sortino_ratio(returns, bar_frequency, avg_hold)

    avg_cost = round(df["cost_pct"].mean(), 4) if "cost_pct" in df.columns else 0

    return {
        "total_trades": total,
        "winning_trades": win_count,
        "losing_trades": loss_count,
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_rr": round(avg_rr, 2),
        "max_drawdown": round(max_dd, 2),
        "avg_hold_bars": round(avg_hold, 1),
        "false_breakout_pct": round(false_pct, 2),
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_cost_pct": avg_cost,
    }


def _annualized_sharpe(returns: np.ndarray, bar_frequency: str,
                       avg_hold_bars: float) -> float:
    if len(returns) < 2 or returns.std() < 1e-10:
        return 0.0
    mean_r = np.mean(returns)
    std_r = np.std(returns, ddof=1)
    bars_per_year = BARS_PER_YEAR.get(bar_frequency, 252)
    scale = math.sqrt(bars_per_year / max(avg_hold_bars, 1))
    return mean_r / std_r * scale


def _sortino_ratio(returns: np.ndarray, bar_frequency: str,
                   avg_hold_bars: float) -> float:
    if len(returns) < 2:
        return 0.0
    r = np.array(returns)
    mean_r = np.mean(r)
    downside = r[r < 0]
    if len(downside) < 1 or downside.std() < 1e-10:
        return 0.0
    downside_std = np.std(downside, ddof=1)
    bars_per_year = BARS_PER_YEAR.get(bar_frequency, 252)
    scale = math.sqrt(bars_per_year / max(avg_hold_bars, 1))
    return mean_r / downside_std * scale
