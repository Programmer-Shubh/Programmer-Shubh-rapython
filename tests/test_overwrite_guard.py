"""Overwrite guard: 0-trade fresh run kabhi non-zero saved history na mitaye
(ce-sell-ppp ke 188 trades isi se ude the)."""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

TAG = "t-overwrite-guard-probe"


def _stored(n, ev=None):
    d = {"run_id": "bt-old", "metrics": {"total_trades": n, "net_pnl": 100.0 * n},
         "trade_list": [{"symbol": "NIFTY", "pnl": 100.0}] * n}
    if ev is not None:
        d["engine_version"] = ev
    return d


def test_should_overwrite_saved_matrix():
    from routes.strategies import _should_overwrite_saved
    from core.services.backtest_engine import ENGINE_VERSION as EV
    # Current-engine saved result: 0-trade fresh run history nahi mitata.
    assert _should_overwrite_saved(json.dumps(_stored(188, EV)), {"metrics": {"total_trades": 0}}) is False
    assert _should_overwrite_saved(json.dumps(_stored(3, EV)), {"metrics": {"total_trades": 0}}) is False
    # Purane engine ka saved: 0-trade fresh bhi usse mitata hai (stale history
    # serve karna galat - fake 100%-win v2 jaisa).
    assert _should_overwrite_saved(json.dumps(_stored(188)), {"metrics": {"total_trades": 0}}) is True
    assert _should_overwrite_saved(json.dumps(_stored(188, "v1")), {"metrics": {"total_trades": 0}}) is True
    assert _should_overwrite_saved(json.dumps(_stored(188, "v99")), {"metrics": {"total_trades": 0}}) is True
    # Khali/kamat history + normal cases.
    assert _should_overwrite_saved(json.dumps(_stored(0, EV)), {"metrics": {"total_trades": 0}}) is True
    assert _should_overwrite_saved("", {"metrics": {"total_trades": 0}}) is True
    assert _should_overwrite_saved(json.dumps(_stored(3, EV)), {"metrics": {"total_trades": 5}}) is True
    assert _should_overwrite_saved(json.dumps(_stored(3, EV)), {"metrics": {"total_trades": 3}}) is True


def test_stored_engine_stale_classifier():
    from routes.strategies import _stored_engine_stale
    from core.services.backtest_engine import ENGINE_VERSION as EV
    assert _stored_engine_stale({}) is True             # tag missing = purana
    assert _stored_engine_stale({"engine_version": EV}) is False
    assert _stored_engine_stale({"engine_version": "v1"}) is True


def test_strategy_backtest_keeps_saved_on_zero_fresh(monkeypatch):
    """Endpoint: current-engine saved history ko fresh 0-trade run nahi mitata."""
    import routes.strategy_builder as sbuilder
    from routes.strategies import strategy_backtest
    from core.services.backtest_engine import ENGINE_VERSION as EV
    from core.models.database import Database
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE name=?", [TAG])
    legs = [{"option_type": "CE", "transaction": "buy", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly"}]
    sid = db.execute(
        "INSERT INTO strategies (user_id, name, symbol, start_date, end_date,"
        " timeframe, indicators, legs, advanced_options, risk_management,"
        " status, last_backtest, last_backtest_hash, last_backtest_at)"
        " VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [TAG, "NIFTY", "2026-09-01", "2026-09-20", "daily", "[]",
         json.dumps(legs), "{}", "{}", "active",
         json.dumps(_stored(2, EV)), "stalehash", "2026-10-01 10:00"])
    try:
        monkeypatch.setattr(
            sbuilder, "_run_backtest_core",
            lambda req, progress_cb=None: {"metrics": {"total_trades": 0, "net_pnl": 0.0},
                                           "trade_list": [], "params": {}})
        r = strategy_backtest(int(sid))
        assert r.get("kept_saved") is True, r
        row = db.fetch_one("SELECT last_backtest FROM strategies WHERE id=?", [int(sid)])
        back = json.loads(row["last_backtest"])
        assert back["run_id"] == "bt-old", back
        assert len(back["trade_list"]) == 2
    finally:
        db.execute("DELETE FROM strategies WHERE name=?", [TAG])
