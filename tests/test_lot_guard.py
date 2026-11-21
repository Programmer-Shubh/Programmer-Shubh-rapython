"""Official-lot gate: galat/unverified lot par koi order na lage.

Covers: SENSEX 20 / BANKEX 30 corrected map, official_lot verified flag,
unknown-symbol BLOCK on both place endpoints, fantasy-premium removal
(paths must error, not fabricate) - purana crash/fantasy code proof.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))


def _cold_lots(monkeypatch):
    import core.services.lot_master as lm
    import utils.helpers as H
    monkeypatch.setattr(lm, "_MEM", None)
    monkeypatch.setattr(lm, "_MEM_TS", 0.0)
    monkeypatch.setattr(H, "_load_nse_lots", lambda: None)


def test_bse_lots_corrected(monkeypatch):
    import utils.helpers as H
    _cold_lots(monkeypatch)
    assert H.get_lot_size("SENSEX") == 20, H.get_lot_size("SENSEX")
    assert H.get_lot_size("BANKEX") == 30, H.get_lot_size("BANKEX")
    assert H.get_lot_size("NIFTY") == 65
    assert H.get_lot_size("BANKNIFTY") == 30


def test_official_lot_verified_flag(monkeypatch):
    import utils.helpers as H
    _cold_lots(monkeypatch)
    lot, src, ok = H.official_lot("SENSEX")
    assert (lot, ok) == (20, True), (lot, src, ok)
    lot2, src2, ok2 = H.official_lot("TOTALLYUNKNOWN")
    assert (lot2, src2, ok2) == (50, "default", False), (lot2, src2, ok2)
    assert H.lot_source("TOTALLYUNKNOWN") == "default"
    assert H.lot_guard_error("TOTALLYUNKNOWN") is not None
    assert H.lot_guard_error("NIFTY") is None


def test_paper_place_blocks_unverified_lot(monkeypatch):
    import core.services.sl_monitor as _sl
    monkeypatch.setattr(_sl, "market_open_ist", lambda *_a, **_k: True)
    monkeypatch.setattr(_sl, "entry_open_ist", lambda *_a, **_k: True)
    from routes.paper_trade import PlaceTradeRequest, place_trade
    r = place_trade(PlaceTradeRequest(
        symbol="TOTALLYUNKNOWN", option_type="CE", strike=100.0,
        transaction_type="BUY", quantity=1, entry_price=10.0,
        stop_loss=1500.0, take_profit=3000.0, date="2026-10-14"))
    assert "error" in r and "lot" in r["error"].lower(), r


def test_chain_place_blocks_unverified_lot(monkeypatch):
    import core.services.sl_monitor as _sl
    monkeypatch.setattr(_sl, "market_open_ist", lambda *_a, **_k: True)
    monkeypatch.setattr(_sl, "entry_open_ist", lambda *_a, **_k: True)
    from routes.option_chain import TradeRequest, place_trade
    r = place_trade(TradeRequest(
        symbol="TOTALLYUNKNOWN", option_type="CE", strike=100.0,
        transaction_type="BUY", quantity=1, stop_loss=1500.0,
        take_profit=3000.0, date="2026-10-14", expiry="2026-10-29"))
    assert "error" in r and "lot" in r["error"].lower(), r


def test_execution_engine_blocks_unverified_lot(monkeypatch):
    import core.services.sl_monitor as _sl
    monkeypatch.setattr(_sl, "market_open_ist", lambda *_a, **_k: True)
    monkeypatch.setattr(_sl, "entry_open_ist", lambda *_a, **_k: True)
    from core.services.execution_engine import execute
    r = execute([{"option_type": "CE", "transaction": "buy", "lots": 1}],
                "TOTALLYUNKNOWN", mode="PAPER")
    assert "error" in r and "lot" in r["error"].lower(), r
