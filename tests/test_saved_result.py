"""Backtest Detail instant path: saved-result endpoint banate-samay wali
poori trade history de (hash-gate ke bina), config badalne par flag ke saath.
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

TAG = "t-saved-result-probe"


def _seed():
    from core.models.database import Database
    from routes.strategies import _canonical_config_hash
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE name=?", [TAG])
    legs = [{"option_type": "PE", "transaction": "sell", "lots": 1,
             "strike_selection": "otm", "otm_distance": 3, "expiry": "weekly"}]
    inds = [{"id": "supertrend", "params": {"period": 10, "multiplier": 3}}]
    adv = {"trade_mode": "intraday", "entry_time": "09:35", "exit_time": "15:05"}
    risk = {"daily_stop_loss": 1500, "daily_take_profit": 3000,
            "max_trades_per_day": 5}
    stored = {"run_id": "bt-probe1",
              "metrics": {"total_trades": 1, "win_rate": 100.0, "net_pnl": 2896.01},
              "trade_list": [{"symbol": "NIFTY", "entry_date": "2026-09-19",
                              "exit_date": "2026-09-20", "option_type": "PE",
                              "strike": 24800, "position": "sell", "quantity": 65,
                              "entry_price": 190.4, "exit_price": 150.0,
                              "pnl": 2896.01, "exit_reason": "target"}],
              "params": {"symbols": ["NIFTY"]}}
    h = _canonical_config_hash("NIFTY", "2026-09-19", "2026-10-17", "daily",
                               inds, [], [], legs, adv, risk)
    sid = db.execute(
        "INSERT INTO strategies (user_id, name, symbol, start_date, end_date,"
        " timeframe, indicators, legs, advanced_options, risk_management,"
        " status, last_backtest, last_backtest_hash, last_backtest_at)"
        " VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [TAG, "NIFTY", "2026-09-19", "2026-10-17", "daily",
         json.dumps(inds), json.dumps(legs), json.dumps(adv), json.dumps(risk),
         "active", json.dumps(stored), h, "2026-10-14 14:37"])
    return int(sid)


def _drop():
    from core.models.database import Database
    try:
        Database.get_instance().execute("DELETE FROM strategies WHERE name=?", [TAG])
    except Exception:
        pass


def test_saved_result_returns_full_trade_history():
    from routes.strategies import strategy_saved_result
    sid = _seed()
    try:
        r = strategy_saved_result(sid)
        assert not r.get("error"), r
        assert r.get("stored") is True
        assert isinstance(r.get("trade_list"), list) and len(r["trade_list"]) == 1
        assert r["trade_list"][0]["strike"] == 24800
        assert r.get("metrics", {}).get("total_trades") == 1
        assert r.get("config_changed") is False
    finally:
        _drop()


def test_saved_result_survives_config_change_with_flag():
    """Config badalne ke baad bhi SAVED history mile + flag lage."""
    from core.models.database import Database
    from routes.strategies import strategy_saved_result
    sid = _seed()
    try:
        db = Database.get_instance()
        row = db.fetch_one("SELECT legs FROM strategies WHERE id=?", [sid])
        legs = json.loads(row["legs"])
        legs[0]["otm_distance"] = 5  # config badli
        db.execute("UPDATE strategies SET legs=? WHERE id=?", [json.dumps(legs), sid])
        r = strategy_saved_result(sid)
        assert not r.get("error"), r
        assert len(r.get("trade_list") or []) == 1  # history phir bhi mile
        assert r.get("config_changed") is True
    finally:
        _drop()


def test_saved_result_missing_is_honest_error():
    from core.models.database import Database
    from routes.strategies import strategy_saved_result
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE name=?", [TAG])
    sid = db.execute(
        "INSERT INTO strategies (user_id, name, symbol, status) VALUES (1,?, 'NIFTY', 'active')",
        [TAG])
    try:
        r = strategy_saved_result(int(sid))
        assert r.get("error"), r
    finally:
        _drop()
