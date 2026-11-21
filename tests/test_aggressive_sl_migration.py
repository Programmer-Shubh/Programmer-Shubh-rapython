import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.models.database import Database
from core.services.default_strategies import normalize_aggressive_sl

NAME = "t-aggressive-sl-migration"


def test_aggressive_sl_normalizes_strategy_and_open_trade():
    db = Database.get_instance()
    db.execute("DELETE FROM paper_trades WHERE strategy_id IN (SELECT id FROM strategies WHERE name=?)", [NAME])
    db.execute("DELETE FROM strategies WHERE name=?", [NAME])
    try:
        db.execute(
            "INSERT INTO strategies (user_id, name, symbol, legs, risk_management, status) "
            "VALUES (1, ?, 'NIFTY', ?, ?, 'active')",
            [NAME,
             json.dumps([{"option_type": "CE", "transaction": "buy", "lots": 1, "stop_loss": "150%"}]),
             json.dumps({"daily_stop_loss_pct": 150, "daily_take_profit_pct": 5})],
        )
        sid = int(db.fetch_one("SELECT id FROM strategies WHERE name=?", [NAME])["id"])
        db.execute(
            "INSERT INTO paper_trades (user_id, strategy_id, symbol, option_type, strike_price, "
            "transaction_type, quantity, lot_size, entry_price, stop_loss, target, sl_unit, tp_unit, "
            "entry_date, status) VALUES (1, ?, 'NIFTY', 'CE', 25000, 'BUY', 1, 65, 100, 150, 5, 'pct', 'pct', '2026-11-27', 'open')",
            [sid],
        )
        assert normalize_aggressive_sl(db) == 1
        rm = json.loads(db.fetch_one("SELECT risk_management FROM strategies WHERE id=?", [sid])["risk_management"])
        assert rm["daily_stop_loss_pct"] == 20.0 and rm["daily_stop_loss"] == 0
        trade = db.fetch_one("SELECT stop_loss, sl_unit FROM paper_trades WHERE strategy_id=? AND status='open'", [sid])
        assert float(trade["stop_loss"]) == 20.0 and trade["sl_unit"] == "pct"
        assert normalize_aggressive_sl(db) == 0  # idempotent
    finally:
        db.execute("DELETE FROM paper_trades WHERE strategy_id IN (SELECT id FROM strategies WHERE name=?)", [NAME])
        db.execute("DELETE FROM strategies WHERE name=?", [NAME])
