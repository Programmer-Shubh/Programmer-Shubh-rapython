"""Tick-by-tick fast SL/TP exit watcher for OPEN paper + live trades.

Problem: `sl_monitor` scans every 10s (and is throttled 20s on portfolio polls).
Ek tez option spike target cross karke wapas aa jata tha -> target "miss ho
gaya" (agle scan me price level ke neeche). User rule: LTP target cross kare
to candle-close / poll ka intezaar nahi - usi tick par square-off.

How: every ~0.75s in market hours, ONE batched Fyers quote call (latest tick)
for every open option contract. Decision usi tick par:
  - TP hit -> exit AT the target level (backtest parity: target book hota hai,
              overshoot ka fake profit nahi).
  - SL hit -> exit at current LTP (gap-aware).
No candle close, no 20s throttle, no per-trade sequential quotes.

Safety (same guards as scripts/auto_trade.check_stoploss_target):
  - stale/bad tick: quote aaj ke traded range (low/high/atp/volume) ke bahar
    ho to ignore (next tick).
  - wrong-contract guard: price entry ka >=3x ya <=1/3 ho to skip.
  - fallback (broker quote na mile) shared pricer se aata hai: fallback price
    sirf SL trigger kar sakta hai, TP kabhi nahi (model price target book
    nahi karega).
  - claim + DB `status='open'` re-check: do monitors ek hi trade kabhi
    double-close nahi kar sakte.
  - trailing-lock SL (lock_profit_at) sl_monitor ke paas hi rehta hai; ye
    watcher base SL/TP par fast exit karta hai (lock level base SL se hamesha
    tighter hota hai, isliye koi exit miss nahi hota).
"""
import threading
import time

_POLL_S = 0.75          # tick poll (Fyers quote call latency ke saath ~1s)
_TRADES_TTL = 2.0       # open-trade list cache (DB read har tick nahi)
_FALLBACK_EVERY = 2.0   # broker quote missing -> shared pricer at most 1/2s
_SKIP_LOG_EVERY = 30.0  # same skip reason ko har 30s me ek baar log karo
_CLAIM_TTL = 180.0

_LOCK = threading.Lock()
_CLAIMS = {}            # trade_id -> claim ts
_LAST_FALLBACK = {}     # trade_id -> ts
_LAST_SKIP_LOG = {}     # trade_id -> ts
_LEVEL_CACHE = {}       # trade_id -> (signature, levels)
_TRADES_CACHE = (0.0, [])
_STARTED = False
_LAST_SCAN = {"ts": "", "closed": [], "skipped": []}


def _now_ts():
    return time.time()


def _ist_now_str():
    try:
        from datetime import datetime, timedelta, timezone
        ist = timezone(timedelta(hours=5, minutes=30))
        return datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _log_skip(trade, reason):
    """JSONL skip log (throttled per trade so a bad quote doesn't spam disk)."""
    try:
        tid = int(trade.get("id") or 0)
        now = _now_ts()
        if now - float(_LAST_SKIP_LOG.get(tid) or 0) < _SKIP_LOG_EVERY:
            return
        _LAST_SKIP_LOG[tid] = now
        from utils.helpers import log_exit_skip as _log
        _log("fast_exit", tid, trade.get("symbol"), reason)
    except Exception:
        pass


def _open_trades():
    global _TRADES_CACHE
    now = _now_ts()
    ts, rows = _TRADES_CACHE
    if now - ts < _TRADES_TTL:
        return rows
    rows = []
    try:
        from core.models.trade_model import TradeModel
        rows = TradeModel().get_open_trades() or []
    except Exception:
        rows = []
    _TRADES_CACHE = (now, rows)
    return rows


def _levels(trade):
    """Cached SL/TP premium levels (single source: utils.helpers)."""
    try:
        tid = int(trade.get("id") or 0)
    except Exception:
        tid = 0
    sig = (trade.get("entry_price"), trade.get("stop_loss"), trade.get("target"),
           trade.get("sl_unit"), trade.get("tp_unit"), trade.get("quantity"),
           trade.get("lot_size"), trade.get("transaction_type"), trade.get("symbol"))
    hit = _LEVEL_CACHE.get(tid)
    if hit and hit[0] == sig:
        return hit[1]
    entry = float(trade.get("entry_price") or 0)
    txn = trade.get("transaction_type")
    is_buy = str(txn or "BUY").upper() != "SELL"
    qty = max(int(trade.get("quantity") or 1), 1)
    try:
        from utils.helpers import effective_lot_size as _el
        lot = int(_el(trade.get("symbol"), trade.get("lot_size")) or 0)
    except Exception:
        lot = int(trade.get("lot_size") or 0)
    if lot <= 0:
        lot = 50
    from utils.helpers import sltp_premium_level as _lvl
    lv = {
        "entry": entry,
        "is_buy": is_buy,
        "sl": _lvl(entry, trade.get("stop_loss"), qty, lot, txn, True,
                   unit=trade.get("sl_unit") or None),
        "tp": _lvl(entry, trade.get("target"), qty, lot, txn, False,
                   unit=trade.get("tp_unit") or None),
    }
    _LEVEL_CACHE[tid] = (sig, lv)
    return lv


