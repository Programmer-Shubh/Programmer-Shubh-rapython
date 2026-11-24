# -*- coding: utf-8 -*-
"""strict_trend gate debug: ADX/VWAP values + trades ON/OFF."""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
from routes.strategy_builder import _run_backtest_core, BacktestRequest

CFG = {
    "symbol": "BANKNIFTY", "symbols": ["BANKNIFTY"],
    "start_date": "2026-09-08", "end_date": "2026-11-14",
    "indicators": [{"id": "rsi", "params": {"period": 14}},
                   {"id": "ema", "params": {"period": 9}},
                   {"id": "ema", "params": {"period": 21}},
                   {"id": "supertrend", "params": {"period": 10, "multiplier": 3}},
                   {"id": "market_regime", "params": {"vwap_period": 20, "ema_fast": 20,
                                                      "ema_slow": 50, "adx_period": 14,
                                                      "adx_threshold": 20}}],
    "entry_conditions": [], "exit_conditions": [],
    "legs": [{"option_type": "CE", "transaction": "buy", "lots": 1,
              "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly",
              "stop_loss": "25.0pt", "take_profit": "35.0pt"}],
    "advanced": {"trade_mode": "intraday", "timeframe": "5m",
                 "entry_time": "09:35", "exit_time": "15:15", "slippage_pct": 0.5},
    "risk": {"daily_stop_loss": 25, "daily_take_profit": 35,
             "sl_unit": "pt", "tp_unit": "pt", "max_trades_per_day": -1},
}

for strict in (False, True):
    cfg = json.loads(json.dumps(CFG))
    if strict:
        cfg["advanced"]["strict_trend"] = True
    req = BacktestRequest(**cfg)
    res = _run_backtest_core(req) or {}
    m = res.get("metrics") or {}
    diag = res.get("diagnostics") or {}
    print(f"strict={strict}: trades={m.get('total_trades')} win={m.get('win_rate')} "
          f"pnl={m.get('net_pnl')} | rejected={ (diag.get('rejected') or {}) }")

# Raw gate values check
from core.services.historical_fetcher import fetch_historical
try:
    from core.services.free_data import fetch_yahoo_intraday
    bars = fetch_yahoo_intraday("BANKNIFTY", "5m", days=20) or []
    from core.services.indicator_engine import IndicatorEngine
    ie = IndicatorEngine()
    adx = ie.calculate_adx(bars, 14) or {}
    vw = ie.calculate_vwap(bars, 20, 2.0) or {}
    a = list((adx.get("adx") or []))[-8:]
    v = list((vw.get("vwap") or []))[-8:]
    c = [float(b.get("close_price") or 0) for b in bars][-8:]
    print("bars:", len(bars))
    print("adx tail :", [round(float(x), 2) if x is not None else None for x in a])
    print("vwap tail:", [round(float(x), 2) if x is not None else None for x in v])
    print("close tail:", [round(x, 2) for x in c])
except Exception as e:
    print("debug err", e)
