# -*- coding: utf-8 -*-
"""Full-window backtest of the 5 candidate configs for the production save.
Window: 2026-09-15..2026-11-13 (max 5m history Yahoo returns).
Prints metrics JSON per config; writes scripts/_save_runs.json."""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from routes.strategy_builder import BacktestRequest, _run_backtest_core

ST = {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}
RSI = {"id": "rsi", "params": {"period": 14}}
IDX_SLTP = {"NIFTY": (15.0, 20.0), "FINNIFTY": (18.0, 22.0),
            "BANKNIFTY": (25.0, 35.0), "MIDCPNIFTY": (15.0, 20.0)}

# (symbol, otm, ind_set)
CASES = [
    ("NIFTY", 4, "A"),
    ("NIFTY", 4, "C"),
    ("MIDCPNIFTY", 3, "A"),
    ("BANKNIFTY", 4, "A"),
    ("FINNIFTY", 4, "A"),
]

def run(sym, otm, idl, start, end):
    sl, tp = IDX_SLTP[sym]
    ind = [ST, RSI] if idl == "A" else [ST]
    req = BacktestRequest(
        symbol=sym, symbols=[sym], start_date=start, end_date=end,
        indicators=[dict(x, params=dict(x["params"])) for x in ind],
        legs=[{"option_type": "PE", "transaction": "sell", "lots": 1,
               "strike_selection": "otm", "otm_distance": otm,
               "expiry": "weekly"}],
        advanced={"trade_mode": "intraday", "timeframe": "5m",
                  "entry_time": "09:35", "exit_time": "15:15",
                  "strict_trend": False,
                  "reentry_on_sl": "No Re-entry",
                  "reentry_on_tp": "RE ASAP",
                  "trailing": "5", "lock_profit_at": 10.0,
                  "lock_profit": 5.0, "lock_profit_unit": "pt"},
        risk={"max_trades_per_day": 5,
              "daily_stop_loss": sl, "sl_unit": "pt",
              "daily_take_profit": tp, "tp_unit": "pt"},
    )
    r = _run_backtest_core(req) or {}
    m = r.get("metrics") or {}
    return r

def slim(r):
    m = (r or {}).get("metrics") or {}
    keep = {k: m.get(k) for k in (
        "total_trades", "win_rate", "loss_rate", "net_pnl", "total_return_pct",
        "profit_factor", "max_drawdown", "expectancy", "avg_win", "avg_loss",
        "avg_profit_per_trade", "winning_trades", "losing_trades",
        "total_brokerage", "reward_risk", "expectancy", "total_return")}
    return keep

ap = argparse.ArgumentParser()
ap.add_argument("--start", default="2026-09-15")
ap.add_argument("--end", default="2026-11-13")
args = ap.parse_args()

out = []
for sym, otm, idl in CASES:
    r = run(sym, otm, idl, args.start, args.end)
    m = slim(r)
    print(f"{sym:11} OTM{otm} {idl}  trades={m['total_trades']} wr={round(m['win_rate'] or 0,1)}% "
          f"net={round(m['net_pnl'] or 0)} pf={round(m['profit_factor'] or 0,2)} "
          f"dd={round(m['max_drawdown'] or 0,2)} exp={round(m['expectancy'] or 0,1)}", flush=True)
    out.append({"symbol": sym, "otm": otm, "ind": idl, "metrics": m, "full": r})

with open("scripts/_save_runs.json", "w") as f:
    json.dump(out, f, indent=1, default=str)
print("saved scripts/_save_runs.json")
