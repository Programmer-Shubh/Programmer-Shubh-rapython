"""Save-time result attach: match par jude + result_saved True; mismatch
par honest reason (pehle chup-chaap skip + jhootha 'saved' message tha)."""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

TAG = "t-save-attach-probe"


def _res():
    return {
        "run_id": "bt-probe9", "symbol": "NIFTY", "symbols": ["NIFTY"],
        "params": {"symbols": ["NIFTY"],
                   "legs": [{"option_type": "CE", "transaction": "buy", "lots": 1,
                             "strike_selection": "atm", "otm_distance": 0,
                             "expiry": "weekly"}],
                   "timeframe": "daily", "start_date": "2026-09-01",
                   "end_date": "2026-09-20", "indicators": ["supertrend"],
                   "trade_mode": "intraday", "entry_time": "09:35",
                   "exit_time": "15:05", "sl": 1500.0, "tp": 3000.0,
                   "max_trades_per_day": 5},
        "metrics": {"total_trades": 188, "win_rate": 60.0, "net_pnl": 12345.0},
        "trade_list": [{"symbol": "NIFTY", "pnl": 100.0}],
    }


def _body(**kw):
    b = {"name": TAG, "symbol": "NIFTY", "start_date": "2026-09-01",
         "end_date": "2026-09-20", "timeframe": "daily",
         "indicators": [{"id": "supertrend", "params": {"period": 10}}],
         "legs": [{"option_type": "CE", "transaction": "buy", "lots": 1,
                   "strike_selection": "atm", "otm_distance": 0,
                   "expiry": "weekly"}],
         "advanced_options": {"trade_mode": "intraday", "entry_time": "09:35",
                              "exit_time": "15:05"},
         "risk_management": {"daily_stop_loss": 1500, "daily_take_profit": 3000,
                             "max_trades_per_day": 5},
         "status": "paused", "last_backtest": _res()}
    b.update(kw)
    return b


def _drop():
    from core.models.database import Database
    try:
        Database.get_instance().execute("DELETE FROM strategies WHERE name=?", [TAG])
    except Exception:
        pass


def test_match_attaches_with_count():
    from routes.strategies import StrategyRequest, save_strategy
    _drop()
    try:
        r = save_strategy(StrategyRequest(**_body()))
        assert r.get("id"), r
        assert r.get("result_saved") is True, r
        assert r.get("result_trades") == 188, r
    finally:
        _drop()


def test_mismatch_honest_reason():
    """Run ke baad date badal kar save -> attach nahi + wajah saaf."""
    from routes.strategies import StrategyRequest, save_strategy
    _drop()
    try:
        r = save_strategy(StrategyRequest(**_body(end_date="2026-09-25")))
        assert r.get("id"), r
        assert r.get("result_saved") is not True, r
        assert "end date" in str(r.get("result_skip_reason", "")).lower(), r
    finally:
        _drop()


def test_matcher_reasons_direct():
    from routes.strategies import _result_matches_config
    reasons = []
    ok = _result_matches_config(
        _res(), "NIFTY", "2026-09-01", "2026-09-20",
        [{"id": "supertrend"}], _body()["legs"],
        _body()["risk_management"], _body()["advanced_options"],
        reasons=reasons)
    assert ok is True and reasons == [], (ok, reasons)
    reasons2 = []
    ok2 = _result_matches_config(
        _res(), "BANKNIFTY", "2026-09-01", "2026-09-20",
        [{"id": "supertrend"}], _body()["legs"],
        _body()["risk_management"], _body()["advanced_options"],
        reasons=reasons2)
    assert ok2 is False and any("symbol" in s for s in reasons2), (ok2, reasons2)
