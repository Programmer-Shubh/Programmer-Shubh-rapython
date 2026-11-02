"""Backtest-vs-paper intraday execution parity (win-rate gap fix).

Proofs of the four changes that removed the 84-93% vs 40% gap:
1. A clocked (5m/15m) bar NEVER reuses the daily bhavcopy option print.
2. Two intraday bars price differently when the spot moves (no fill freeze).
3. Intraday SL/TP no longer reads the whole daily option high/low range.
4. SL exit fills at the breached extreme (gap-aware), TP at the level.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.backtest_engine import BacktestEngine


def _engine():
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    eng.bt_symbol = "NIFTY"
    eng.bt_expiry = "2026-11-12"
    eng.premium_cache = {
        "2026-11-02_25000_CE_e": 500.0,
        "2026-11-02_25000_CE_c": 480.0,
        "2026-11-02_25000_CE_h": 800.0,
        "2026-11-02_25000_CE_l": 200.0,
    }
    return eng


def test_intraday_bar_never_uses_daily_print():
    eng = _engine()
    eng._cur_bar_time = ""
    assert eng._entry_premium("2026-11-02", 25000.0, 25000, "CE") == 500.0
    eng._cur_bar_time = "12:05"
    intra = eng._entry_premium("2026-11-02", 25000.0, 25000, "CE")
    assert intra and intra != 500.0          # not the daily open print
    assert intra != 480.0                    # not the daily close either


def test_intraday_prices_follow_the_bar_spot():
    eng = _engine()
    eng._cur_bar_time = "12:05"
    p1 = eng._entry_premium("2026-11-02", 25000.0, 25000, "CE")
    eng._cur_bar_time = "12:10"
    p2 = eng._entry_premium("2026-11-02", 25120.0, 25000, "CE")
    assert p2 > p1, (p1, p2)                 # CE premium rises with spot


def test_intraday_sltp_ignores_daily_high_low():
    eng = _engine()
    eng._cur_bar_time = "12:05"
    # Entry at the model price of this bar; whole-day range (200..800) holds
    # both TP and SL levels, but a 12:05 bar only knows its own 20-pt range.
    entry = {"price": eng._entry_premium("2026-11-02", 25000.0, 25000, "CE"),
             "strike": 25000, "quantity": 75, "legs": [{"option_type": "CE"}]}
    cur = {"trade_date": "2026-11-02", "open_price": 25000, "high_price": 25020,
           "low_price": 24980, "close_price": 25000, "bar_time": "12:05"}
    # TP level = entry - 20 (sell, 1500 rs / 75 units) is far away inside the
    # daily range but NOT inside this bar's range
    assert eng._check_sl_tp(cur, entry, "CE", "sell", 1500, 1500, 75) is None


def test_sl_fill_is_gap_aware_tp_stays_at_level():
    eng = _engine()
    # SSELL (txn sell): SL above entry. Bar option high 800 breaches SL 300.
    out = eng._check_sl_tp_levels("sell", 300.0, 100.0, 800.0, 100.0)
    assert out["reason"] == "stoploss"
    assert out["level"] == 300.0
    assert out["fill"] == 800.0              # gap through SL fills worse
    # BUY: bar low 50 breaches SL 100 -> fill at 50 (worse), TP no-touch
    out2 = eng._check_sl_tp_levels("buy", 100.0, 0.0, 120.0, 50.0)
    assert out2["reason"] == "stoploss" and out2["fill"] == 50.0
    # SELL TP touch: fill stays at the level (never books a phantom gap bonus)
    out3 = eng._check_sl_tp_levels("sell", 0.0, 100.0, 100.0, 99.0)
    assert out3["reason"] == "target" and out3["fill"] == 100.0


def test_no_double_slippage_on_sell_entry():
    from core.services.transaction_costs import TransactionCosts
    eng = _engine()
    leg = {"option_type": "CE", "transaction": "sell", "lots": 1}
    eng.premium_cache["2026-11-02_25100_CE_e"] = 100.0
    eng.premium_cache["2026-11-02_25100.0_CE_e"] = 100.0
    eng._cur_bar_time = ""                    # daily bar -> the daily print
    trade = eng._enter_single("2026-11-02", 25000.0, "NIFTY", leg, "otm", 0.0, 2)
    once = TransactionCosts.apply_fill_slippage(100.0, "SELL", False,
                                                slippage_pct=0.5)
    assert abs(trade["price"] - round(once, 2)) < 0.02, (trade["price"], once)
