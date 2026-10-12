"""Market-hours gate: paper entries only when market open (exits untouched)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.services.sl_monitor as slm
from routes import option_chain as oc
from routes import paper_trade as pt


def _chain_req():
    return oc.TradeRequest(symbol="NIFTY", option_type="CE", strike=25000.0,
                           expiry="", date="2026-10-12", transaction_type="BUY",
                           quantity=1, stop_loss=1500.0, take_profit=3000.0,
                           trade_type="intraday", strategy_id=0)


def _paper_req():
    return pt.PlaceTradeRequest(symbol="NIFTY", option_type="CE", strike=25000.0,
                                transaction_type="BUY", quantity=1,
                                entry_price=100.0, stop_loss=1500.0,
                                take_profit=3000.0, date="2026-10-12")


def test_chain_place_blocked_when_closed(monkeypatch):
    monkeypatch.setattr(slm, "market_open_ist", lambda: False)
    r = oc.place_trade(_chain_req())
    assert "Market band" in (r.get("error") or ""), r


def test_chain_place_passes_gate_when_open(monkeypatch):
    # gate passes -> fails later on option-type validation (proves gate open,
    # and no DB write happens on this path)
    monkeypatch.setattr(slm, "market_open_ist", lambda: True)
    req = _chain_req()
    req.option_type = "XX"
    r = oc.place_trade(req)
    assert "Market band" not in (r.get("error") or ""), r
    assert r.get("error"), r


def test_trades_place_blocked_when_closed(monkeypatch):
    monkeypatch.setattr(slm, "market_open_ist", lambda: False)
    r = pt.place_trade(_paper_req())
    assert "Market band" in (r.get("error") or ""), r


def test_execution_engine_paper_gated(monkeypatch):
    from core.services import execution_engine as ee
    monkeypatch.setattr(slm, "market_open_ist", lambda: False)
    r = ee.execute([{"option_type": "CE", "transaction": "buy", "lots": 1}],
                   "NIFTY", mode="PAPER")
    assert "Market band" in (r.get("error") or ""), r
