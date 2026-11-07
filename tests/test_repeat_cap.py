"""Daily trade-count policy: repeats always allowed (cap removed 2026-11)."""
import os
import sys
import json
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

import core.services.auto_paper as ap
from core.models.database import Database


def _ist():
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _seed_strategy(max_day):
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE name='t-repeat-cap'")
    db.execute(
        "INSERT INTO strategies (user_id, name, symbol, legs, risk_management,"
        " status) VALUES (1,'t-repeat-cap','NIFTY',?,?,?)",
        [json.dumps([{"option_type": "CE", "transaction": "buy", "lots": 1}]),
         json.dumps({"daily_stop_loss": 1500, "daily_take_profit": 3000,
                     "max_trades_per_day": max_day}),
         "active"])
    row = db.fetch_one("SELECT id FROM strategies WHERE name='t-repeat-cap'")
    return int(row["id"])


def _seed_closed(sid, n, today):
    db = Database.get_instance()
    db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
    for _ in range(n):
        db.execute(
            "INSERT INTO paper_trades (symbol, option_type, strike_price,"
            " transaction_type, quantity, lot_size, entry_price, exit_price,"
            " entry_date, exit_date, pnl, status, strategy_id) VALUES "
            "('NIFTY','CE',25000,'BUY',1,75,100.0,90.0,?,?,-750.0,'closed',?)",
            [today, today, sid])


def _entry_open(monkeypatch):
    import core.services.sl_monitor as _slm
    monkeypatch.setattr(_slm, "entry_open_ist", lambda *_a, **_k: True)


def test_repeat_allowed_until_cap(monkeypatch):
    _entry_open(monkeypatch)
    sid = _seed_strategy(5)
    try:
        today = _ist()
        _seed_closed(sid, 1, today)  # 1 closed today, none open
        calls = []
        monkeypatch.setattr(ap, "_today_signal", lambda a, b, c, need_more_than=0: (True, "t"))
        monkeypatch.setattr(
            ap, "_execute_legs",
            lambda s, legs, db, tm, today: (calls.append(1) or ([{"trade_id": 1}], [])))
        out = ap.place_for_strategy(sid, signal_check="daily")
        assert calls, f"repeat entry blocked with 1/5 used: {out}"
        assert len(out.get("placed", [])) == 1
    finally:
        db = Database.get_instance()
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        db.execute("DELETE FROM strategies WHERE id=?", [sid])


def test_no_daily_cap_blocks_at_any_count(monkeypatch):
    """User rule (2026-11): daily trade-count cap REMOVED - even after many
    closed trades today a fresh valid signal must still execute."""
    _entry_open(monkeypatch)
    sid = _seed_strategy(5)  # legacy cap value in DB must be ignored
    try:
        today = _ist()
        _seed_closed(sid, 50, today)  # way beyond the old 5/day cap
        calls = []
        monkeypatch.setattr(ap, "_today_signal", lambda a, b, c, need_more_than=0: (True, "t"))
        monkeypatch.setattr(
            ap, "_execute_legs",
            lambda s, legs, db, tm, today: (calls.append(1) or ([{"trade_id": 1}], [])))
        out = ap.place_for_strategy(sid, signal_check="daily")
        assert calls, f"daily cap still blocking after 50 trades: {out}"
        assert len(out.get("placed", [])) == 1
    finally:
        db = Database.get_instance()
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        db.execute("DELETE FROM strategies WHERE id=?", [sid])


def test_open_position_blocks(monkeypatch):
    _entry_open(monkeypatch)
    sid = _seed_strategy(5)
    db = Database.get_instance()
    try:
        today = _ist()
        _seed_closed(sid, 0, today)
        db.execute(
            "INSERT INTO paper_trades (symbol, option_type, strike_price,"
            " transaction_type, quantity, lot_size, entry_price,"
            " entry_date, status, strategy_id) VALUES "
            "('NIFTY','CE',25000,'BUY',1,75,100.0,?,'open',?)",
            [today, sid])
        calls = []
        monkeypatch.setattr(ap, "_today_signal", lambda a, b, c, need_more_than=0: (True, "t"))
        monkeypatch.setattr(
            ap, "_execute_legs",
            lambda s, legs, db, tm, today: (calls.append(1) or ([{"trade_id": 1}], [])))
        out = ap.place_for_strategy(sid, signal_check="daily")
        assert not calls, f"entered over open position: {out}"
    finally:
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        db.execute("DELETE FROM strategies WHERE id=?", [sid])
