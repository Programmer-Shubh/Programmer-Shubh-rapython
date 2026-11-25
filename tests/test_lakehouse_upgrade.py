"""Zero-copy lakehouse: idempotent ingest, pruned query, library, missing dates."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services import option_lakehouse as lh


def test_store_query_dedupe_library_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(lh, "OPTION_TICKS_ROOT", Path(tmp_path))
    rows = [
        {"timestamp": "2026-11-25 09:15:00", "underlying": "NIFTY", "expiry": "2026-11-26",
         "option_type": "CE", "strike": 25000, "ltp": 120.5, "high_price": 125,
         "low_price": 118, "source": "nse_bhavcopy", "data_mode": "1m_chain"},
        {"timestamp": "2026-11-25 09:16:00", "underlying": "NIFTY", "expiry": "2026-11-26",
         "option_type": "CE", "strike": 25000, "ltp": 122, "source": "nse_bhavcopy",
         "data_mode": "1m_chain"},
    ]
    assert lh.store_option_ticks(rows) == 2
    assert lh.store_option_ticks(rows) == 0          # idempotent, no re-upload
    df = lh.query_option_ticks("NIFTY", "1m_chain", "2026-11-26", "2026-11-25", "2026-11-25",
                               option_type="CE", strike=25000)
    assert len(df) == 2
    assert df.sort("timestamp")["ltp"].to_list() == [120.5, 122.0]
    assert df["bid"].to_list()[0] == 120.5          # bid/ask fallback to real ltp
    st = lh.get_lakehouse_status()
    assert st["symbols"] == ["NIFTY"]
    assert st["modes"] == ["1m_chain"]
    lib = lh.get_lakehouse_library()
    assert lib["library"][0]["symbol"] == "NIFTY"
    assert lib["library"][0]["label"] == "1M OPTION CHAIN"
    assert lib["library"][0]["days"] >= 1
    miss = lh.missing_dates("NIFTY", "1m_chain", "2026-11-23", "2026-11-25")
    assert "2026-11-23" in miss and "2026-11-25" not in miss


def test_synthetic_source_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(lh, "OPTION_TICKS_ROOT", Path(tmp_path))
    out = lh.store_option_ticks([{"timestamp": "2026-11-25 09:15:00", "underlying": "NIFTY",
                                  "expiry": "2026-11-26", "option_type": "CE", "strike": 25000,
                                  "ltp": 10, "source": "model", "data_mode": "tick"}])
    assert out == 0


def test_compaction_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(lh, "OPTION_TICKS_ROOT", Path(tmp_path))
    r = {"timestamp": "2026-11-24 09:15:00", "underlying": "NIFTY", "expiry": "2026-11-26",
         "option_type": "CE", "strike": 25000, "ltp": 100, "source": "csv_upload",
         "data_mode": "tick"}
    assert lh.store_option_ticks([r]) == 1
    assert lh.store_option_ticks([dict(r, timestamp="2026-11-24 09:16:00", ltp=101)]) == 1
    out = lh.compact_lakehouse()
    assert out["errors"] == 0
    assert len(lh.query_option_ticks("NIFTY", "tick", "2026-11-26", "2026-11-24", "2026-11-24")) == 2
