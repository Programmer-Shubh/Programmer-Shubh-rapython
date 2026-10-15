"""MCX Phase 1: registry/lots/sessions, FUT plumbing, exchange-aware exits."""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

UID = 99995


def test_registry_lots_sessions():
    import utils.helpers as H
    assert H.get_lot_size("CRUDEOIL") == 100
    assert H.get_lot_size("GOLD") == 100
    assert H.get_lot_size("GOLDM") == 10
    assert H.get_lot_size("SILVER") == 30
    assert H.get_lot_size("SILVERM") == 5
    assert H.get_lot_size("NATURALGAS") == 1250
    assert H.get_lot_size("COPPER") == 2500
    assert H.get_lot_size("NICKEL") == 1500
    assert H.get_lot_size("ZINC") == 5000
    assert H.is_mcx("CRUDEOIL") and not H.is_mcx("NIFTY")
    assert H.exchange_of("GOLD") == "MCX" and H.exchange_of("NIFTY") == "NSE"
    lot, src, ok = H.official_lot("CRUDEOIL")
    assert (lot, ok) == (100, True) and src == "mcx-static", (lot, src, ok)
    lot2, src2, ok2 = H.official_lot("TOTALLYUNKNOWN")
    assert ok2 is False and src2 == "default"
    se = H.session_of("CRUDEOIL")
    assert se["entry_cutoff_min"] == 23 * 60 and se["eod_min"] == 23 * 60 + 20
    se2 = H.session_of("NIFTY")
    assert se2["entry_cutoff_min"] == 15 * 60 + 15 and se2["eod_min"] == 15 * 60 + 20


def test_mcx_gates_and_ctt(monkeypatch):
    import core.services.sl_monitor as slm
    monkeypatch.setattr(slm, "market_open_ist", lambda *_a, **_k: True)
    _eve = datetime.datetime(2026, 10, 14, 20, 0)
    assert slm.entry_open_ist(_eve, symbol="CRUDEOIL") is True
    assert slm.entry_open_ist(_eve, symbol="NIFTY") is False
    assert "11:00" in slm.entry_block_reason("CRUDEOIL") or "23:00" in slm.entry_block_reason("CRUDEOIL")
    from core.services.transaction_costs import TransactionCosts
    c = TransactionCosts.calculate(100000.0, True, exchange="MCX")
    assert abs(c["stt"] - 10.0) < 0.01, c  # CTT 0.01% sell-side
    c2 = TransactionCosts.calculate(100000.0, True, exchange="NSE")
    assert abs(c2["stt"] - 100.0) < 0.01, c2  # STT 0.1% (unchanged)


def test_mcx_ltp_conversion_mocked(monkeypatch):
    import core.services.free_data as fd
    import core.services.mcx_feed as mf

    def _fake(sym, timeout=8):
        return {"spot": {"CL=F": 70.0, "GC=F": 2500.0, "USDINR=X": 90.0}.get(sym, 0)}
    monkeypatch.setattr(fd, "_yahoo_fallback_quote", _fake)
    mf._FX_CACHE["v"] = 0.0
    assert mf.get_mcx_ltp("CRUDEOIL") == 6300.0  # 70*90
    assert mf.get_mcx_ltp("GOLD") == round(2500.0 / 31.1035 * 10.0 * 90.0, 2)
    assert mf.get_mcx_ltp("NICKEL") is None  # no proxy -> honest None
    bars, src = mf.fetch_mcx_history("CRUDEOIL", "2026-09-01", "2026-09-05")
    assert isinstance(bars, list) and src in ("proxy", "none")


