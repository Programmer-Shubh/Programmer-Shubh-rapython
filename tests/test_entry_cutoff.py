"""3:15 PM entry cutoff: iske baad KOI nayi entry nahi (exits khule)."""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.services.sl_monitor as slm


def _at(hh, mm):
    return datetime.datetime(2026, 10, 14, hh, mm)  # Wednesday


def test_entry_open_before_cutoff(monkeypatch):
    monkeypatch.setattr(slm, "market_open_ist", lambda *_a, **_k: True)
    assert slm.entry_open_ist(_at(9, 15)) is True
    assert slm.entry_open_ist(_at(15, 14)) is True


def test_entry_closed_at_and_after_cutoff(monkeypatch):
    monkeypatch.setattr(slm, "market_open_ist", lambda *_a, **_k: True)
    assert slm.entry_open_ist(_at(15, 15)) is False
    assert slm.entry_open_ist(_at(15, 34)) is False


def test_entry_closed_when_market_closed(monkeypatch):
    monkeypatch.setattr(slm, "market_open_ist", lambda: False)
    assert slm.entry_open_ist(_at(10, 0)) is False


def test_chain_place_blocked_after_cutoff(monkeypatch):
    from routes import option_chain as oc
    monkeypatch.setattr(slm, "market_open_ist", lambda *_a, **_k: True)
    monkeypatch.setattr(slm, "entry_open_ist", lambda *_a, **_k: False)
    r = oc.place_trade(oc.TradeRequest(
        symbol="NIFTY", option_type="CE", strike=25000.0, expiry="",
        date="2026-10-14", transaction_type="BUY", quantity=1,
        stop_loss=1500.0, take_profit=3000.0, trade_type="intraday",
        strategy_id=0))
    assert "15:15" in (r.get("error") or ""), r


def test_paper_place_blocked_after_cutoff(monkeypatch):
    from routes import paper_trade as pt
    monkeypatch.setattr(slm, "market_open_ist", lambda *_a, **_k: True)
    monkeypatch.setattr(slm, "entry_open_ist", lambda *_a, **_k: False)
    r = pt.place_trade(pt.PlaceTradeRequest(
        symbol="NIFTY", option_type="CE", strike=25000.0,
        transaction_type="BUY", quantity=1, entry_price=100.0,
        stop_loss=1500.0, take_profit=3000.0, date="2026-10-14"))
    assert "15:15" in (r.get("error") or ""), r


def test_auto_paper_blocked_after_cutoff(monkeypatch):
    import json
    from core.services import auto_paper as ap
    from core.models.database import Database
    monkeypatch.setattr(slm, "entry_open_ist", lambda *_a, **_k: False)
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE name='t-entry-cutoff'")
    db.execute(
        "INSERT INTO strategies (user_id, name, symbol, legs, risk_management,"
        " status) VALUES (1,'t-entry-cutoff','NIFTY',?,?,?)",
        [json.dumps([{"option_type": "CE", "transaction": "buy", "lots": 1}]),
         json.dumps({"daily_stop_loss": 1500, "daily_take_profit": 3000,
                     "max_trades_per_day": 5}),
         "active"])
    try:
        sid = int(db.fetch_one("SELECT id FROM strategies WHERE name='t-entry-cutoff'")["id"])
        r = ap.place_for_strategy(sid)
        assert r.get("placed") == []
        assert any("entry band" in str(n) for n in r.get("notes", [])), r
    finally:
        db.execute("DELETE FROM strategies WHERE name='t-entry-cutoff'")


def test_mcx_entry_open_evening(monkeypatch):
    """MCX shaam ko khula (NSE cutoff ke baad bhi)."""
    monkeypatch.setattr(slm, "market_open_ist", lambda *_a, **_k: True)
    assert slm.entry_open_ist(_at(20, 0), symbol="CRUDEOIL") is True
    assert slm.entry_open_ist(_at(23, 0), symbol="CRUDEOIL") is False
    assert slm.entry_open_ist(_at(15, 14)) is True
    assert slm.entry_open_ist(_at(15, 15)) is False


def test_execution_engine_blocked_after_cutoff(monkeypatch):
    from core.services import execution_engine as ee
    monkeypatch.setattr(slm, "market_open_ist", lambda *_a, **_k: True)
    monkeypatch.setattr(slm, "entry_open_ist", lambda *_a, **_k: False)
    r = ee.execute([{"option_type": "CE", "transaction": "buy", "lots": 1}],
                   "NIFTY", mode="PAPER")
    assert "15:15" in (r.get("error") or ""), r
