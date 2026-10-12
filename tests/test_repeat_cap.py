"""max_trades_per_day: same-day repeats allowed up to cap, cap blocks after."""
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


def test_repeat_allowed_until_cap(monkeypatch):
    sid = _seed_strategy(5)
    try:
        today = _ist()
        _seed_closed(sid, 1, today)  # 1 closed today, none open
        calls = []
        monkeypatch.setattr(ap, "_today_signal", lambda a, b, c: (True, "t"))
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


def test_cap_blocks_at_five(monkeypatch):
    sid = _seed_strategy(5)
    try:
        today = _ist()
        _seed_closed(sid, 5, today)  # cap reached
        calls = []
        monkeypatch.setattr(ap, "_today_signal", lambda a, b, c: (True, "t"))
        monkeypatch.setattr(
            ap, "_execute_legs",
            lambda s, legs, db, tm, today: (calls.append(1) or ([{"trade_id": 1}], [])))
        out = ap.place_for_strategy(sid, signal_check="daily")
        assert not calls, f"entered beyond cap: {out}"
        assert "max" in " ".join(out.get("notes", [])).lower()
    finally:
        db = Database.get_instance()
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        db.execute("DELETE FROM strategies WHERE id=?", [sid])


def test_open_position_blocks(monkeypatch):
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
        monkeypatch.setattr(ap, "_today_signal", lambda a, b, c: (True, "t"))
        monkeypatch.setattr(
            ap, "_execute_legs",
            lambda s, legs, db, tm, today: (calls.append(1) or ([{"trade_id": 1}], [])))
        out = ap.place_for_strategy(sid, signal_check="daily")
        assert not calls, f"entered over open position: {out}"
    finally:
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        db.execute("DELETE FROM strategies WHERE id=?", [sid])
