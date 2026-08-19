import os
import json
from datetime import datetime

os.environ.setdefault("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "ratrade.db"))

from core.models.database import Database
from core.models.trade_model import TradeModel
from core.services.live_market_data import LiveMarketData

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def check_stoploss_target():
    trade_model = TradeModel()
    live = LiveMarketData()
    open_trades = trade_model.get_open_trades()
    closed = []

    for trade in open_trades:
        sl = float(trade.get("stop_loss", 0))
        tp = float(trade.get("target", 0))
        if sl <= 0 and tp <= 0:
            continue

        current = trade_model.get_option_premium(
            trade["symbol"], trade["option_type"],
            trade["strike_price"], trade["expiry_date"]
        )
        if current is None:
            continue

        entry = trade["entry_price"]
        is_buy = trade["transaction_type"] == "BUY"

        hit_sl = False
        hit_tp = False

        if sl > 0:
            if sl > 20:
                sl_level = entry - sl if is_buy else entry + sl
            else:
                sl_level = entry * (1 - sl / 100) if is_buy else entry * (1 + sl / 100)
            hit_sl = (current <= sl_level) if is_buy else (current >= sl_level)

        if tp > 0 and not hit_sl:
            if tp > 20:
                tp_level = entry + tp if is_buy else entry - tp
            else:
                tp_level = entry * (1 + tp / 100) if is_buy else entry * (1 - tp / 100)
            hit_tp = (current >= tp_level) if is_buy else (current <= tp_level)

        if hit_sl or hit_tp:
            reason = "stoploss" if hit_sl else "target"
            trade_model.close_trade(trade["id"], current, datetime.now().strftime("%Y-%m-%d"), reason)
            closed.append({"id": trade["id"], "reason": reason, "exit_price": current})
            print(f"  Closed #{trade['id']}: {reason} at ₹{current:.2f}")

    return closed


def run_auto_trade():
    print(f"[{datetime.now()}] Checking open trades for SL/TP...")
    closed = check_stoploss_target()
    log = {
        "timestamp": datetime.now().isoformat(),
        "checked": len(TradeModel().get_open_trades()),
        "closed": closed,
    }
    log_file = os.path.join(LOG_DIR, f"trade_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(log_file, "w") as f:
        json.dump(log, f, indent=2)
    print(f"[{datetime.now()}] Done. Closed {len(closed)} trades.")


if __name__ == "__main__":
    run_auto_trade()
