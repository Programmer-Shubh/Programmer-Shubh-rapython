"""Auto paper-trading for ACTIVE saved strategies (the missing executor).

Why: backtest never creates paper trades and the Auto Trade button only
flipped a label - so saved strategies produced ZERO history. This worker
mirrors the manual Paper Trade button once daily per strategy, in market
hours: live-ATM strike resolution, same SL/TP/costs path (place_trade),
strategy_id attached, expired strategies auto-rolled by place_trade.

Safety rails (no blind loops):
- One entry per strategy per day (skip if open position OR today's entry).
- AUTO option_type legs are skipped (no signal engine here) with a note.
- Throttled: at most one full scan per 20 min, thread-safe.
- Any exception -> skip that strategy, never crash the loop.
"""
import threading
import time

_LOCK = threading.Lock()
_LAST_RUN = 0.0
_MIN_INTERVAL = 1200


def _ist_today():
    try:
        from datetime import datetime, timedelta, timezone
        ist = timezone(timedelta(hours=5, minutes=30))
        return datetime.now(ist).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _market_open():
    try:
        from core.services.sl_monitor import market_open_ist
        return bool(market_open_ist())
    except Exception:
        return True


def run_once(min_interval=_MIN_INTERVAL, market_hours=True):
    """Place today's paper trades for due ACTIVE strategies."""
    global _LAST_RUN
    try:
        with _LOCK:
            if time.time() - _LAST_RUN < min_interval:
                return {"placed": [], "skipped": "throttled"}
            _LAST_RUN = time.time()
    except Exception:
        pass
    if market_hours and not _market_open():
        return {"placed": [], "skipped": "market-closed"}
    placed, notes = [], []
    try:
        import json as _js
        from core.models.database import Database
        from core.models.trade_model import TradeModel
        from core.services.live_market_data import LiveMarketData
        from utils.helpers import get_strike_step
        db = Database.get_instance()
        tm = TradeModel()
        today = _ist_today()
        try:
            rows = db.fetch_all(
                "SELECT * FROM strategies WHERE status='active' ORDER BY updated_at DESC"
            )
        except Exception:
            return {"placed": [], "skipped": "no-strategies-table"}
        for s in rows or []:
            try:
                sid = s.get("id")
                # Due check: no open position, no entry today (any status)
                try:
                    has_open = db.fetch_one(
                        "SELECT id FROM paper_trades WHERE strategy_id=? AND status='open' LIMIT 1",
                        [sid])
                    if has_open:
                        continue
                    traded = db.fetch_one(
                        "SELECT id FROM paper_trades WHERE strategy_id=? AND entry_date=? LIMIT 1",
                        [sid, today])
                    if traded:
                        continue
                except Exception:
                    continue
                try:
                    legs = _js.loads(s.get("legs") or "[]")
                except Exception:
                    legs = []
                if not legs:
                    continue
                if any(str(l.get("option_type", "")).upper() == "AUTO" for l in legs):
                    notes.append(f"{s.get('name')}: AUTO legs need manual direction - skipped")
                    continue
                try:
                    rm = _js.loads(s.get("risk_management") or "{}")
                except Exception:
                    rm = {}
                sl = float(rm.get("daily_stop_loss") or 1500)
                tp = float(rm.get("daily_take_profit") or 3000)
                sym = str(s.get("symbol") or "").upper()
                if not sym:
                    continue
                # Live ATM (same math as the Paper Trade button)
                try:
                    spot = LiveMarketData().get_spot_price(sym) or 0
                except Exception:
                    spot = 0
                if spot <= 0:
                    notes.append(f"{s.get('name')}: no live spot - skipped")
                    continue
                step = get_strike_step(sym)
                atm = round(spot / step) * step if step > 0 else spot
                from routes.option_chain import place_trade, TradeRequest
                for leg in legs:
                    try:
                        opt = str(leg.get("option_type") or "CE").upper()
                        if opt not in ("CE", "PE"):
                            continue
                        txn = str(leg.get("transaction") or "buy").upper()
                        if txn not in ("BUY", "SELL"):
                            txn = "BUY"
                        sel = str(leg.get("strike_selection") or "atm").lower()
                        dist = int(leg.get("otm_distance") or 0)
                        strike = atm
                        if sel == "otm":
                            strike = atm + (dist * step if opt == "CE" else -dist * step)
                        elif sel == "itm":
                            strike = atm + (-dist * step if opt == "CE" else dist * step)
                        req = TradeRequest(
                            symbol=sym, option_type=opt, strike=float(strike),
                            expiry="", date=today, transaction_type=txn,
                            quantity=max(1, int(leg.get("lots") or 1)),
                            stop_loss=sl, take_profit=tp,
                            trade_type="intraday", strategy_id=int(sid or 0))
                        res = place_trade(req)
                        if isinstance(res, dict) and res.get("trade_id"):
                            placed.append({"strategy": s.get("name"), "trade_id": res["trade_id"],
                                           "entry": res.get("entry_price"),
                                           "rolled_to": res.get("rolled_to", "")})
                        elif isinstance(res, dict) and res.get("error"):
                            notes.append(f"{s.get('name')}: {str(res['error'])[:100]}")
                            break
                    except Exception as e:
                        notes.append(f"{s.get('name')}: leg failed {e}"[:120])
                        break
            except Exception:
                continue
        return {"placed": placed, "skipped": "", "notes": notes[:10]}
    except Exception as e:
        return {"placed": [], "skipped": f"error: {e}"[:150]}


def start_background(interval_s=1800):
    """Daemon thread: auto paper-trade scan every `interval_s` (default 30min)."""
    def _loop():
        while True:
            try:
                time.sleep(interval_s)
                r = run_once()
                if r.get("placed"):
                    try:
                        print(f"[auto-paper] placed: {r['placed']}", flush=True)
                    except Exception:
                        pass
            except Exception:
                try:
                    time.sleep(interval_s)
                except Exception:
                    break
    try:
        th = threading.Thread(target=_loop, name="auto-paper", daemon=True)
        th.start()
        return True
    except Exception:
        return False
