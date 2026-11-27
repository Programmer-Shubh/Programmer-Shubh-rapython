"""User rule 27-11: data routing, EOD-intraday risk warning, all-symbol
lot/strike-step, expiry payoff Net Credit aur zero-trade diagnostics.

Run: python -m pytest tests/test_data_routing_diagnostics.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_all_supported_symbols_have_exact_strike_step():
    from utils.helpers import get_strike_step
    assert get_strike_step("NIFTY") == 50
    assert get_strike_step("BANKNIFTY") == 100
    assert get_strike_step("FINNIFTY") == 50
    assert get_strike_step("MIDCPNIFTY") == 50
    assert get_strike_step("SENSEX") == 100


def test_midcpnifty_lot_is_not_default_50():
    from utils.helpers import get_lot_size, _STATIC_LOTS
    assert _STATIC_LOTS["MIDCPNIFTY"] == 120
    assert get_lot_size("MIDCPNIFTY") != 50  # purana galat default tha


def test_strike_step_endpoint_matches_single_source():
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as c:
        for sym, step in (("NIFTY", 50), ("BANKNIFTY", 100),
                          ("FINNIFTY", 50), ("MIDCPNIFTY", 50),
                          ("SENSEX", 100)):
            r = c.get("/api/strike-step/" + sym)
            assert r.status_code == 200, (sym, r.text)
            assert r.json() == {"symbol": sym, "strike_step": step}


def test_backend_surfaces_eod_risk_and_per_symbol_source():
    src = open(os.path.join(os.path.dirname(__file__), "..", "routes",
                            "strategy_builder.py"), encoding="utf-8").read()
    for key in ("executed_is_eod_daily", "symbol_sources", "coverage_warning",
                "risk_mode_warning", "no_real_option_premium", '"summary"'):
        assert key in src, key


def test_frontend_renders_new_routing_and_payoff_fields():
    html = open(os.path.join(os.path.dirname(__file__), "..", "static",
                             "index.html"), encoding="utf-8").read()
    assert "/api/strike-step/" in html
    assert "Net Credit" in html
    assert "symbol_sources" in html
    assert "risk_mode_warning" in html
    assert "diagHtml2(r)" in html
