"""Audit: win rate = realized NET pnl STRICTLY > 0 / closed trades.
Zero and NULL pnl count as losses. Metric must match the table rows."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.models.trade_model import TradeModel


def _seed(tm, rows):
    tm.db.execute("DELETE FROM paper_trades WHERE user_id=999")
    for r in rows:
        tm.db.execute(
            "INSERT INTO paper_trades (user_id, symbol, option_type, strike_price, "
            "transaction_type, quantity, lot_size, entry_price, exit_price, "
            "entry_date, exit_date, pnl, status) "
            "VALUES (999,'T','CE',100,'BUY',1,50,10,12,'2026-01-01','2026-01-02',?, 'closed')",
            [r],
        )


def test_win_counts_only_net_positive():
    tm = TradeModel()
    try:
        # 2 clear wins, 3 losses, 1 breakeven-zero, 1 NULL -> 2/7 = 28.6%
        _seed(tm, [100.0, 50.5, -10.0, -200.0, -5.0, 0.0, None])
        s = tm.get_stats(999)
        assert s["closed_count"] == 7, s
        assert s["winning_trades"] == 2, s
        assert s["losing_trades"] == 5, s
        assert s["win_rate"] == round(2 / 7 * 100, 1), s
        # table rows use the same rule
        rows = tm.db.fetch_all(
            "SELECT pnl FROM paper_trades WHERE user_id=999 AND status='closed'")
        w = sum(1 for t in rows if float(t.get("pnl") or 0) > 0)
        assert w == s["winning_trades"] == 2
        assert round(w / len(rows) * 100, 1) == s["win_rate"]
    finally:
        tm.db.execute("DELETE FROM paper_trades WHERE user_id=999")


def test_win_rate_empty():
    tm = TradeModel()
    tm.db.execute("DELETE FROM paper_trades WHERE user_id=999")
    s = tm.get_stats(999)
    assert s["closed_count"] == 0
    assert s["win_rate"] == 0
