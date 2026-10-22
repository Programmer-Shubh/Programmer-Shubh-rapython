"""Dynamic lot sizes: broker master parse + priority + delegation."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

MASTER_CSV = """SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_EXCH_INSTRUMENT_TYPE,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_LOT_UNITS
NSE,D,OPT,NIFTY,NIFTY-Oct2026-25000-CE,12345,2026-10-29,25000,CE,75
NSE,D,OPT,APOLLOHOSP,APOLLOHOSP-Oct2026-7800-CE,23456,2026-10-29,7800,CE,125
NSE,D,OPT,BAJAJHLDNG,BAJAJHLDNG-Oct2026-10800-PE,34567,2026-10-29,10800,PE,75
NSE,EQ,EQ,RELIANCE,RELIANCE-EQ,45678,,, ,0
BSE,D,OPT,SENSEX,SENSEX-Oct2026-71000-CE,56789,2026-10-29,71000,CE,10
NSE,D,OPT,ZEROLOT,ZEROLOT-Oct2026-100-CE,67890,2026-10-29,100,CE,0
"""


def test_parse_scrip_master():
    from core.services.lot_master import _parse_master_lines
    m = _parse_master_lines(iter(MASTER_CSV.splitlines()))
    assert m.get("NIFTY") == 75, m
    assert m.get("APOLLOHOSP") == 125, m
    assert m.get("BAJAJHLDNG") == 75, m
    assert "RELIANCE" not in m  # EQ segment skipped
    assert "SENSEX" not in m  # BSE skipped
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
                     ("HDFCAMC", 300), ("MPHASIS", 275), ("OFSS", 100),
                     ("PAGEIND", 20), ("POWERINDIA", 25), ("SOLARINDS", 50),
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
