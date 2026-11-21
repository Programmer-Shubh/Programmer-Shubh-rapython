# -*- coding: utf-8 -*-
"""Replicate the user's own NIFTY PE-sell winner config across indices.

Winners (DB ids 1956/1958/1959): 5m, entry 09:35, supertrend(10,3)+RSI(14),
OTM 4 weekly, SL Rs 6000 (no TP), EOD 15:15 exit, max 8/day, no trailing.
We test SL Rs 6000 with/without VIX<=18 filter and OTM 3/4.
Honestly reported: modeled premiums, ~60 days of 5m Yahoo history max.
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from routes.strategy_builder import BacktestRequest, _run_backtest_core

ST = {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}
RSI = {"id": "rsi", "params": {"period": 14}}
IND = [ST, RSI]

def run_case(sym, otm, vix_max, start, end):
    risk = {"max_trades_per_day": 8,
            "daily_stop_loss": 6000.0, "sl_unit": "rs",
            "daily_take_profit": 0.0, "tp_unit": "rs",
            "vix_max": vix_max}
    req = BacktestRequest(
        symbol=sym, symbols=[sym], start_date=start, end_date=end,
        indicators=[dict(x, params=dict(x["params"])) for x in IND],
        legs=[{"option_type": "PE", "transaction": "sell", "lots": 1,
               "strike_selection": "otm", "otm_distance": otm,
               "expiry": "weekly"}],
        advanced={"trade_mode": "intraday", "timeframe": "5m",
                  "entry_time": "09:35", "exit_time": "15:15",
                  "strict_trend": False},
        risk=risk,
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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="NIFTY,BANKNIFTY,FINNIFTY,MIDCPNIFTY")
    ap.add_argument("--start", default="2026-09-15")
    ap.add_argument("--end", default="2026-10-23")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print(f"Window {args.start}..{args.end}  winner-style")
    print(f"{'SYM':11}{'OTM':4}{'VIX':5}{'TRD':>4}{'WR%':>7}{'NET':>10}{'PF':>7}{'DD':>8}")
    print("-" * 66)
    results = []
    for sym in syms:
        for otm in (3, 4):
            for vix_max in (0, 18):
                m = run_case(sym, otm, vix_max, args.start, args.end)
                if "error" in m:
                    print(f"{sym:11}{otm:<4}{vix_max:<5}  ERROR: {m['error'][:50]}", flush=True)
                    continue
                print(f"{sym:11}{otm:<4}{vix_max:<5}{m['trades']:>4}"
                      f"{m['win_rate']:>6.1f}%{m['net']:>10,.0f}{m['pf']:>7.2f}{m['dd']:>8,.0f}", flush=True)
                results.append({"symbol": sym, "otm": otm, "vix_max": vix_max, **m})
    print("-" * 66)
    out = f"scripts/_winner_style_results{'_' + args.tag if args.tag else ''}.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=1)
    print("saved", out)

if __name__ == "__main__":
    main()
