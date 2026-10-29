"""Saved-strategy Paper Trade button: backtest signal-gated, blind entry nahi.

Purana frontend button seedha live-ATM par saari legs laga deta tha
(indicators ignore) - paper trades backtest se match nahi hote the.
Naya POST /strategies/{id}/paper-trade wahi gate use karta hai jo
auto-worker use karta hai; signal na ho to placed=[] + wajah."""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

TAG = "t-paper-gate-probe"


def _mkpaused(timeframe="daily", adv_tf="1d"):
    from routes.strategies import StrategyRequest, save_strategy
    try:
        from core.models.database import Database
        Database.get_instance().execute(
            "DELETE FROM strategies WHERE name=?", [TAG])
    except Exception:
        pass
    r = save_strategy(StrategyRequest(
        name=TAG, symbol="NIFTY", start_date="2026-09-01",
        end_date="2026-09-20", timeframe=timeframe,
        indicators=[{"id": "supertrend", "params": {"period": 10}}],
        legs=[{"option_type": "CE", "transaction": "buy", "lots": 1,
               "strike_selection": "atm", "otm_distance": 0,
               "expiry": "weekly"}],
        advanced_options={"trade_mode": "intraday", "timeframe": adv_tf},
        risk_management={"daily_stop_loss": 1500, "daily_take_profit": 3000,
                         "max_trades_per_day": 5},
        status="paused"))
    assert r.get("id"), r
    return int(r["id"])


def _drop(sid):
    try:
        from core.models.database import Database
        Database.get_instance().execute(
            "DELETE FROM strategies WHERE id=?", [sid])
    except Exception:
        pass


def test_missing_strategy_errors():
    from routes.strategies import strategy_paper_trade
    r = strategy_paper_trade(999999999, {})
    assert r.get("error"), r


def test_paused_never_places_blind():
    """Paused strategy par gated call kabhi place nahi karta - notes me wajah."""
    from routes.strategies import strategy_paper_trade
    sid = _mkpaused()
    try:
        r = strategy_paper_trade(sid, {})
        assert r.get("strategy_id") == sid, r
        assert r.get("placed") == [], r
        assert r.get("notes"), r
        assert r.get("gate") == "daily", r
        assert r.get("forced") is False, r
    finally:
        _drop(sid)


def test_gate_follows_saved_timeframe():
    """5m saved timeframe => intraday gate; 1d => daily gate."""
    from routes.strategies import strategy_paper_trade
    sid = _mkpaused(timeframe="5m", adv_tf="5m")
    try:
        r = strategy_paper_trade(sid, {})
        assert r.get("gate") == "intraday", r
        assert r.get("placed") == [], r
    finally:
        _drop(sid)
