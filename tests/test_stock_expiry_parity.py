"""STOCK option expiry/symbol + live-limit guard parity (27 Oct 2026).

Bug (user report): paper ne DIVISLAB 9700 CE ~Rs 40 / APOLLOHOSP 8200 CE
~Rs 27 dikhaya, par LIVE order Rs 1.35-1.65 limit par gaya aur reject hua.

Root cause (verified vs Fyers NSE_FO master):
- Stock options me WEEKLY expiry hoti hi nahi (sirf monthly). Phir bhi
  stock par NIFTY ki weekly expiry list (2026-11-03) lag rahi thi ->
  symbol `DIVISLAB26N03...` banta tha jo exchange par exist nahi karta ->
  broker quote empty -> shared pricer MODEL price (floor Rs 1.5) par chala
  jata tha.
- Live ne asli monthly (27 Oct) contract par order bheja jahan real
  premium ~Rs 1.5 tha -> LIMIT Rs 1.65. Isliye paper (fake Nov contract
  ka model price Rs 45) vs live (asli Oct contract Rs 1.5) me huge gap.

Fix: stock ke liye ek hi REAL monthly expiry (month ka last Tuesday, guzar
gayi to next month) - paper, chain aur live teeno same contract par.
Saath me: model/DB-close price par live LIMIT kabhi nahi (None ->
MARKET fail-open).
"""
import asyncio
import calendar
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.services.contract_pricer as CP
from core.services.symbol_resolver import (
    _fyers_symbol, is_index_symbol, resolve_contract, stock_monthly_expiry,
)


def _last_tuesday(y, m):
    d = datetime.date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() != 1:
        d -= datetime.timedelta(days=1)
    return d


def test_is_index_symbol():
    assert is_index_symbol("NIFTY") and is_index_symbol("SENSEX")
    assert not is_index_symbol("DIVISLAB") and not is_index_symbol("RELIANCE")


def test_stock_monthly_expiry_is_last_tuesday_on_or_after_today():
    got = stock_monthly_expiry("DIVISLAB")
    d = datetime.datetime.strptime(got, "%Y-%m-%d").date()
    assert d.weekday() == 1, got                      # Tuesday
    assert d >= datetime.date.today(), got
    assert d == _last_tuesday(d.year, d.month), got   # last Tuesday of its month


def test_stock_symbol_never_weekly_format():
    """2026-11-03 (NIFTY weekly) par bhi STOCK symbol monthly hona chahiye -
    weekly code (26N03) exchange par exist nahi karta."""
    s = _fyers_symbol("DIVISLAB", "2026-11-03", 9700, "CE")
    assert s == "NSE:DIVISLAB26NOV9700CE", s
    # index par weekly format waisa hi rahe (2026-11-03 = Tuesday, non-last)
    n = _fyers_symbol("NIFTY", "2026-11-03", 22750, "CE")
    assert n == "NSE:NIFTY26N0322750CE", n


def test_stock_explicit_nifty_weekly_date_snaps_to_monthly():
    got = CP.resolve_expiry_hint("DIVISLAB", "2026-11-03")
    assert got == "2026-11-24", got          # last Tuesday of Nov 2026
    d = datetime.datetime.strptime(got, "%Y-%m-%d").date()
    assert d.weekday() == 1, got


def test_stock_weekly_hint_uses_monthly_not_nifty_list(monkeypatch):
    """'weekly' hint par stock ko NIFTY weekly list (2026-11-03) kabhi na mile."""
    monkeypatch.setattr(CP, "_fyers_expiry_list", lambda: ["2026-11-03", "2026-11-10"])
    monkeypatch.setattr(CP, "_nse_map", lambda s: ("2026-11-03", {7700.0: {"CE": 1.0}}))
    got = CP.resolve_expiry_hint("DIVISLAB", "weekly")
    d = datetime.datetime.strptime(got, "%Y-%m-%d").date()
    assert d.weekday() == 1, got
    assert d == _last_tuesday(d.year, d.month), got
    # index ke liye broker list hi jeetegi (purana behaviour intact)
    assert CP.resolve_expiry_hint("NIFTY", "weekly") == "2026-11-03"


