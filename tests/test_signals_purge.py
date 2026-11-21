"""TV SuperTrend flip arrows + history strategy linkage + symbol purge."""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

TAG = "t-sigpurge-probe"
SYM = "TSTPURGE"


def test_st_flip_times():
    from routes.tvchart import _st_flip_times
    series = [
        {"time": "2026-10-01", "value": 100.0, "bullish": False},
        {"time": "2026-10-02", "value": 101.0, "bullish": False},
        {"time": "2026-10-03", "value": 102.0, "bullish": True},
        {"time": "2026-10-04", "value": 103.0, "bullish": True},
        {"time": "2026-10-05", "value": 104.0, "bullish": False},
    ]
    r = _st_flip_times(series)
    assert r["flip_up"] == ["2026-10-03"], r
    assert r["flip_down"] == ["2026-10-05"], r
    assert _st_flip_times([]) == {"flip_up": [], "flip_down": []}
    assert _st_flip_times([{"time": "x", "bullish": True}]) == {"flip_up": [], "flip_down": []}


def _seed():
    from core.models.database import Database
    db = Database.get_instance()
    db.execute("DELETE FROM paper_trades WHERE symbol=?", [SYM])
    db.execute("DELETE FROM strategy_results WHERE strategy_id IN "
               "(SELECT id FROM strategies WHERE name=?)", [TAG])
    db.execute("DELETE FROM strategies WHERE name=?", [TAG])
    sid = db.execute(
        "INSERT INTO strategies (user_id, name, symbol, status) VALUES (1,?, 'NIFTY', 'active')",
        [TAG])
    db.execute(
        "INSERT INTO paper_trades (user_id, strategy_id, symbol, option_type, strike_price,"
        " transaction_type, quantity, lot_size, entry_price, exit_price,"
        " entry_date, exit_date, pnl, status, exit_status)"
        " VALUES (1,?,'" + SYM + "','CE',25000,'BUY',1,65,100.0,110.0,"
        " '2026-10-14','2026-10-14',500.0,'closed','target')",
        [int(sid)])
    db.execute(
        "INSERT INTO strategy_results (strategy_id, run_id, stored_at, trades, net_pnl,"
        " win_rate, result_json) VALUES (?, 'bt-x', 'now', 1, 500.0, 100.0, '{}')",
        [int(sid)])
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
        db.execute("DELETE FROM paper_trades WHERE symbol=?", [SYM])
    except Exception:
        pass
    try:
        db.execute("DELETE FROM strategies WHERE name=?", [TAG])
    except Exception:
        pass


def test_history_has_strategy_columns():
    from routes.paper_trade import get_history
    from routes.dashboard import get_trade_history
    _seed()
    try:
        r = get_history()
        mine = [t for t in r.get("trades", []) if t.get("symbol") == SYM]
        assert mine, "seeded trade history me nahi"
        assert mine[0]["strategy_name"] == TAG, mine[0]
        assert mine[0]["strategy_id"] > 0
        d = get_trade_history()
        mine2 = [t for t in d.get("trades", []) if t.get("symbol") == SYM]
        assert mine2 and mine2[0]["strategy_name"] == TAG, mine2
    finally:
        _drop()


def test_purge_dry_run_then_confirm():
    from routes.strategies import purge_symbol
    _seed()
    try:
        d = purge_symbol({"symbol": SYM})
        assert not d.get("error"), d
        assert d.get("deleted") is False, d
        assert d.get("closed_trades") == 1 and d.get("strategies") == 0, d
        # strategy row ka symbol NIFTY hai (trade SYM par) - strategy delete nahi hogi
        r = purge_symbol({"symbol": SYM, "confirm": True})
        assert r.get("deleted") is True, r
        assert r["removed"]["trades"] == 1, r
        from core.models.database import Database
        left = Database.get_instance().fetch_all(
            "SELECT id FROM paper_trades WHERE symbol=?", [SYM])
        assert left == [], left
    finally:
        _drop()


def test_purge_missing_symbol_error():
    from routes.strategies import purge_symbol
    r = purge_symbol({})
    assert r.get("error"), r
