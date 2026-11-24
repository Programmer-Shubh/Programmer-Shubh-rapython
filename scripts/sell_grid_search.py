# -*- coding: utf-8 -*-
"""Honest grid search: intraday option-SELLING profiles, 5m bars (the
timeframe the user's own NIFTY winners used).

Train window and validation window are DISJOINT calendar slices of the
available Yahoo 5m history (~60 trading days). Selection rule:
  - net > 0 on train,
  - net > 0 on validation (out-of-sample),
  - profit factor >= 1.1 on validation.

Option premium is Black-Scholes modelled by the engine when real premium
history is absent - treat absolute P&L as indicative, not gospel.
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from routes.strategy_builder import BacktestRequest, _run_backtest_core

IDX_SLTP = {"NIFTY": (15.0, 20.0), "FINNIFTY": (18.0, 22.0),
            "BANKNIFTY": (25.0, 35.0), "MIDCPNIFTY": (15.0, 20.0)}

ST = {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}
RSI = {"id": "rsi", "params": {"period": 14}}
REG = {"id": "market_regime", "params": {"vwap_period": 20, "ema_fast": 20,
                                        "ema_slow": 50, "adx_period": 14,
                                        "adx_threshold": 22}}
IND_SETS = {"A_st_rsi": [ST, RSI], "B_st_rsi_reg": [ST, RSI, REG]}

def run_case(sym, opt, otm, entry, ind_key, start, end):
    sl, tp = IDX_SLTP[sym]
    req = BacktestRequest(
        symbol=sym, symbols=[sym], start_date=start, end_date=end,
        indicators=[dict(x, params=dict(x["params"])) for x in IND_SETS[ind_key]],
        legs=[{"option_type": opt, "transaction": "sell", "lots": 1,
               "strike_selection": "otm", "otm_distance": otm,
               "expiry": "weekly"}],
        advanced={"trade_mode": "intraday", "timeframe": "5m",
                  "entry_time": entry, "exit_time": "15:15",
                  "strict_trend": True,
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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-15")
    ap.add_argument("--end", default="2026-10-23")
    args = ap.parse_args()

    combos = [(s, o, d, e, k)
              for s in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
              for o in ("CE", "PE")
              for d in (3, 4)
              for e in ("09:35", "10:15")
              for k in ("A_st_rsi", "B_st_rsi_reg")]
    print(f"Window {args.start}..{args.end}  ({len(combos)} cases)")
    print(f"{'SYM':11}{'OPT':4}{'OTM':4}{'ENT':6}{'IND':13}{'TRD':>4}{'WR%':>7}{'NET':>10}{'PF':>7}")
    print("-" * 72)
    results = []
    for sym, opt, otm, entry, ind_key in combos:
        m = run_case(sym, opt, otm, entry, ind_key, args.start, args.end)
        if "error" in m:
            print(f"{sym:11}{opt:4}{otm:<4}{entry:6}{ind_key:13}  ERROR: {m['error'][:50]}", flush=True)
            continue
        flag = ""
        if m["trades"] >= 8 and m["net"] > 0 and m["pf"] >= 1.1:
            flag = "  <<<"
        print(f"{sym:11}{opt:4}{otm:<4}{entry:6}{ind_key:13}{m['trades']:>4}"
              f"{m['win_rate']:>6.1f}%{m['net']:>10,.0f}{m['pf']:>7.2f}{flag}", flush=True)
        results.append({"symbol": sym, "option": opt, "otm": otm,
                        "entry": entry, "indicators": ind_key, **m})
    print("-" * 72)
    out = "scripts/_sell_grid_results.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=1)
    print("saved", out)

if __name__ == "__main__":
    main()
