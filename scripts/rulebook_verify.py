# -*- coding: utf-8 -*-
"""Rulebook verification backtests (16-11-2026).

Master rulebook ke Buy/Sell default profiles par har index ki strategy ka
backtest chalata hai aur win-rate / net P&L print karta hai. Ye script
production config ko hi use karta hai (koi alag simulation nahi):

    python scripts/rulebook_verify.py [start_date] [end_date]

Profiles:
  SELL : entry 10:15, SL/TP index-specific (NIFTY 15/20, FINNIFTY 18/22,
         BANKNIFTY 25/35), trailing 10->5 (step 5), max 5/day, no re-entry.
  BUY  : entry 09:35, same index SL/TP, trailing 12->6 (step 5), max 2/day,
         strict ADX+VWAP gate.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from routes.strategy_builder import BacktestRequest, _run_backtest_core

START = sys.argv[1] if len(sys.argv) > 1 else "2026-06-01"
END = sys.argv[2] if len(sys.argv) > 2 else "2026-08-20"

CASES = [
    ("SELL PE", "NIFTY", "PE", "sell"),
    ("SELL CE", "BANKNIFTY", "CE", "sell"),
    ("SELL PE", "FINNIFTY", "PE", "sell"),
    ("BUY CE", "NIFTY", "CE", "buy"),
    ("BUY PE", "BANKNIFTY", "PE", "buy"),
    ("BUY CE", "FINNIFTY", "CE", "buy"),
]

IND = [
    {"id": "rsi", "params": {"period": 14}},
    {"id": "ema", "params": {"period": 21}},
    {"id": "supertrend", "params": {"period": 10, "multiplier": 3}},
]


def _f(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def main():
    print(f"Rulebook backtest {START} .. {END}\n")
    print(f"{'CASE':10} {'SYMBOL':10} {'TRADES':>7} {'WIN%':>6} "
          f"{'NET':>11} {'PF':>6} {'MAXDD':>10}")
    print("-" * 70)
    worst = []
    for label, sym, opt, txn in CASES:
        req = BacktestRequest(
            symbol=sym, symbols=[sym], start_date=START, end_date=END,
            indicators=[dict(x) for x in IND],
            legs=[{"option_type": opt, "transaction": txn, "lots": 1,
                   "strike_selection": "atm" if txn == "buy" else "otm",
                   "otm_distance": 1, "expiry": "weekly"}],
            advanced={"trade_mode": "intraday", "timeframe": "15m"},
            risk={},  # rulebook defaults yahin se lagte hain
        )
        try:
            r = _run_backtest_core(req)
        except Exception as e:  # pragma: no cover
            print(f"{label:10} {sym:10}  ERROR: {e}")
            continue
        m = (r or {}).get("metrics", {}) or {}
        trades = int(m.get("total_trades", 0) or 0)
        wr = _f(m.get("win_rate"))
        net = _f(m.get("net_pnl"))
        pf = _f(m.get("profit_factor"))
        dd = _f(m.get("max_drawdown"))
        print(f"{label:10} {sym:10} {trades:>7} {wr:>5.1f}% "
              f"{net:>11,.0f} {pf:>6.2f} {dd:>10,.0f}")
        if trades <= 0 or wr < 45 or net <= 0:
            worst.append((label, sym, trades, wr, net))
    print("-" * 70)
    if worst:
        print("\nNeeds attention (0 trades / <45% win / negative net):")
        for w in worst:
            print("  ", w)
    else:
        print("\nAll cases: trades > 0, win >= 45%, net > 0")


if __name__ == "__main__":
    main()
