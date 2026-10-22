"""Expiry + entry-timing parity guards (26 Oct kal wala paper/backtest gap).

Root causes these cover:
- Phantom expiry: paper 'weekly' resolved to today+7 = a Thursday (10-29)
  jo REAL NIFTY expiry nahi - broker/NSE quotes us contract par aate hi
  nahi aur paper entry MODEL price par lag jati thi (backtest DB-real
  premiums se durr). Ab: real weekday rule (NSE Tue / BSE Thu) + explicit
  future dates ka nearest-real-expiry snap.
- Wrong-contract price: NSE-chain tier ka 'nearest expiry' fallback DOOSRE
  month ka LTP de deta tha (10-29 request me 11-03 rate).
- Entry-timing: auto-worker entries entry_time window ke bahar (10:12,
  12:42) = backtest 09:35 bar se alag price = results mismatch.
"""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.services.contract_pricer as CP


def _future_date(days_ahead: int):
    today = datetime.date.today()
    return (today + datetime.timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def test_weekly_fallback_is_real_weekday_nse(monkeypatch):
    """Broker + NSE list dono fail -> weekday rule (NSE weekly = Tuesday),
    na ki today+7 phantom (Thursday)."""
    monkeypatch.setattr(CP, "_fyers_expiry_list", lambda: [])
    monkeypatch.setattr(CP, "_nse_map", lambda s: ("", {}))
    exp = CP.resolve_weekly_expiry("NIFTY")
    assert exp and len(exp) == 10
    d = datetime.datetime.strptime(exp, "%Y-%m-%d").date()
    assert d.weekday() == 1, exp  # Tuesday
    assert d >= datetime.date.today(), exp


def test_weekly_fallback_is_real_weekday_bse(monkeypatch):
    monkeypatch.setattr(CP, "_fyers_expiry_list", lambda: [])
    monkeypatch.setattr(CP, "_nse_map", lambda s: ("", {}))
    exp = CP.resolve_weekly_expiry("SENSEX")
    d = datetime.datetime.strptime(exp, "%Y-%m-%d").date()
    assert d.weekday() == 3, exp  # Thursday


def test_broker_list_still_wins(monkeypatch):
    """Fyers live list milta hai to WAHI date (live order routing wali)."""
    monkeypatch.setattr(CP, "_fyers_expiry_list", lambda: ["2026-11-05", "2026-10-27"])
    assert CP.resolve_weekly_expiry("NIFTY") == "2026-10-27"


def _next_thursday(days_from=21):
    today = datetime.date.today()
    d = today + datetime.timedelta(days=days_from)
    while d.weekday() != 3:  # Thursday
        d += datetime.timedelta(days=1)
    return d


def test_phantom_thursday_explicit_snaps_to_tuesday():
    """Explicit future date jo NIFTY ki real expiry nahi (Thursday) ->
    nearest real weekly (Tuesday, distance 2 <= 5)."""
    d = _next_thursday()
    got = CP.resolve_expiry_hint("NIFTY", d.strftime("%Y-%m-%d"))
    got_d = datetime.datetime.strptime(got, "%Y-%m-%d").date()
    assert got_d.weekday() == 1, (d, got)
    assert abs((got_d - d).days) == 2, (d, got)


def test_real_tuesday_explicit_kept():
    d = _next_thursday() - datetime.timedelta(days=2)  # Tuesday
    got = CP.resolve_expiry_hint("NIFTY", d.strftime("%Y-%m-%d"))
    assert got == d.strftime("%Y-%m-%d"), got


def test_sensex_tuesday_snaps_to_thursday():
    """BSE weekly = Thursday: explicit Tuesday SENSEX ke liye snap."""
    d = _next_thursday() - datetime.timedelta(days=2)  # a Tuesday
    got = CP.resolve_expiry_hint("SENSEX", d.strftime("%Y-%m-%d"))
    got_d = datetime.datetime.strptime(got, "%Y-%m-%d").date()
    assert got_d.weekday() == 3, (d, got)
    assert abs((got_d - d).days) == 2, (d, got)


def test_weekly_hint_never_returns_thursday_nifty(monkeypatch):
    """Kal wala bug: 'weekly' hint kabhi Thursday NIFTY expiry na de."""
    monkeypatch.setattr(CP, "_fyers_expiry_list", lambda: [])
    monkeypatch.setattr(CP, "_nse_map", lambda s: ("", {}))
    for _ in range(3):
        out = CP.resolve_expiry_hint("NIFTY", "weekly")
        assert len(out) == 10
        d = datetime.datetime.strptime(out, "%Y-%m-%d").date()
        assert d.weekday() == 1, out
        break


def test_nse_chain_ltp_exact_expiry_only(monkeypatch):
    """Wrong-contract guard: exact expiry absent -> 0 (nahi: doosre month
    ka LTP). Wrong price par SELL entry ka paper-loss mai chal cancer tha."""
    from core.services import free_data as FD

    monkeypatch.setattr(FD, "fetch_nse_option_chain", lambda s: {
        "2026-10-22": {7700.0: {"CE": 12.0}},
        "2026-11-03": {7700.0: {"CE": 148.0}},
    })
    # exact match works
    assert FD.nse_chain_ltp("APOLLOHOSP", 7700, "CE", "2026-10-22") == 12.0
    # requested expiry absent -> 0 (kabhi wrong-contract ka 148 nahi)
    assert FD.nse_chain_ltp("APOLLOHOSP", 7700, "CE", "2026-10-29") == 0.0


def test_auto_paper_entry_window_gate():
    """Entry-time parity: window bahar ka auto-entry skip, andar ka allowed.
    (conftest suite-wide flag production path ko chalane par fail-open.)"""
    from core.services.auto_paper import _entry_window_ok_for

    notes = []
    adv = {"entry_time": "09:35"}
    ok, used = _entry_window_ok_for(adv, notes, _now="2026-10-22T09:38:00+05:30")
    assert ok is True and used == "09:35"
    ok2, _ = _entry_window_ok_for(adv, notes, _now="2026-10-22T12:42:00+05:30")
    assert ok2 is False
    assert any("entry_time" in n for n in notes)
    # default entry_time jab saved na ho
    ok3, used3 = _entry_window_ok_for({}, [], _now="2026-10-22T09:35:00+05:30")
    assert ok3 is True and used3 == "09:35"
    # bogus entry_time -> default 09:35 rule lagta hai (crash nahi)
    ok4, _ = _entry_window_ok_for({"entry_time": "bogus"}, [], _now="2026-10-22T17:00:00+05:30")
    assert ok4 is False
    ok5, _ = _entry_window_ok_for({"entry_time": "bogus"}, [], _now="2026-10-22T09:40:00+05:30")
    assert ok5 is True


def test_snap_inside_all_localised_helpers():
    # direct helper sanity (no resolver chain)
    assert CP._snap_future_expiry("NIFTY", _next_thursday().strftime("%Y-%m-%d")) \
        == (_next_thursday() - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    # BankNifty same NSE rule
    got = CP._snap_future_expiry("BANKNIFTY", _next_thursday().strftime("%Y-%m-%d"))
    assert datetime.datetime.strptime(got, "%Y-%m-%d").date().weekday() == 1
