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


# Last scan ka poora hisaab (API/monitor me dikhane ke liye): closed +
# skipped-with-reason. "SL kyu nahi kata" ka jawab yahi + JSONL log me.
LAST_SCAN = {"ts": "", "closed": [], "skipped": []}


def check_stoploss_target(confirm_secs=5):
    """Intra-candle SL/TP check - same levels as backtest (shared helper
    utils.helpers.sltp_premium_level - BacktestEngine._check_sl_tp parity).
    For live, current LTP is treated as both High/Low - hit if level breached.
    confirm_secs: breach par itne second baad dobara quote karke pakka karo
      (single bad-tick/spread spike par kaatna band - wahi backtest-vs-paper
      gap tha: backtest smooth bar me tikta, paper ek tick par kat-ta).
      0 = purana turant-close behavior. TP single touch par (favourable).
    Returns {closed, skipped}: har skip ka reason likha jata hai (JSONL log
    data/logs/exit_skips_*.jsonl + LAST_SCAN) - silent `continue` khatm."""
    global LAST_SCAN
    trade_model = TradeModel()
    open_trades = trade_model.get_open_trades()
    closed, skipped = [], []

    def _skip(trade, reason):
        try:
            skipped.append({"id": trade.get("id"), "symbol": trade.get("symbol", ""),
                            "reason": str(reason)[:150]})
            from utils.helpers import log_exit_skip as _log
            _log("sltp_monitor", trade.get("id"), trade.get("symbol"), reason)
        except Exception:
            pass

    for trade in open_trades:
        sl = float(trade.get("stop_loss", 0) or 0)
        tp = float(trade.get("target", 0) or 0)
        if sl <= 0 and tp <= 0:
            _skip(trade, "SL aur Target dono 0 - is trade par monitor disabled hai")
            continue
        current = trade_model.get_option_premium(
            trade["symbol"], trade["option_type"],
            trade["strike_price"], trade["expiry_date"]
        )
        if current is None:
            _skip(trade, "live premium nahi mila (market band/expiry/quote fail) - SL/TP check skip")
            continue
        entry = float(trade["entry_price"])
        is_buy = trade["transaction_type"] == "BUY"
        # Shared levels (utils.helpers.sltp_premium_level): backtest se identical.
        # Old inline copy yahan duplicate thi (drift ka khatra) - ab single source.
        from utils.helpers import sltp_premium_level as _lvl
        _qty = int(trade.get("quantity", 1) or 1)
        _lot = int(trade.get("lot_size", 0) or 0)
        sl_level = _lvl(entry, sl, _qty, _lot, trade["transaction_type"], True)
        tp_level = _lvl(entry, tp, _qty, _lot, trade["transaction_type"], False)
        # Conservative intra-candle: current is proxy for High/Low - hit if breached
        hit_sl = (current <= sl_level) if is_buy else (current >= sl_level) if sl_level > 0 else False
        hit_tp = (current >= tp_level) if is_buy else (current <= tp_level) if tp_level > 0 else False
        # Re-quote confirmation (SL only): ek akeli tick (bad print / wide
        # spread spike, khaas taur par illiquid OTM me) par mat kaato - kuch
        # second baad dobara quote lo, ab bhi breach to pakka. TP single
        # touch par (favourable, backtest bar-touch jaisa).
        if hit_sl and confirm_secs and confirm_secs > 0:
            try:
                import time as _t
                _t.sleep(min(float(confirm_secs), 15))
                requote = trade_model.get_option_premium(
                    trade["symbol"], trade["option_type"],
                    trade["strike_price"], trade["expiry_date"])
                if requote is not None and requote > 0:
                    current = float(requote)
                    hit_sl = (current <= sl_level) if is_buy else (current >= sl_level) if sl_level > 0 else False
                    hit_tp = (current >= tp_level) if is_buy else (current <= tp_level) if tp_level > 0 else False
            except Exception:
                pass
        # SL has priority (worst case) same as backtest (confirmation ke BAAD,
        # taaki un-confirmed SL TP ko na kha jaye).
        if hit_sl and hit_tp:
            hit_tp = False
        if hit_sl or hit_tp:
            reason = "stoploss" if hit_sl else "target"
            # Pass is_live=True via close_trade slippage (trade_model.py)
            trade_model.close_trade(trade["id"], current, datetime.now().strftime("%Y-%m-%d"), reason)
            closed.append({"id": trade["id"], "reason": reason, "exit_price": current})
    try:
        LAST_SCAN = {"ts": datetime.now().isoformat(timespec="seconds"),
                     "closed": closed, "skipped": skipped}
    except Exception:
        pass
    return {"closed": closed, "skipped": skipped}


def run_auto_trade():
    print(f"[{datetime.now()}] Checking open trades...")
    res = check_stoploss_target() or {}
    closed = res.get("closed", []) if isinstance(res, dict) else (res or [])
    skipped = res.get("skipped", []) if isinstance(res, dict) else []
    log = {"timestamp": datetime.now().isoformat(), "checked": len(TradeModel().get_open_trades()), "closed": closed, "skipped": skipped}
    log_file = os.path.join(LOG_DIR, f"trade_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(log_file, "w") as f:
        json.dump(log, f, indent=2)
    print(f"[{datetime.now()}] Done. Closed {len(closed)} trades.")


if __name__ == "__main__":
    run_auto_trade()
