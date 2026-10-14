"""3:20 PM intraday square-off (background se), positional carry.
SL/TP monitor 15:20 tak (aur 15:45 tak) chalta hai - gate sirf ENTRY rokta hai.
"""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

UID = 99996


def _now(hh, mm):
    return datetime.datetime(2026, 10, 14, hh, mm)  # Wednesday


def _mk(tm, trade_type, entry_date="2026-10-14"):
    return tm.db.execute(
        "INSERT INTO paper_trades (user_id, symbol, option_type, strike_price,"
        " transaction_type, quantity, lot_size, entry_price, stop_loss, target,"
        " total_cost, entry_date, trade_type, status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [UID, "NIFTY", "CE", 25000, "BUY", 1, 65, 100.0,
         1500, 3000, 0.0, entry_date, trade_type, "open"])


def _cleanup(tm):
    try:
        tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
    except Exception:
        pass


def test_intraday_exits_at_1520_positional_carries(monkeypatch):
    from core.models.trade_model import TradeModel
    monkeypatch.setattr(TradeModel, "get_option_premium", lambda *a, **k: 90.0)
    tm = TradeModel()
    _cleanup(tm)
    try:
        t_intra = _mk(tm, "intraday")
        t_pos = _mk(tm, "positional")
        hit = tm.close_intraday_trades(user_id=UID, _now=_now(15, 20))
        assert [h["id"] for h in hit] == [int(t_intra)], hit
        assert hit[0]["reason"] == "intraday_auto"
        assert tm.db.fetch_one(
            "SELECT status FROM paper_trades WHERE id=?", [int(t_pos)])["status"] == "open"
        assert tm.db.fetch_one(
            "SELECT status, exit_status FROM paper_trades WHERE id=?",
            [int(t_intra)])["exit_status"] == "intraday_auto"
    finally:
        _cleanup(tm)


def test_no_exit_before_1520(monkeypatch):
    from core.models.trade_model import TradeModel
    tm = TradeModel()
    _cleanup(tm)
    try:
        t_intra = _mk(tm, "intraday")
        assert tm.close_intraday_trades(user_id=UID, _now=_now(15, 19)) == []
        assert tm.db.fetch_one(
            "SELECT status FROM paper_trades WHERE id=?", [int(t_intra)])["status"] == "open"
    finally:
        _cleanup(tm)


def test_monitor_runs_eod_scan_without_page_view(monkeypatch):
    """Background run_once me intraday scan chalta hai (market khula ho ya band)."""
    import core.services.sl_monitor as slm
    from core.models.trade_model import TradeModel
    calls = []
    monkeypatch.setattr(
        TradeModel, "close_intraday_trades",
        lambda self, user_id=1, _now=None: (calls.append(1) or [{"id": 7, "reason": "intraday_auto"}]))
    monkeypatch.setattr(
        TradeModel, "close_strategy_exit_time_trades",
        lambda self, user_id=1, now_hhmm="": [])
    monkeypatch.setattr(slm, "_check_fn", lambda: (lambda: {"closed": [], "skipped": []}))
    for mkt in (True, False):
        calls.clear()
        monkeypatch.setattr(slm, "market_open_ist", lambda: mkt)
        r = slm.run_once(min_interval=0, market_hours=True)
        assert calls, f"intraday scan nahi chala (market_open={mkt}): {r}"
        assert any(x.get("reason") == "intraday_auto" for x in r.get("closed", [])), r
