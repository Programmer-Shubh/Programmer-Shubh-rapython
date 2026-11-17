# -*- coding: utf-8 -*-
"""Save the 5 rulebook-candidate strategies on PRODUCTION via
https://ratrade-iwi2.onrender.com/api/strategies/save

All saved PAUSED with full metrics in the description (honest disclosure).
Attaches the locally-computed full-window backtest result so the saved
strategy card shows identical numbers.

Usage:
  python scripts/prod_save_strategies.py --dry-run
  python scripts/prod_save_strategies.py --live
"""
import os
import sys
import json
import argparse
import requests

ROOT = os.path.join(os.path.dirname(__file__), "..")
RUNS = os.path.join(os.path.dirname(__file__), "_save_runs.json")
HOST = "https://ratrade-iwi2.onrender.com"

IDX_SLTP = {"NIFTY": (15.0, 20.0), "FINNIFTY": (18.0, 22.0),
            "BANKNIFTY": (25.0, 35.0), "MIDCPNIFTY": (15.0, 20.0)}
LOTS = {"NIFTY": 65, "FINNIFTY": 60, "BANKNIFTY": 30, "MIDCPNIFTY": 50}
NAMES = {
    ("NIFTY", 4, "A"): "NIFTY PE Sell OTM-4 (ST+RSI) - 5m [v17-Nov]",
    ("NIFTY", 4, "C"): "NIFTY PE Sell OTM-4 (SuperTrend) - 5m [v17-Nov]",
    ("MIDCPNIFTY", 3, "A"): "MIDCPNIFTY PE Sell OTM-3 (ST+RSI) - 5m [v17-Nov]",
    ("BANKNIFTY", 4, "A"): "BANKNIFTY PE Sell OTM-4 (ST+RSI) - 5m [v17-Nov]",
    ("FINNIFTY", 4, "A"): "FINNIFTY PE Sell OTM-4 (ST+RSI) - 5m [v17-Nov]",
}

def _ind(idl):
    st = {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}
    if idl == "A":
        return [st, {"id": "rsi", "params": {"period": 14}}]
    return [st]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    with open(RUNS, encoding="utf-8") as f:
        runs = json.load(f)

    for run in runs:
        sym, otm, idl = run["symbol"], run["otm"], run["ind"]
        key = (sym, otm, idl)
        if key not in NAMES:
            print("skip unknown case", key)
            continue
        sl, tp = IDX_SLTP[sym]
        full = run.get("full") or {}
        m = run.get("metrics") or {}
        wr = round(float(m.get("win_rate") or 0), 1)
        net = round(float(m.get("net_pnl") or 0))
        pf = round(float(m.get("profit_factor") or 0), 2)
        exp = round(float(m.get("expectancy") or 0), 1)
        desc = (
            f"Auto-saved 17-Nov-2026 from honest grid search (1 lot, {LOTS[sym]} qty, weekly). "
            f"Backtest 2026-09-15..2026-11-13 (modeled BS premium, 0.5% slippage): "
            f"trades={m.get('total_trades')} WR={wr}% net=Rs {net:,} PF={pf} expectancy Rs {exp}. "
            f"Out-of-sample 2026-10-24..2026-11-13 also run and published in the repo report. "
            f"EVIDENCE IS WEAK - saved PAUSED for review only; do NOT fund live from this alone. "
            f"Not investment advice."
        )
        payload = {
            "name": NAMES[key],
            "symbol": sym,
            "start_date": "2026-09-15",
            "end_date": "2026-11-13",
            "timeframe": "5m",
            "description": desc,
            "indicators": _ind(idl),
            "entry_conditions": [],
            "exit_conditions": [],
            "legs": [{
                "option_type": "PE", "transaction": "sell", "lots": 1,
                "strike_selection": "otm", "otm_distance": int(otm),
                "expiry": "weekly",
                "stop_loss": f"{sl}pt", "take_profit": f"{tp}pt",
            }],
            "advanced_options": {
                "trade_mode": "intraday", "timeframe": "5m",
                "entry_time": "09:35", "exit_time": "15:15",
                "strict_trend": False, "allow_intraday_time_exit": False,
                "auto_signal": False, "max_holding_bars": 0,
                "trailing": "5", "trailing_sl": True,
                "lock_profit_at": 10.0, "lock_profit": 5.0,
                "lock_profit_unit": "pt",
                "reentry_on_sl": "No Re-entry", "reentry_on_tp": "RE ASAP",
            },
            "risk_management": {
                "daily_stop_loss": float(sl), "sl_unit": "pt",
                "daily_take_profit": float(tp), "tp_unit": "pt",
                "daily_stop_loss_pct": 0, "daily_take_profit_pct": 0,
                "max_trades_per_day": 5, "signal_lookback_days": 0,
                "vix_max": 0,
                "daily_loss_limit": 0, "daily_max_loss": 0, "max_daily_loss": 0,
            },
            "status": "paused",
            "last_backtest": full,
        }
        if not args.live:
            print(json.dumps({k: v for k, v in payload.items() if k not in ("last_backtest",)}, indent=1)[:1200])
            print("--- metrics:", json.dumps(m, indent=1))
            continue
        r = requests.post(f"{HOST}/api/strategies/save", json=payload, timeout=290)
        print(sym, otm, idl, "->", r.status_code, r.text[:300], flush=True)

if __name__ == "__main__":
    main()
