"""3:15 PM entry cutoff: iske baad KOI nayi entry nahi (exits khule)."""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.services.sl_monitor as slm


def _at(hh, mm):
    return datetime.datetime(2026, 10, 14, hh, mm)  # Wednesday


def test_entry_open_before_cutoff(monkeypatch):
    monkeypatch.setattr(slm, "market_open_ist", lambda: True)
    assert slm.entry_open_ist(_at(9, 15)) is True
    assert slm.entry_open_ist(_at(15, 14)) is True


def test_entry_closed_at_and_after_cutoff(monkeypatch):
    monkeypatch.setattr(slm, "market_open_ist", lambda: True)
    assert slm.entry_open_ist(_at(15, 15)) is False
    assert slm.entry_open_ist(_at(15, 34)) is False


def test_entry_closed_when_market_closed(monkeypatch):
    monkeypatch.setattr(slm, "market_open_ist", lambda: False)
    assert slm.entry_open_ist(_at(10, 0)) is False


def test_chain_place_blocked_after_cutoff(monkeypatch):
    from routes import option_chain as oc
    monkeypatch.setattr(slm, "market_open_ist", lambda: True)
    monkeypatch.setattr(slm, "entry_open_ist", lambda *_a, **_k: False)
    r = oc.place_trade(oc.TradeRequest(
        symbol="NIFTY", option_type="CE", strike=25000.0, expiry="",
        date="2026-10-14", transaction_type="BUY", quantity=1,
        stop_loss=1500.0, take_profit=3000.0, trade_type="intraday",
        strategy_id=0))
    assert "3:15" in (r.get("error") or ""), r


def test_paper_place_blocked_after_cutoff(monkeypatch):
    from routes import paper_trade as pt
    monkeypatch.setattr(slm, "market_open_ist", lambda: True)
    monkeypatch.setattr(slm, "entry_open_ist", lambda *_a, **_k: False)
    r = pt.place_trade(pt.PlaceTradeRequest(
        symbol="NIFTY", option_type="CE", strike=25000.0,
        transaction_type="BUY", quantity=1, entry_price=100.0,
        stop_loss=1500.0, take_profit=3000.0, date="2026-10-14"))
    assert "3:15" in (r.get("error") or ""), r


def test_auto_paper_blocked_after_cutoff(monkeypatch):
    from core.services import auto_paper as ap
    monkeypatch.setattr(slm, "entry_open_ist", lambda *_a, **_k: False)
    r = ap.place_for_strategy(999999)
    assert r.get("placed") == []
    assert any("3:15" in str(n) for n in r.get("notes", [])), r


def test_execution_engine_blocked_after_cutoff(monkeypatch):
    from core.services import execution_engine as ee
    monkeypatch.setattr(slm, "market_open_ist", lambda: True)
    monkeypatch.setattr(slm, "entry_open_ist", lambda *_a, **_k: False)
    r = ee.execute([{"option_type": "CE", "transaction": "buy", "lots": 1}],
                   "NIFTY", mode="PAPER")
    assert "3:15" in (r.get("error") or ""), r