def _node_ltp(node):
    """Latest usable LTP from one Fyers quote node (0.0 = stale/bad tick)."""
    if not isinstance(node, dict):
        return 0.0
    lp = 0.0
    for f in ("lp", "last_price", "lastPrice", "ltp", "LTP"):
        try:
            v = float(node.get(f, 0) or 0)
        except Exception:
            v = 0
        if v > 0:
            lp = v
            break
    if lp <= 0:
        return 0.0
    try:
        lo = float(node.get("low_price", 0) or 0)
        hi = float(node.get("high_price", 0) or 0)
        pc = float(node.get("prev_close_price", 0) or 0)
        vol = float(node.get("volume", 0) or 0)
        atp = float(node.get("atp", 0) or 0)
        if lo > 0 and lp < lo - 0.05:
            return 0.0
        if hi > 0 and lp > hi + 0.05:
            return 0.0
        if vol <= 0 and pc > 0 and abs(lp - pc) < 0.05:
            return 0.0
        if vol <= 0 and atp > 0 and abs(lp - atp) > 0.05:
            return 0.0
    except Exception:
        pass
    return lp


def _batch_quotes(pairs):
    """pairs=[(trade_id, fyers_symbol)] -> {trade_id: ltp}.

    ONE /data/quotes call per <=20 symbols (URL-safe chunk) - all open
    contracts ek hi tick snapshot me."""
    out = {}
    try:
        from core.services.contract_pricer import (_fyers_auth, _fy_quote_rows,
                                                   _fyers_quotes_raw)
        app_id, token = _fyers_auth()
        if not app_id or not token or not pairs:
            return out
        for i in range(0, len(pairs), 20):
            chunk = pairs[i:i + 20]
            body = _fyers_quotes_raw(",".join(str(s) for _, s in chunk),
                                     app_id, token)
            rows = _fy_quote_rows(body)
            if not rows:
                continue
            for tid, fs in chunk:
                node = rows.get(fs) or rows.get(fs.replace("NSE:", "")) or {}
                v = _node_ltp(node)
                if v > 0:
                    out[tid] = v
    except Exception:
        pass
    return out


def _claim(trade_id):
    """Atomically claim a trade + re-check it is still open in the DB."""
    now = _now_ts()
    with _LOCK:
        ts = _CLAIMS.get(trade_id)
        if ts and now - ts < _CLAIM_TTL:
            return False
        _CLAIMS[trade_id] = now
    try:
        from core.models.database import Database
        row = Database.get_instance().fetch_one(
            "SELECT status FROM paper_trades WHERE id=?", [int(trade_id)])
        if not row or str(row.get("status") or "").lower() != "open":
            with _LOCK:
                _CLAIMS.pop(trade_id, None)
            return False
        return True
    except Exception:
        with _LOCK:
            _CLAIMS.pop(trade_id, None)
        return False


def _release(trade_id):
    with _LOCK:
        _CLAIMS.pop(trade_id, None)


def _fallback_price(trade, trade_id):
    now = _now_ts()
    if now - float(_LAST_FALLBACK.get(trade_id) or 0) < _FALLBACK_EVERY:
        return None
    _LAST_FALLBACK[trade_id] = now
    try:
        from core.models.trade_model import TradeModel
        return TradeModel().get_option_premium(
            trade.get("symbol"), trade.get("option_type"),
            trade.get("strike_price"), trade.get("expiry_date"))
    except Exception:
        return None


