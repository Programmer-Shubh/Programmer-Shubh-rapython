"""Strategy exit_time EOD exit (b) + SL/TP skip-reason logging (c).

Sandbox: user_id=99999 rows only (real dev DB untouched otherwise),
deleted in finally. Follows tests/test_bt_paper_parity.py pattern.
"""
import os
import sys
import json
import importlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

UID = 99999


def _tm():
    from core.models.trade_model import TradeModel
    return TradeModel()


def _mk_strategy(tm, exit_time="15:05", mode="intraday"):
    adv = json.dumps({"trade_mode": mode, "exit_time": exit_time})
    return tm.db.execute(
        "INSERT INTO strategies (user_id, name, symbol, advanced_options,"
        " risk_management, status) VALUES (?,?,?,?,?,?)",
        [UID, "exit-test", "NIFTY", adv, "{}", "active"])


def _mk_trade(tm, sid, trade_type="intraday", entry_date="2026-10-13"):
    return tm.db.execute(
        "INSERT INTO paper_trades (user_id, strategy_id, symbol, option_type,"
        " strike_price, transaction_type, quantity, lot_size, entry_price,"
        " stop_loss, target, total_cost, entry_date, trade_type, status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [UID, sid, "NIFTY", "CE", 25000, "BUY", 1, 75, 100.0,
         1500, 3000, 0.0, entry_date, trade_type, "open"])


def _cleanup(tm):
    try:
        tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
    except Exception:
        pass
    try:
        tm.db.execute("DELETE FROM strategies WHERE user_id=?", [UID])
    except Exception:
        pass


def test_strategy_exit_closes_after_exit_time(monkeypatch):
    """exit_time (15:05) guzar chuka + market band (19:00) -> EOD exit."""
    from core.models.trade_model import TradeModel
    monkeypatch.setattr(TradeModel, "get_option_premium", lambda *a, **k: None)
    tm = _tm()
    _cleanup(tm)
    try:
        sid = _mk_strategy(tm)
        tid = _mk_trade(tm, sid)
        hit = tm.close_strategy_exit_time_trades(user_id=UID, now_hhmm="19:00")
        assert len(hit) == 1 and hit[0]["id"] == int(tid)
        assert hit[0]["reason"] == "strategy_exit"
        row = tm.db.fetch_one("SELECT status, exit_status FROM paper_trades WHERE id=?",
                              [int(tid)])
        assert row["status"] == "closed" and row["exit_status"] == "strategy_exit"
    finally:
        _cleanup(tm)


def test_strategy_exit_skips_positional_and_early():
    """Positional carry + exit time se pehle -> koi exit nahi."""
    from core.models.trade_model import TradeModel
    tm = _tm()
    _cleanup(tm)
    try:
        sid = _mk_strategy(tm)
        _today = tm._ist_today()
        tid_pos = _mk_trade(tm, sid, trade_type="positional", entry_date=_today)
        tid_early = _mk_trade(tm, sid, entry_date=_today)
        # exit time se pehle: dono khule rehne chahiye
        hit = tm.close_strategy_exit_time_trades(user_id=UID, now_hhmm="14:00")
        assert hit == []
        # exit time ke baad bhi positional carry hota hai, sirf intraday kat-ta hai
        hit = tm.close_strategy_exit_time_trades(user_id=UID, now_hhmm="15:10")
        assert [h["id"] for h in hit] == [int(tid_early)]
        row = tm.db.fetch_one("SELECT status FROM paper_trades WHERE id=?",
                              [int(tid_pos)])
        assert row["status"] == "open"
    finally:
        _cleanup(tm)


def _load_auto_trade():
    try:
        return importlib.import_module("scripts.auto_trade")
    except Exception:
        import importlib.util as _ilu
        base = os.path.join(os.path.dirname(__file__), "..", "scripts", "auto_trade.py")
        spec = _ilu.spec_from_file_location("ratrade_auto_trade_test", os.path.abspath(base))
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def test_sltp_skip_logged_with_reason(monkeypatch):
    """SL=0/Target=0 trade skip ho to reason logged (silent continue nahi)."""
    from core.models.trade_model import TradeModel
    fake = {"id": 4242, "symbol": "NIFTY", "stop_loss": 0, "target": 0,
            "option_type": "CE", "strike_price": 25000, "expiry_date": "",
            "transaction_type": "BUY", "quantity": 1, "lot_size": 75,
            "entry_price": 100.0}
    monkeypatch.setattr(TradeModel, "get_open_trades", lambda self, user_id=1: [dict(fake)])
    mod = _load_auto_trade()
    res = mod.check_stoploss_target(confirm_secs=0)
    assert isinstance(res, dict), res
    assert res["closed"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["id"] == 4242
    assert "0" in res["skipped"][0]["reason"]
    assert mod.LAST_SCAN["skipped"] == res["skipped"]
