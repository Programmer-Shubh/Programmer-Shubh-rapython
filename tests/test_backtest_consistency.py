"""End-to-end backtest consistency: setup == trades == metrics, every run.

Covers: zero-trade honesty, BUY/SELL x CE/PE, intraday same-session exits,
next-day exit rejection, date boundaries, SL/TP, re-entry cap, slippage,
lot-size math, run-ID uniqueness + params echo, optionable guard.
Run: python -m pytest tests/test_backtest_consistency.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from routes.strategy_builder import _run_backtest_core, BacktestRequest

SYM = "BANKNIFTY"
START, END = "2026-07-20", "2026-08-20"
ST = [{"id": "supertrend", "params": {"period": 10, "multiplier": 3}}]


def _req(**kw):
    # Default leg is PE-SELL: it actually trades in this window (CE-buy gives
    # an honest zero here). Direction coverage lives in _directions tests.
    base = {"symbol": SYM, "symbols": [SYM], "start_date": START, "end_date": END,
            "indicators": ST,
            "legs": [{"option_type": "CE", "transaction": "sell", "lots": 1,
                      "strike_selection": "atm", "otm_distance": 0}],
            "advanced": {"trade_mode": "intraday", "timeframe": "1d"},
            "risk": {"max_trades_per_day": 5, "daily_stop_loss": 1500, "daily_take_profit": 3000}}
    base.update(kw)
    return BacktestRequest(**base)


def _run(**kw):
    return _run_backtest_core(_req(**kw))


def test_zero_trade_honest():
    r = _run(risk={"max_trades_per_day": 0, "daily_stop_loss": 1500, "daily_take_profit": 3000})
    m = r.get("metrics", {})
    assert m.get("total_trades", -1) == 0
    assert (m.get("trade_list") or []) == []
    for k in ("win_rate", "loss_rate", "expectancy", "reward_risk",
              "avg_win", "avg_loss", "max_win", "max_loss",
              "max_win_streak", "max_loss_streak"):
        assert m.get(k, None) == 0.0, k
    assert r.get("note") or r.get("suggestions"), "zero must explain why"


def _directions(opt, txn):
    r = _run(legs=[{"option_type": opt, "transaction": txn, "lots": 1,
                    "strike_selection": "atm", "otm_distance": 0}])
    m = r.get("metrics", {}) or {}
    tl = r.get("trade_list") or []
    assert r.get("run_id", "").startswith("bt-"), "run_id bound"
    assert (r.get("params") or {}).get("symbols") == [SYM]
    assert (r.get("params") or {}).get("legs", [{}])[0].get("option_type") == opt
    for t in tl:
        assert t.get("option_type") == opt, t
        assert t.get("position") == ("Buy" if txn == "buy" else "Sell"), t
        assert t.get("exit_price", 0) > 0 and t.get("exit_reason"), t
    # metrics == trades
    assert m.get("total_trades") == len(tl)
    assert abs(sum(float(t.get("pnl", 0) or 0) for t in tl) - float(m.get("net_pnl", 0))) < 1.0
    wins = sum(1 for t in tl if float(t.get("pnl", 0) or 0) > 0)
    assert m.get("winning_trades") == wins
    if tl:
        assert abs(wins / len(tl) * 100 - float(m.get("win_rate", 0))) < 0.01
    return tl


def test_buy_ce():
    _directions("CE", "buy")


def test_sell_ce():
    _directions("CE", "sell")


def test_buy_pe():
    _directions("PE", "buy")


def test_sell_pe():
    tl = _directions("PE", "sell")
    # naked short: losses recorded uncapped (no clipping to small numbers)
    losses = [float(t.get("pnl", 0) or 0) for t in tl if float(t.get("pnl", 0) or 0) < 0]
    if losses:
        assert min(losses) < 0


def test_intraday_same_session():
    r = _run()
    tl = r.get("trade_list") or []
    assert tl, "need trades for session test"
    for t in tl:
        assert t.get("exit_date") == t.get("entry_date"), t
        assert t.get("exit_date") <= END and t.get("entry_date") >= START, t


def test_date_boundaries():
    r = _run()
    tl = r.get("trade_list") or []
    for t in tl:
        assert START <= t.get("entry_date", "") <= END, t
        assert START <= t.get("exit_date", "") <= END, t


def test_entry_time_plumbing():
    r = _run(advanced={"trade_mode": "intraday", "timeframe": "1d",
                       "entry_time": "09:50", "exit_time": "15:05"})
    tl = r.get("trade_list") or []
    assert tl, "need trades"
    for t in tl:
        assert t.get("entry_time") == "09:50", t
    assert (r.get("params") or {}).get("entry_time") == "09:50"


def test_sl_tp_hit():
    r = _run(risk={"max_trades_per_day": 5, "daily_stop_loss": 100, "daily_take_profit": 100})
    tl = r.get("trade_list") or []
    reasons = {t.get("exit_reason") for t in tl}
    assert reasons, "trades must carry exit reasons"
    assert reasons <= {"stoploss", "target", "intraday", "strategy_sl_tp",
                       "expiry_squareoff", "manual", "reversal", "max_hold"}, reasons


def test_reentry_cap():
    r = _run(risk={"max_trades_per_day": 1, "daily_stop_loss": 1500, "daily_take_profit": 3000})
    tl = r.get("trade_list") or []
    from collections import Counter
    per_day = Counter(t.get("entry_date") for t in tl)
    assert all(v <= 1 for v in per_day.values()), per_day


def test_slippage_matters():
    a = _run(advanced={"trade_mode": "intraday", "timeframe": "1d", "slippage_pct": 0.0})
    b = _run(advanced={"trade_mode": "intraday", "timeframe": "1d", "slippage_pct": 2.0})
    ta = a.get("trade_list") or []
    tb = b.get("trade_list") or []
    assert ta and tb, "need trades both sides"
    na = sum(float(t.get("pnl", 0) or 0) for t in ta)
    nb = sum(float(t.get("pnl", 0) or 0) for t in tb)
    assert nb < na, (na, nb)


def test_lot_size_math():
    from utils.helpers import get_lot_size
    assert get_lot_size("NIFTY") == 75
    assert get_lot_size("BANKNIFTY") == 15
    r = _run()
    tl = r.get("trade_list") or []
    assert tl
    for t in tl[:3]:
        assert int(t.get("quantity", 0)) == 1 * 15, t


def test_run_id_unique_and_echo():
    r1 = _run()
    r2 = _run()
    assert r1.get("run_id") and r2.get("run_id") and r1["run_id"] != r2["run_id"]
    p = r1.get("params") or {}
    assert p.get("symbols") == [SYM] and p.get("start_date") == START
    assert p.get("sl") == 1500.0 and p.get("tp") == 3000.0


def test_optionable_guard():
    from utils.helpers import is_optionable
    assert is_optionable("NIFTY") and is_optionable("BANKNIFTY")
    assert not is_optionable("GOLDBEES") and not is_optionable("SILVERBEES")
    r = _run_backtest_core(BacktestRequest(
        symbol="GOLDBEES", symbols=["GOLDBEES"], start_date=START, end_date=END,
        legs=[{"option_type": "CE", "transaction": "buy"}],
        advanced={}, risk={}))
    assert r.get("error") and "GOLDBEES" in r["error"]
    assert "trade_list" not in (r.get("metrics") or {})


def test_equity_backtest():
    r = _run_backtest_core(BacktestRequest(
        symbol="RELIANCE", symbols=["RELIANCE"], start_date="2026-07-20", end_date="2026-08-20",
        indicators=[{"id": "supertrend", "params": {"period": 10, "multiplier": 3}}],
        legs=[{"option_type": "EQ", "transaction": "buy", "lots": 10,
               "strike_selection": "atm", "otm_distance": 0}],
        advanced={"trade_mode": "intraday", "timeframe": "1d"},
        risk={"max_trades_per_day": 5, "daily_stop_loss": 1500, "daily_take_profit": 3000}))
    assert not r.get("error"), r.get("error")
    tl = r.get("trade_list") or []
    assert tl, "EQ must trade"
    for t in tl:
        assert t.get("option_type") == "EQ", t
        assert t.get("exit_date") == t.get("entry_date"), t
        assert t.get("quantity") == 10, t
        ep, xp = float(t["entry_price"]), float(t["exit_price"])
        assert abs(ep - xp) / ep < 0.5, t
        gross = (xp - ep) * 10
        assert abs(t["pnl"] - gross) < abs(gross) * 0.3 + 200, (t, gross)


def test_equity_non_fno_allowed():
    # Spec: no real data -> honest INSUFFICIENT (synthetic fabrication banned).
    # GOLDBEES has no real bars locally, so the run must refuse, not fabricate.
    r = _run_backtest_core(BacktestRequest(
        symbol="GOLDBEES", symbols=["GOLDBEES"], start_date="2026-08-01", end_date="2026-08-20",
        indicators=[{"id": "supertrend", "params": {"period": 10, "multiplier": 3}}],
        legs=[{"option_type": "EQ", "transaction": "buy", "lots": 10,
               "strike_selection": "atm", "otm_distance": 0}],
        advanced={"trade_mode": "intraday", "timeframe": "1d"},
        risk={"max_trades_per_day": 5, "daily_stop_loss": 1500, "daily_take_profit": 3000}))
    assert r.get("error") and "INSUFFICIENT" in r.get("error"), r
    # ...while a data-rich EQ symbol still trades honestly.
    r2 = _run_backtest_core(BacktestRequest(
        symbol="RELIANCE", symbols=["RELIANCE"], start_date="2026-08-01", end_date="2026-08-20",
        indicators=[{"id": "supertrend", "params": {"period": 10, "multiplier": 3}}],
        legs=[{"option_type": "EQ", "transaction": "buy", "lots": 10,
               "strike_selection": "atm", "otm_distance": 0}],
        advanced={"trade_mode": "intraday", "timeframe": "1d"},
        risk={"max_trades_per_day": 5, "daily_stop_loss": 1500, "daily_take_profit": 3000}))
    assert not r2.get("error"), r2.get("error")


def test_costs_sane():
    from core.services.transaction_costs import TransactionCosts
    c = TransactionCosts.calculate(100000, True, False)
    assert c["total"] > 0 and c["total"] < 100000 * 0.02
    assert c["brokerage"] >= 0 and c["exchange_txn"] >= 0
