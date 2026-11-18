import json, time, urllib.request

HOST = 'https://ratrade-iwi2.onrender.com'
ST = {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}
RSI = {"id": "rsi", "params": {"period": 14}}
REG = {"id": "market_regime",
       "params": {"vwap_period": 20, "ema_fast": 20,
                  "ema_slow": 50, "adx_period": 14, "adx_threshold": 22}}
IDX = {"NIFTY": (15.0, 20.0), "BANKNIFTY": (25.0, 35.0),
       "FINNIFTY": (18.0, 22.0), "MIDCPNIFTY": (15.0, 20.0)}

def run(tag, sym, tx, opt, otm, ind, tf="5m"):
    sl, tp = IDX[sym]
    body = {
        "symbol": sym, "symbols": [sym],
        "start_date": "2026-08-01", "end_date": "2026-11-17",
        "timeframe": tf,
        "indicators": [dict(x, params=dict(x["params"])) for x in ind],
        "legs": [{"option_type": opt, "transaction": tx, "lots": 1,
                  "strike_selection": "otm" if tx == "sell" else "atm",
                  "otm_distance": otm if tx == "sell" else otm,
                  "expiry": "weekly"}],
        "advanced": {"trade_mode": "intraday", "timeframe": tf,
                     "entry_time": "09:35", "exit_time": "15:15",
                     "reentry_on_sl": "No Re-entry", "reentry_on_tp": "RE ASAP",
                     "trailing": "5", "lock_profit_at": 12.0,
                     "lock_profit": 6.0, "lock_profit_unit": "pt",
                     "max_holding_bars": 0, "allow_intraday_time_exit": False,
                     "strict_trend": False},
        "risk": {"daily_stop_loss": sl, "sl_unit": "pt",
                 "daily_take_profit": tp, "tp_unit": "pt",
                 "max_trades_per_day": 2, "signal_lookback_days": 0,
                 "vix_max": 0,
                 "daily_loss_limit": 0, "daily_max_loss": 0, "max_daily_loss": 0},
    }
    req = urllib.request.Request(HOST + "/api/backtest/run-async",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        r = json.load(urllib.request.urlopen(req, timeout=60))
    except Exception as e:
        print(tag, "req ERR:", e)
        return
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
            m = (rr.get("result") or {}).get("metrics") or {}
            print(json.dumps({"tag": tag, "trades": m.get("total_trades"),
                              "wr": m.get("win_rate"), "net": m.get("net_pnl"),
                              "pf": m.get("profit_factor"), "exp": m.get("expectancy")}),
                  flush=True)
            return
        if rr.get("status") == "error":
            print(tag, "error:", rr.get("error"), flush=True)
            return
    print(tag, "timeout", flush=True)

run("BNK-BU-CE-ATM-REG", "BANKNIFTY", "buy", "CE", 0, (ST, RSI, REG))
run("FIN-BU-CE-ATM-REG", "FINNIFTY", "buy", "CE", 0, (ST, RSI, REG))
run("FIN-BU-CE-ATM", "FINNIFTY", "buy", "CE", 0, (ST, RSI))
run("MID-BU-CE-ATM-REG", "MIDCPNIFTY", "buy", "CE", 0, (ST, RSI, REG))
run("NIFTY-BU-CE-ATM-REG-22", "NIFTY", "buy", "CE", 0, (ST, RSI, REG))
run("NIFTY-BU-PE-OTM1-REG", "NIFTY", "buy", "PE", 1, (ST, RSI, REG))
