"""Backtest vs Paper parity audit: failing-first proofs.

Covers: replay==backtest contract, no future-price expiry exit,
paper close math == engine close math, incomplete (forming) candle,
position-state caps. No strategy params are tuned here.
"""
import os
import sys
import datetime
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.backtest_engine import BacktestEngine
from core.services.historical_replay import HistoricalReplayEngine


def _bars(n=60, seed=11, start=datetime.date(2026, 6, 1)):
    rnd = random.Random(seed)
    bars = []
    px = 25000.0
    d = start
    plan = ([0.006] * 15 + [-0.007] * 8 + [0.008] * 15 + [-0.004] * 10) * 3
    k = 0
    while len(bars) < n:
        if d.weekday() < 5:
            drift = plan[k % len(plan)] + rnd.uniform(-0.005, 0.005)
            k += 1
            o = px
            c = max(100.0, px * (1 + drift))
            h = max(o, c) * 1.004
            low = min(o, c) * 0.996
            bars.append({"symbol": "NIFTY", "trade_date": d.strftime("%Y-%m-%d"),
                         "open_price": round(o, 2), "high_price": round(h, 2),
                         "low_price": round(low, 2), "close_price": round(c, 2),
                         "volume": 500000, "oi": 100000})
            px = c
        d += datetime.timedelta(days=1)
    return bars


def _cfg():
    legs = [{"option_type": "CE", "transaction": "buy", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0}]
    adv = {"trade_mode": "intraday", "timeframe": "1d"}
    risk = {"max_trades_per_day": 5, "daily_stop_loss": 1500,
            "daily_take_profit": 3000}
    inds = [{"id": "supertrend", "params": {"period": 10, "multiplier": 3}}]
    return inds, legs, adv, risk


def test_replay_matches_backtest_contract():
    """Same bars + same config => replay metrics must equal backtest metrics
    (trades, net, wins). Replay advertises 'exact backtest logic'."""
    hist = _bars()
    inds, legs, adv, risk = _cfg()
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    bt = eng.run(list(hist), "NIFTY", hist[0]["trade_date"], hist[-1]["trade_date"],
                 inds, [], [], legs, dict(adv), dict(risk), is_live=False)
    rp = HistoricalReplayEngine()
    rp.symbol, rp.start_date, rp.end_date = "NIFTY", hist[0]["trade_date"], hist[-1]["trade_date"]
    rp.ind_list, rp.entry_conditions, rp.exit_conditions = inds, [], []
    rp.legs, rp.advanced_options, rp.risk_management = legs, dict(adv), dict(risk)
    rp.historical_data = list(hist)
    rp.bt_engine._skip_db = True
    closes = [h["close_price"] for h in hist]
    rp.pre_calc = rp.bt_engine._pre_calc(hist, closes,
                                         [h["high_price"] for h in hist],
                                         [h["low_price"] for h in hist], inds)
    rp.bt_engine.ind_modes = {"supertrend": "both"}
    out = rp.run_replay()
    bm, rm = bt["metrics"], out["metrics"]
    assert out.get("success"), out
    assert rm["total_trades"] == bm["total_trades"], (rm["total_trades"], bm["total_trades"])
    assert abs(rm["net_pnl"] - bm["net_pnl"]) < 1.0, (rm["net_pnl"], bm["net_pnl"])
    assert rm["winning_trades"] == bm["winning_trades"]
    # every replay trade must carry realized (nonzero-able) pnl + valid dates
    for t in rm["trade_list"]:
        assert t.get("entry_date") and t.get("exit_date"), t
        assert str(t["exit_date"]) >= str(t["entry_date"]), t


def test_no_future_price_expiry_close():
    """Position open while window ends on expiry Thursday: must NOT be closed
    early at the future (last-bar) price with re-entry allowed."""
    import datetime as _dt
    # 40 bars from a Monday, then truncate AT the last Thursday in series
    hist = _bars(n=40, start=_dt.date(2026, 8, 10))
    thu_idx = [k for k, b in enumerate(hist)
               if _dt.datetime.strptime(b["trade_date"], "%Y-%m-%d").weekday() == 3]
    assert len(thu_idx) >= 2
    hist = hist[:thu_idx[-1] + 1]
    assert _dt.datetime.strptime(hist[-1]["trade_date"], "%Y-%m-%d").weekday() == 3
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    entries = [{"date": hist[2]["trade_date"], "strike": "25000", "price": 100.0,
                "quantity": 75, "costs": {"total": 40.0},
                "total_cost": 100.0 * 75 + 40.0, "type": "buy",
                "legs": [{"option_type": "CE", "strike": "25000", "type": "buy", "lots": 1}],
                "time": "09:35"}]
    exits = []
    # simulate a mid-window call (bar 5 of N): must NOT close (not last bar)
    eng._square_off_expiry(hist, entries, exits, "NIFTY", "CE", "buy", 75,
                           {}, cur_date=hist[5]["trade_date"])
    assert len(exits) == 0, "mid-window expiry scan must not close"


