"""Dynamic lot sizes: broker master parse + priority + delegation."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

MASTER_CSV = """101126111735028,NIFTY 17 Nov 26 17350 CE,14,75,0.05,,0915-1540|1815-1915:,2026-11-16,1794910200,NSE:NIFTY26N1717350CE,10,11,35028,NIFTY,26000,17350.0,CE,101000000026010,None,0,0.0
101126112224901,APOLLOHOSP 20 Oct 26 7800 CE,14,125,0.05,,0915-1540|1815-1915:,2026-11-16,1794910200,NSE:APOLLOHOSP26O207800CE,10,11,40290,APOLLOHOSP,28100,7800.0,CE,101000000028100,None,0,0.0
101126112224902,BAJAJHLDNG 20 Oct 26 10800 PE,14,75,0.05,,0915-1540|1815-1915:,2026-11-16,1794910200,NSE:BAJAJHLDNG26O2010800PE,10,11,40291,BAJAJHLDNG,28101,10800.0,PE,101000000028101,None,0,0.0
101126112224903,RELIANCE Cash Equity,1,500,0.05,,0915-1530|1515-1815:,2026-11-16,1794910200,NSE:RELIANCE-EQ,10,11,40292,RELIANCE,28102,-1.0,XX,101000000028102,None,0,0.0
101126112224904,ZEROLOT 20 Oct 26 100 CE,14,0,0.05,,0915-1540|1815-1915:,2026-11-16,1794910200,NSE:ZEROLOT26O20100CE,10,11,40293,ZEROLOT,28103,100.0,CE,101000000028103,None,0,0.0
"""


def test_parse_scrip_master():
    from core.services.lot_master import _parse_master_lines
    m = _parse_master_lines(iter(MASTER_CSV.splitlines()))
    assert m.get("NIFTY") == 75, m
    assert m.get("APOLLOHOSP") == 125, m
    assert m.get("BAJAJHLDNG") == 75, m
    assert "RELIANCE" not in m  # CM (non-F&O) segment skipped
    assert "ZEROLOT" not in m  # zero lot skipped


def test_hyphen_insensitive_lookup(monkeypatch):
    import core.services.lot_master as lm
    import time as _t
    monkeypatch.setattr(lm, "_MEM", {"BAJAJAUTO": 50})
    monkeypatch.setattr(lm, "_MEM_TS", _t.time())
    assert lm.get_lot("BAJAJ-AUTO") == (50, "broker-master")


def test_new_fno_static_lots(monkeypatch):
    import core.services.lot_master as lm
    import utils.helpers as H
    monkeypatch.setattr(lm, "_MEM", None)
    monkeypatch.setattr(lm, "_MEM_TS", 0.0)
    monkeypatch.setattr(H, "_load_nse_lots", lambda: None)
    for sym, lot in [("BOSCHLTD", 25), ("EICHERMOT", 100), ("FORCEMOT", 25),
                     ("HDFCAMC", 300), ("MPHASIS", 275), ("OFSS", 50),
                     ("PAGEIND", 20), ("POWERINDIA", 25), ("SOLARINDS", 25),
                     ("SRF", 200), ("WAAREEENER", 175)]:
        assert H.get_lot_size(sym) == lot, (sym, H.get_lot_size(sym))
        assert H.official_lot(sym)[2] is True, sym


def test_broker_beats_static(monkeypatch):
    import core.services.lot_master as lm
    import time as _t
    monkeypatch.setattr(lm, "_MEM", {"APOLLOHOSP": 125, "NIFTY": 75})
    monkeypatch.setattr(lm, "_MEM_TS", _t.time())
    lot, src = lm.get_lot("apollohosp")
    assert (lot, src) == (125, "broker-master"), (lot, src)
    lot2, src2 = lm.get_lot("UNKNOWNXYZ")
    assert (lot2, src2) == (None, "cold"), (lot2, src2)


def test_get_lot_size_dynamic_and_corrected(monkeypatch):
    import core.services.lot_master as lm
    import utils.helpers as H
    # broker says 125 even though old map had 75
    monkeypatch.setattr(lm, "_MEM", {"APOLLOHOSP": 125})
    import time as _t
    monkeypatch.setattr(lm, "_MEM_TS", _t.time())
    assert H.get_lot_size("APOLLOHOSP") == 125
    # cold broker + NSE fail -> corrected static map
    monkeypatch.setattr(lm, "_MEM", None)
    monkeypatch.setattr(lm, "_MEM_TS", 0.0)
    monkeypatch.setattr(H, "_load_nse_lots", lambda: None)
    assert H.get_lot_size("APOLLOHOSP") == 125
    assert H.get_lot_size("BAJAJHLDNG") == 75
    assert H.get_lot_size("NIFTY") == 65
    assert H.get_lot_size("BANKNIFTY") == 30
    assert H.get_lot_size("TOTALLYUNKNOWN") == 50
    assert H.lot_source("APOLLOHOSP") in ("static-map", "broker-master", "nse")


def test_effective_lot_prefers_dynamic(monkeypatch):
    import utils.helpers as H
    monkeypatch.setattr(H, "get_lot_size", lambda s: 125)
    assert H.effective_lot_size("APOLLOHOSP", 50) == 125  # stale 50 fixed
    assert H.effective_lot_size("APOLLOHOSP", 175) == 125  # stale 175 fixed

    def _boom(s):
        raise RuntimeError("down")

    monkeypatch.setattr(H, "get_lot_size", _boom)
    assert H.effective_lot_size("APOLLOHOSP", 175) == 175  # fallback stored
    assert H.effective_lot_size("APOLLOHOSP", None) == 50
