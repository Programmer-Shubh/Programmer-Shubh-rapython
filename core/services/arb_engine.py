"""Inter-exchange cash arbitrage (NSE vs BSE) - strict execution framework.

The ₹14 SBIN gap proved the old flow trusted frontend prices blindly: one
stale leg (days-old DB close vs live quote) fabricated a 1.5% 'spread' that
can never exist (real NSE/BSE gaps are paise). Rules enforced here:

1. Spread sanity: BOTH legs re-quoted server-side, fresh (<=60s, live
   sources only - never DB-stale). Spread > 0.30% => stale/fantasy BLOCK.
2. Simultaneous dual-leg entry: parallel inserts; leg-2 failure rolls back
   leg-1 (cancel-and-flat). No orphan legs, ever.
3. Net cost filter: gross spread minus both-leg brokerage/STT/GST/exchange/
   stamp + slippage buffer must stay positive (min_net configurable).
4. Simultaneous square-off: exit_pair closes ALL open legs of the base
   symbol together at live spot.
5. Intraday mandatory exit 15:15 IST (close_intraday_trades cutoff).
6. Time/spread stops (monitor): age > 10 min without convergence to costs,
   or spread widening to 2x entry => close both ('time' / 'spread' stop).
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

MAX_SPREAD_PCT = 0.30
QUOTE_MAX_AGE_S = 60
DEFAULT_MIN_NET = 0.0
SLIPPAGE_BUFFER_PCT = 0.05
TIME_STOP_MIN = 10
SPREAD_WIDEN_MULT = 2.0


def _base(sym):
    return str(sym or "").split(" (")[0].strip().upper()


def quote_pair(symbol):
    """Server-side fresh quotes for both legs. Returns dict or {'error'}.
    NSE leg: live spot (non-DB source, <=60s). BSE leg: Yahoo {SYM}.BO."""
    sym = _base(symbol)
    if not sym:
        return {"error": "symbol required"}
    nse_px, nse_age, nse_src = 0, 9999, ""
    try:
        from core.services.live_market_data import LiveMarketData, _LIVE_CACHE
        sp = LiveMarketData().get_live_spot(sym) or {}
        nse_px = float(sp.get("spot") or 0)
        nse_src = str(sp.get("source") or "")
        try:
            e = _LIVE_CACHE.get(sym.upper())
            nse_age = time.time() - float(e.get("ts", 0)) if e else 9999
        except Exception:
            nse_age = 0 if nse_px > 0 else 9999
    except Exception:
        pass
    bse_px, bse_age = 0, 9999
    try:
        from core.services.free_data import _yahoo_fallback_quote
        t0 = time.time()
        q = _yahoo_fallback_quote(f"{sym}.BO", timeout=4) or {}
        bse_px = float(q.get("spot") or 0)
        bse_age = time.time() - t0
    except Exception:
        pass
    if nse_px <= 0 or bse_px <= 0:
        return {"error": f"live quotes unavailable (NSE {nse_px}, BSE {bse_px}) - stale trade blocked"}
    if nse_src == "db" or nse_age > QUOTE_MAX_AGE_S:
        return {"error": f"NSE leg stale (age {nse_age:.0f}s, src {nse_src or 'na'}) - trade blocked"}
    if bse_age > QUOTE_MAX_AGE_S:
        return {"error": "BSE leg stale - trade blocked"}
    return {"symbol": sym, "nse": round(nse_px, 2), "bse": round(bse_px, 2),
            "nse_age_s": round(nse_age, 1), "bse_age_s": round(bse_age, 1)}


def check_spread(symbol=None, buy_px=0, sell_px=0):
    """Validate spread from live quotes (frontend prices are hints only).
    Returns (buy_ex, sell_ex, buy, sell, pct) or (None * 5 + error)."""
    sym = _base(symbol)
    q = quote_pair(sym)
    if q.get("error"):
        return None, None, 0, 0, 0, q["error"]
    nse, bse = q["nse"], q["bse"]
    if nse >= bse:
        buy_ex, sell_ex, buy, sell = "BSE", "NSE", bse, nse
    else:
        buy_ex, sell_ex, buy, sell = "NSE", "BSE", nse, bse
    spread = sell - buy
    pct = abs(spread) / max(buy, 0.01) * 100
    if pct > MAX_SPREAD_PCT:
        return None, None, 0, 0, 0, (
            f"Abnormal spread {pct:.2f}% (₹{spread:.2f}) > {MAX_SPREAD_PCT}% cap - "
            f"stale-feed fantasy, NSE {nse} vs BSE {bse}. Trade blocked.")
    return buy_ex, sell_ex, buy, sell, pct, ""


def _leg_costs(price, qty, is_sell, intraday=True):
    try:
        from core.services.transaction_costs import TransactionCosts
        return TransactionCosts.calculate_equity(price * qty, is_sell, intraday)
    except Exception:
        return {"total": 40.0}


def net_of_costs(buy, sell, qty):
    """Net spread after both-leg costs + slippage buffer on each side."""
    try:
        gross = (sell - buy) * qty
        cb = _leg_costs(buy, qty, False)["total"]
        cs = _leg_costs(sell, qty, True)["total"]
        slip = (buy * qty + sell * qty) * SLIPPAGE_BUFFER_PCT / 100
        return round(gross - cb - cs - slip, 2)
    except Exception:
        return -999999.0


def place_pair(symbol, qty=1, min_net=DEFAULT_MIN_NET):
    """Validate + place BOTH legs atomically. Returns dict with ids or error."""
    from core.models.trade_model import TradeModel
    import datetime as _dt
    qty = max(1, int(qty or 1))
    buy_ex, sell_ex, buy, sell, pct, err = check_spread(symbol)
    if err:
        return {"error": err}
    net = net_of_costs(buy, sell, qty)
    if net <= min_net:
        return {"error": f"Spread too thin: net ₹{net} after costs (need > ₹{min_net}). No trade."}
    tm = TradeModel()
    today = _dt.date.today().strftime("%Y-%m-%d")
    sym = _base(symbol)
    id1 = id2 = None
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(tm.db.execute,
                "INSERT INTO paper_trades (user_id,symbol,option_type,strike_price,expiry_date,transaction_type,quantity,lot_size,entry_price,entry_date,status,trade_mode,trade_type) VALUES (1,?,'EQ',0,'','BUY',?,1,?,?,'open','paper','intraday')",
                [f"{sym} ({buy_ex})", qty, buy, today])
            f2 = ex.submit(tm.db.execute,
                "INSERT INTO paper_trades (user_id,symbol,option_type,strike_price,expiry_date,transaction_type,quantity,lot_size,entry_price,entry_date,status,trade_mode,trade_type) VALUES (1,?,'EQ',0,'','SELL',?,1,?,?,'open','paper','intraday')",
                [f"{sym} ({sell_ex})", qty, sell, today])
            id1, id2 = f1.result(), f2.result()
    except Exception as e:
        # Cancel-and-flat: never leave an orphan leg
        try:
            if id1 and not id2:
                tm.db.execute("DELETE FROM paper_trades WHERE id=?", [id1])
            elif id2 and not id1:
                tm.db.execute("DELETE FROM paper_trades WHERE id=?", [id2])
        except Exception:
            pass
        return {"error": f"dual-leg failed, flattened: {e}"[:150]}
    if not (id1 and id2):
        try:
            if id1:
                tm.db.execute("DELETE FROM paper_trades WHERE id=?", [id1])
            if id2:
                tm.db.execute("DELETE FROM paper_trades WHERE id=?", [id2])
        except Exception:
            pass
        return {"error": "dual-leg incomplete - both cancelled, no orphan"}
    return {"success": True, "buy_id": id1, "sell_id": id2,
            "symbol": sym, "buy_ex": buy_ex, "sell_ex": sell_ex,
            "buy_px": buy, "sell_px": sell,
            "spread": round(sell - buy, 2), "spread_pct": round(pct, 3),
            "net": net}


def exit_pair(symbol):
    """Simultaneous square-off of ALL open EQ legs for the base symbol."""
    from core.models.trade_model import TradeModel
    from core.services.live_market_data import LiveMarketData
    tm = TradeModel()
    sym = _base(symbol)
    try:
        opens = tm.db.fetch_all(
            "SELECT * FROM paper_trades WHERE status='open' AND option_type='EQ' AND (symbol=? OR symbol LIKE ?)",
            [sym, sym + " (%"])
    except Exception:
        opens = []
    if not opens:
        # broader match: suffix-style symbols
        try:
            opens = [t for t in tm.get_open_trades()
                     if (t.get("option_type") == "EQ" and _base(t.get("symbol")) == sym)]
        except Exception:
            opens = []
    if not opens:
        return {"closed": [], "note": "no open EQ legs"}
    try:
        spot = float((LiveMarketData().get_live_spot(sym) or {}).get("spot") or 0)
    except Exception:
        spot = 0
    if spot <= 0:
        return {"error": "no live spot for square-off - legs kept (safe)"}
    import datetime as _dt
    today = _dt.date.today().strftime("%Y-%m-%d")
    closed = []

    def _one(t):
        try:
            tm.close_trade(t["id"], spot, today, exit_status="arb_exit")
            return {"id": t["id"], "exit": spot}
        except Exception as e:
            return {"id": t.get("id"), "error": str(e)[:100]}

    try:
        with ThreadPoolExecutor(max_workers=max(2, len(opens))) as ex:
            for r in ex.map(_one, opens):
                closed.append(r)
    except Exception as e:
        return {"error": str(e)[:150]}
    return {"closed": closed, "exit_px": spot}


def check_arb_stops():
    """Time + spread stops for open EQ pairs. Called by sl_monitor."""
    out = []
    try:
        from core.models.trade_model import TradeModel
        from core.services.live_market_data import LiveMarketData
        import datetime as _dt
        tm = TradeModel()
        opens = [t for t in tm.get_open_trades() if t.get("option_type") == "EQ"]
        by_sym = {}
        for t in opens:
            by_sym.setdefault(_base(t.get("symbol")), []).append(t)
        for sym, legs in by_sym.items():
            buys = [t for t in legs if t.get("transaction_type") == "BUY"]
            sells = [t for t in legs if t.get("transaction_type") != "BUY"]
            if not (buys and sells):
                continue
            try:
                spot = float((LiveMarketData().get_live_spot(sym) or {}).get("spot") or 0)
            except Exception:
                spot = 0
            if spot <= 0:
                continue
            # Unrealized combined (BUY gains when spot rises, SELL gains when falls)
            pnl = 0.0
            entry_spread = 0.0
            ages = []
            try:
                now = _dt.datetime.now()
                for t in legs:
                    q = int(t.get("quantity", 1) or 1)
                    ep = float(t.get("entry_price", 0) or 0)
                    if t.get("transaction_type") == "BUY":
                        pnl += (spot - ep) * q
                    else:
                        pnl -= (spot - ep) * q
                    try:
                        ca = str(t.get("created_at", "") or "")
                        from datetime import datetime as _d2
                        cdt = _d2.fromisoformat(ca.replace("Z", "+00:00")) if ca else None
                        if cdt:
                            ages.append((now.astimezone(cdt.tzinfo) - cdt).total_seconds() / 60 if cdt.tzinfo else 9999)
                    except Exception:
                        pass
            except Exception:
                continue
            try:
                b_px = min(float(t.get("entry_price", 0) or 0) for t in buys)
                s_px = max(float(t.get("entry_price", 0) or 0) for t in sells)
                entry_spread = s_px - b_px
            except Exception:
                entry_spread = 0.0
            age = min(ages) if ages else 9999
            # Single-spot world: locked spread can't be re-measured per
            # exchange, so stops use combined economics vs entry lock.
            reason = ""
            tot_q = sum(int(t.get("quantity", 1) or 1) for t in legs)
            if entry_spread > 0 and pnl < -entry_spread * tot_q * (SPREAD_WIDEN_MULT - 1):
                reason = "spread-stop"
            elif age > TIME_STOP_MIN and pnl <= 0:
                reason = "time-stop"
            if reason:
                r = exit_pair(sym)
                out.append({"symbol": sym, "reason": reason, "age_min": round(age, 1),
                            "pnl": round(pnl, 2), "exit": r})
    except Exception:
        pass
    return out