def test_paper_close_math_matches_engine():
    """TradeModel.close_trade net must equal engine _close_position net."""
    from core.models.trade_model import TradeModel
    tm = TradeModel()
    tm.db.execute("DELETE FROM paper_trades WHERE user_id=99999")
    try:
        tid = tm.db.execute(
            "INSERT INTO paper_trades (user_id, symbol, option_type, strike_price,"
            " transaction_type, quantity, lot_size, entry_price, stop_loss, target,"
            " total_cost, entry_date, status) VALUES "
            "(99999,'NIFTY','CE',25000,'BUY',1,75,100.0,1500,3000,0.0,'2026-08-01','open')")
        # entry costs as stored at placement (recompute same way)
        from core.services.transaction_costs import TransactionCosts
        ec = TransactionCosts.calculate(100.0 * 1 * 75, False, True)["total"]
        tm.db.execute("UPDATE paper_trades SET total_cost=? WHERE id=?", [ec, tid])
        tm.close_trade(int(tid), 110.0, "2026-08-02", exit_status="manual")
        row = tm.db.fetch_one("SELECT * FROM paper_trades WHERE id=?", [int(tid)])
        # engine math on identical inputs
        eng = BacktestEngine(is_live=False)
        entries, exits = [], []
        entries.append({"date": "2026-08-01", "strike": "25000", "price": 100.0,
                        "quantity": 75, "costs": {"total": ec},
                        "total_cost": round(100.0 * 75 + ec, 2), "type": "buy",
                        "legs": [{"option_type": "CE", "strike": "25000", "type": "buy", "lots": 1}],
                        "time": "09:35"})
        from core.services.transaction_costs import TransactionCosts as TC
        xp = TC.apply_fill_slippage(110.0, "SELL", False)
        eng._close_position(entries, exits, entries[0], xp, "manual",
                            "2026-08-02", 75, "buy")
        assert abs(float(row["pnl"]) - float(exits[0]["pnl"])) < 2.0, (row["pnl"], exits[0]["pnl"])
        assert float(row["pnl"]) > 0  # 100 -> 110 BUY must win
        # SELL mirror loses on the same move
        tid2 = tm.db.execute(
            "INSERT INTO paper_trades (user_id, symbol, option_type, strike_price,"
            " transaction_type, quantity, lot_size, entry_price, stop_loss, target,"
            " total_cost, entry_date, status) VALUES "
            "(99999,'NIFTY','CE',25000,'SELL',1,75,100.0,1500,3000,0.0,'2026-08-01','open')")
        tm.db.execute("UPDATE paper_trades SET total_cost=? WHERE id=?", [ec, int(tid2)])
        tm.close_trade(int(tid2), 110.0, "2026-08-02", exit_status="manual")
        row2 = tm.db.fetch_one("SELECT * FROM paper_trades WHERE id=?", [int(tid2)])
        assert float(row2["pnl"]) < 0, row2["pnl"]
    finally:
        tm.db.execute("DELETE FROM paper_trades WHERE user_id=99999")


def test_incomplete_last_bar_no_entry():
    """Signal ONLY on the last (forming) bar must not enter (no next bar)."""
    hist = _bars(n=30, seed=21)
    # flatten everything except a last-bar spike that flips ST bullish
    for b in hist[:-1]:
        b["open_price"] = b["high_price"] = b["low_price"] = b["close_price"] = 25000.0
    last = hist[-1]
    last["open_price"] = 25000.0
    last["close_price"] = 26000.0
    last["high_price"] = 26050.0
    last["low_price"] = 24950.0
    inds, legs, adv, risk = _cfg()
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    bt = eng.run(list(hist), "NIFTY", hist[0]["trade_date"], hist[-1]["trade_date"],
                 inds, [], [], legs, dict(adv), dict(risk), is_live=False)
    entries = [t for t in bt["metrics"]["trade_list"]]
    # no entry may reference the forming last bar as ENTRY bar business:
    # entries execute at next-bar open, which does not exist -> zero entries
    assert len(entries) == 0, entries


def test_position_state_caps():
    """Never >1 open; max N/day respected (reconstructed from trade list)."""
    hist = _bars(n=60, seed=5)
    inds, legs, adv, risk = _cfg()
    risk = dict(risk, max_trades_per_day=1)
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    bt = eng.run(list(hist), "NIFTY", hist[0]["trade_date"], hist[-1]["trade_date"],
                 inds, [], [], legs, dict(adv), dict(risk), is_live=False)
    tl = bt["metrics"]["trade_list"]
    from collections import Counter
    per_day = Counter(t["entry_date"] for t in tl)
    assert all(v <= 1 for v in per_day.values()), per_day
    # overlap check: at any date, open positions <= 1
    evts = []
    for t in tl:
        evts.append((t["entry_date"], 1))
        evts.append((t["exit_date"], -1))
    # entries and exits same date (intraday) net to intraday holds; sort exits first
    evts.sort(key=lambda e: (e[0], e[1]))
    cur = mx = 0
    for _, d in evts:
        cur += d
        mx = max(mx, cur)
    assert mx <= 1, evts
