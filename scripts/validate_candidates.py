# -*- coding: utf-8 -*-
"""Out-of-sample validation of the train-positive candidates.
Disjoint calendar slice: 2026-10-24..2026-11-13 (never used for selection)."""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from routes.strategy_builder import BacktestRequest, _run_backtest_core

ST = {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}
RSI = {"id": "rsi", "params": {"period": 14}}
IDX_SLTP = {"NIFTY": (15.0, 20.0), "FINNIFTY": (18.0, 22.0),
            "BANKNIFTY": (25.0, 35.0), "MIDCPNIFTY": (15.0, 20.0)}

# (symbol, otm, entry, ind_set, label)
CASES = [
    ("MIDCPNIFTY", 3, "09:35", [ST, RSI], "A"),
    ("MIDCPNIFTY", 4, "09:35", [ST, RSI], "A"),
    ("MIDCPNIFTY", 3, "10:15", [ST, RSI], "A"),
    ("NIFTY", 3, "09:35", [ST], "C"),
    ("NIFTY", 4, "09:35", [ST, RSI], "A"),
    ("NIFTY", 4, "09:35", [ST], "C"),
    ("BANKNIFTY", 4, "09:35", [ST, RSI], "A"),
    ("FINNIFTY", 4, "09:35", [ST, RSI], "A"),
]

def run(sym, otm, entry, ind, start, end):
    sl, tp = IDX_SLTP[sym]
    req = BacktestRequest(
        symbol=sym, symbols=[sym], start_date=start, end_date=end,
        indicators=[dict(x, params=dict(x["params"])) for x in ind],
        legs=[{"option_type": "PE", "transaction": "sell", "lots": 1,
               "strike_selection": "otm", "otm_distance": otm,
               "expiry": "weekly"}],
        advanced={"trade_mode": "intraday", "timeframe": "5m",
                  "entry_time": entry, "exit_time": "15:15",
                  "strict_trend": False,
                  "reentry_on_sl": "No Re-entry",
                  "reentry_on_tp": "RE ASAP",
                  "trailing": "5", "lock_profit_at": 10.0,
                  "lock_profit": 5.0, "lock_profit_unit": "pt"},
        risk={"max_trades_per_day": 5,
              "daily_stop_loss": sl, "sl_unit": "pt",
              "daily_take_profit": tp, "tp_unit": "pt"},
    )
    try:
        r = _run_backtest_core(req) or {}
    except Exception as e:
        return {"error": str(e)}
    m = r.get("metrics") or {}
    return {
        "trades": int(m.get("total_trades") or 0),
        "win_rate": float(m.get("win_rate") or 0),
        "net": float(m.get("net_pnl") or 0),
        "pf": float(m.get("profit_factor") or 0),
        "dd": float(m.get("max_drawdown") or 0),
        "exp": float(m.get("expectancy") or 0),
    }

print(f"{'SYM':11}{'OTM':4}{'ENT':6}{'IND':4}{'TRD':>4}{'WR%':>7}{'NET':>10}{'PF':>7}{'DD':>8}")
print("-" * 68)
results = []
for sym, otm, entry, ind, idl in CASES:
    m = run(sym, otm, entry, ind, "2026-10-24", "2026-11-13")
    if "error" in m:
        print(f"{sym:11}{otm:<4}{entry:6}{idl:4}  ERROR: {m['error'][:60]}", flush=True)
        continue
    ok = "  <<< VALID" if (m["trades"] >= 5 and m["net"] > 0 and m["pf"] >= 1.1) else ""
    print(f"{sym:11}{otm:<4}{entry:6}{idl:4}{m['trades']:>4}"
          f"{m['win_rate']:>6.1f}%{m['net']:>10,.0f}{m['pf']:>7.2f}{m['dd']:>8,.0f}{ok}", flush=True)
    results.append({"symbol": sym, "otm": otm, "entry": entry, "ind": idl, **m})
print("-" * 68)
with open("scripts/_validation_results.json", "w") as f:
    json.dump(results, f, indent=1)
print("saved scripts/_validation_results.json")
