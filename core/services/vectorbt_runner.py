import logging

logger = logging.getLogger(__name__)

try:
    import vectorbt as vbt
    HAS_VECTORBT = True
except ImportError:
    vbt = None
    HAS_VECTORBT = False

import numpy as np
from core.models.database import Database


class VectorbtRunner:
    def __init__(self):
        self.initial_capital = 1000000.0

    def run_vectorized(self, historical, symbol, start_date, end_date, ind_list,
                       entry_conditions, exit_conditions, legs, advanced_options, risk_management):
        if not HAS_VECTORBT:
            return {"success": False, "error": "vectorbt is not installed. Run: pip install vectorbt"}

        closes = np.array([h["close_price"] for h in historical], dtype=float)
        if len(closes) < 30:
            return {"success": False, "error": "Insufficient data for vectorbt backtest"}

        # Build a simple entry/exit signal series from close price movers
        entries = np.array([False] * len(closes), dtype=bool)
        exits = np.array([False] * len(closes), dtype=bool)

        buy_sig = closes[1:] > closes[:-1]
        sell_sig = closes[1:] < closes[:-1]
        entries[1:] = buy_sig
        exits[1:] = sell_sig

        portfolio = vbt.Portfolio.from_signals(
            closes,
            entries,
            exits,
            init_cash=self.initial_capital,
            freq="D",
        )

        stats = portfolio.stats()
        equity = portfolio.value().to_numpy()

        total_return = float(portfolio.total_return())
        total_trades = int(portfolio.trades.count())
        win_rate = float(portfolio.win_rate() * 100) if total_trades > 0 else 0.0
        sharpe = float(portfolio.sharpe_ratio())
        max_dd = float(portfolio.max_drawdown() * 100)

        return {
            "success": True,
            "engine": "vectorbt",
            "metrics": {
                "initial_capital": self.initial_capital,
                "final_capital": round(float(portfolio.final_value()), 2),
                "total_return": round(total_return * self.initial_capital, 2),
                "total_return_pct": round(total_return * 100, 4),
                "win_rate": round(win_rate, 2),
                "max_drawdown": round(max_dd, 4),
                "sharpe_ratio": round(sharpe, 4),
                "profit_factor": round(float(portfolio.profit_factor()), 4) if total_trades > 0 else 0,
                "total_trades": total_trades,
            },
            "equity_curve": [round(float(v), 2) for v in equity],
            "trade_list": [],
            "monthly_pnl": {},
        }