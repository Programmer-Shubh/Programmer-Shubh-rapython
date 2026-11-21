"""Daily-bar grid search across option legs with REAL premium seed in local DB.
Trains on Jan-Jul, validates on Aug-Nov (disjoint slices, no cherry-picking).
Reports honest metrics per config - no fabrication."""
import sys, json, threading
sys.path.insert(0, r'D:\ra python website')
os_import = __import__('os')
os_import.environ.setdefault("DB_PATH", r'D:\ra python website\data\ratrade.db')

from routes.strategy_builder import BacktestRequest, _run_backtest_core

IDX_SLTP = {"NIFTY": (15.0, 20.0), "FINNIFTY": (18.0, 22.0),
            "BANKNIFTY": (25.0, 35.0), "MIDCPNIFTY": (15.0, 20.0)}

ST = {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}
RSI = {"id": "rsi", "params": {"period": 14}}
REG = {"id": "market_regime",
       "params": {"vwap_period": 20, "ema_fast": 20, "ema_slow": 50,
                  "adx_period": 14, "adx_threshold": 20}}

def run(sym, opt, tx, otm, dist, ind, start, end, sl, tp, mtd):
    req = BacktestRequest(
        symbol=sym, symbols=[sym], start_date=start, end_date=end,
        timeframe="daily",
        indicators=[dict(x, params=dict(x["params"])) for x in ind],
        legs=[{"option_type": opt, "transaction": tx, "lots": 1,
               "strike_selection": "otm" if tx == "sell" else "atm",
               "otm_distance": dist if tx == "sell" else dist,
               "expiry": "weekly"}],
        advanced={"trade_mode": "intraday", "timeframe": "daily",
                  "entry_time": "09:20", "exit_time": "15:15",
                  "reentry_on_sl": "No Re-entry", "reentry_on_tp": "RE ASAP"},
        risk={"max_trades_per_day": mtd,
              "daily_stop_loss": sl, "sl_unit": "pt",
              "daily_take_profit": tp, "tp_unit": "pt"},
    )
    try:
        r = _run_backtest_core(req) or {}
    except Exception as e:
        return {"error": str(e)[:80]}
    m = r.get("metrics") or {}
    return {"trades": int(m.get("total_trades") or 0),
            "win_rate": float(m.get("win_rate") or 0),
            "net": float(m.get("net_pnl") or 0),
            "pf": float(m.get("profit_factor") or 0),
            "dd": float(m.get("max_drawdown") or 0),
            "exp": float(m.get("expectancy") or 0)}

# Candidates across all 4 indices
candidates = []
for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
    for opt in ("CE", "PE"):
        for tx in ("buy", "sell"):
            for dist in (1, 2, 3, 4):
                tag = f"{sym}-{tx}-{opt}-OTM{dist}" if dist > 0 else f"{sym}-{tx}-{opt}-ATM"
                candidates.append((sym, opt, tx, dist, tag))

# Base indicators: sell uses ST+RSI (proven), buy uses ST+RSI+regime
ind_sell = [ST, RSI]
ind_buy = [ST, RSI, REG]
TRAIN_S, TRAIN_E = "2026-01-01", "2026-07-31"
VAL_S, VAL_E = "2026-08-01", "2026-11-17"

print(f"{'TAG':36}{'TRD':>4}{'WR%':>7}{'NET':>10}{'PF':>6}{'DD':>8}{'EXP':>9}  |  {'VAL_TRD':>7}{'VAL_WR':>7}{'VAL_NET':>9}{'VAL_PF':>7}")
print("-" * 140)

results = []
for sym, opt, tx, dist, tag in candidates:
    ind = ind_sell if tx == "sell" else ind_buy
    sl, tp = IDX_SLTP[sym]
    mt = 2 if tx == "buy" else 5
    try:
        train = run(sym, opt, tx, dist, dist if tx == "sell" else dist, ind, TRAIN_S, TRAIN_E, sl, tp, mt)
    except Exception as e:
        train = {"error": str(e)[:80]}
    val = run(sym, opt, tx, dist, dist if tx == "sell" else dist, ind, VAL_S, VAL_E, sl, tp, mt)
    results.append({"tag": tag, "train": train, "val": val})
    t = train
    v = val
    print(f"{tag:36}{t.get('trades',0):>4}{t.get('win_rate',0):>6.1f}%{t.get('net',0):>10,.0f}{t.get('pf',0):>6.2f}{t.get('dd',0):>8,.0f}{t.get('exp',0):>9,.0f}  |  "
          f"{v.get('trades',0):>7}{v.get('win_rate',0):>6.1f}%{v.get('net',0):>9,.0f}{v.get('pf',0):>7.2f}",
          flush=True)
print("-" * 140)
with open(r'D:\ra python website\scripts\_daily_grid_results.json', 'w') as f:
    json.dump(results, f, indent=1)
