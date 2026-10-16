"""MCX data routing: MCX symbols kabhi generic {SYM}.NS Yahoo par nahi
(SILVER->SILVERBEES ~Rs221 fantasy ka source tha). Proxy (COMEX/NYMEX+FX)
par route, scale sahi, warna khaali (honest INSUFFICIENT)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))


def test_mcx_daily_routes_to_proxy(monkeypatch):
    import core.services.free_data as fd
    import core.services.mcx_feed as mf
    seen = {}

    def _fake_hist(sym, s, e):
        seen["daily"] = sym
        return ([{"symbol": "GOLD", "trade_date": "2026-10-01",
                  "open_price": 150000.0, "high_price": 150100.0,
                  "low_price": 149900.0, "close_price": 150050.0,
                  "volume": 1000, "oi": 0, "source": "proxy"}], "proxy")
    monkeypatch.setattr(mf, "fetch_mcx_history", _fake_hist)
    out = fd.fetch_yahoo_daily("GOLD", "2026-10-01", "2026-10-05")
    assert seen.get("daily") == "GOLD", seen
    assert len(out) == 1 and out[0]["close_price"] == 150050.0


def test_mcx_intraday_routes_to_proxy(monkeypatch):
    import core.services.free_data as fd
    import core.services.mcx_feed as mf
    seen = {}

    def _fake_intra(sym, iv="15m", days=5):
        seen["intra"] = (sym, iv)
        return [{"symbol": "CRUDEOIL", "trade_date": "2026-10-14",
                 "bar_time": "20:15", "open_price": 6300.0,
                 "high_price": 6310.0, "low_price": 6290.0,
                 "close_price": 6305.0, "volume": 500, "oi": 0,
                 "source": "proxy"}]
    monkeypatch.setattr(mf, "fetch_mcx_intraday", _fake_intra)
    out = fd.fetch_yahoo_intraday("CRUDEOIL", "15m", days=5)
    assert seen.get("intra") == ("CRUDEOIL", "15m"), seen
    assert out and out[0]["bar_time"] == "20:15" and out[0]["close_price"] == 6305.0


def test_mcx_intraday_conversion_math(monkeypatch):
    """CL $70 x FX 90 = Rs6300/bbl; bar_time IST preserved."""
    import core.services.free_data as fd
    import core.services.mcx_feed as mf

    def _fake(sym, iv="15m", days=5):
        if sym == "USDINR=X":
            return [{"trade_date": "2026-10-14", "bar_time": "20:15",
                     "open_price": 90.0, "high_price": 90.0,
                     "low_price": 90.0, "close_price": 90.0, "volume": 0}]
        assert sym == "CL=F", sym
        return [{"trade_date": "2026-10-14", "bar_time": "20:15",
                 "open_price": 70.0, "high_price": 70.5,
                 "low_price": 69.8, "close_price": 70.2, "volume": 1200}]
    monkeypatch.setattr(fd, "fetch_yahoo_intraday", _fake)
    monkeypatch.setattr(mf, "_usdinr", lambda: 0.0)  # force bar-FX path
    out = mf.fetch_mcx_intraday("CRUDEOIL", "15m", days=5)
    assert len(out) == 1, out
    assert out[0]["close_price"] == round(70.2 * 90.0, 2), out[0]
    assert out[0]["bar_time"] == "20:15" and out[0]["source"] == "proxy"


def test_mcx_fx_shared_across_symbols(monkeypatch):
    """Ek run me FX 1 baar fetch ho (3 symbols = 1 FX chain, pahle 3x tha)."""
    import core.services.free_data as fd
    import core.services.mcx_feed as mf
    mf._FXSER_CACHE.clear()
    calls = {"fx": 0, "daily": 0}

    def _fake_intra(sym, iv="15m", days=5):
        if sym == "USDINR=X":
            calls["fx"] += 1
            return [_px_bar2("2026-10-14", "20:15", 90.0)]
        return [_px_bar2("2026-10-14", "20:15", 70.0, v=100)]

    def _fake_daily(sym, s, e, timeout=10):
        calls["daily"] += 1
        return [{"trade_date": "2026-10-14", "close_price": 90.0}]

    def _px_bar2(d, t, c, v=0):
        return {"trade_date": d, "bar_time": t, "open_price": c,
                "high_price": c, "low_price": c, "close_price": c,
                "volume": v}

    monkeypatch.setattr(fd, "fetch_yahoo_intraday", _fake_intra)
    monkeypatch.setattr(fd, "fetch_yahoo_daily", _fake_daily)
    monkeypatch.setattr(mf, "_usdinr", lambda: 0.0)
    assert len(mf.fetch_mcx_intraday("CRUDEOIL", "15m", days=5)) == 1
    assert len(mf.fetch_mcx_intraday("CRUDEOIL", "15m", days=5)) == 1
    assert calls["fx"] == 1 and calls["daily"] == 1, calls
    mf._FXSER_CACHE.clear()


def test_mcx_no_proxy_means_empty(monkeypatch):
    import core.services.mcx_feed as mf
    assert mf.get_mcx_ltp("NICKEL") is None
    bars, src = mf.fetch_mcx_history("NICKEL", "2026-09-01", "2026-09-05")
    assert bars == [] and src == "none"


def _px_bar(d, t, o, h, l, c, v=100):
    return {"trade_date": d, "bar_time": t, "open_price": o,
            "high_price": h, "low_price": l, "close_price": c, "volume": v}


def test_mcx_intraday_fx_fallback_daily_seed(monkeypatch):
    """Intraday-FX na mile to daily-FX seed se bars bachen (skip nahi)."""
    import core.services.free_data as fd
    import core.services.mcx_feed as mf

    def _fake_intra(sym, iv="15m", days=5):
        if sym == "USDINR=X":
            return []
        assert sym == "CL=F", sym
        return [_px_bar("2026-10-14", "20:15", 70.0, 70.5, 69.8, 70.2),
                _px_bar("2026-10-14", "20:30", 70.2, 70.6, 70.0, 70.4)]

    def _fake_daily(sym, s, e, timeout=10):
        assert sym == "USDINR=X", sym
        return [{"trade_date": "2026-10-14", "close_price": 90.0}]

    monkeypatch.setattr(fd, "fetch_yahoo_intraday", _fake_intra)
    monkeypatch.setattr(fd, "fetch_yahoo_daily", _fake_daily)
    monkeypatch.setattr(mf, "_usdinr", lambda: 0.0)
    out = mf.fetch_mcx_intraday("CRUDEOIL", "15m", days=5)
    assert len(out) == 2, out
    assert out[0]["close_price"] == round(70.2 * 90.0, 2)
    assert out[1]["close_price"] == round(70.4 * 90.0, 2)
