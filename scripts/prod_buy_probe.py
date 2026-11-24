"""BUY-side probes: NIFTY + MIDCPNIFTY, OTM-1/2, CE/PE, ST+RSI vs ST+RSI+REG."""
import sys, json, time, urllib.request

HOST = "https://ratrade-iwi2.onrender.com"
ST = {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}
RSI = {"id": "rsi", "params": {"period": 14}}
REG = {"id": "market_regime", "params": {"vwap_period": 20, "ema_fast": 20,
                                         "ema_slow": 50, "adx_period": 14,
                                         "adx_threshold": 20}}
IDX = {"NIFTY": (15.0, 20.0), "MIDCPNIFTY": (15.0, 20.0),
       "BANKNIFTY": (25.0, 35.0), "FINNIFTY": (18.0, 22.0)}

def run(tag, sym, opt, otm, ind):
    sl, tp = IDX[sym]
    body = {
        "symbol": sym, "symbols": [sym],
        "start_date": "2026-08-01", "end_date": "2026-11-17",
        "timeframe": "5m",
        "indicators": [dict(x, params=dict(x["params"])) for x in ind],
        "legs": [{"option_type": opt, "transaction": "buy", "lots": 1,
                  "strike_selection": "otm" if otm > 0 else "atm",
                  "otm_distance": otm, "expiry": "weekly"}],
        "advanced": {"trade_mode": "intraday", "timeframe": "5m",
                     "entry_time": "09:35", "exit_time": "15:15",
                     "reentry_on_sl": "No Re-entry", "reentry_on_tp": "RE ASAP",
                     "trailing": "5", "lock_profit_at": 12.0,
                     "lock_profit": 6.0, "lock_profit_unit": "pt",
                     "max_holding_bars": 0, "allow_intraday_time_exit": False,
                     "strict_trend": False},
        "risk": {"daily_stop_loss": sl, "sl_unit": "pt",
                 "daily_take_profit": tp, "tp_unit": "pt",
                 "max_trades_per_day": 2, "signal_lookback_days": 0,
                 "vix_max": 0, "daily_loss_limit": 0, "daily_max_loss": 0, "max_daily_loss": 0},
    }
    try:
        req = urllib.request.Request(HOST + "/api/backtest/run-async",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        r = json.load(urllib.request.urlopen(req, timeout=60))
    except Exception as e:
        return {"tag": tag, "error": f"req:{e}"}
    jid = r.get("job_id")
    t0 = time.time()
    while time.time() - t0 < 240:
        time.sleep(4)
        try:
            rr = json.load(urllib.request.urlopen(
                urllib.request.Request(f"{HOST}/api/backtest/result/{jid}"), timeout=30))
        except Exception:
            continue
        if rr.get("status") == "done":
            res = rr.get("result") or {}
            m = res.get("metrics") or {}
            return {"tag": tag, "sym": sym, "opt": opt, "otm": otm,
                    "ind": [i["id"] for i in ind],
                    "trades": m.get("total_trades"), "wr": m.get("win_rate"),
                    "net": m.get("net_pnl"), "pf": m.get("profit_factor"),
                    "dd": m.get("max_drawdown"), "exp": m.get("expectancy")}
        if rr.get("status") == "error":
            return {"tag": tag, "error": rr.get("error")}
    return {"tag": tag, "error": "timeout"}

for sym in ("NIFTY", "MIDCPNIFTY"):
    for opt in ("CE", "PE"):
        for otm in (1, 2):
            for ind_k, ind in [("st_rsi", (ST, RSI)), ("st_rsi_reg", (ST, RSI, REG))]:
                tag = f"{sym}-buy-{opt}-OTM{otm}-{ind_k}"
                print(json.dumps(run(tag, sym, opt, otm, ind)), flush=True)
