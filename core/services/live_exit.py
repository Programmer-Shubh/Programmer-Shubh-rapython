"""Real brokr EXIT for LIVE trades (trade_mode='live').

Problem kya thi: TradeModel.close_trade sirf DB record band karta tha -
REAL broker position khulti nahi thi (paper-close ho gaya, broker par
position padi rahti). Ab har close par jo trade LIVE hai + REAL broker
order id rakhta hai, uska OPPOSITE-SIDE MARKET order broker ko jata hai:

- SL/TP            : scripts/auto_trade.check_stoploss_target
- EOD squareoff    : TradeModel.close_intraday_trades (15:15 IST, user rule)
- Strategy exit    : TradeModel.close_strategy_exit_time_trades (exit_time)
- Manual close     : /api/trades/close

Rules (user ke real-money lifecycle):
- Nayi entry sirf 15:00 (3 PM) tak (entry_open_ist) - EXITS hamesha open.
- Intraday position 15:15 par square-off; 155:15+ market band hone tak
  monitor chalta hai (market_open_ist window 9:00-15:45).
- Positional trades ka broker-exit YAHAN se nahi jata (overnight/expiry
  lifecycle unka khud ka hai) - sirf intraday/option closes par.
- Broker bhejna fail ho to DB close FIR BHI hota (paper math parity);
  broker tak position ke liye Fyers ka MIS auto-square-off last backstop
  hai. Fail ko log_exit_skip me note karte hain.
"""
import logging
import threading

logger = logging.getLogger(__name__)

_SENT_LOCK = threading.Lock()
_SENT: set = set()  # trade ids jinke broker-exit already plan ho chuke


def _seeded_exit(trade: dict) -> bool:
    """True agar is trade ka broker-exit ABHI bheja ja sakta hai."""
    try:
        if str((trade or {}).get("trade_mode") or "").lower() != "live":
            return False
        if not (trade or {}).get("broker_order_id"):
            return False
        opt = str((trade or {}).get("option_type") or "").upper()
        strike = float((trade or {}).get("strike_price") or 0)
        if opt not in ("CE", "PE") or strike <= 0:
            return False  # sirf option trades (EQ/arb exit alag flow)
        tok = str((trade or {}).get("broker_order_id") or "")
        if tok.startswith(("SIM-", "ANG-")):
            return False  # simulated token = demo, kuch bhi na bhejo
        _exp = str((trade or {}).get("expiry_date") or "")
        if len(_exp) < 10:
            return False
        tid = int((trade or {}).get("id") or 0)
        if tid <= 0:
            return False
        with _SENT_LOCK:
            if tid in _SENT:
                return False
            _SENT.add(tid)
        return True
    except Exception:
        return False


async def send_broker_exit(trade: dict, reason: str = "") -> dict:
    """OPPOSITE-side MARKET order (broker), phir 3x fill verify (avg price).
    Returns {sent, order_id, exit_price, error}."""
    out = {"sent": False, "order_id": "", "exit_price": 0.0, "error": ""}
    try:
        symbol = str(trade.get("symbol") or "").upper()
        opt = str(trade.get("option_type") or "").upper()
        strike = float(trade.get("strike_price") or 0)
        exp_ymd = str(trade.get("expiry_date") or "")[:10]
        lots = int(trade.get("quantity") or 1)
        lot = int(trade.get("lot_size") or 0)
        if lot <= 0:
            from utils.helpers import get_lot_size as _gls
            lot = int(_gls(symbol))
        qty = lots * lot
        txn = str(trade.get("transaction_type") or "").upper()
        closing_side = "SELL" if txn == "BUY" else "BUY"
        from core.services.symbol_resolver import _fyers_symbol
        bsym = _fyers_symbol(symbol, exp_ymd, strike, opt)
        from routes.broker import _get_config, _fyers_client
        cfg = _get_config("fyers") or {}
        if not (cfg.get("app_id") and cfg.get("access_token")):
            out["error"] = "fyers token missing (exit not sent)"
            return out
        fy = _fyers_client(cfg)
        logger.info("LIVE EXIT %s %s %d qty (trade %s, %s)",
                    closing_side, bsym, qty, trade.get("id"), reason or "auto")
        res = await fy.place_order(bsym, closing_side, qty, order_type="MARKET")
        if not res.get("success"):
            out["error"] = str(res.get("error") or res.get("data") or "place failed")[:220]
            return out
        out["sent"] = True
        out["order_id"] = str(((res.get("data") or {}).get("order_id") or ""))
        # Naya token save (auto-login mid-flight ho to)
        try:
            if fy.refreshed and fy.access_token:
                cfg["access_token"] = fy.access_token
                if fy.refresh_token:
                    cfg["refresh_token"] = fy.refresh_token
                from routes.broker import _save_config
                _save_config("fyers", cfg)
        except Exception:
            pass
        # Fill verify (short): real avg price exit record me chahiye
        import asyncio as _aio
        for _ in range(3):
            try:
                await _aio.sleep(1.2)
                ob = await fy.get_order_book()
                rows = ((ob or {}).get("data") or {}).get("orderBook") or []
                for r in rows if isinstance(rows, list) else []:
                    if not isinstance(r, dict):
                        continue
                    if str(r.get("id") or r.get("orderId") or "") != out["order_id"]:
                        continue
                    try:
                        avg = float(r.get("averagePrice") or r.get("average_price") or 0)
                    except Exception:
                        avg = 0.0
                    if avg > 0:
                        out["exit_price"] = round(avg, 2)
                    st = str(r.get("status") or r.get("orderStatus") or "").upper()
                    if st in ("EXECUTED", "TRADED", "COMPLETE", "COMPLETED", "FILLED"):
                        break
                if out["exit_price"] > 0:
                    break
            except Exception:
                continue
        return out
    except Exception as e:
        out["error"] = str(e)[:200]
        return out


def send_broker_exit_threaded(trade: dict, reason: str = "") -> dict:
    """close_trade (sync) se fire-and-forget: background thread me chalata
    hai, DB close block nahi hota. Dedup per trade id. Returns plan msg."""
    if not _seeded_exit(trade):
        return {"planned": False}
    tid = int(trade.get("id") or 0)

    def _run():
        try:
            import asyncio as _aio
            if _aio.get_event_loop().is_running():
                loop = _aio.new_event_loop()
                try:
                    res = loop.run_until_complete(send_broker_exit(trade, reason))
                finally:
                    loop.close()
            else:
                res = _aio.run(send_broker_exit(trade, reason))
            if res.get("sent"):
                msg = (f"broker-exit {res.get('order_id')} @ {res.get('exit_price') or 'pending-fill'}")
            else:
                msg = f"broker-exit FAIL: {res.get('error')}"
            try:
                from utils.helpers import log_exit_skip as _log
                _log("broker_exit", tid, trade.get("symbol"),
                     f"[{reason or 'auto'}] {msg}")
            except Exception:
                pass
            logger.info("LIVE EXIT trade %s: %s", tid, msg)
        except Exception as e:
            logger.error("LIVE EXIT trade %s crashed: %s", tid, e)

    try:
        threading.Thread(target=_run, daemon=True, name=f"lx-{tid}").start()
        return {"planned": True, "trade_id": tid}
    except Exception as e:
        # Thread fail (rare) -> dedup revert, phir kisi manual close par
        # bhej saken.
        with _SENT_LOCK:
            _SENT.discard(tid)
        return {"planned": False, "error": str(e)[:120]}
