"""Fail-closed gates + per-day signal cache (no duplicate engine runs)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_gate_crash_skips_not_places(monkeypatch):
    import core.services.auto_paper as ap
    from core.models.database import Database
    import json
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE name='t-gate-crash'")
    db.execute(
        "INSERT INTO strategies (user_id, name, symbol, legs, risk_management,"
        " status) VALUES (1,'t-gate-crash','NIFTY',?,?,?)",
        [json.dumps([{"option_type": "CE", "transaction": "buy", "lots": 1}]),
         json.dumps({"daily_stop_loss": 1500, "daily_take_profit": 3000,
                     "max_trades_per_day": 5}),
         "active"])
    sid = int(db.fetch_one("SELECT id FROM strategies WHERE name='t-gate-crash'")["id"])
    import core.services.sl_monitor as _slm
    monkeypatch.setattr(_slm, "entry_open_ist", lambda *_a, **_k: True)
    try:
        calls = []

        def boom(a, b, c):
            raise RuntimeError("gate exploded")

        monkeypatch.setattr(ap, "_today_signal", boom)
        monkeypatch.setattr(
            ap, "_execute_legs",
            lambda s, legs, db_, tm_, tday: (calls.append(1) or ([{"trade_id": 1}], [])))
        sync_exc = None
        out = ap.place_for_strategy(sid, signal_check="daily")
        assert calls == [], f"placed despite gate crash: {out}"
        assert any("crash" in str(n).lower() or "skip" in str(n).lower()
                   for n in out.get("notes", [])), out

        # intraday crash path too
        def boom2(a, b, c):
            raise RuntimeError("live gate exploded")

        monkeypatch.setattr(ap, "_intraday_signal", boom2)
        out2 = ap.place_for_strategy(sid, signal_check="intraday")
        assert calls == [], out2
        assert any("crash" in str(n).lower() or "skip" in str(n).lower()
                   for n in out2.get("notes", [])), out2
    finally:
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        db.execute("DELETE FROM strategies WHERE id=?", [sid])


def test_signal_result_cached_per_day(monkeypatch):
    import core.services.auto_paper as ap
    ap._SIGNAL_CACHE.clear()
    calls = {"n": 0}

    def fake_hist(sym, s, e):
        import datetime as _dt
        out, d = [], _dt.date(2026, 8, 1)
        px = 25000.0
        while len(out) < 40:
            if d.weekday() < 5:
                px *= 1.002
                out.append({"symbol": sym, "trade_date": d.strftime("%Y-%m-%d"),
                            "open_price": px, "high_price": px * 1.001,
                            "low_price": px * 0.999, "close_price": px,
                            "volume": 500000, "oi": 0})
            d += _dt.timedelta(days=1)
        return out

    def fake_bt(req):
        calls["n"] += 1
        return {"metrics": {"total_trades": 0},
                "trade_list": []}

    monkeypatch.setattr("core.services.historical_fetcher.fetch_historical", fake_hist)
    monkeypatch.setattr("routes.strategy_builder._run_backtest_core", fake_bt)
    s = {"id": 424242, "symbol": "NIFTY", "legs": "[1]", "indicators": "[]",
         "advanced_options": "{}", "risk_management": "{}"}
    f1, _ = ap._today_signal(424242, s, "2026-10-12")
    f2, _ = ap._today_signal(424242, s, "2026-10-12")
    assert (f1, f2) == (False, False)
    assert calls["n"] == 1, calls  # second call served from per-day cache
    ap._SIGNAL_CACHE.clear()
