import time
from collections import Counter
from routes.strategy_builder import _run_backtest_core, BacktestRequest

req = BacktestRequest(symbol="CIPLA", symbols=["CIPLA", "SUNPHARMA"], start_date="2026-08-25", end_date="2026-09-22",
    indicators=[{"id": "rsi", "params": {"period": 14}},
                {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}],
    legs=[{"option_type": "PE", "transaction": "sell", "lots": 1, "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly"}],
    advanced={"trade_mode": "intraday", "timeframe": "1d"},
    risk={"max_trades_per_day": 5, "daily_stop_loss": 1500, "daily_take_profit": 3000})
t = time.time()
r = _run_backtest_core(req)
m = r.get("metrics", {})
print("TIME:", round(time.time() - t, 2), flush=True)
print("TRADES:", m.get("total_trades"), "WIN%:", m.get("win_rate"), "NET:", m.get("net_pnl"), flush=True)
print("MAXWIN:", m.get("max_win"), "MAXLOSS:", m.get("max_loss"), flush=True)
print("REASONS:", dict(Counter([x.get("exit_reason", "?") for x in r.get("trade_list", [])])), flush=True)
for x in r.get("trade_list", [])[:4]:
    print(x.get("symbol"), x.get("entry_date"), x.get("strike"), "E=", x.get("entry_price"), "X=", x.get("exit_price"), x.get("exit_reason"), "P=", x.get("pnl"), flush=True)
