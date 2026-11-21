"""Stale broker-quote guard (11-Nov-2026).

Fyers `lp` = last traded price. Thin/illiquid contract me wo minutes/hours
purana ho sakta hai (ya pichle din ka close). Aaj NIFTY 25200 CE:
low_price 202.3, high 380, prev_close 169.85 - app ne ENTRY 175 par lagai
jo aaj kabhi trade nahi hua -> turant fake "+42% target" profit.

Guard: lp aaj ke traded range (low/high) ke bahar ho to stale maana jata
hai (atp use hota hai agar wo range ke andar ho), aur volume 0 + lp ==
prev_close bhi stale hai.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services import contract_pricer as CP


def _node(lp, low=0, high=0, prev=0, vol=1, atp=0):
    return {"lp": lp, "low_price": low, "high_price": high,
            "prev_close_price": prev, "volume": vol, "atp": atp}


def test_quote_outside_day_range_is_rejected(monkeypatch):
    """lp < low_price (ya > high_price) = stale -> use mat karo."""
    rows = {"NSE:NIFTY26N1725200CE": _node(175.02, low=202.3, high=380.0,
                                          prev=169.85, vol=68397615)}
    monkeypatch.setattr(CP, "_fyers_quotes_raw", lambda *a, **k: {"d": [{"n": "NSE:NIFTY26N1725200CE", "v": rows["NSE:NIFTY26N1725200CE"], "s": "ok"}]})
    monkeypatch.setattr(CP, "_fyers_auth", lambda: ("APP-100", "tok"))
    monkeypatch.setattr(CP, "_fyers_symbol",
                        lambda *a, **k: "NSE:NIFTY26N1725200CE", raising=False)
    import core.services.symbol_resolver as SR
    monkeypatch.setattr(SR, "_fyers_symbol",
                        lambda *a, **k: "NSE:NIFTY26N1725200CE")
    out = CP._fyers_quote_ltp("NIFTY", "2026-11-17", [(25200, "CE")])
    assert (25200, "CE") not in out, out


def test_quote_inside_day_range_is_accepted(monkeypatch):
    rows = {"NSE:NIFTY26N1725200CE": _node(233.7, low=202.3, high=380.0,
                                          prev=169.85, vol=68397615)}
    monkeypatch.setattr(CP, "_fyers_quotes_raw", lambda *a, **k: {"d": [{"n": "NSE:NIFTY26N1725200CE", "v": rows["NSE:NIFTY26N1725200CE"], "s": "ok"}]})
    monkeypatch.setattr(CP, "_fyers_auth", lambda: ("APP-100", "tok"))
    import core.services.symbol_resolver as SR
    monkeypatch.setattr(SR, "_fyers_symbol",
                        lambda *a, **k: "NSE:NIFTY26N1725200CE")
    out = CP._fyers_quote_ltp("NIFTY", "2026-11-17", [(25200, "CE")])
    assert out.get((25200, "CE")) == 233.7


def test_no_trade_today_with_prev_close_is_stale(monkeypatch):
    """volume 0 + lp == prev_close = aaj koi trade nahi (stale close)."""
    rows = {"NSE:NIFTY26N1725200CE": _node(169.85, low=0, high=0,
                                          prev=169.85, vol=0)}
    monkeypatch.setattr(CP, "_fyers_quotes_raw", lambda *a, **k: {"d": [{"n": "NSE:NIFTY26N1725200CE", "v": rows["NSE:NIFTY26N1725200CE"], "s": "ok"}]})
    monkeypatch.setattr(CP, "_fyers_auth", lambda: ("APP-100", "tok"))
    import core.services.symbol_resolver as SR
    monkeypatch.setattr(SR, "_fyers_symbol",
                        lambda *a, **k: "NSE:NIFTY26N1725200CE")
    out = CP._fyers_quote_ltp("NIFTY", "2026-11-17", [(25200, "CE")])
    assert (25200, "CE") not in out, out


def test_atp_used_when_inside_range(monkeypatch):
    """Stale lp par atp (average traded) range ke andar ho to atp use karo."""
    rows = {"NSE:NIFTY26N1725200CE": _node(150.0, low=202.3, high=380.0,
                                          prev=169.85, vol=1000, atp=240.0)}
    monkeypatch.setattr(CP, "_fyers_quotes_raw", lambda *a, **k: {"d": [{"n": "NSE:NIFTY26N1725200CE", "v": rows["NSE:NIFTY26N1725200CE"], "s": "ok"}]})
    monkeypatch.setattr(CP, "_fyers_auth", lambda: ("APP-100", "tok"))
    import core.services.symbol_resolver as SR
    monkeypatch.setattr(SR, "_fyers_symbol",
                        lambda *a, **k: "NSE:NIFTY26N1725200CE")
    out = CP._fyers_quote_ltp("NIFTY", "2026-11-17", [(25200, "CE")])
    assert out.get((25200, "CE")) == 240.0
