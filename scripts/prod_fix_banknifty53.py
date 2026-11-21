# -*- coding: utf-8 -*-
"""Fix production strategy id 53 ('BANKNIFTY Intraday CE Buy - 5-Nov Default').

Diagnosis: as saved it can NEVER trade - advanced_options.auto_signal=false,
legs fixed CE Buy, no entry_conditions, and the confluence+ADX>22 gates never
combined -> 0 trades on 2026-09-08..2026-11-17 (reproduced locally).

Fix (honest, minimal): convert to the closest config that deliberately
trades - BANKNIFTY PE Sell OTM-4, supertrend+RSI, 5m, EOD 15:15, weekly,
1 lot, rulebook trailing (10pt -> 5 lock, step 5). PAUSED status: NOT
validated reliably profitable either (train 53.9% WR -2,701; validation
62.5% -529 on modeled premium); left paused for operator review.

Usage:
  python scripts/prod_fix_banknifty53.py --live
"""
import os
import sys
import json
import argparse
import requests

HOST = "https://ratrade-iwi2.onrender.com"
SID = 53

RUNS = os.path.join(os.path.dirname(__file__), "_save_runs.json")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    full = None
    m = {}
    try:
        with open(RUNS, encoding="utf-8") as f:
            for run in json.load(f):
                if run["symbol"] == "BANKNIFTY" and run["otm"] == 4 and run["ind"] == "A":
                    full = run.get("full")
                    m = run.get("metrics") or {}
                    break
    except Exception as e:
        print("warn: could not load save_runs.json:", e)

    desc = (
        "17-Nov-2026: converted from broken 'CE Buy + VIX' default (auto_signal=false, "
        "0 trades on backtest 2026-09-08..2026-11-17 - signal gate never fired). "
        "Now: PE Sell OTM-4 weekly, supertrend(10,3)+RSI(14), 5m, entry 09:35, EOD 15:15, "
        "1 lot, rulebook trailing (10pt->5 lock, step 5), SL/TP 25/35 pts (BANKNIFTY). "
        f"Full window 2026-09-15..2026-11-13: trades={m.get('total_trades')} "
        f"WR={round(float(m.get('win_rate') or 0), 1)}% net=Rs {round(float(m.get('net_pnl') or 0)):,} "
        f"PF={round(float(m.get('profit_factor') or 0), 2)} (modeled BS premium). "
        "NOT validated profitable - PAUSED for operator review. Not investment advice."
    )

    payload = {
        "id": SID,
        "name": "BANKNIFTY PE Sell OTM-4 (ST+RSI) - 5m [17-Nov, was broken CE Buy]",
        "symbol": "BANKNIFTY",
        "start_date": "2026-09-15",
        "end_date": "2026-11-13",
        "timeframe": "5m",
        "description": desc,
        "indicators": [
            {"id": "supertrend", "params": {"period": 10, "multiplier": 3}},
            {"id": "rsi", "params": {"period": 14}},
        ],
        "entry_conditions": [],
        "exit_conditions": [],
        "legs": [{
            "option_type": "PE", "transaction": "sell", "lots": 1,
            "strike_selection": "otm", "otm_distance": 4,
            "expiry": "weekly", "stop_loss": "25.0pt", "take_profit": "35.0pt",
        }],
        "advanced_options": {
            "trade_mode": "intraday", "timeframe": "5m",
            "entry_time": "09:35", "exit_time": "15:15",
            "strict_trend": False, "allow_intraday_time_exit": False,
            "auto_signal": False, "max_holding_bars": 0,
            "trailing": "5", "trailing_sl": True,
            "lock_profit_at": 10.0, "lock_profit": 5.0, "lock_profit_unit": "pt",
            "reentry_on_sl": "No Re-entry", "reentry_on_tp": "RE ASAP",
        },
        "risk_management": {
            "daily_stop_loss": 25.0, "sl_unit": "pt",
            "daily_take_profit": 35.0, "tp_unit": "pt",
            "daily_stop_loss_pct": 0, "daily_take_profit_pct": 0,
            "max_trades_per_day": 5, "signal_lookback_days": 0, "vix_max": 0,
            "daily_loss_limit": 0, "daily_max_loss": 0, "max_daily_loss": 0,
        },
        "status": "paused",
        "last_backtest": full,
    }

    if not args.live:
        print(json.dumps({k: v for k, v in payload.items() if k != "last_backtest"}, indent=1))
        return
    r = requests.post(f"{HOST}/api/strategies/save", json=payload, timeout=290)
    print("->", r.status_code, r.text[:400])

if __name__ == "__main__":
    main()
