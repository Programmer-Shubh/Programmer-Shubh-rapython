import os
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "ratrade.db"))

from core.models.database import Database
from core.models.trade_model import TradeModel

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def check_stoploss_target():
    """Intra-candle SL/TP check - conservative approach synced with backtest.
    Uses same pct logic as BacktestEngine._check_sl_tp (backtest_engine.py:387) with is_live=True.
    For live, current LTP is treated as both High/Low - hit if level breached (conservative).
    """
    trade_model = TradeModel()
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
        entry = float(trade["entry_price"])
        is_buy = trade["transaction_type"] == "BUY"
        # Unified SL/TP level calc synced with backtest (pct if >20 else Rs)
        sl_pct = (sl / max(entry, 0.01)) * 100 if sl > 20 else sl
        tp_pct = (tp / max(entry, 0.01)) * 100 if tp > 20 else tp
        if is_buy:
            sl_level = entry * (1 - sl_pct / 100) if sl_pct > 0 else 0
            tp_level = entry * (1 + tp_pct / 100) if tp_pct > 0 else 0
        else:
            sl_level = entry * (1 + sl_pct / 100) if sl_pct > 0 else 0
            tp_level = entry * (1 - tp_pct / 100) if tp_pct > 0 else 0
        # Conservative intra-candle: current is proxy for High/Low - hit if breached
        hit_sl = (current <= sl_level) if is_buy else (current >= sl_level) if sl_level > 0 else False
        hit_tp = (current >= tp_level) if is_buy else (current <= tp_level) if tp_level > 0 else False
        # SL has priority (worst case) same as backtest
        if hit_sl and hit_tp:
            hit_tp = False
        if hit_sl or hit_tp:
            reason = "stoploss" if hit_sl else "target"
            # Pass is_live=True via close_trade slippage (trade_model.py)
            trade_model.close_trade(trade["id"], current, datetime.now().strftime("%Y-%m-%d"), reason)
            closed.append({"id": trade["id"], "reason": reason, "exit_price": current})
    return closed


def run_auto_trade():
    print(f"[{datetime.now()}] Checking open trades...")
    closed = check_stoploss_target()
    log = {"timestamp": datetime.now().isoformat(), "checked": len(TradeModel().get_open_trades()), "closed": closed}
    log_file = os.path.join(LOG_DIR, f"trade_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(log_file, "w") as f:
        json.dump(log, f, indent=2)
    print(f"[{datetime.now()}] Done. Closed {len(closed)} trades.")


if __name__ == "__main__":
    run_auto_trade()
