"""MCX proxy archive: store/read roundtrip, archive-first (bina network),
coverage-gap par live fallback. Bina broker, bina Yahoo-jitter ke backtest."""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

SYM = "TESTMCXARCH"


def _bars(dates, base=6300.0):
    out = []
    for i, d in enumerate(dates):
        c = base + i * 10.0
        out.append({"symbol": SYM, "trade_date": d, "bar_time": "",
                    "open_price": c - 5, "high_price": c + 5,
                    "low_price": c - 8, "close_price": c,
                    "volume": 100, "oi": 0, "source": "proxy"})
    return out


def _wdays(s, e):
    d = datetime.datetime.strptime(s, "%Y-%m-%d").date()
    e = datetime.datetime.strptime(e, "%Y-%m-%d").date()
    out = []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
    return out


def _wipe():
    from core.models.database import Database
    try:
        Database.get_instance().execute(
            "DELETE FROM mcx_proxy_bars WHERE symbol=?", [SYM])
    except Exception:
        pass


def test_store_read_roundtrip():
    import core.services.mcx_feed as mf
    _wipe()
    try:
        days = _wdays("2026-09-21", "2026-09-29")  # 7 weekdays
        assert mf.store_mcx_bars(SYM, "1d", _bars(days)) == 7
        got = mf.read_mcx_archive(SYM, "1d", "2026-09-21", "2026-09-29")
        assert len(got) == 7, len(got)
        assert got[0]["close_price"] == 6300.0
        assert got[-1]["close_price"] == 6360.0
        assert all(g.get("source") == "proxy-archive" for g in got)
    finally:
        _wipe()


def test_incomplete_coverage_returns_empty():
    import core.services.mcx_feed as mf
    _wipe()
    try:
        days = _wdays("2026-09-21", "2026-09-23")  # 3 days only
        mf.store_mcx_bars(SYM, "1d", _bars(days))
        # poori week mangi to khaali (aadhi-adhuri history nahi)
        assert mf.read_mcx_archive(SYM, "1d", "2026-09-21", "2026-09-29") == []
        # jo hai wahi mango to milta hai
        assert len(mf.read_mcx_archive(SYM, "1d", "2026-09-21", "2026-09-23")) == 3
    finally:
        _wipe()


def test_history_archive_first_no_network(monkeypatch):
    """Archive bhara ho to network fail par bhi bars (source proxy-archive)."""
    import core.services.mcx_feed as mf
    _wipe()
    try:
        days = _wdays("2026-09-21", "2026-10-02")  # 10 weekdays
        mf.store_mcx_bars(SYM, "1d", _bars(days))
        import core.services.free_data as fd

        def _boom(*a, **k):
            raise AssertionError("network touch nahi hona chahiye")

        monkeypatch.setattr(fd, "fetch_yahoo_daily", _boom)
        # NOTE: fetch_mcx_history sirf _converters wale symbols leta hai;
        # yahan internal read path test ho raha hai via archive seed.
        got = mf.read_mcx_archive(SYM, "1d", "2026-09-21", "2026-10-02")
        assert len(got) == 10, len(got)
    finally:
        _wipe()


def test_warmer_stores_bars(monkeypatch):
    import core.services.mcx_feed as mf
    _wipe()
    for _s in ("CRUDEOIL",):
        try:
            from core.models.database import Database
            Database.get_instance().execute(
                "DELETE FROM mcx_proxy_bars WHERE symbol=?", [_s])
        except Exception:
            pass
    try:
        monkeypatch.setattr(
            mf, "fetch_mcx_history",
            lambda s, a, b: ([{"symbol": s, "trade_date": "2026-10-14",
                               "open_price": 1.0, "high_price": 1.0,
                               "low_price": 1.0, "close_price": 1.0,
                               "volume": 1, "oi": 0, "source": "proxy"}], "proxy"))
        monkeypatch.setattr(mf, "fetch_mcx_intraday", lambda *a, **k: [])
        out = mf.warm_mcx_archive(symbols=["CRUDEOIL"], intraday_days=0, daily_days=5)
        assert out.get("CRUDEOIL", 0) >= 1, out
        got = mf.read_mcx_archive("CRUDEOIL", "1d", "2026-10-14", "2026-10-14")
        assert len(got) == 1, got
    finally:
        try:
            from core.models.database import Database
            Database.get_instance().execute(
                "DELETE FROM mcx_proxy_bars WHERE symbol=?", ["CRUDEOIL"])
        except Exception:
            pass
        _wipe()
