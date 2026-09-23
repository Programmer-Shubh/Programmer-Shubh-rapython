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
        # Backtest-parity levels (BacktestEngine._check_sl_tp): rupee SL/TP
        # (>20) convert to premium points via TOTAL units, NOT percent.
        # Old code did (sl/entry)*100 -> SL 1500 on 22.84 premium became level
        # 1523 (never hit), so open positions never exited. Fixed here.
        units = int(trade.get("quantity", 1) or 1) * int(trade.get("lot_size", 0) or 0)
        if units <= 0:
            units = 1

        def _level(amt, is_sl):
            amt = float(amt or 0)
            if amt <= 0:
                return 0
            if amt > 20:
                pts = amt / max(units, 1)
                if is_buy:
                    lvl = (entry - pts) if is_sl else (entry + pts)
                else:
                    lvl = (entry + pts) if is_sl else (entry - pts)
                return max(0.05, lvl)
            if is_buy:
                return entry * (1 - amt / 100) if is_sl else entry * (1 + amt / 100)
            return entry * (1 + amt / 100) if is_sl else entry * (1 - amt / 100)

        sl_level = _level(sl, True)
        tp_level = _level(tp, False)
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
