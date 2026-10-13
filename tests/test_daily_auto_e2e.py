"""E2E verification: Daily Auto pipeline (no orders placed - dry paths only).

1. Yahoo 3-attempt retry (fail,fail,ok + all-fail logging).
2. 15-bar single-day gate / 25-bar worker gate.
3. Worker gating plumbing (market closed -> skip, no strategies -> empty).
4. Lookback entry uses CURRENT saved legs + respects max/day.
5. Mode separation: auto path never touches broker/live.place endpoints.
"""
import os
import sys
import json
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Sess:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        item = self.script.pop(0) if self.script else (500, {})
        if isinstance(item, Exception):
            raise item
        code, payload = item
        return _Resp(code, payload)


def _bars(n, start="2026-10-13", same_day=False):
    import datetime as _dt
    out = []
    if same_day:
        # N intraday bars on ONE date (09:15 onwards, 15m apart)
        d = _dt.datetime.strptime(start, "%Y-%m-%d").date()
        px = 25000.0
        mins = 9 * 60 + 15
        for k in range(n):
            px *= 1.001
            hh, mm = divmod(mins + k * 15, 60)
            out.append({"symbol": "NIFTY", "trade_date": d.strftime("%Y-%m-%d"),
                        "bar_time": f"{hh:02d}:{mm:02d}",
                        "open_price": px, "high_price": px * 1.001,
                        "low_price": px * 0.999, "close_price": px,
                        "volume": 500000, "oi": 0})
        return out
    d = _dt.datetime.strptime(start, "%Y-%m-%d").date()
    px = 25000.0
    while len(out) < n:
        if d.weekday() < 5:
            px *= 1.001
            out.append({"symbol": "NIFTY", "trade_date": d.strftime("%Y-%m-%d"),
                        "bar_time": "10:00", "open_price": px, "high_price": px * 1.001,
                        "low_price": px * 0.999, "close_price": px,
                        "volume": 500000, "oi": 0})
        d += _dt.timedelta(days=1)
    return out


def test_retry_then_success(monkeypatch, capsys):
    import core.services.free_data as fd
    good_ts = [1720000000 + i * 900 for i in range(30)]
    payload = {"chart": {"result": [{
        "timestamp": good_ts,
        "indicators": {"quote": [{
            "open": [100.0] * 30, "high": [101.0] * 30,
            "low": [99.0] * 30, "close": [100.5] * 30,
            "volume": [1000] * 30}]}}]}}
    sess = _Sess([(429, {}), TimeoutError("t/o"), (200, payload)])
    monkeypatch.setattr(fd, "_yahoo_session", lambda: sess)
    bars = fd.fetch_yahoo_intraday("NIFTY", "15m", days=2, timeout=5)
    assert len(bars) == 30, len(bars)
    assert len(sess.calls) == 3, sess.calls  # 3 attempts used
    assert "query2." in sess.calls[1]  # host rotation
    assert all(b.get("trade_date") and b.get("bar_time") for b in bars)


def test_all_fail_empty_and_logged(monkeypatch, capsys):
    import core.services.free_data as fd
    sess = _Sess([(429, {}), (500, {}), TimeoutError("x")])
    monkeypatch.setattr(fd, "_yahoo_session", lambda: sess)
    bars = fd.fetch_yahoo_intraday("NIFTY", "15m", days=2, timeout=5)
    assert bars == []
    assert len(sess.calls) == 3
    out = capsys.readouterr().out
    assert out.count("FAILED") >= 2 and "EMPTY after 3 tries" in out, out


def test_single_day_gate_span_aware(monkeypatch):
    import routes.strategy_builder as sb2
    # fetch is imported inside free_data users; patch at source module
    monkeypatch.setattr("core.services.free_data.fetch_yahoo_intraday",
                        lambda *a, **k: _bars(26, same_day=True))
    bars, src = sb2._intraday_full_range("NIFTY", "15m", "2026-10-13", "2026-10-13")
    assert src == "yahoo_intraday" and len(bars) == 26, (src, len(bars))
    # 10 bars -> honestly refused
    monkeypatch.setattr("core.services.free_data.fetch_yahoo_intraday",
                        lambda *a, **k: _bars(10))
    bars2, src2 = sb2._intraday_full_range("NIFTY", "15m", "2026-10-13", "2026-10-13")
    assert bars2 == [] and src2 == "no-intraday-data", (src2, len(bars2))