def check_once():
    """One tick pass. Returns {closed, skipped, checked}."""
    global _TRADES_CACHE, _LAST_SCAN
    closed, skipped = [], []
    try:
        from core.services.sl_monitor import market_open_ist
        if not market_open_ist():
            return {"closed": [], "skipped": "market-closed", "checked": 0}
    except Exception:
        pass
    trades = []
    for t in _open_trades():
        opt = str(t.get("option_type") or "").upper()
        if opt not in ("CE", "PE"):
            continue          # equity/arb legs: arb_engine + sl_monitor
        try:
            if float(t.get("entry_price") or 0) <= 0:
                continue
        except Exception:
            continue
        trades.append(t)
    if not trades:
        _TRADES_CACHE = (0.0, [])
        _LEVEL_CACHE.clear()
        return {"closed": [], "skipped": "", "checked": 0}
    # ---- ONE batched broker quote for every open contract ----
    pairs = []
    try:
        from core.services.symbol_resolver import _fyers_symbol as _fsym
        for t in trades:
            try:
                fs = _fsym(t.get("symbol"), t.get("expiry_date"),
                           float(t.get("strike_price") or 0),
                           t.get("option_type"))
            except Exception:
                fs = ""
            if fs:
                pairs.append((int(t.get("id") or 0), fs))
    except Exception:
        pairs = []
    quotes = _batch_quotes(pairs)
    for t in trades:
        tid = int(t.get("id") or 0)
        if not tid:
            continue
        lv = _levels(t)
        sl_level, tp_level = lv["sl"], lv["tp"]
        if sl_level <= 0 and tp_level <= 0:
            continue
        price = float(quotes.get(tid) or 0)
        source = "broker"
        if price <= 0:
            fb = _fallback_price(t, tid)
            if fb is None or float(fb) <= 0:
                skipped.append({"id": tid, "symbol": t.get("symbol"),
                                "reason": "koi quote nahi (broker + fallback)"})
                continue
            price = float(fb)
            source = "fallback"
        entry = lv["entry"]
        # wrong-contract / hard spike guard (same as auto_trade)
        try:
            ratio = (price / entry) if entry > 0 else 0.0
            if (not lv["is_buy"] and ratio >= 3.0) or (lv["is_buy"] and ratio <= 0.3333):
                reason = (f"price {price:.2f} entry {entry:.2f} se {ratio:.2f}x - "
                          "bad contract/spike, tick skip")
                skipped.append({"id": tid, "symbol": t.get("symbol"), "reason": reason})
                _log_skip(t, reason)
                continue
        except Exception:
            pass
        is_buy = lv["is_buy"]
        hit_sl = (price <= sl_level) if is_buy else (price >= sl_level) if sl_level > 0 else False
        hit_tp = (price >= tp_level) if is_buy else (price <= tp_level) if tp_level > 0 else False
        # Model/fallback price target book nahi karega (sirf risk exit).
        if hit_tp and source != "broker":
            hit_tp = False
        if hit_sl and hit_tp:
            hit_tp = False       # SL priority (worst case), backtest parity
        if not (hit_sl or hit_tp):
            continue
        reason = "stoploss" if hit_sl else "target"
        exit_px = tp_level if (hit_tp and tp_level > 0) else price
        if not _claim(tid):
            continue
        try:
            from core.models.trade_model import TradeModel
            rc = TradeModel().close_trade(tid, float(exit_px), _ist_now_str()[:10], reason)
        except Exception as e:
            _release(tid)
            skipped.append({"id": tid, "symbol": t.get("symbol"),
                            "reason": f"close fail: {e}"[:150]})
            continue
        if not rc:
            _release(tid)
            continue
        row = {"id": tid, "reason": reason,
               "exit_price": round(float(exit_px), 2),
               "ltp": round(float(price), 2), "src": source,
               "symbol": t.get("symbol"), "option_type": t.get("option_type"),
               "strike": t.get("strike_price"), "mode": t.get("trade_mode")}
        closed.append(row)
        try:
            print(f"[fast-exit] {reason} closed #{tid} {t.get('symbol')} "
                  f"{t.get('option_type')}{t.get('strike_price')} "
                  f"@ {exit_px:.2f} (ltp {price:.2f}, {source})", flush=True)
        except Exception:
            pass
    # cache cleanup: sirf live trades ke levels rakho
    live_ids = {int(t.get("id") or 0) for t in trades}
    for k in list(_LEVEL_CACHE.keys()):
        if k not in live_ids:
            _LEVEL_CACHE.pop(k, None)
    for k in list(_CLAIMS.keys()):
        if k not in live_ids:
            _CLAIMS.pop(k, None)
    _LAST_SCAN = {"ts": _ist_now_str(), "closed": closed, "skipped": skipped,
                  "checked": len(trades)}
    return {"closed": closed, "skipped": skipped, "checked": len(trades)}


def last_scan():
    return dict(_LAST_SCAN)


def is_started():
    """Watcher thread zinda hai? (/api/version diagnostics)"""
    try:
        return bool(_STARTED) and any(
            t.name == "fast-exit" and t.is_alive() for t in threading.enumerate())
    except Exception:
        return bool(_STARTED)


def start_background(interval_s=_POLL_S):
    """Daemon thread: tick-by-tick SL/TP exit watcher (idempotent)."""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return True
        _STARTED = True

    def _loop():
        while True:
            started = _now_ts()
            try:
                r = check_once()
                if r.get("closed"):
                    try:
                        from core.services.auto_paper import _beat
                        _beat("fast-exit")
                    except Exception:
                        pass
            except Exception:
                pass
            elapsed = _now_ts() - started
            try:
                time.sleep(max(0.05, float(interval_s) - elapsed))
            except Exception:
                break

    try:
        th = threading.Thread(target=_loop, name="fast-exit", daemon=True)
        th.start()
        return True
    except Exception:
        _STARTED = False
        return False
