"""Heavy-DB ops must be chunked (pooler statement timeout killed PG)."""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.models.database import Database
from core.models.bhavcopy_model import BhavcopyModel


def _mkrows(sym, date, n, strike0=25000.0):
    out = []
    for i in range(n):
        out.append({"symbol": sym, "trade_date": date, "expiry_date": "2026-10-29",
                    "strike_price": strike0 + i, "option_type": "CE",
                    "open_price": 10.0, "high_price": 11.0, "low_price": 9.0,
                    "close_price": 10.5, "volume": 100, "oi": 50})
    return out


def test_import_chunked_and_counted():
    db = Database.get_instance()
    db.execute("DELETE FROM bhavcopy_data WHERE symbol='TCHUNK'")
    n = BhavcopyModel().import_data(_mkrows("TCHUNK", "2026-09-01", 2500))
    assert n == 2500, n
    c = db.fetch_one("SELECT COUNT(*) c FROM bhavcopy_data WHERE symbol='TCHUNK'")
    assert int(c["c"]) == 2500, c
    # re-import same keys: dedupe, no duplicates, count stable
    n2 = BhavcopyModel().import_data(_mkrows("TCHUNK", "2026-09-01", 2500))
    assert n2 == 2500, n2
    c2 = db.fetch_one("SELECT COUNT(*) c FROM bhavcopy_data WHERE symbol='TCHUNK'")
    assert int(c2["c"]) == 2500, c2
    db.execute("DELETE FROM bhavcopy_data WHERE symbol='TCHUNK'")


def test_purge_chunked():
    from core.services import nsefin_bhav as fb
    db = Database.get_instance()
    db.execute("DELETE FROM bhavcopy_data WHERE symbol='TPURGE'")
    BhavcopyModel().import_data(_mkrows("TPURGE", "2020-01-02", 100))
    BhavcopyModel().import_data(_mkrows("TPURGE", "2026-10-01", 100))
    n = fb.purge_old_data(months=1)  # cutoff ~Sep 2026 -> kills 2020 rows
    assert n > 0, n
    c = db.fetch_one("SELECT COUNT(*) c FROM bhavcopy_data WHERE symbol='TPURGE'")
    assert int(c["c"]) == 100, c  # recent survives
    db.execute("DELETE FROM bhavcopy_data WHERE symbol='TPURGE'")


def test_missing_recent_single_scan(monkeypatch):
    from core.services import nsefin_bhav as fb
    calls = {"n": 0}
    orig_fetch = Database.get_instance().fetch_all

    def counting(*a, **k):
        calls["n"] += 1
        return orig_fetch(*a, **k)

    monkeypatch.setattr(Database.get_instance(), "fetch_all", counting)
    out = fb.missing_recent_dates(5)
    assert calls["n"] == 1, calls  # exactly ONE grouped query
    assert isinstance(out, list)
