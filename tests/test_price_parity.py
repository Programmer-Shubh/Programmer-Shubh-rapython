"""Paper vs Live price parity.

The paper trade and the live trade of the SAME contract must be priced by
the SAME function (contract_pricer.entry_price) so they cannot drift:
- same LTP source (broker quote -> fresh DB -> model)
- same slippage model (TransactionCosts.apply_fill_slippage)
- same expiry resolution (the broker's live expiry, not a 'weekly' string)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.services.contract_pricer as CP


def _stub_quote(monkeypatch, ltp):
    """entry_price reads the BATCH quote path (get_contract_ltps) - the
    same tier stack as paper and live. Source = broker-quote."""
    def _stub(symbol, contracts, expiry_ymd="", fresh=False, sources=None):
        out = {}
        for key in (contracts or []):
            out[(float(key[0]),
                 "CE" if str(key[1] or "CE").upper().startswith("C") else "PE")] = float(ltp)
            if sources is not None:
                sources[(float(key[0]),
                         "CE" if str(key[1] or "CE").upper().startswith("C") else "PE")] = "broker-quote"
        return out
    monkeypatch.setattr(CP, "get_contract_ltps", _stub)


def test_entry_price_applies_shared_slippage(monkeypatch):
    from core.services.transaction_costs import TransactionCosts
    _stub_quote(monkeypatch, 100.0)
    buy, src = CP.entry_price("NIFTY", 24000, "CE", "2026-10-29", side="BUY")
    sell, _ = CP.entry_price("NIFTY", 24000, "CE", "2026-10-29", side="SELL")
    assert src == "broker-quote"
    exp_buy = round(TransactionCosts.apply_fill_slippage(100.0, "BUY", is_live=True), 2)
    exp_sell = round(TransactionCosts.apply_fill_slippage(100.0, "SELL", is_live=True), 2)
    assert buy == exp_buy, (buy, exp_buy)
    assert sell == exp_sell, (sell, exp_sell)
    # BUY pays more, SELL receives less
    assert buy > 100.0 > sell


def test_paper_and_live_same_price(monkeypatch):
    """Two consecutive calls (paper then live) return the SAME price."""
    _stub_quote(monkeypatch, 85.60)
    paper, _ = CP.entry_price("NIFTY", 24000, "CE", "2026-10-29", side="BUY")
    live, _ = CP.entry_price("NIFTY", 24000, "CE", "2026-10-29", side="BUY")
    assert paper == live, (paper, live)


def test_entry_price_model_fallback(monkeypatch):
    """No broker quote -> model fallback (same for paper and live)."""
    monkeypatch.setattr(CP, "get_contract_ltp", lambda *a, **k: None)
    monkeypatch.setattr(CP, "get_contract_ltps", lambda *a, **k: {})
    monkeypatch.setattr(CP, "_fallback_ltp",
                        lambda *a, **k: (42.0, "model") if k.get("with_source") else 42.0)
    px, src = CP.entry_price("NIFTY", 24000, "CE", "2026-10-29", side="BUY")
    assert src == "model"
    assert px > 42.0  # slippage applied


def test_price_source_detail(monkeypatch):
    _stub_quote(monkeypatch, 12.34)
    det = CP.price_source_detail("NIFTY", 24000, "CE", "2026-10-29")
    assert det["ltp"] == 12.34
    assert det["source"] == "broker-quote"


def test_expiry_hint_weekly_resolves_to_a_date(monkeypatch):
    """"weekly" must resolve to a concrete expiry - never priced as the
    literal string 'weekly' (that was the paper/live price drift)."""
    monkeypatch.setattr(CP, "_fyers_expiry_list", lambda: ["2026-10-29", "2026-11-05"])
    exp = CP.resolve_expiry_hint("NIFTY", "weekly")
    assert exp == "2026-10-29", exp
    assert len(exp) == 10 and "-" in exp

    monkeypatch.setattr(CP, "_fyers_expiry_list", lambda: [])
    exp2 = CP.resolve_expiry_hint("NIFTY", "weekly")
    assert len(exp2) == 10 and "-" in exp2, exp2


def test_expiry_hint_explicit_date_kept(monkeypatch):
    monkeypatch.setattr(CP, "_fyers_expiry_list", lambda: ["2026-10-29"])
    # a past explicit date is not kept (it cannot trade) -> resolved
    out = CP.resolve_expiry_hint("NIFTY", "2020-01-01")
    assert out != "2020-01-01", out
    assert len(out) == 10


def test_resolve_weekly_expiry_prefers_broker_list(monkeypatch):
    monkeypatch.setattr(CP, "_fyers_expiry_list", lambda: ["2026-11-05", "2026-10-29"])
    assert CP.resolve_weekly_expiry("NIFTY") == "2026-10-29"
