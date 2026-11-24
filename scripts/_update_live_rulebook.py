# -*- coding: utf-8 -*-
"""Live site par saved strategies ko MASTER RULEBOOK ke hisaab se update karo:
   - Tight per-index SL/TP (points): NIFTY 15/20, FINNIFTY 18/22, BANKNIFTY 25/35
   - advanced_options.strict_trend = True (ADX>22 + VWAP gate)
   - No daily limit (max_trades_per_day=-1, daily loss caps 0)
   - Fresh backtest attach (TAKAH card ka BT Win% / P&L naye config ka ho)
"""
import json, time, urllib.request, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "https://ratrade-iwi2.onrender.com"

RULE_SLTP = {"NIFTY": (15.0, 20.0), "FINNIFTY": (18.0, 22.0), "BANKNIFTY": (25.0, 35.0)}


def post(path, payload, timeout=200):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(path, timeout=200):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def run_bt(cfg):
    job = post("/api/backtest/run-async", cfg)
    jid = job.get("job_id")
    res = job.get("result")
    if not res:
        for _ in range(90):
            time.sleep(4)
            st = get(f"/api/backtest/result/{jid}")
            if st.get("status") == "done":
                res = st.get("result")
                break
            if st.get("status") == "error":
                raise RuntimeError(st.get("error"))
    return res


strats = get("/api/strategies/list").get("strategies") or []
print("strategies:", len(strats))

for s in strats:
    sym = str(s.get("symbol") or "").upper()
    sl, tp = RULE_SLTP.get(sym, (None, None))
    if sl is None:
        print("SKIP", sym, s.get("name"))
        continue
    rm = dict(s.get("risk_management") or {})
    rm.update({"daily_stop_loss": sl, "daily_take_profit": tp,
               "daily_stop_loss_pct": 0, "daily_take_profit_pct": 0,
               "sl_unit": "pt", "tp_unit": "pt",
               "max_trades_per_day": -1, "daily_loss_limit": 0,
               "daily_max_loss": 0, "max_daily_loss": 0})
    legs = []
    for l in (s.get("legs") or []):
        l = dict(l)
        l["stop_loss"] = f"{sl:g}pt"
        l["take_profit"] = f"{tp:g}pt"
        legs.append(l)
    adv = dict(s.get("advanced_options") or {})
    adv["strict_trend"] = True          # ADX>22 + VWAP gate (engine me)
    adv.setdefault("trade_mode", "intraday")
    cfg = {
        "symbol": sym,
        "start_date": str(s.get("start_date") or ""),
        "end_date": str(s.get("end_date") or ""),
        "indicators": s.get("indicators") or [],
        "entry_conditions": s.get("entry_conditions") or [],
        "exit_conditions": s.get("exit_conditions") or [],
        "legs": legs,
        "advanced": adv,
        "risk": rm,
    }
    try:
        bt = run_bt(cfg)
        m = (bt or {}).get("metrics") or {}
        print(f"BT {sym} id={s.get('id')}: {m.get('total_trades')} trades | "
              f"{m.get('win_rate')}% | {m.get('net_pnl')} | PF {m.get('profit_factor')}")
    except Exception as e:
        bt = None
        print("BT fail", sym, e)
    payload = {
        "id": s.get("id"), "name": s.get("name"), "symbol": sym,
        "start_date": cfg["start_date"], "end_date": cfg["end_date"],
        "timeframe": s.get("timeframe") or "5m",
        "description": s.get("description") or "",
        "indicators": cfg["indicators"],
        "entry_conditions": cfg["entry_conditions"],
        "exit_conditions": cfg["exit_conditions"],
        "legs": legs, "advanced_options": adv, "risk_management": rm,
        "status": s.get("status") or "active",
    }
    if bt:
        payload["last_backtest"] = bt
    out = post("/api/strategies/save", payload)
    print("  saved:", json.dumps(out, default=str)[:220])
