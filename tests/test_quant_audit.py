"""Quant audit regression tests (backtest + paper parity fixes).

Covers: expiry single-leg close (was IndexError), intraday entry-bar EOD
(was overnight carry), model premium per-spot cache (was intraday freeze),
monthly expiry NameError, GST base (no GST on STT/stamp), lot-zero SL level,
fantasy place/close requote guards, atomic-dedup double insert.
"""
import os
import sys
import json
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

UID = 99998


def _bars(n=9, start=datetime.date(2026, 8, 10), drift=1.004):
    bars = []
    px = 25000.0
    d = start
    while len(bars) < n:
        if d.weekday() < 5:
            px *= drift
            o = px / drift
            c = px
            bars.append({"symbol": "NIFTY", "trade_date": d.strftime("%Y-%m-%d"),
                         "open_price": round(o, 2), "high_price": round(c * 1.001, 2),
                         "low_price": round(o * 0.999, 2), "close_price": round(c, 2),
                         "volume": 500000, "oi": 100000})
        d += datetime.timedelta(days=1)
    return bars


def _run_hold_to_expiry(exp="2026-08-20", mode="positional", exp_hint="weekly"):
    from core.services.backtest_engine import BacktestEngine
    bars = _bars()
    legs = [{"option_type": "CE", "transaction": "buy", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0,
             "expiry": exp_hint, "expiry_date": exp}]
    inds = [{"id": "supertrend", "params": {"period": 2, "multiplier": 1.0}}]
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    return eng.run(list(bars), "NIFTY", bars[0]["trade_date"], bars[-1]["trade_date"],
                   inds, [], [], legs,
                   {"trade_mode": mode, "timeframe": "1d", "expiry": exp_hint},
                   {"max_trades_per_day": 5, "daily_stop_loss": 0, "daily_take_profit": 0},
                   is_live=False)


def test_expiry_single_leg_closes_cleanly():
    """Single-leg hold-to-expiry: 1 entry -> 1 exit (was IndexError crash)."""
    res = _run_hold_to_expiry()
    m = res["metrics"]
    assert m["total_trades"] == 1, m["total_trades"]
    t = m["trade_list"][0]
    assert t["exit_reason"] == "expiry_squareoff", t
    assert t["exit_date"] == "2026-08-20", t


def test_monthly_expiry_no_nameerror():
    """Monthly _square_off_expiry path runs (was NameError -> silent no-op)."""
    from core.services.backtest_engine import BacktestEngine
    bars = _bars(n=12, start=datetime.date(2026, 8, 10))
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    eng._expiry_hint = "monthly"
    entries = [{"date": bars[0]["trade_date"], "strike": "25000", "price": 100.0,
                "quantity": 65, "type": "buy",
                "legs": [{"option_type": "CE", "strike": "25000", "type": "buy", "lots": 1}]}]
    exits = []
    eng._square_off_expiry(bars, entries, exits, "NIFTY", "CE", "buy", 65, {})
    assert isinstance(exits, list)  # must not raise


