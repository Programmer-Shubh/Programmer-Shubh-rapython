"""NSE index archive (FINNIFTY broker-free official source)."""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

_CSV = """Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%),Volume
Nifty Financial Services,29-09-2026,27000.00,27150.00,26900.00,27100.00,100.00,.37,101195775
Nifty Bank,29-09-2026,55350.15,55416.45,54708.80,54918.00,-338.65,-.61,164295019
Nifty Financial Services,30-09-2026,27100.00,27200.00,27050.00,27150.00,50.00,.18,100000000
"""


class _Resp:
    status_code = 200
    text = _CSV


def _dyn_resp(url="", **kw):
    """URL ki date ke hisab se rows (weekday par data, weekend khaali)."""
    import datetime as _dt
    import re as _re

    class _R:
        status_code = 200

        def __init__(self, t):
            self.text = t
    m = _re.search(r"(\d{8})\.csv", str(url))
    try:
        d = _dt.datetime.strptime(m.group(1), "%d%m%Y").date() if m else None
    except Exception:
        d = None
    if not d or d.weekday() >= 5:
        return _R("Index Name,Index Date\n")
    px = 27000.0 + d.day
    t = (("Nifty Financial Services", px, "FIN"),
         ("Nifty Bank", 55000.0 + d.day, "BNK"))
    lines = ["Index Name,Index Date,Open,High,Low,Close,Chg,Chg%,Vol"]
    for nm, c, _ in t:
        lines.append(f"{nm},{d.strftime('%d-%m-%Y')},{c},{c + 50},{c - 50},{c},10,.05,1000")
    return _R("\n".join(lines))


def test_archive_parses_indices(monkeypatch):
    import requests as _rq
    import core.services.historical_fetcher as hf
    monkeypatch.setattr(_rq, "get", _dyn_resp)
    f = hf._fetch_nseindex_historical("FINNIFTY", "2026-09-21", "2026-09-29")
    assert len(f) == 7 and f[0]["close_price"] == 27021.0, f
    assert f[0]["symbol"] == "FINNIFTY"
    b = hf._fetch_nseindex_historical("BANKNIFTY", "2026-09-21", "2026-09-29")
    assert len(b) == 7 and b[0]["close_price"] == 55021.0, b
    # removed symbol -> no network, honest empty
    assert hf._fetch_nseindex_historical("MIDCPNIFTY", "2026-09-21", "2026-09-29") == []


def test_archive_unknown_symbol_no_network(monkeypatch):
    import core.services.historical_fetcher as hf

    def _boom(*a, **k):
        raise AssertionError("network touch nahi hona chahiye")

    import requests as _rq
    monkeypatch.setattr(_rq, "get", _boom)
    assert hf._fetch_nseindex_historical("RELIANCE", "2026-09-29", "2026-09-30") == []


def test_archive_short_window_empty(monkeypatch):
    """<5 bars mile to [] (chain aage badhe, partial jhooth nahi)."""
    import requests as _rq
    import core.services.historical_fetcher as hf

    class _One:
        status_code = 200
        text = ("Index Name,Index Date,Open Index Value,High Index Value,"
                "Low Index Value,Closing Index Value\n"
                "Nifty Financial Services,29-09-2026,27000,27150,26900,27100")
    monkeypatch.setattr(_rq, "get", lambda *a, **k: _One())
    out = hf._fetch_nseindex_historical("FINNIFTY", "2026-09-29", "2026-10-14")
    assert out == [], out


def _big_csv():
    import datetime as _dt
    lines = ["Index Name,Index Date,Open Index Value,High Index Value,"
             "Low Index Value,Closing Index Value,Points Change,Change(%),Volume"]
    d = _dt.date(2026, 9, 1)
    px = 27000.0
    while d <= _dt.date(2026, 9, 28):
        if d.weekday() < 5:
            px += 10.0
            lines.append(
                f"Nifty Financial Services,{d.strftime('%d-%m-%Y')},{px},{px + 50},{px - 50},{px + 20},20,.07,100000000")
        d += _dt.timedelta(days=1)
    return "\n".join(lines)


def test_daily_fallback_prefers_archive(monkeypatch):
    """nsepython+fyers khaali ho to archive se 15+ bars (Yahoo tak jaaye bina)."""
    import routes.strategy_builder as sb
    import core.services.historical_fetcher as hf
    import core.services.free_data as fd
    monkeypatch.setattr(hf, "_fetch_nsepython_historical", lambda *a, **k: [])
    monkeypatch.setattr(hf, "_fetch_fyers_historical", lambda *a, **k: [])

    class _Big:
        status_code = 200
        text = _big_csv()

    import requests as _rq
    monkeypatch.setattr(_rq, "get", lambda *a, **k: _Big())
    monkeypatch.setattr(fd, "fetch_yahoo_daily",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("Yahoo tak pahunchna hi nahi chahiye")))
    bars, src = sb._daily_fallback_range("FINNIFTY", "2026-09-01", "2026-09-28")
    assert src == "nse_index_archive" and len(bars) >= 15, (src, len(bars or []))