def test_fut_backtest_end_to_end():
    """MCX-style futures series par FUT leg: entry/exit/SL sab price par."""
    import random
    from core.services.backtest_engine import BacktestEngine
    rnd = random.Random(11)
    bars, px = [], 5000.0
    d = datetime.date(2026, 9, 1)
    plan = ([0.004] * 12 + [-0.005] * 8 + [0.006] * 12) * 4
    k = 0
    while len(bars) < 90:
        if d.weekday() < 5:
            drift = plan[k % len(plan)] + rnd.uniform(-0.003, 0.003)
            k += 1
            o = px
            c = max(100.0, px * (1 + drift))
            bars.append({"symbol": "CRUDEOIL", "trade_date": d.strftime("%Y-%m-%d"),
                         "open_price": round(o, 2), "high_price": round(max(o, c) * 1.002, 2),
                         "low_price": round(min(o, c) * 0.998, 2), "close_price": round(c, 2),
                         "volume": 200000 if (k % 9 == 4) else 50000, "oi": 0})
            px = c
        d += datetime.timedelta(days=1)
    legs = [{"option_type": "FUT", "transaction": "buy", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly"}]
    inds = [{"id": "supertrend", "params": {"period": 5, "multiplier": 2.0}}]
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    res = eng.run(list(bars), "CRUDEOIL", bars[0]["trade_date"], bars[-1]["trade_date"],
                  inds, [], [], legs,
                  {"trade_mode": "intraday", "timeframe": "1d", "expiry": "weekly"},
                  {"max_trades_per_day": 5, "daily_stop_loss": 1500,
                   "daily_take_profit": 3000}, is_live=False)
    assert not res.get("error"), res.get("error")
    m = res.get("metrics", {})
    assert m.get("total_trades", 0) >= 1, m
    t0 = (m.get("trade_list") or [])[0]
    assert t0.get("quantity") == 100, t0  # 1 lot x 100 bbl


def test_fut_reaches_pricing_not_type_error(monkeypatch):
    """Chain FUT order type-gate paar karke price par fail ho (live down)."""
    import core.services.sl_monitor as _sl
    monkeypatch.setattr(_sl, "market_open_ist", lambda *_a, **_k: True)
    monkeypatch.setattr(_sl, "entry_open_ist", lambda *_a, **_k: True)
    from routes.option_chain import TradeRequest, place_trade
    r = place_trade(TradeRequest(
        symbol="CRUDEOIL", option_type="FUT", strike=0.0,
        transaction_type="BUY", quantity=1, stop_loss=1500.0,
        take_profit=3000.0, date="2026-10-14", expiry="2026-11-19"))
    assert "must be CE/PE" not in str(r.get("error", "")), r
    assert "error" in r, r  # no live feed here -> honest error


def _today():
    import datetime as _dt
    return (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def test_mcx_strategy_exit_at_night():
    """MCX intraday trade exit_time 23:05 par night me exit; 22:00 par khula."""
    import json
    from core.models.trade_model import TradeModel
    tm = TradeModel()
    tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
    tm.db.execute("DELETE FROM strategies WHERE user_id=? AND name='t-mcx-exit'", [UID])
    try:
        sid = tm.db.execute(
            "INSERT INTO strategies (user_id, name, symbol, advanced_options,"
            " risk_management, status) VALUES (?,?,?,?,?,?)",
            [UID, "t-mcx-exit", "CRUDEOIL",
             json.dumps({"trade_mode": "intraday", "exit_time": "23:05"}),
             json.dumps({"daily_stop_loss": 1500, "daily_take_profit": 3000}),
             "active"])
        tid = tm.db.execute(
            "INSERT INTO paper_trades (user_id, strategy_id, symbol, option_type,"
            " strike_price, transaction_type, quantity, lot_size, entry_price,"
            " stop_loss, target, total_cost, entry_date, trade_type, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [UID, int(sid), "CRUDEOIL", "FUT", 0, "BUY", 1, 100, 6300.0,
             1500, 3000, 0.0, _today(), "intraday", "open"])
        assert tm.close_strategy_exit_time_trades(user_id=UID, now_hhmm="22:00") == []
        hit = tm.close_strategy_exit_time_trades(user_id=UID, now_hhmm="23:10")
        assert [h["id"] for h in hit] == [int(tid)], hit
        assert hit[0]["reason"] == "strategy_exit"
    finally:
        tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
        tm.db.execute("DELETE FROM strategies WHERE user_id=? AND name='t-mcx-exit'", [UID])


def test_mcx_strategy_exit_default_2300():
    """exit_time missing ho to MCX default 23:00 (NSE 15:15 nahi)."""
    import json
    from core.models.trade_model import TradeModel
    tm = TradeModel()
    tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
    tm.db.execute("DELETE FROM strategies WHERE user_id=? AND name='t-mcx-exit2'", [UID])
    try:
        sid = tm.db.execute(
            "INSERT INTO strategies (user_id, name, symbol, advanced_options,"
            " risk_management, status) VALUES (?,?,?,?,?,?)",
            [UID, "t-mcx-exit2", "GOLD",
             json.dumps({"trade_mode": "intraday"}),
             json.dumps({"daily_stop_loss": 1500, "daily_take_profit": 3000}),
             "active"])
        tid = tm.db.execute(
            "INSERT INTO paper_trades (user_id, strategy_id, symbol, option_type,"
            " strike_price, transaction_type, quantity, lot_size, entry_price,"
            " stop_loss, target, total_cost, entry_date, trade_type, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [UID, int(sid), "GOLD", "FUT", 0, "BUY", 1, 100, 150000.0,
             1500, 3000, 0.0, _today(), "intraday", "open"])
        assert tm.close_strategy_exit_time_trades(user_id=UID, now_hhmm="22:30") == []
        hit = tm.close_strategy_exit_time_trades(user_id=UID, now_hhmm="23:05")
        assert [h["id"] for h in hit] == [int(tid)], hit
    finally:
        tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
        tm.db.execute("DELETE FROM strategies WHERE user_id=? AND name='t-mcx-exit2'", [UID])


def test_eod_per_exchange():
    """16:00 par NIFTY exit, CRUDEOIL khula; 23:25 par crude bhi exit."""
    from core.models.trade_model import TradeModel
    tm = TradeModel()
    tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
    try:
        ids = {}
        for sym in ("NIFTY", "CRUDEOIL"):
            ids[sym] = tm.db.execute(
                "INSERT INTO paper_trades (user_id, symbol, option_type, strike_price,"
                " transaction_type, quantity, lot_size, entry_price, stop_loss, target,"
                " total_cost, entry_date, trade_type, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [UID, sym, "FUT" if sym != "NIFTY" else "CE",
                 0 if sym != "NIFTY" else 25000, "BUY", 1,
                 100 if sym != "NIFTY" else 65, 5000.0 if sym != "NIFTY" else 100.0,
                 1500, 3000, 0.0, _today(), "intraday", "open"])
        hit = tm.close_intraday_trades(user_id=UID,
                                       _now=datetime.datetime(2026, 10, 14, 16, 0))
        assert [h["id"] for h in hit] == [int(ids["NIFTY"])], hit
        st = tm.db.fetch_one("SELECT status FROM paper_trades WHERE id=?",
                             [int(ids["CRUDEOIL"])])["status"]
        assert st == "open", st
        hit2 = tm.close_intraday_trades(user_id=UID,
                                        _now=datetime.datetime(2026, 10, 14, 23, 25))
        assert int(ids["CRUDEOIL"]) in [h["id"] for h in hit2], hit2
    finally:
        tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
