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


def test_strategy_backtest_returns_saved_instantly(monkeypatch):
    """Backtest Detail (refresh=0): SAVED result turant wapas - koi chhup-ta
    fresh run nahi (click-time 45s timeout + 'saved result nahi dikhta' bug).
    0-trade fresh run saved KO MITATA BHI NAHI (refresh ke bina run hi nahi)."""
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
         json.dumps(_stored(2, EV)), "old-hash", "2026-10-01 10:00"])
    try:
        _called = {"n": 0}

        def _no_run(req, progress_cb=None):
            _called["n"] += 1
            return {"metrics": {"total_trades": 0, "net_pnl": 0.0},
                    "trade_list": [], "params": {}}
        monkeypatch.setattr(sbuilder, "_run_backtest_core", _no_run)
        r = strategy_backtest(int(sid))
        assert r.get("stored") is True, r
        assert r.get("run_id") == "bt-old", r
        assert len(r.get("trade_list") or []) == 2, r
        assert _called["n"] == 0, "fresh run ko click par nahi chalna chahiye"
        # DB me saved history untouched.
        row = db.fetch_one("SELECT last_backtest, last_backtest_at FROM strategies WHERE id=?", [int(sid)])
        back = json.loads(row["last_backtest"])
        assert back["run_id"] == "bt-old", back
        assert row["last_backtest_at"] == "2026-10-01 10:00", row
    finally:
        db.execute("DELETE FROM strategies WHERE name=?", [TAG])


def test_strategy_backtest_fresh_run_without_saved(monkeypatch):
    """Saved result ho hi nahi to fresh run hota hai aur save bhi (engine tag
    stamp ke saath) - pehli baar wala flow."""
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
        " VALUES (1,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
        [TAG, "NIFTY", "2026-09-01", "2026-09-20", "daily", "[]",
         json.dumps(legs), "{}", "{}", "active"])
    try:
        def _fake_run(req, progress_cb=None):
            return {"run_id": "bt-new", "metrics": {"total_trades": 3, "net_pnl": 111.0},
                    "trade_list": [{"i": 1}], "params": {}}
        monkeypatch.setattr(sbuilder, "_run_backtest_core", _fake_run)
        r = strategy_backtest(int(sid))
        assert r.get("run_id") == "bt-new", r
        assert r.get("stored") is False, r
        assert r.get("engine_version") == EV, "fresh save par engine tag stamp ho"
        row = db.fetch_one("SELECT last_backtest, last_backtest_hash FROM strategies WHERE id=?", [int(sid)])
        back = json.loads(row["last_backtest"])
        assert back["run_id"] == "bt-new", back
        assert row["last_backtest_hash"], "hash save hota hai"
    finally:
        db.execute("DELETE FROM strategies WHERE name=?", [TAG])
