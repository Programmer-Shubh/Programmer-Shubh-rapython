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


# Trailing-lock config per strategy (advanced_options.lock_profit_at /
# lock_profit) + short TTL cache - DB read har 10s scan me nahi.
_TRAIL_CFG = {}
_TRAIL_CFG_TS = {}


def _strategy_trail_cfg(strategy_id):
    """{lock_at: %, lock_amt: %} from the strategy's advanced_options."""
    try:
        sid = int(strategy_id or 0)
    except Exception:
        return None
    if not sid:
        return None
    import time as _t
    now = _t.time()
    if now - float(_TRAIL_CFG_TS.get(sid) or 0) < 300:
        return _TRAIL_CFG.get(sid)
    cfg = None
    try:
        import json as _js
        from core.models.database import Database
        row = Database.get_instance().fetch_one(
            "SELECT advanced_options FROM strategies WHERE id=?", [sid])
        adv = _js.loads((row or {}).get("advanced_options") or "{}") or {}
        if isinstance(adv, dict):
            _la = float(adv.get("lock_profit_at") or 0)
            _lp = float(adv.get("lock_profit") or 0)
            if _la > 0:
                cfg = {"lock_at": _la, "lock_amt": _lp,
                       "mode": str(adv.get("trailing") or "")}
    except Exception:
        cfg = None
    _TRAIL_CFG[sid] = cfg
    _TRAIL_CFG_TS[sid] = now
    return cfg


def check_stoploss_target(confirm_secs=2):
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
        # BAD-CONTRACT / SPIKE GUARD (bug 27-10-2026): BANKNIFTY 54500 CE
        # (expiry Aaj) ka price galti se NEXT-month contract ka (Rs 904 vs
        # entry Rs 43) lag gaya -> SL ne 904 par kaat diya (fake -25,894).
        # Ab agar price entry se 3x se zyada (SELL) / 1/3 se kam (BUY) ho to
        # is scan me exit SKIP - agle scan par asli price par SL/TP check
        # hoga; 3:15 square-off to hamesha chalta hai (position atki nahi).
        try:
            _ratio = (float(current) / entry) if entry > 0 else 0.0
            if (not is_buy and _ratio >= 3.0) or (is_buy and _ratio <= 0.3333):
                _skip(trade, f"price {float(current):.2f} entry {entry:.2f} se "
                             f"{_ratio:.2f}x - bad contract/spike, exit skip (agle scan par check)")
                continue
            # SOFT spike band: 10s scan me 40% tak ka jump = stale/wrong
            # quote nahi hota (11-11 bug: entry 175 par quote 249 aa raha tha
            # -> fake +42% target). Ek scan skip karo, agla scan confirm kare.
            if _ratio > 1.6 or _ratio < 0.6:
                _skip(trade, f"price {float(current):.2f} entry {entry:.2f} se "
                             f"{_ratio:.2f}x - soft spike (stale quote?), exit skip (agle scan par check)")
                continue
        except Exception:
            pass
        # Shared levels (utils.helpers.sltp_premium_level): backtest se identical.
        # Old inline copy yahan duplicate thi (drift ka khatra) - ab single source.
        from utils.helpers import sltp_premium_level as _lvl
        _qty = int(trade.get("quantity", 1) or 1)
        _sl_u = trade.get("sl_unit") or None
        _tp_u = trade.get("tp_unit") or None
        # Lot fallback: stored 0/NULL par effective_lot_size (sibling paths
        # trade_model 208-211 jaisa). Seedha 0 jaane par units=1 hokar SL
        # level 0.05/1600 ban jata tha = SL kabhi nahi kat-ta tha.
        try:
            from utils.helpers import effective_lot_size as _el
            _lot = int(_el(trade.get("symbol"), trade.get("lot_size")) or 0)
        except Exception:
            _lot = int(trade.get("lot_size", 0) or 0)
        if _lot <= 0:
            _lot = 50
        sl_level = _lvl(entry, sl, _qty, _lot, trade["transaction_type"], True, unit=_sl_u)
        tp_level = _lvl(entry, tp, _qty, _lot, trade["transaction_type"], False, unit=_tp_u)
        # ---- TRAILING LOCK (user rule): profit >= lock_profit_at% par SL
        # cost-price (breakeven) par chala jata hai; lock_profit% diya ho to
        # us profit ko LOCK kar deta hai (SL entry+lock%). Backtest engine
        # (BacktestEngine._check_sl_tp) ke EXACT same rule.
        _trail_cfg = _strategy_trail_cfg(trade.get("strategy_id"))
        _trail_armed = False
        _best_px = 0.0
        if _trail_cfg and entry > 0:
            try:
                import json as _js
                _aa = trade.get("auto_action")
                _aa_d = _js.loads(_aa) if isinstance(_aa, str) and _aa.strip().startswith("{") else {}
                _best_px = float((_aa_d or {}).get("trail_best") or 0) or 0.0
                if _best_px <= 0 or (is_buy and current > _best_px) or ((not is_buy) and current < _best_px):
                    _best_px = float(current)
                    # best price persist (deploy/restart par bhi yaad rahe)
                    try:
                        from core.models.database import Database as _DBT
                        _aa_d = dict(_aa_d or {})
                        _aa_d["trail_best"] = round(_best_px, 2)
                        _DBT.get_instance().execute(
                            "UPDATE paper_trades SET auto_action=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            [_js.dumps(_aa_d), int(trade.get("id") or 0)])
                    except Exception:
                        pass
                from utils.helpers import trailing_lock_level as _tll
                _la = float(_trail_cfg.get("lock_at") or 0)
                _lp = float(_trail_cfg.get("lock_amt") or 0)
                _lock_level = _tll(entry, _best_px, is_buy, _la, _lp)
                # % rule exclusive: agar strategy me lock_profit_at set hai
                # to wahi chalega (purana "any-profit breakeven lock" nahi).
                if _lock_level > 0:
                    if is_buy:
                        if _lock_level > sl_level:
                            sl_level = _lock_level
                            _trail_armed = True
                    else:
                        if sl_level == 0 or _lock_level < sl_level:
                            sl_level = _lock_level
                            _trail_armed = True
            except Exception:
                _trail_armed = False
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
            if hit_sl and _trail_armed:
                reason = "trailing_lock"
            # EXIT PRICE HONESTY: TP par LEVEL par exit (backtest parity -
            # "TP fill stays at the level"). Pehle current quote par exit
            # hota tha, jisse target hit par +40% fake profit book hota tha.
            # SL par current quote (gap-aware - jaisa backtest me bar extreme).
            _exit_px = current
            if hit_tp and tp_level > 0:
                _exit_px = tp_level
            # Pass is_live=True via close_trade slippage (trade_model.py)
            trade_model.close_trade(trade["id"], _exit_px, datetime.now().strftime("%Y-%m-%d"), reason)
            closed.append({"id": trade["id"], "reason": reason, "exit_price": round(float(_exit_px), 2)})
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
