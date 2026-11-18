"""Saved strategy updates per honest validation (Aug-Nov 5m + real BS/IV):
- 81 -> NIFTY OTM-5 ST+RSI (validated 73.7% WR, +Rs736)
- 82 -> MIDCPNIFTY OTM-4 ST+RSI (64.0% WR, +Rs2,830)
- 83 -> MIDCPNIFTY OTM-5 ST+RSI (64.0% WR, +Rs1,753)   (was BANKNIFTY -Rs3,757)
- 84 -> FINNIFTY OTM-4 ST+RSI keep (flat +Rs82)        unchanged
Then add 2 new BUY strategies (NIFTY + MIDCPNIFTY) with best validated config.
"""
import sys, json, urllib.request

HOST = "https://ratrade-iwi2.onrender.com"
ST = {"id": "supertrend", "params": {"period": 10, "multiplier": 3}}
RSI = {"id": "rsi", "params": {"period": 14}}

def _ind(idl):
    out = [dict(ST, params=dict(ST["params"]))]
    if idl == "A":
        out.append(dict(RSI, params=dict(RSI["params"])))
    return out

def _payload(sid, name, sym, legs, advanced, risk, status="paused"):
    return {
        "id": sid,
        "name": name,
        "symbol": sym,
        "start_date": "2026-08-01",
        "end_date": "2026-11-17",
        "timeframe": "5m",
        "description": f"Backtest validated 2026-08-01..2026-11-17 (5m, real IV): see description field.",
        "indicators": _ind("A"),
        "entry_conditions": [],
        "exit_conditions": [],
        "legs": legs,
        "advanced_options": advanced,
        "risk_management": risk,
        "status": status,
    }

def _sell_legs(opt, otm, sym):
    return [{"option_type": opt, "transaction": "sell", "lots": 1,
             "strike_selection": "otm", "otm_distance": int(otm),
             "expiry": "weekly",
             "stop_loss": "15.0pt" if sym == "NIFTY" else "15.0pt",
             "take_profit": "20.0pt" if sym == "NIFTY" else "20.0pt"}]

def _sell_adv():
    return {"trade_mode": "intraday", "timeframe": "5m",
            "entry_time": "09:35", "exit_time": "15:15",
            "strict_trend": False, "allow_intraday_time_exit": False,
            "auto_signal": False, "max_holding_bars": 0,
            "trailing": "5", "trailing_sl": True,
            "lock_profit_at": 10.0, "lock_profit": 5.0,
            "lock_profit_unit": "pt",
            "reentry_on_sl": "No Re-entry", "reentry_on_tp": "RE ASAP"}

def _sell_risk(sym):
    sltp = {"NIFTY": (15.0, 20.0), "MIDCPNIFTY": (15.0, 20.0),
            "FINNIFTY": (18.0, 22.0), "BANKNIFTY": (25.0, 35.0)}
    sl, tp = sltp.get(sym.upper(), (15.0, 20.0))
    return {"daily_stop_loss": sl, "sl_unit": "pt",
            "daily_take_profit": tp, "tp_unit": "pt",
            "daily_stop_loss_pct": 0, "daily_take_profit_pct": 0,
            "max_trades_per_day": 5, "signal_lookback_days": 0,
            "vix_max": 0,
            "daily_loss_limit": 0, "daily_max_loss": 0, "max_daily_loss": 0}

updates = [
    (81, "NIFTY PE Sell OTM-5 (ST+RSI) - 5m [v18-Nov]", "NIFTY",
     _sell_legs("PE", 5, "NIFTY"), _sell_adv(), _sell_risk("NIFTY")),
    (82, "MIDCPNIFTY PE Sell OTM-4 (ST+RSI) - 5m [v18-Nov]", "MIDCPNIFTY",
     _sell_legs("PE", 4, "MIDCPNIFTY"), _sell_adv(), _sell_risk("MIDCPNIFTY")),
    (83, "MIDCPNIFTY PE Sell OTM-5 (ST+RSI) - 5m [v18-Nov]", "MIDCPNIFTY",
     _sell_legs("PE", 5, "MIDCPNIFTY"), _sell_adv(), _sell_risk("MIDCPNIFTY")),
]

for sid, name, sym, legs, adv, risk in updates:
    payload = _payload(sid, name, sym, legs, adv, risk)
    p = json.dumps(payload).encode()
    req = urllib.request.Request(HOST + "/api/strategies/save", data=p,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        r = json.load(urllib.request.urlopen(req, timeout=60))
        print(f"id={sid} -> {r.get('status')} result_saved={r.get('result_saved')} trades={r.get('result_trades')}")
    except urllib.error.HTTPError as e:
        print(f"id={sid} -> HTTP {e.code}: {e.read().decode()[:300]}")
