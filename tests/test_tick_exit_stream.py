"""Tick-by-tick exit stream tests (Fyers WebSocket path).

Rule: har exchange TICK par SL/TP decision - candle close ya poll interval ka
wait nahi. WS fail ho to 0.75s REST poll (check_once) safety net chalta hai.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

import core.services.fast_exit as fe
from utils.helpers import effective_lot_size


def _trade():
    return {
        "id": 991, "symbol": "NIFTY", "option_type": "CE", "strike_price": 25000.0,
        "entry_price": 100.0, "stop_loss": 1500, "target": 3000,
        "sl_unit": "rs", "tp_unit": "rs", "quantity": 1, "lot_size": 75,
        "transaction_type": "BUY", "expiry_date": "2026-06-30",
        "trade_mode": "paper", "status": "open",
    }


class _FakeTM:
    def __init__(self):
        self.calls = []

    def close_trade(self, tid, px, day, reason):
        self.calls.append({"id": tid, "px": float(px), "reason": reason})
        return 1


def _setup(monkeypatch, fake, trade=None):
    fe._SYM_MAP = {"NSE:NIFTY25JUN25000CE": 991}
    fe._TRADES_CACHE = (time.time(), [trade or _trade()])
    fe._LEVEL_CACHE.clear()
    fe._CLAIMS.clear()
    fe._WS["ticks"] = 0
    fe._WS["exits"] = 0
    monkeypatch.setattr(fe, "_claim", lambda tid: True)
    monkeypatch.setattr(fe, "_release", lambda tid: None)
    monkeypatch.setattr("core.models.trade_model.TradeModel", lambda: fake)


def test_levels_match_shared_helper():
    """TP/SL levels wahi hain jo paper monitor / backtest use karte hain."""
    lot = effective_lot_size("NIFTY")
    lv = fe._levels(_trade())
    assert lv["tp"] == 100.0 + 3000.0 / (1 * lot)
    assert lv["sl"] == 100.0 - 1500.0 / (1 * lot)


def test_tick_above_target_closes_immediately_at_level(monkeypatch):
    fake = _FakeTM()
    _setup(monkeypatch, fake)
    tp = fe._levels(_trade())["tp"]
    fe._on_message({"symbol": "NSE:NIFTY25JUN25000CE", "ltp": tp + 5.0})
    assert fake.calls, "target tick par exit turant hona chahiye"
    assert fake.calls[0]["reason"] == "target"
    assert fake.calls[0]["px"] == tp
    assert fe._WS["ticks"] == 1 and fe._WS["exits"] == 1


def test_tick_below_stop_closes_at_ltp(monkeypatch):
    fake = _FakeTM()
    _setup(monkeypatch, fake)
    sl = fe._levels(_trade())["sl"]
    fe._on_message({"symbol": "NSE:NIFTY25JUN25000CE", "ltp": sl - 5.0})
    assert fake.calls and fake.calls[0]["reason"] == "stoploss"
    assert fake.calls[0]["px"] == sl - 5.0


def test_tick_between_levels_does_nothing(monkeypatch):
    fake = _FakeTM()
    _setup(monkeypatch, fake)
    fe._on_message({"symbol": "NSE:NIFTY25JUN25000CE", "ltp": 100.5})
    assert not fake.calls
    assert fe._WS["ticks"] == 1 and fe._WS["exits"] == 0


def test_bad_contract_spike_is_skipped(monkeypatch):
    """SELL position par 4x quote = wrong contract -> exit skip (guard)."""
    fake = _FakeTM()
    trade = _trade()
    trade["transaction_type"] = "SELL"
    _setup(monkeypatch, fake, trade=trade)
    fe._on_message({"symbol": "NSE:NIFTY25JUN25000CE", "ltp": 400.0})
    assert not fake.calls, fake.calls


def test_unknown_symbol_is_ignored(monkeypatch):
    fake = _FakeTM()
    _setup(monkeypatch, fake)
    fe._on_message({"symbol": "NSE:SOMETHINGELSE", "ltp": 999.0})
    assert not fake.calls


def test_batch_message_list_is_handled(monkeypatch):
    fake = _FakeTM()
    _setup(monkeypatch, fake)
    tp = fe._levels(_trade())["tp"]
    fe._on_message([{"symbol": "NSE:NIFTY25JUN25000CE", "ltp": tp + 5.0}])
    assert fake.calls and fake.calls[0]["reason"] == "target"


def test_ws_status_reports_fallback_interval():
    st = fe.ws_status()
    assert st["rest_fallback_s"] == 0.75
    assert "connected" in st and "ticks" in st
