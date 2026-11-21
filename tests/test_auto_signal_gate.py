"""Auto-paper signal gate: reads the right trade_list path + lookback."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.services.auto_paper import _recent_signal_ok


def test_today_hit_needs_no_lookback():
    # helper only handles PAST signals; today-hit is caller's branch
    ok, d = _recent_signal_ok(["2026-10-12"], "2026-10-12", 5, [])
    assert (ok, d) == (False, "")


def test_recent_untraded_signal_fires():
    ok, d = _recent_signal_ok(["2026-10-05", "2026-10-09"], "2026-10-12", 5, [])
    assert ok and d == "2026-10-09", (ok, d)


def test_old_signal_ignored():
    ok, d = _recent_signal_ok(["2026-10-01"], "2026-10-12", 5, [])
    assert (ok, d) == (False, "")


def test_already_traded_signal_skipped():
    # signal 10-09 par 10-10 ko entry ho chuki -> dobara nahi
    ok, d = _recent_signal_ok(["2026-10-09"], "2026-10-12", 5, ["2026-10-10"])
    assert (ok, d) == (False, "")
    # trade ke BAAD purane signals bhi block (stale-chase nahi)
    ok, d = _recent_signal_ok(["2026-10-08", "2026-10-09"], "2026-10-12", 5,
                              ["2026-10-10"])
    assert (ok, d) == (False, "")
    # trade signal se PEHLE ka ho to naya signal actionable hai
    ok, d = _recent_signal_ok(["2026-10-08", "2026-10-09"], "2026-10-12", 5,
                              ["2026-10-07"])
    assert ok and d == "2026-10-09", (ok, d)


def test_lookback_zero_disables():
    ok, d = _recent_signal_ok(["2026-10-11"], "2026-10-12", 0, [])
    assert (ok, d) == (False, "")


def test_today_signal_reads_top_level_list(monkeypatch):
    """_run_backtest_core returns trade_list TOP-LEVEL: gate must see it
    (regression: old code read metrics.trade_list = always [] = never auto)."""
    import core.services.auto_paper as ap

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

    made = {"n": 0}

    def fake_bt(req):
        made["n"] += 1
        # top-level trade_list like the real _run_backtest_core
        return {"metrics": {"total_trades": 1},
                "trade_list": [{"entry_date": "2026-10-12", "pnl": 100.0}]}

    monkeypatch.setattr("core.services.historical_fetcher.fetch_historical", fake_hist)
    monkeypatch.setattr("routes.strategy_builder._run_backtest_core", fake_bt)
    s = {"id": 1, "symbol": "NIFTY", "legs": "[1]", "indicators": "[]",
         "advanced_options": "{}", "risk_management": "{}"}
    fires, note = ap._today_signal(1, s, "2026-10-12")
    assert made["n"] == 1
    assert fires is True and "aaj" in note, (fires, note)