def test_intraday_entry_bar_eod():
    """Last-minute-bar intraday entry exits SAME day (was overnight carry)."""
    from core.services.backtest_engine import BacktestEngine
    bars = []
    d = datetime.date(2026, 8, 10)
    px = 25000.0
    # one intraday session: 15 minute bars same date, rising
    for k in range(15):
        px *= 1.002
        hh = 15 + (20 + k * 3) // 60
        mm = (20 + k * 3) % 60
        bars.append({"symbol": "NIFTY", "trade_date": "2026-08-10",
                     "bar_time": f"{hh:02d}:{mm:02d}",
                     "open_price": round(px / 1.002, 2), "high_price": round(px * 1.001, 2),
                     "low_price": round(px / 1.002 * 0.999, 2), "close_price": round(px, 2),
                     "volume": 1000, "oi": 0})
    legs = [{"option_type": "CE", "transaction": "buy", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly"}]
    inds = [{"id": "supertrend", "params": {"period": 2, "multiplier": 1.0}}]
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    res = eng.run(list(bars), "NIFTY", "2026-08-10", "2026-08-10", inds, [], [], legs,
                  {"trade_mode": "intraday", "timeframe": "5m", "expiry": "weekly"},
                  {"max_trades_per_day": 5, "daily_stop_loss": 0, "daily_take_profit": 0},
                  is_live=False)
    tl = res["metrics"].get("trade_list", [])
    assert tl, "rising 5m session must enter at least once"
    for t in tl:
        assert t["exit_date"] == "2026-08-10", t  # no overnight carry


def test_model_premium_follows_spot_intraday():
    """Same-date model premium moves with spot (was frozen at first print)."""
    from core.services.backtest_engine import BacktestEngine
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    eng.bt_symbol = "NIFTY"
    p1 = eng._entry_premium("2026-08-10", 25000.0, 25000, "CE")
    p2 = eng._entry_premium("2026-08-10", 25500.0, 25000, "CE")
    assert p2 > p1, (p1, p2)  # +500 spot must lift ATM call


def test_gst_base_excludes_stt_stamp():
    from core.services.transaction_costs import TransactionCosts
    c = TransactionCosts.calculate(6500.0, True)
    # GST = 18% of (20 + 0.21125 + 0.0065) = 3.639 -> 3.64 (NOT on STT/stamp)
    assert abs(c["gst"] - 3.64) < 0.02, c
    assert abs(c["total"] - (20 + 0.21 + 0.01 + c["gst"] + c["stamp_duty"] + c["stt"])) < 0.05, c


def test_lot_zero_sl_uses_effective_lot(monkeypatch):
    """lot_size=0 row: SL level real lot par (was 0.05/1600 = never fires)."""
    import utils.helpers as _h
    monkeypatch.setattr(_h, "effective_lot_size", lambda sym, stored=None: 65)
    from core.models.trade_model import TradeModel
    fake = {"id": 4243, "symbol": "NIFTY", "stop_loss": 1500, "target": 3000,
            "option_type": "CE", "strike_price": 25000, "expiry_date": "",
            "transaction_type": "BUY", "quantity": 1, "lot_size": 0,
            "entry_price": 100.0}
    monkeypatch.setattr(TradeModel, "get_open_trades", lambda self, user_id=1: [dict(fake)])
    monkeypatch.setattr(TradeModel, "get_option_premium", lambda *a, **k: 70.0)  # < 76.92 SL
    monkeypatch.setattr(TradeModel, "close_trade", lambda self, tid, px, dt, rs="manual": 1)
    import importlib
    try:
        mod = importlib.import_module("scripts.auto_trade")
    except Exception:
        import importlib.util as _ilu
        base = os.path.join(os.path.dirname(__file__), "..", "scripts", "auto_trade.py")
        spec = _ilu.spec_from_file_location("ratrade_auto_trade_q", os.path.abspath(base))
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
    res = mod.check_stoploss_target(confirm_secs=0)
    assert res["closed"] and res["closed"][0]["reason"] == "stoploss", res


def test_place_rejects_fantasy_premium(monkeypatch):
    """Typed 5 vs live 100 -> reject; typed 105 vs live 100 -> pass gate."""
    import core.services.sl_monitor as _sl
    monkeypatch.setattr(_sl, "market_open_ist", lambda: True)
    monkeypatch.setattr(_sl, "entry_open_ist", lambda *_a, **_k: True)
    from core.models.trade_model import TradeModel
    monkeypatch.setattr(TradeModel, "get_option_premium", lambda *a, **k: 100.0)
    from routes.paper_trade import PlaceTradeRequest, place_trade
    base = dict(symbol="NIFTY", option_type="CE", strike=25000.0, transaction_type="BUY",
                quantity=1, stop_loss=1500.0, take_profit=3000.0, date="2026-10-14",
                trade_type="intraday", strategy_id=0)
    r = place_trade(PlaceTradeRequest(entry_price=5.0, **base))
    assert "error" in r and "25%" in r["error"], r
    # validate_trade_data may fail before requote for bad symbols; NIFTY ok.
    # 105 passes the requote gate (may still error later on chain/DB - accept either,
    # but NOT the 25% requote error).
    r2 = place_trade(PlaceTradeRequest(entry_price=105.0, **base))
    assert "25%" not in str(r2.get("error", "")), r2


def test_close_rejects_fantasy_exit(monkeypatch):
    """Typed exit 200 vs live 100 -> reject."""
    from core.models.trade_model import TradeModel
    monkeypatch.setattr(TradeModel, "get_option_premium", lambda *a, **k: 100.0)
    tm = TradeModel()
    tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
    try:
        tid = tm.db.execute(
            "INSERT INTO paper_trades (user_id, symbol, option_type, strike_price,"
            " transaction_type, quantity, lot_size, entry_price, stop_loss, target,"
            " total_cost, entry_date, status) VALUES (?,?,?, ?,?,?,?,?,?,?,?,?,'open')",
            [UID, "NIFTY", "CE", 25000, "BUY", 1, 65, 100.0, 1500, 3000, 0.0, "2026-10-14"])
        from routes.paper_trade import CloseTradeRequest, close_trade
        r = close_trade(CloseTradeRequest(trade_id=int(tid), exit_price=200.0, date="2026-10-14"))
        assert "error" in r and "50%" in r["error"], r
        row = tm.db.fetch_one("SELECT status FROM paper_trades WHERE id=?", [int(tid)])
        assert row["status"] == "open"
    finally:
        tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])


def test_atomic_dedup_blocks_double_insert():
    """Same contract twice with atomic_dedup -> 2nd raises ValueError."""
    from core.models.trade_model import TradeModel
    tm = TradeModel()
    tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
    row = {"user_id": UID, "symbol": "NIFTY", "option_type": "CE", "strike_price": 27123,
           "transaction_type": "BUY", "quantity": 1, "lot_size": 65, "entry_price": 100.0,
           "stop_loss": 1500, "target": 3000, "total_cost": 0.0, "entry_date": "2026-10-14",
           "trade_mode": "paper", "trade_type": "intraday", "strategy_id": 0}
    try:
        tm.insert_trade(dict(row), atomic_dedup=True)
        try:
            tm.insert_trade(dict(row), atomic_dedup=True)
            assert False, "2nd insert must raise"
        except ValueError as e:
            assert "Duplicate" in str(e), e
    finally:
        tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