def test_worker_gate_passes_with_25_bars(monkeypatch):
    import core.services.auto_paper as ap
    ap._INTRA_CACHE.clear()
    bars = _bars(30, same_day=True)
    monkeypatch.setattr("core.services.scanner.OptionScanner._fetch_intraday_bars",
                        lambda self, sym, tf="15m", **k: bars)
    s = {"id": 1, "symbol": "NIFTY",
         "legs": json.dumps([{"option_type": "CE", "transaction": "buy", "lots": 1}]),
         "indicators": json.dumps([{"id": "supertrend",
                                    "params": {"period": 10, "multiplier": 3}}]),
         "advanced_options": json.dumps({"trade_mode": "intraday", "timeframe": "15m"}),
         "risk_management": json.dumps({"daily_stop_loss": 1500,
                                        "daily_take_profit": 3000,
                                        "max_trades_per_day": 5})}
    fired, note = ap._intraday_signal(1, s, "2026-10-13")
    assert "no fresh 15m bars" not in note, note
    # 20 bars -> gate refuses (below worker minimum)
    ap._INTRA_CACHE.clear()
    monkeypatch.setattr("core.services.scanner.OptionScanner._fetch_intraday_bars",
                        lambda self, sym, tf="15m", **k: _bars(20))
    fired2, note2 = ap._intraday_signal(1, s, "2026-10-13")
    assert fired2 is None and "25+" in note2, (fired2, note2)


def test_run_once_market_closed_skips(monkeypatch):
    import core.services.auto_paper as ap
    monkeypatch.setattr(ap, "_market_open", lambda: False)
    out = ap.run_once(min_interval=0, market_hours=True)
    assert out.get("skipped") == "market-closed", out
    assert out.get("placed") == []


def test_lookback_entry_uses_current_legs_and_cap(monkeypatch):
    import core.services.auto_paper as ap
    from core.models.database import Database
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE name='t-lb-verify'")
    db.execute(
        "INSERT INTO strategies (user_id, name, symbol, legs, risk_management,"
        " status) VALUES (1,'t-lb-verify','NIFTY',?,?,?)",
        [json.dumps([{"option_type": "CE", "transaction": "buy", "lots": 2}]),
         json.dumps({"daily_stop_loss": 1500, "daily_take_profit": 3000,
                     "max_trades_per_day": 5, "signal_lookback_days": 5}),
         "active"])
    sid = int(db.fetch_one("SELECT id FROM strategies WHERE name='t-lb-verify'")["id"])
    today = (datetime.datetime.utcnow()
             + datetime.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
    old = (datetime.datetime.strptime(today, "%Y-%m-%d")
           - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    seen = {}

    def fake_signal(a, b, c):
        # old signal only (no today hit) -> lookback path
        from core.services import auto_paper as _ap
        tl = [{"entry_date": old, "pnl": 10.0}]
        ok, _ = _ap._recent_signal_ok([old], c, 5, [])
        assert ok
        return True, "2 din purana"

    def fake_exec(s, legs, db_, tm_, tday):
        seen["legs"] = legs
        return [{"trade_id": 99}], []

    try:
        monkeypatch.setattr(ap, "_today_signal", fake_signal)
        monkeypatch.setattr(ap, "_execute_legs", fake_exec)
        out = ap.place_for_strategy(sid, signal_check="daily")
        assert out["placed"], out
        # legs forwarded must be the CURRENT saved legs (lots 2), not stale
        assert seen["legs"][0].get("lots") == 2, seen
        # cap still enforced with lookback on: fill 5 closed today -> blocked
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        for _ in range(5):
            db.execute(
                "INSERT INTO paper_trades (symbol, option_type, strike_price,"
                " transaction_type, quantity, lot_size, entry_price, exit_price,"
                " entry_date, exit_date, pnl, status, strategy_id) VALUES "
                "('NIFTY','CE',25000,'BUY',1,75,100.0,90.0,?,?, -750.0,'closed',?)",
                [today, today, sid])
        out2 = ap.place_for_strategy(sid, signal_check="daily")
        assert out2["placed"] == [] and any(
            "max" in str(n).lower() for n in out2.get("notes", [])), out2
    finally:
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        db.execute("DELETE FROM strategies WHERE id=?", [sid])


def test_auto_path_never_touches_broker():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    checked = []
    for rel in ("core/services/auto_paper.py",
                "core/services/sl_monitor.py",
                "scripts/auto_trade.py",
                "core/services/execution_engine.py"):
        src = (root / rel).read_text(encoding="utf-8", errors="replace")
        checked.append(rel)
        assert "place-live" not in src, rel
        assert "place_live" not in src, rel
    # heartbeat route itself must not reference broker either
    src = (root / "routes" / "paper_trade.py").read_text(encoding="utf-8", errors="replace")
    assert "place-live" not in src
    assert checked
