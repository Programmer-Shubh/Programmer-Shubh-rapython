# -*- coding: utf-8 -*-
"""Exit-reason deep dive: kaun sa exit paisa khaa raha hai."""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
from routes.strategy_builder import _run_backtest_core, BacktestRequest

cfg = {
    "symbol": "BANKNIFTY", "symbols": ["BANKNIFTY"],
    "start_date": "2026-09-08", "end_date": "2026-11-14",
    "indicators": [{"id": "rsi", "params": {"period": 14}},
                   {"id": "ema", "params": {"period": 9}},
                   {"id": "ema", "params": {"period": 21}},
                   {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}],
    "entry_conditions": [], "exit_conditions": [],
    "legs": [{"option_type": "CE", "transaction": "buy", "lots": 1,
              "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly"}],
    "advanced": {"trade_mode": "intraday", "timeframe": "5m",
                 "entry_time": "09:35", "exit_time": "15:15",
                 "slippage_pct": 0.5, "strict_trend": True},
    "risk": {"daily_stop_loss": 25, "daily_take_profit": 35,
             "sl_unit": "pt", "tp_unit": "pt", "max_trades_per_day": -1},
}
res = _run_backtest_core(BacktestRequest(**cfg)) or {}
m = res.get("metrics") or {}
tl = res.get("trade_list") or []
print("trades", m.get("total_trades"), "win%", m.get("win_rate"), "pnl", m.get("net_pnl"))
print("avg_win", m.get("avg_win"), "avg_loss", m.get("avg_loss"),
      "expectancy", m.get("expectancy"), "PF", m.get("profit_factor"))
buckets = {}
for t in tl:
    r = str(t.get("exit_reason") or "?")
    b = buckets.setdefault(r, {"n": 0, "pnl": 0.0, "w": 0, "pts": []})
    b["n"] += 1
    b["pnl"] += float(t.get("pnl") or 0)
    if float(t.get("pnl") or 0) > 0:
        b["w"] += 1
    try:
        b["pts"].append(round(float(t.get("exit_price") or 0) - float(t.get("entry_price") or 0), 2))
    except Exception:
        pass
for r, b in sorted(buckets.items(), key=lambda kv: kv[1]["pnl"]):
    avg_pts = round(sum(b["pts"]) / len(b["pts"]), 2) if b["pts"] else 0
    print(f"  {r:12s} n={b['n']:4d} wins={b['w']:4d} pnl={round(b['pnl'],1):12} avg_premium_move={avg_pts}")
# trades per day
from collections import Counter
c = Counter(str(t.get("entry_date")) for t in tl)
print("trades/day:", sorted(c.values(), reverse=True)[:12], "| days:", len(c))
print("sample stops:", [ (t.get('entry_date'), t.get('entry_time'), t.get('entry_price'), t.get('exit_price'), t.get('exit_reason')) for t in tl[:6] ])
