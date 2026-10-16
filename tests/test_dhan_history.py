"""Dhan historical (FINNIFTY/MIDCPNIFTY): parsing + fail-fast + chain wiring."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

_DHAN_BARS = {
    "open": [27000.0, 27100.0], "high": [27150.0, 27200.0],
    "low": [26900.0, 27050.0], "close": [27100.0, 27150.0],
    "volume": [1000, 1200],
    # 2026-09-29/30 00:00 UTC (Tue/Wed - weekday bars, skip nahi honge)
    "timestamp": [1790640000, 1790726400],
}


def test_index_history_parses_dhan_payload():
    """Dhan charts/historical payload -> IST bar dicts."""
    import asyncio as _aio
    from core.services.broker_dhan_live import DhanLive

    async def _fake_req(method, path, data=None):
        assert path == "/charts/historical"
        assert data["securityId"] == "27" and data["exchangeSegment"] == "IDX_I"
        return {"success": True, "data": dict(_DHAN_BARS), "error": ""}

    dl = DhanLive(client_id="C", access_token="T")
    dl._request = _fake_req
    bars = _aio.run(dl.get_index_history("FINNIFTY", "2026-09-29", "2026-09-30"))
    assert len(bars) == 2, bars
    assert bars[0]["trade_date"] == "2026-09-29" and bars[0]["close_price"] == 27100.0
    assert bars[0]["symbol"] == "FINNIFTY"
    # unknown symbol -> []
    assert _aio.run(dl.get_index_history("NOPE", "2026-09-29", "2026-09-30")) == []


def test_fetch_dhan_no_creds_no_network(monkeypatch):
    """Token na ho to turant [] (network touch nahi)."""
    import core.services.contract_pricer as cp
    import core.services.historical_fetcher as hf
    monkeypatch.setattr(cp, "_broker_cfg", lambda name: {})
    called = []
    import urllib.request as _ur
    orig = _ur.urlopen
    monkeypatch.setattr(_ur, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network!")))
    try:
        assert hf._fetch_dhan_historical("FINNIFTY", "2026-09-01", "2026-09-20") == []
    finally:
        monkeypatch.setattr(_ur, "urlopen", orig)
    assert called == []


def test_fetch_dhan_with_creds(monkeypatch):
    import core.services.contract_pricer as cp
    import core.services.historical_fetcher as hf
    import core.services.broker_dhan_live as bdl
    monkeypatch.setattr(cp, "_broker_cfg",
                        lambda name: {"client_id": "C", "access_token": "T"})
    monkeypatch.setattr(cp, "_real_token", lambda tok: True)

    async def _fake_hist(self, symbol, s, e):
        assert symbol == "MIDCPNIFTY"
        return [{"symbol": "MIDCPNIFTY", "trade_date": "2026-09-29",
                 "open_price": 12000.0, "high_price": 12100.0,
                 "low_price": 11950.0, "close_price": 12050.0,
                 "volume": 500, "oi": 0}]

    monkeypatch.setattr(bdl.DhanLive, "get_index_history", _fake_hist)
    out = hf._fetch_dhan_historical("MIDCPNIFTY", "2026-09-29", "2026-09-30")
    assert len(out) == 1 and out[0]["close_price"] == 12050.0
    # non-index symbol -> []
    assert hf._fetch_dhan_historical("RELIANCE", "2026-09-29", "2026-09-30") == []


def test_stooq_wrong_mappings_removed():
    """FINNIFTY/MIDCPNIFTY ke galat Stooq map hate (wrong-instrument guard)."""
    import inspect
    import core.services.historical_fetcher as hf
    src = inspect.getsource(hf._fetch_stooq_historical)
    assert '"FINNIFTY": "^cnxfin"' not in src, src[:600]
    assert '"MIDCPNIFTY": "^nsemidcap50"' not in src, src[:600]
    assert '"NIFTY": "^nsei"' in src  # sahi maps bane rahen
