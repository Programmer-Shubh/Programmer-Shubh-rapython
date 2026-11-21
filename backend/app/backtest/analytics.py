from __future__ import annotations

from datetime import date

import polars as pl


def compute_trade_metrics(trades: list[dict], lot_size: int) -> dict:
    """MAE/MFE, expectancy, profit factor, win rate, gross P&L."""
    pnls = [t.get("pnl", 0.0) for t in trades]
    if not pnls:
        return {
            "trades": 0,
            "net_pnl": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "avg_mae": 0.0,
            "avg_mfe": 0.0,
        }

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    win_count = sum(1 for p in pnls if p > 0)

    maes = [t.get("mae", 0.0) for t in trades]
    mfes = [t.get("mfe", 0.0) for t in trades]

    return {
        "trades": len(pnls),
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(win_count / len(pnls) * 100, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "expectancy": round(sum(pnls) / len(pnls), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_mae": round(sum(maes) / len(maes), 2) if maes else 0.0,
        "avg_mfe": round(sum(mfes) / len(mfes), 2) if mfes else 0.0,
    }


def compute_portfolio_metrics(trades: list[dict], lot_size: int, periods_per_year: int = 252) -> dict:
    """Sharpe, Calmar, Max Drawdown from the equity curve (daily P&L grouped by exit date)."""
    if not trades:
        return {"sharpe": 0.0, "calmar": 0.0, "max_drawdown": 0.0, "max_drawdown_pct": 0.0}

    df = pl.DataFrame(trades).with_columns(
        pl.col("exit_date").cast(pl.Date).alias("date"),
        pl.col("pnl").cast(pl.Float64).alias("pnl"),
    )
    daily = df.group_by("date").agg(pl.col("pnl").sum()).sort("date")
    equity = daily.with_columns(pl.col("pnl").cum_sum().alias("equity")).to_dicts()

    running_max = None
    max_dd = 0.0
    max_dd_pct = 0.0
    peak_equity = 0.0
    for row in equity:
        eq = row["equity"]
        if eq > peak_equity:
            peak_equity = eq
        dd = eq - peak_equity
        if dd < max_dd:
            max_dd = dd
            max_dd_pct = (dd / peak_equity * 100) if peak_equity else 0.0

    daily_pnls = [row["pnl"] for row in equity]
    n = len(daily_pnls)
    if n > 1:
        mean_ret = sum(daily_pnls) / n
        std_ret = (sum((p - mean_ret) ** 2 for p in daily_pnls) / (n - 1)) ** 0.5
        sharpe = (mean_ret / std_ret * (periods_per_year ** 0.5)) if std_ret > 0 else 0.0
    else:
        sharpe = 0.0

    total_return = sum(daily_pnls)
    years = max(n / periods_per_year, 1 / periods_per_year)
    annual_return = total_return / years
    calmar = annual_return / abs(max_dd) if max_dd != 0 else 0.0

    return {
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
    }


def oos_metrics(is_trades: list[dict], oos_trades: list[dict], lot_size: int) -> dict:
    """Compare In-Sample vs Out-of-Sample for walk-forward overfit detection."""
    return {
        "in_sample": compute_trade_metrics(is_trades, lot_size),
        "out_of_sample": compute_trade_metrics(oos_trades, lot_size),
        "is_oos_net_pnl_diff": round(
            compute_trade_metrics(oos_trades, lot_size)["net_pnl"]
            - compute_trade_metrics(is_trades, lot_size)["net_pnl"],
            2,
        ),
    }
