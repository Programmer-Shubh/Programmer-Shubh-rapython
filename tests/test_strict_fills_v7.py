"""v7 STRICT FILL tests - 80% backtest vs 30% paper gap ka core fix.

Rule (v7): target (TP) sirf tab book hota hai jab COMPLETED candle ka CLOSE
level ke paar ho. Candle ka high/low (wick) level ko touch kare to target
NAHI book hota - real paper monitor bhi bad-tick/spike touch par exit nahi
karta. SL waisa hi rehta hai: worst-case extreme par fill (koi optimism nahi).

`slippage/exit latency` paper parity ke liye default 0.0 hai; explicit
`advanced.latency_slippage_exit_pct` set karne par hi lagta hai.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.backtest_engine import BacktestEngine, ENGINE_VERSION


def _eng():
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    eng.bt_symbol = "NIFTY"
    eng._advanced = {"timeframe": "1d"}
    return eng


# ---------------------------------------------------------------- unit: confirm
def test_engine_version_is_v7():
    assert ENGINE_VERSION == "v7", ENGINE_VERSION


def test_tp_wick_touch_is_not_booked():
    """High target cross kare par CLOSE neeche -> koi target nahi (strict)."""
    eng = _eng()
    hit = {"reason": "target", "level": 110.0, "fill": 110.0}
    out = eng._confirm_tp(hit, "buy", 110.0, opt_close=104.0)
    assert out is None
    assert eng._n_tp_unconfirmed == 1
    assert eng._n_tp_confirmed == 0


def test_tp_close_through_is_booked():
    eng = _eng()
    hit = {"reason": "target", "level": 110.0, "fill": 110.0}
    out = eng._confirm_tp(hit, "buy", 110.0, opt_close=112.5)
    assert out is hit
    assert eng._n_tp_confirmed == 1


def test_sell_tp_needs_close_below_level():
    eng = _eng()
    hit = {"reason": "target", "level": 90.0, "fill": 90.0}
    # wick neeche gaya par close 92 -> book nahi
    assert eng._confirm_tp(hit, "sell", 90.0, opt_close=92.0) is None
    # close 88 -> book
    assert eng._confirm_tp(hit, "sell", 90.0, opt_close=88.0) is hit


def test_sl_is_never_softened_by_confirmation():
    """SL extreme par hi fill hota hai - confirmation sirf TP par lagti hai."""
    eng = _eng()
    hit = {"reason": "stoploss", "level": 80.0, "fill": 74.0}
    assert eng._confirm_tp(hit, "buy", 110.0, opt_close=95.0) is hit


def test_touch_mode_restores_legacy_behaviour():
    eng = _eng()
    eng._tp_fill_mode = "touch"
    hit = {"reason": "target", "level": 110.0, "fill": 110.0}
    assert eng._confirm_tp(hit, "buy", 110.0, opt_close=90.0) is hit


# ------------------------------------------------------- integration: daily bars
def test_daily_bar_tp_ignored_when_option_close_below_level():
    """Daily DB print: day ka high TP cross kare, close nahi -> no target."""
    eng = _eng()
    eng._cur_bar_time = ""            # daily bar
    eng.bt_expiry = "2026-06-30"
    d = "2026-06-02"
    strike = 25000.0
    # whole-session option prints: open 100, high 120 (wick), low 90, close 95
    for k, v in (("e", 100.0), ("h", 120.0), ("l", 90.0), ("c", 95.0)):
        eng.premium_cache[f"{d}_{strike}_{'CE'}_{k}"] = v
    cur = {"trade_date": d, "open_price": 25000.0, "high_price": 25120.0,
           "low_price": 24900.0, "close_price": 25040.0}
    entry = {"date": d, "strike": str(strike), "price": 100.0, "quantity": 75,
             "type": "buy", "legs": [{"option_type": "CE"}]}
    # TP = entry + 10 pts (Rs 750 / 75) = 110 -> high 120 ne cross kiya
    out = eng._check_sl_tp(cur, entry, "CE", "buy", 0, 750, 75, entry_idx=0)
    assert out is None, out
    assert eng._n_tp_unconfirmed == 1

    # ab close bhi level ke paar -> target booked
    eng.premium_cache[f"{d}_{strike}_{'CE'}_c"] = 115.0
    out2 = eng._check_sl_tp(cur, entry, "CE", "buy", 0, 750, 75, entry_idx=0)
    assert out2 and out2["reason"] == "target", out2
    assert out2["fill"] == 110.0


def test_daily_bar_sl_still_fills_at_worst_extreme():
    eng = _eng()
    eng._cur_bar_time = ""
    eng.bt_expiry = "2026-06-30"
    d = "2026-06-03"
    strike = 25000.0
    for k, v in (("e", 100.0), ("h", 102.0), ("l", 80.0), ("c", 99.0)):
        eng.premium_cache[f"{d}_{strike}_{'CE'}_{k}"] = v
    cur = {"trade_date": d, "open_price": 25000.0, "high_price": 25010.0,
           "low_price": 24600.0, "close_price": 24990.0}
    entry = {"date": d, "strike": str(strike), "price": 100.0, "quantity": 75,
             "type": "buy", "legs": [{"option_type": "CE"}]}
    # SL = entry - 10 pts = 90 -> low 80 breach -> fill 80 (worst case)
    out = eng._check_sl_tp(cur, entry, "CE", "buy", 750, 0, 75, entry_idx=0)
    assert out and out["reason"] == "stoploss", out
    assert out["fill"] == 80.0, out


# ------------------------------------------------------------- exit latency knob
def test_exit_latency_is_off_by_default_for_paper_parity():
    eng = _eng()
    assert eng._latency_pct_exit == 0.0
    entries, exits = [], []
    entries.append({"date": "2026-06-02", "strike": "25000", "price": 100.0,
                    "quantity": 75, "costs": {"total": 0.0},
                    "total_cost": 7500.0, "type": "buy", "time": "09:35",
                    "legs": [{"option_type": "CE", "strike": "25000",
                              "type": "buy", "lots": 1}]})
    eng._close_position(entries, exits, entries[0], 110.0, "intraday",
                        "2026-06-02", 75, "buy")
    assert exits[0]["price"] == 110.0, exits[0]
    assert eng._n_exit_latency == 0


def test_exit_latency_can_be_enabled_explicitly():
    eng = _eng()
    eng._latency_pct_exit = 0.5
    entries, exits = [], []
    entries.append({"date": "2026-06-02", "strike": "25000", "price": 100.0,
                    "quantity": 75, "costs": {"total": 0.0},
                    "total_cost": 7500.0, "type": "buy", "time": "09:35",
                    "legs": [{"option_type": "CE", "strike": "25000",
                              "type": "buy", "lots": 1}]})
    eng._close_position(entries, exits, entries[0], 110.0, "intraday",
                        "2026-06-02", 75, "buy")
    assert exits[0]["price"] < 110.0, exits[0]
    assert eng._n_exit_latency == 1
