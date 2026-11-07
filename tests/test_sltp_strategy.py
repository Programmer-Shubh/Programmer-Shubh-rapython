"""Per-strategy SL/TP: DB is the single source execution reads.

Covers the user rule (2026-11):
  - POST /api/strategies/{id}/sltp saves % or Rs and mirrors every leg
  - Fyers place_live_order resolves SL/TP from that saved DB row
  - no daily trade-count / max-loss cap survives a strategy save
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.models.database import Database  # noqa: E402
from routes.strategies import (  # noqa: E402
    SltpUpdateRequest, StrategyRequest, save_strategy, update_strategy_sltp)
from routes.broker import _strategy_sltp_from_db  # noqa: E402

TAG = "t-sltp-strategy"


def _seed(rm, legs=None, name=TAG):
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE name=?", [name])
    db.execute(
        "INSERT INTO strategies (user_id, name, symbol, legs, risk_management,"
        " status) VALUES (1,?,?,?,?,?)",
        [name, "NIFTY",
         json.dumps(legs if legs is not None else
                    [{"option_type": "CE", "transaction": "buy", "lots": 1}]),
         json.dumps(rm), "active"])
    return int(db.fetch_one("SELECT id FROM strategies WHERE name=?", [name])["id"])


def _rm(sid):
    row = Database.get_instance().fetch_one(
        "SELECT risk_management, legs FROM strategies WHERE id=?", [sid])
    return json.loads(row["risk_management"]), json.loads(row["legs"])


def test_sltp_endpoint_saves_percent_and_mirrors_legs():
    sid = _seed({"daily_stop_loss": 1500, "daily_take_profit": 3000})
    out = update_strategy_sltp(sid, SltpUpdateRequest(
        sl=35, sl_unit="pct", tp=65, tp_unit="pct"))
    assert out.get("status") == "updated", out
    rm, legs = _rm(sid)
    assert rm["daily_stop_loss_pct"] == 35.0 and rm["daily_stop_loss"] == 0
    assert rm["daily_take_profit_pct"] == 65.0 and rm["daily_take_profit"] == 0
    assert legs[0]["stop_loss"] == "35.0%" and legs[0]["take_profit"] == "65.0%"
    # unlimited by default - the edit must not resurrect a daily cap
    assert rm["max_trades_per_day"] == -1


def test_sltp_endpoint_saves_rupees():
    sid = _seed({"daily_stop_loss_pct": 30, "daily_take_profit_pct": 70})
    out = update_strategy_sltp(sid, SltpUpdateRequest(
        sl=800, sl_unit="rs", tp=1600, tp_unit="rs"))
    assert out.get("status") == "updated", out
    rm, legs = _rm(sid)
    assert rm["daily_stop_loss"] == 800 and rm["daily_stop_loss_pct"] == 0
    assert rm["daily_take_profit"] == 1600 and rm["daily_take_profit_pct"] == 0
    assert legs[0]["stop_loss"] == 800 and legs[0]["take_profit"] == 1600


def test_sltp_endpoint_rejects_non_positive():
    sid = _seed({})
    assert "error" in update_strategy_sltp(sid, SltpUpdateRequest(sl=0))
    assert "error" in update_strategy_sltp(sid, SltpUpdateRequest(tp=-5))


def test_broker_resolves_saved_percent_sltp():
    sid = _seed({"daily_stop_loss_pct": 30, "daily_take_profit_pct": 70})
    assert _strategy_sltp_from_db(sid) == (30.0, "pct", 70.0, "pct")


def test_broker_resolves_saved_rupee_sltp():
    sid = _seed({"daily_stop_loss": 1500, "daily_take_profit": 3000})
    assert _strategy_sltp_from_db(sid) == (1500.0, "rs", 3000.0, "rs")


def test_broker_percent_wins_over_zero_rupee_key():
    # Saved by the /sltp endpoint: rupee key 0, percent key set.
    sid = _seed({"daily_stop_loss": 0, "daily_stop_loss_pct": 12.5,
                 "daily_take_profit": 0, "daily_take_profit_pct": 25})
    assert _strategy_sltp_from_db(sid) == (12.5, "pct", 25.0, "pct")


def test_broker_returns_none_when_nothing_saved():
    sid = _seed({})
    assert _strategy_sltp_from_db(sid) == (None, None, None, None)
    assert _strategy_sltp_from_db(0) == (None, None, None, None)


def test_save_strategy_forces_unlimited_daily_caps():
    sid = _seed({})
    out = save_strategy(StrategyRequest(
        id=sid, name=TAG, symbol="NIFTY",
        legs=[{"option_type": "CE", "transaction": "buy", "lots": 1}],
        risk_management={"max_trades_per_day": 5, "daily_loss_limit": 5000,
                         "daily_stop_loss_pct": 25, "daily_take_profit_pct": 50}))
    assert out.get("id"), out
    rm, _ = _rm(sid)
    assert rm["max_trades_per_day"] == -1, rm
    assert rm["daily_loss_limit"] == 0, rm
    # user SL/TP survives the save untouched
    assert rm["daily_stop_loss_pct"] == 25 and rm["daily_take_profit_pct"] == 50
