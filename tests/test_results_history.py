"""Result version history: har save naya version, purana kabhi na mite;
restore (history + browser-copy) current ko archive karke wapas laye.
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

TAG = "t-results-history-probe"


def _mkrow():
    from core.models.database import Database
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE name=?", [TAG])
    sid = db.execute(
        "INSERT INTO strategies (user_id, name, symbol, start_date, end_date,"
        " status) VALUES (1,?,'NIFTY','2026-09-01','2026-09-20','paused')", [TAG])
    return int(sid)


def _drop():
    from core.models.database import Database
    db = Database.get_instance()
    try:
        db.execute("DELETE FROM strategy_results WHERE strategy_id IN "
                   "(SELECT id FROM strategies WHERE name=?)", [TAG])
    except Exception:
        pass
    try:
        db.execute("DELETE FROM strategies WHERE name=?", [TAG])
    except Exception:
        pass


def _res(n, run):
    return {"run_id": run, "symbol": "NIFTY", "symbols": ["NIFTY"],
            "params": {"symbols": ["NIFTY"]},
            "metrics": {"total_trades": n, "net_pnl": 100.0 * n, "win_rate": 50.0},
            "trade_list": [{"symbol": "NIFTY", "pnl": 100.0}] * n}


def test_archive_list_restore_flow():
    from routes.strategies import (_archive_result, strategy_results_history,
                                   strategy_restore_result)
    sid = _mkrow()
    try:
        a1 = _archive_result(sid, _res(188, "bt-old188"), "hash1", "2026-10-12 09:01")
        assert a1 > 0
        # same run_id dobara archive nahi (dedupe)
        assert _archive_result(sid, _res(188, "bt-old188"), "hash1", "x") == a1
        a2 = _archive_result(sid, _res(0, "bt-zero"), "hash2", "2026-10-14 14:39")
        assert a2 > 0 and a2 != a1
        lst = strategy_results_history(sid)
        assert lst.get("count") == 2, lst
        assert lst["versions"][0]["trades"] == 0  # naya sabse upar
        assert lst["versions"][1]["trades"] == 188
        # purana version view me poori trade history
        full = strategy_results_history(sid, history_id=a1)
        assert len(full.get("trade_list") or []) == 188, len(full.get("trade_list") or [])
        # restore purana -> current banta hai, metrics me 188
        r = strategy_restore_result(sid, {"history_id": a1})
        assert not r.get("error"), r
        assert (r.get("metrics") or {}).get("total_trades") == 188
        from core.models.database import Database
        row = Database.get_instance().fetch_one(
            "SELECT last_backtest FROM strategies WHERE id=?", [sid])
        assert json.loads(row["last_backtest"])["run_id"] == "bt-old188"
    finally:
        _drop()


def test_restore_rejects_symbol_mismatch():
    from routes.strategies import strategy_restore_result
    sid = _mkrow()  # NIFTY strategy
    try:
        r = strategy_restore_result(sid, {"result": dict(
            _res(5, "bt-x"), symbol="BANKNIFTY", symbols=["BANKNIFTY"],
            params={"symbols": ["BANKNIFTY"]})})
        assert r.get("error") and "mismatch" in r["error"].lower(), r
    finally:
        _drop()


def test_restore_browser_copy_ok():
    from routes.strategies import strategy_restore_result
    sid = _mkrow()
    try:
        r = strategy_restore_result(sid, {"result": _res(188, "bt-browser188")})
        assert not r.get("error"), r
        assert (r.get("metrics") or {}).get("total_trades") == 188
    finally:
        _drop()
