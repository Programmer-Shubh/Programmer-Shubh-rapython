import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

UID = 99996


def test_fast_positions_use_cached_price_without_network(monkeypatch):
    from core.models.trade_model import TradeModel

    tm = TradeModel()
    tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
    expiry = "2026-12-31"
    monkeypatch.setattr(tm, "get_option_premium", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network path used")))
    try:
        tm.db.execute(
            "INSERT INTO paper_trades (user_id, symbol, option_type, strike_price,"
            " transaction_type, quantity, lot_size, entry_price, exit_price,"
            " entry_date, exit_date, pnl, expiry_date, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open')",
            [UID, "NIFTY", "CE", 25000, "BUY", 1, 65, 100.0, 0.0,
             tm._ist_today(), "", 0.0, expiry],
        )
        tm._last_premiums[f"NIFTY_CE_25000.0_{expiry}"] = 120.0
        rows = tm.get_open_positions_cached(user_id=UID)
        assert len(rows) == 1, rows
        assert rows[0]["current_price"] == 120.0, rows
        assert rows[0]["unrealized_pnl"] == 1300.0, rows
        assert rows[0]["unrealized_pct"] == 20.0, rows
    finally:
        tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
        tm._last_premiums.pop(f"NIFTY_CE_25000.0_{expiry}", None)
