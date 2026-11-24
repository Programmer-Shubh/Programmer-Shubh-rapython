"""Broad but honest config sweep on prod: sell PE (NIFTY + MIDCPNIFTY).
Walk-forward: Jan-Jul (train) & Aug-Nov (val). Only configs with
net>0 AND pf>=1.1 on BOTH are marked SV-CANDIDATE. No cherry-pick."""
import sys, json, time, urllib.request

HOST = "https://ratrade-iwi2.onrender.com"
ST = {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}
RSI = {"id": "rsi", "params": {"period": 14}}
REG = {"id": "market_regime", "params": {"vwap_period": 20, "ema_fast": 20,
                                         "ema_slow": 50, "adx_period": 14,
                                         "adx_threshold": 20}}
IDX = {"NIFTY": (15.0, 20.0), "MIDCPNIFTY": (15.0, 20.0),
       "BANKNIFTY": (25.0, 35.0), "FINNIFTY": (18.0, 22.0)}

def load(win):
    # TRAIN: Jan-Jul; VAL: Aug-Nov (disjoint calendars)
    return {"start_date": "2026-01-02", "end_date": "2026-07-31"} if win == "train" \
        else {"start_date": "2026-08-01", "end_date": "2026-11-17"}

def run(tag, sym, otm, tf, ind, start, end):
    sl, tp = IDX[sym]
    body = {
        "symbol": sym, "symbols": [sym],
        "start_date": start, "end_date": end,
        "timeframe": f"{tf}m",
        "indicators": [dict(x, params=dict(x["params"])) for x in ind],
        "legs": [{"option_type": "PE", "transaction": "sell", "lots": 1,
                  "strike_selection": "otm", "otm_distance": otm, "expiry": "weekly"}],
        "advanced": {"trade_mode": "intraday", "timeframe": f"{tf}m",
                     "entry_time": "09:35", "exit_time": "15:15",
                     "reentry_on_sl": "No Re-entry", "reentry_on_tp": "RE ASAP",
                     "trailing": "5", "lock_profit_at": 10.0, "lock_profit": 5.0,
                     "lock_profit_unit": "pt", "max_holding_bars": 0,
                     "allow_intraday_time_exit": False, "strict_trend": False},
        "risk": {"daily_stop_loss": sl, "sl_unit": "pt",
                 "daily_take_profit": tp, "tp_unit": "pt",
                 "max_trades_per_day": 5, "signal_lookback_days": 0,
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
            return {"tag": tag, "sym": sym, "otm": otm, "tf": tf,
                    "ind": [i["id"] for i in ind],
                    "trades": m.get("total_trades"), "wr": m.get("win_rate"),
                    "net": m.get("net_pnl"), "pf": m.get("profit_factor"),
                    "dd": m.get("max_drawdown"), "exp": m.get("expectancy")}
        if rr.get("status") == "error":
            return {"tag": tag, "error": rr.get("error")}
    return {"tag": tag, "error": "timeout"}

ind_sets = {"st_rsi": (ST, RSI), "st_only": (ST,)}
planned = []
for sym in ("NIFTY", "MIDCPNIFTY", "BANKNIFTY", "FINNIFTY"):
    for otm in (3, 4, 5):
        for tf in (5, 15):
            for ind in ("st_rsi", "st_only"):
                planned.append((sym, otm, tf, ind))
print(f"total cases: {len(planned) * 2}", flush=True)
for sym, otm, tf, ind_k in planned:
    for phase in ("train", "val"):
        w = load(phase)
        tag = f"{sym}-{otm}-{tf}{ind_k[1]}-{phase}"
        r = run(tag, sym, otm, tf, ind_sets[ind_k], w["start_date"], w["end_date"])
        print(json.dumps(r), flush=True)
