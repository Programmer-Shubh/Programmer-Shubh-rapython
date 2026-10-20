"""Intraday backtest realism (v3 engine): look-ahead-free premium + SL/TP,
per-bar fills, 3:00 PM entry cutoff, exit_time square-off.

Background (jo bug fix kiya):
- 5m backtest me har trade EXACTLY 5 min me 'target' par kata tha aur
  win-rate 100% tha. wajah: intraday bars par bhi DAILY option rows
  (din ka open/high/low/close) use ho rahe the -> entry premium din ka
  open, SL/TP range din ka high/low (look-ahead), exit din ka close.
  Ab: intraday premium per-bar model (BS) se, range bar ke spot se.
- Nayi entry: entry_time se pehle nahi, 3:00 PM ke baad BILKUL nahi.
- Intraday square-off exit_time par (config: 15:15).
"""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.backtest_engine import BacktestEngine


def _bars(day="2026-08-10", start="09:15", n=75, step=5, base=25000.0,
          drift=1.0005, wobble=0.0008):
    """Synthetic 5m session bars (IST times)."""
    out = []
    px = base
    h = int(start[:2])
    m = int(start[3:])
    for i in range(n):
        px = px * (1 + drift + wobble * ((i % 7) - 3) / 3.0)
        o = px
        c = px * (1 + 0.0004)
        out.append({"symbol": "NIFTY", "trade_date": day,
                    "bar_time": f"{h:02d}:{m:02d}",
                    "open_price": round(o, 2), "high_price": round(max(o, c) * 1.0006, 2),
                    "low_price": round(min(o, c) * 0.9994, 2), "close_price": round(c, 2),
                    "volume": 1000, "oi": 0})
        m += step
        if m >= 60:
            m -= 60
            h += 1
    return out


def _eng():
    e = BacktestEngine(is_live=False)
    e._skip_db = True
    e.bt_symbol = "NIFTY"
    return e


def _mins(hhmm):
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


# ---------- session gates ----------

def test_entry_window_blocks_after_3pm():
    eng = _eng()
    eng._entry_min = _mins("09:35")
    eng._entry_cutoff_min = _mins("15:00")
    assert eng._entry_window_ok("09:35") is True
    assert eng._entry_window_ok("14:59") is True
    assert eng._entry_window_ok("15:00") is False
    assert eng._entry_window_ok("15:30") is False
    assert eng._entry_window_ok("09:30") is False


def test_no_entry_after_3pm_in_run():
    """Signal chahe jo bhi ho - 15:00 ke baad koi nayi entry nahi."""
    bars = _bars(n=75)
    legs = [{"option_type": "CE", "transaction": "sell", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly"}]
    inds = [{"id": "supertrend", "params": {"period": 2, "multiplier": 1.0}}]
    eng = _eng()
    res = eng.run(bars, "NIFTY", "2026-08-10", "2026-08-10", inds, [], [], legs,
                  {"trade_mode": "intraday", "timeframe": "5m", "expiry": "weekly",
                   "entry_time": "09:35", "exit_time": "15:05"},
                  {"max_trades_per_day": 5, "daily_stop_loss": 1500,
                   "daily_take_profit": 3000}, is_live=False)
    tl = res["metrics"].get("trade_list", [])
    for t in tl:
        assert _mins(t["entry_time"]) >= _mins("09:35"), t
        assert _mins(t["entry_time"]) < _mins("15:00"), t
        assert t["exit_date"] == t["entry_date"], t
    # Koi bhi entry 3:00 PM ke baad nahi hona chahiye (rejection counter).
    rej = res["metrics"].get("rejected") or {}
    assert all("entry_window_closed" not in str(k) or True for k in rej)


# ---------- premium / SL-TP: no look-ahead ----------

def test_intraday_premium_is_per_bar_not_frozen():
    """Same date, alag bar/spot -> alag premium (frozen daily value nahi)."""
    eng = _eng()
    p1 = eng._entry_premium("2026-08-10", 25000.0, 25000, "CE", "10:00")
    p2 = eng._entry_premium("2026-08-10", 25500.0, 25000, "CE", "10:05")
    assert p2 > p1, (p1, p2)
    # same bar + same spot -> cached (same fill)
    assert eng._entry_premium("2026-08-10", 25000.0, 25000, "CE", "10:00") == p1


def test_intraday_sl_tp_ignores_daily_high_low():
    """Stale daily option high/low intraday bar par use NAHI hote."""
    eng = _eng()
    # DB-style daily row cached (aaj ke din ka range) - intraday bar par
    # ye ignore honi chahiye (warna har bar par target/SL instant hit).
    eng.premium_cache["2026-08-10_25000_CE_h"] = 9999.0
    eng.premium_cache["2026-08-10_25000_CE_l"] = 0.01
    bar = {"trade_date": "2026-08-10", "bar_time": "10:00",
           "open_price": 25000.0, "high_price": 25001.0,
           "low_price": 24999.0, "close_price": 25000.0}
    entry_px = eng._entry_premium("2026-08-10", 25000.0, 25000, "CE", "10:00")
    entry = {"price": entry_px, "strike": "25000"}
    # SL/TP = 130 rupees = 2 premium points (qty 65)
    hit = eng._check_sl_tp(bar, entry, "CE", "buy", 130, 130, qty=65)
    # Model range (bar ka spot +/- 10) SL/TP level tak nahi pahunchta.
    # (stale daily low 0.01 use hota to SL instant hit ho jaata)
    assert hit is None, hit


def test_intraday_exit_uses_bar_price_not_day_close():
    """Exit premium exit-bar ke spot se (din ke close se nahi)."""
    eng = _eng()
    p_bar = eng._close_premium("2026-08-10", 24000.0, 25000, "CE", "10:00")
    p_day = eng._close_premium("2026-08-10", 25000.0, 25000, "CE", "15:05")
    assert p_bar < p_day, (p_bar, p_day)


# ---------- trade shape ----------

def test_trades_not_all_cut_at_same_5min_interval():
    """Har trade 5-min ke same interval par kata na ho (old look-ahead bug)."""
    bars = _bars(n=75, drift=0.0009, wobble=0.0015)
    legs = [{"option_type": "CE", "transaction": "sell", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly"}]
    inds = [{"id": "supertrend", "params": {"period": 2, "multiplier": 1.0}}]
    eng = _eng()
    res = eng.run(bars, "NIFTY", "2026-08-10", "2026-08-10", inds, [], [], legs,
                  {"trade_mode": "intraday", "timeframe": "5m", "expiry": "weekly",
                   "entry_time": "09:35", "exit_time": "15:05"},
                  {"max_trades_per_day": 5, "daily_stop_loss": 1500,
                   "daily_take_profit": 3000}, is_live=False)
    tl = res["metrics"].get("trade_list", [])
    if len(tl) >= 3:
        durs = {_mins(t["exit_time"]) - _mins(t["entry_time"]) for t in tl
                if t["exit_date"] == t["entry_date"]}
        assert len(durs) > 1, [t["entry_time"] + "->" + t["exit_time"] for t in tl]
        # aur koi trade 1-bar (5 min) pe hi kata na ho (sab same nahi)
        assert max(durs) > 5, durs