def test_resolve_contract_stock_is_monthly_and_symbol_matches_month(monkeypatch):
    async def _no_expiries(_fy):
        return ["2026-11-03"]          # NIFTY weekly list (stock ke liye galat)

    monkeypatch.setattr("core.services.symbol_resolver._fyers_expiries", _no_expiries)
    res = asyncio.run(resolve_contract(
        "fyers", {"access_token": "x"}, "DIVISLAB", "weekly", 9700, "CE"))
    assert res.get("ok") is True, res
    sym = res["refs"]["symbol"]
    exp = res["expiry_used"]
    mon = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"][
               datetime.datetime.strptime(exp, "%Y-%m-%d").month - 1]
    assert sym == f"NSE:DIVISLAB{exp[2:4]}{mon}9700CE", (sym, exp)
    assert res["source"] == "fyers-stock-monthly"


def test_index_resolve_contract_unchanged(monkeypatch):
    async def _expiries(_fy):
        return ["2026-11-03", "2026-11-10"]

    monkeypatch.setattr("core.services.symbol_resolver._fyers_expiries", _expiries)
    res = asyncio.run(resolve_contract(
        "fyers", {"access_token": "x"}, "NIFTY", "weekly", 22750, "CE"))
    assert res["expiry_used"] == "2026-11-03", res
    assert res["refs"]["symbol"] == "NSE:NIFTY26N0322750CE", res


def test_fy_quote_rows_both_shapes():
    from core.services.contract_pricer import _fy_quote_rows
    v3 = {"s": "ok", "d": [{"n": "NSE:X", "v": {"lp": 1.35}, "s": "ok"}]}
    old = {"data": {"NSE:X": {"last_price": 2.0}}}
    assert _fy_quote_rows(v3)["NSE:X"]["lp"] == 1.35
    assert _fy_quote_rows(old)["NSE:X"]["last_price"] == 2.0


def test_fyers_quote_ltp_parses_v3_d_list(monkeypatch):
    """Fyers v3 asli shape {"d": [{"n": sym, "v": {"lp": ...}}]} - pehle
    sirf {"data": {...}} padha jata tha, isliye har stock quote khali aata tha."""
    monkeypatch.setattr(CP, "_fyers_auth", lambda: ("app", "tok"))
    monkeypatch.setattr(CP, "_fyers_quotes_raw", lambda syms, a="", t="": {
        "s": "ok", "d": [{"n": "NSE:DIVISLAB26OCT9700CE",
                          "v": {"lp": 1.35, "bid": 1.4, "ask": 2.5}, "s": "ok"}]})
    out = CP._fyers_quote_ltp("DIVISLAB", "2026-10-27", [(9700, "CE")])
    assert out == {(9700.0, "CE"): 1.35}, out


def test_live_limit_refuses_model_price(monkeypatch):
    """Model/DB-close price par live LIMIT NAHI banna chahiye (Rs 1.65 fiasco)."""
    from routes.broker import _broker_limit_price

    def _model_only(symbol, contracts, expiry_ymd="", fresh=False, sources=None):
        if sources is not None:
            for k in contracts:
                sources[(float(k[0]), str(k[1]).upper())] = "model"
        return {(float(contracts[0][0]), "CE"): 1.5}

    monkeypatch.setattr(CP, "get_contract_ltps", _model_only)
    assert asyncio.run(_broker_limit_price("DIVISLAB", "CE", 9700, "2026-10-27", "BUY")) is None


def test_live_limit_uses_broker_quote(monkeypatch):
    from routes.broker import _broker_limit_price

    def _quote(symbol, contracts, expiry_ymd="", fresh=False, sources=None):
        if sources is not None:
            for k in contracts:
                sources[(float(k[0]), str(k[1]).upper())] = "broker-quote"
        return {(float(contracts[0][0]), "CE"): 45.65}

    monkeypatch.setattr(CP, "get_contract_ltps", _quote)
    px = asyncio.run(_broker_limit_price("DIVISLAB", "CE", 9700, "2026-10-27", "BUY"))
    # 45.65 + buffer 0.23 -> 45.90 (0.05 tick)
    assert px == 45.9, px
    sell = asyncio.run(_broker_limit_price("DIVISLAB", "CE", 9700, "2026-10-27", "SELL"))
    assert sell == 45.4, sell
