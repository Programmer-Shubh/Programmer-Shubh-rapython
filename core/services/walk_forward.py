"""Walk-forward: rolling IS/OOS (Quantman)."""
import datetime
from typing import List, Dict, Tuple
from core.services.historical_fetcher import fetch_historical
from core.services.backtest_engine import BacktestEngine

def walk_forward(symbol, start_date, end_date, indicators, entry_conditions, exit_conditions, legs, advanced, risk,
                 is_months=12, oos_months=3, step_months=3) -> Dict:
    s = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    e = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    windows = []
    cur = s
    while True:
        is_end = cur + datetime.timedelta(days=is_months*30)
        oos_start = is_end + datetime.timedelta(days=1)
        oos_end = oos_start + datetime.timedelta(days=oos_months*30) - datetime.timedelta(days=1)
        if oos_end > e:
            break
        hist_is = fetch_historical(symbol, cur.strftime("%Y-%m-%d"), is_end.strftime("%Y-%m-%d"), allow_synthetic=True)
        hist_oos = fetch_historical(symbol, oos_start.strftime("%Y-%m-%d"), oos_end.strftime("%Y-%m-%d"), allow_synthetic=True)
        if not hist_is or not hist_oos or len(hist_is)<30 or len(hist_oos)<5:
            cur += datetime.timedelta(days=step_months*30)
            continue
        if len(hist_is)>120: hist_is=hist_is[-120:]
        eng = BacktestEngine(is_live=False)
        is_res = eng.run(hist_is, symbol, cur.strftime("%Y-%m-%d"), is_end.strftime("%Y-%m-%d"), indicators, entry_conditions, exit_conditions, legs, advanced, risk, is_live=False)
        eng2 = BacktestEngine(is_live=False)
        oos_res = eng2.run(hist_oos, symbol, oos_start.strftime("%Y-%m-%d"), oos_end.strftime("%Y-%m-%d"), indicators, entry_conditions, exit_conditions, legs, advanced, risk, is_live=False)
        is_m = is_res.get("metrics",{})
        oos_m = oos_res.get("metrics",{})
        eff = (oos_m.get("net_pnl",0)/ is_m.get("net_pnl",1) *100) if is_m.get("net_pnl") else 0
        windows.append({"is":{"start":cur.strftime("%Y-%m-%d"),"end":is_end.strftime("%Y-%m-%d"),"metrics":is_m},
                        "oos":{"start":oos_start.strftime("%Y-%m-%d"),"end":oos_end.strftime("%Y-%m-%d"),"metrics":oos_m},
                        "efficiency":round(eff,2)})
        cur += datetime.timedelta(days=step_months*30)
        if cur > e: break
    avg_eff = sum(w["efficiency"] for w in windows)/len(windows) if windows else 0
    return {"windows":windows, "avg_efficiency":round(avg_eff,2), "count":len(windows)}
