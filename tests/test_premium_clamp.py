"""Bad-tick clamp-and-catch-up: >60% premium jump spot-flat par PIN nahi
hona chahiye (purana bug: SL/TP hamesha ke liye andha), clamped value
agle polls me catch-up kare."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))


def _tm_fresh():
    from core.models.trade_model import TradeModel
    TradeModel._last_premiums.clear()
    TradeModel._last_spots.clear()
    return TradeModel()


def test_premium_jump_clamps_then_catches_up(monkeypatch):
    import core.services.contract_pricer as _cp
    import core.services.live_market_data as _lm
    monkeypatch.setattr(_lm.LiveMarketData, "get_live_spot",
                        lambda self, sym=None: {"spot": 25000.0})
    _ltp = {"v": 100.0}
    monkeypatch.setattr(_cp, "get_contract_ltp",
                        lambda *a, **k: float(_ltp["v"]))
    tm = _tm_fresh()
    assert tm.get_option_premium("NIFTY", "CE", 25000, "2026-10-21") == 100.0
    # Spot flat, premium 100 -> 200 (+100%): pin (100) NAHI, clamp (160) chahiye
    _ltp["v"] = 200.0
    v2 = tm.get_option_premium("NIFTY", "CE", 25000, "2026-10-21")
    assert v2 == 160.0, v2
    # Agla poll: 160 -> 200 (+25%) normal move, poora accept
    v3 = tm.get_option_premium("NIFTY", "CE", 25000, "2026-10-21")
    assert v3 == 200.0, v3


def test_normal_move_passes_through(monkeypatch):
    import core.services.contract_pricer as _cp
    import core.services.live_market_data as _lm
    monkeypatch.setattr(_lm.LiveMarketData, "get_live_spot",
                        lambda self, sym=None: {"spot": 25000.0})
    _ltp = {"v": 100.0}
    monkeypatch.setattr(_cp, "get_contract_ltp",
                        lambda *a, **k: float(_ltp["v"]))
    tm = _tm_fresh()
    assert tm.get_option_premium("NIFTY", "CE", 25000, "2026-10-21") == 100.0
    _ltp["v"] = 120.0  # +20%: seedha accept
    assert tm.get_option_premium("NIFTY", "CE", 25000, "2026-10-21") == 120.0
