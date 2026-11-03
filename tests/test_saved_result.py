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


# ---------------------------------------------------------------------------
# Archive fallback: card par _bt_summary archive se "Saved BT" dikhata tha,
# lekin Backtest Detail sirf last_backtest padhta tha -> click par saved result
# "gayab". Dono ab same source (_resolve_saved_result) use karte hain.
# ---------------------------------------------------------------------------
ARCH_TAG = "t-saved-result-archive-probe"


def _seed_archive_only():
    from core.models.database import Database
    from routes.strategies import _ensure_results_table
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE name=?", [ARCH_TAG])
    _ensure_results_table()
    sid = int(db.execute(
        "INSERT INTO strategies (user_id, name, symbol, start_date, end_date,"
        " timeframe, indicators, legs, advanced_options, risk_management,"
        " status, last_backtest, last_backtest_at)"
        " VALUES (1,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
        [ARCH_TAG, "NIFTY", "2026-09-01", "2026-09-30", "5m", "[]",
         json.dumps([{"option_type": "PE", "transaction": "sell", "lots": 1}]),
         "{}", "{}", "active"]))
    stored = {"run_id": "bt-archived", "engine_version": "v1",
              "metrics": {"total_trades": 7, "win_rate": 71.4, "net_pnl": 1234.0},
              "trade_list": [{"symbol": "NIFTY", "pnl": 100.0}] * 7,
              "params": {"symbols": ["NIFTY"]}}
    db.execute(
        "INSERT INTO strategy_results (strategy_id, config_hash, run_id,"
        " stored_at, trades, net_pnl, win_rate, result_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [sid, "h-arch", "bt-archived", "2026-11-01 10:00", 7, 1234.0, 71.4,
         json.dumps(stored)])
    return sid


def _drop_archive():
    from core.models.database import Database
    db = Database.get_instance()
    try:
        rows = db.fetch_all("SELECT id FROM strategies WHERE name=?", [ARCH_TAG])
        for r in rows or []:
            db.execute("DELETE FROM strategy_results WHERE strategy_id=?", [int(r["id"])])
        db.execute("DELETE FROM strategies WHERE name=?", [ARCH_TAG])
    except Exception:
        pass


def test_saved_result_falls_back_to_history_archive():
    from routes.strategies import strategy_saved_result
    sid = _seed_archive_only()
    try:
        r = strategy_saved_result(sid)
        assert not r.get("error"), r
        assert r.get("stored") is True
        assert r.get("from_history") is True
        assert r.get("strategy_id") == sid
        assert (r.get("metrics") or {}).get("total_trades") == 7
        assert len(r.get("trade_list") or []) == 7
    finally:
        _drop_archive()


def test_backtest_detail_returns_archive_without_fresh_run(monkeypatch):
    import routes.strategy_builder as sbuilder
    from routes.strategies import strategy_backtest
    sid = _seed_archive_only()
    try:
        def _boom(req, progress_cb=None):
            raise AssertionError("archive saved result hai to fresh run nahi hona chahiye")
        monkeypatch.setattr(sbuilder, "_run_backtest_core", _boom)
        r = strategy_backtest(sid)
        assert not r.get("error"), r
        assert r.get("stored") is True
        assert r.get("run_id") == "bt-archived"
        assert (r.get("metrics") or {}).get("total_trades") == 7
        assert len(r.get("trade_list") or []) == 7
    finally:
        _drop_archive()


def test_bt_summary_and_detail_agree_on_archive():
    """Card ka 'Saved BT' aur Backtest Detail same numbers dikhayein."""
    from routes.strategies import _bt_summary, strategy_saved_result
    from core.models.database import Database
    sid = _seed_archive_only()
    try:
        row = Database.get_instance().fetch_one("SELECT * FROM strategies WHERE id=?", [sid])
        summary = _bt_summary(row)
        detail = strategy_saved_result(sid)
        assert summary.get("trades") == 7
        assert (detail.get("metrics") or {}).get("total_trades") == 7
        assert summary.get("run_id") == detail.get("run_id") == "bt-archived"
    finally:
        _drop_archive()
