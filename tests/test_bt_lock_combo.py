"""Backtest hang guards: single-flight lock reaper + Combo RVP gate fires.
"""
import os
import sys
import datetime
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))


def test_bt_lock_reaper_and_gen():
    """Atka lock 8min baad tootata hai; purana worker naya lock nahi kholta."""
    import routes.strategy_builder as sb
    # reset to known state
    sb._BT_ACTIVE["n"] = 0
    sb._BT_ACTIVE["gen"] = 0
    sb._BT_ACTIVE["since"] = 0.0
    g1 = sb._bt_acquire()
    assert g1, "pehla acquire mile"
    assert sb._bt_acquire() == 0, "dusra turant busy ho"
    # stale banao (9 min purana) -> naya acquire lock tod de
    import time as _t
    sb._BT_ACTIVE["since"] = _t.time() - 540
    g2 = sb._bt_acquire()
    assert g2 and g2 != g1, (g1, g2)
    # purane gen se release -> naya lock untouched
    sb._bt_release(g1)
    assert sb._BT_ACTIVE["n"] == 1, sb._BT_ACTIVE
    sb._bt_release(g2)
    assert sb._BT_ACTIVE["n"] == 0, sb._BT_ACTIVE
    sb._BT_ACTIVE["since"] = 0.0


def _spiky_bars(n=120, seed=3):
    rnd = random.Random(seed)
    bars = []
    px = 25000.0
    d = datetime.date(2026, 6, 1)
    plan = ([0.006] * 15 + [-0.007] * 8 + [0.008] * 15 + [-0.004] * 10) * 3
    k = i = 0
    while len(bars) < n:
        if d.weekday() < 5:
            drift = plan[k % len(plan)] + rnd.uniform(-0.004, 0.004)
            k += 1
            o = px
            c = max(100.0, px * (1 + drift))
            bars.append({"symbol": "NIFTY", "trade_date": d.strftime("%Y-%m-%d"),
                         "open_price": round(o, 2), "high_price": round(max(o, c) * 1.004, 2),
                         "low_price": round(min(o, c) * 0.996, 2), "close_price": round(c, 2),
                         "volume": 2000000 if (i % 10 == 5) else 500000, "oi": 0})
            px = c
            i += 1
        d += datetime.timedelta(days=1)
    return bars


def test_combo_rvp_gate_fires_on_ignition():
    """Ignition window me Combo RVP buy deta hai (gate dead nahi)."""
    from core.services.indicator_engine import IndicatorEngine
    bars = _spiky_bars()
    closes = [b["close_price"] for b in bars]
    out = IndicatorEngine().calculate_rsi_vwap_pcr_combo(
        bars, closes,
        {"rsi_period": 14, "rsi_entry": 70, "rsi_exit": 55, "rsi_oversold": 30,
         "st_period": 10, "st_mult": 3.0, "vwap_period": 20, "pcr_min": 1.0,
         "pcr_bear_max": 0.8, "vol_mult": 1.5, "vol_period": 20,
         "fresh_bars": 2, "vol_confirm_bars": 4})
    assert sum(out["buy"]) >= 1, "ignition par kam-se-kam 1 buy"


def test_combo_end_to_end_trades():
    """Engine-run Combo RVP se trades bante hain (full path, no hang)."""
    from core.services.backtest_engine import BacktestEngine
    bars = _spiky_bars()
    legs = [{"option_type": "CE", "transaction": "buy", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly"}]
    inds = [{"id": "combo_rsi_vwap_pcr",
             "params": {"rsi_period": 14, "rsi_entry": 70, "rsi_exit": 55,
                        "st_period": 10, "st_mult": 3.0, "vwap_period": 20,
                        "pcr_min": 1.0, "vol_mult": 1.5}}]
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    import time as _t
    _t0 = _t.time()
    res = eng.run(list(bars), "NIFTY", bars[0]["trade_date"], bars[-1]["trade_date"],
                  inds, [], [], legs,
                  {"trade_mode": "intraday", "timeframe": "1d", "expiry": "weekly"},
                  {"max_trades_per_day": 5, "daily_stop_loss": 1500,
                   "daily_take_profit": 3000}, is_live=False)
    assert _t.time() - _t0 < 60, "combo calc hang nahi hona chahiye"
    assert not res.get("error"), res.get("error")
    assert (res.get("metrics") or {}).get("total_trades", 0) >= 1
