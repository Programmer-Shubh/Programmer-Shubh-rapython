"""Tick-by-tick fast SL/TP exit watcher for OPEN paper + live trades.

Problem: `sl_monitor` scans every 10s (and is throttled 20s on portfolio polls).
Ek tez option spike target cross karke wapas aa jata tha -> target "miss ho
gaya" (agle scan me price level ke neeche). User rule: LTP target cross kare
to candle-close / poll ka intezaar nahi - usi tick par square-off.

How (v7 - TRUE tick-by-tick):
  1. PRIMARY: Fyers WebSocket (`FyersDataSocket`, litemode) subscribed to
     every open option contract. Har EXCHANGE TICK par decision - koi 0.75s/
     1.5s poll wait nahi. Naye/closed contracts ki subscription har 5s me
     reconcile hoti hai.
  2. SAFETY NET: agar token/WS na chale to har ~0.75s ek batched Fyers REST
     quote call (same decision function) - exits kabhi rukte nahi.
  Decision usi tick par:
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

_POLL_S = 0.75          # REST fallback poll (WS up ho to sirf safety net)
_TRADES_TTL = 2.0       # open-trade list cache (DB read har tick nahi)
_FALLBACK_EVERY = 2.0   # broker quote missing -> shared pricer at most 1/2s
_SKIP_LOG_EVERY = 30.0  # same skip reason ko har 30s me ek baar log karo
_CLAIM_TTL = 180.0
_SUB_RECONCILE_S = 5.0  # naye/closed contracts ki WS subscription sync

_LOCK = threading.Lock()
_CLAIMS = {}            # trade_id -> claim ts
_LAST_FALLBACK = {}     # trade_id -> ts
_LAST_SKIP_LOG = {}     # trade_id -> ts
_LEVEL_CACHE = {}       # trade_id -> (signature, levels)
_TRADES_CACHE = (0.0, [])
_STARTED = False
_LAST_SCAN = {"ts": "", "closed": [], "skipped": []}

# ---- True tick-by-tick stream state (Fyers WebSocket) ----
# REST poll (0.75s) sirf safety net hai. Jab WS connected hota hai to har
# EXCHANGE TICK par SL/TP evaluate hota hai - 1.5s/0.75s poll ka wait nahi.
_WS = {
    "enabled": True,
    "started": False,
    "connected": False,
    "subscribed": [],       # fyers symbols
    "ticks": 0,
    "exits": 0,
    "last_tick": "",
    "last_error": "",
    "connected_at": "",
}
_WS_SOCKET = None          # live FyersDataSocket (subscribe/unsubscribe ke liye)
_SYM_MAP = {}              # fyers symbol -> trade id
_SYM_MAP_TS = 0.0
_LAST_RECONCILE = 0.0
_WS_BACKOFF = 5.0


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


def _evaluate_and_exit(t, price, source="broker"):
    """Ek quote/tick -> turant decision (poll aur WS dono yahi use karte hain).

    Returns ("closed", row) | ("skip", {id, symbol, reason}) | (None, None).
    Guards: wrong-contract/spike ratio, model-price TP ban, SL priority,
    atomic claim + DB open re-check (double-close impossible)."""
    tid = int((t or {}).get("id") or 0)
    if not tid:
        return None, None
    lv = _levels(t)
    sl_level, tp_level = lv["sl"], lv["tp"]
    if sl_level <= 0 and tp_level <= 0:
        return None, None
    try:
        price = float(price or 0)
    except Exception:
        price = 0.0
    if price <= 0:
        return None, {"id": tid, "symbol": t.get("symbol"), "reason": "quote 0"}
    entry = lv["entry"]
    # wrong-contract / hard spike guard (same as auto_trade)
    try:
        ratio = (price / entry) if entry > 0 else 0.0
        if (not lv["is_buy"] and ratio >= 3.0) or (lv["is_buy"] and ratio <= 0.3333):
            reason = (f"price {price:.2f} entry {entry:.2f} se {ratio:.2f}x - "
                      "bad contract/spike, tick skip")
            _log_skip(t, reason)
            return None, {"id": tid, "symbol": t.get("symbol"), "reason": reason}
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
        return None, None
    reason = "stoploss" if hit_sl else "target"
    exit_px = tp_level if (hit_tp and tp_level > 0) else price
    if not _claim(tid):
        return None, None
    try:
        from core.models.trade_model import TradeModel
        rc = TradeModel().close_trade(tid, float(exit_px), _ist_now_str()[:10], reason)
    except Exception as e:
        _release(tid)
        return None, {"id": tid, "symbol": t.get("symbol"),
                      "reason": f"close fail: {e}"[:150]}
    if not rc:
        _release(tid)
        return None, None
    row = {"id": tid, "reason": reason,
           "exit_price": round(float(exit_px), 2),
           "ltp": round(float(price), 2), "src": source,
           "symbol": t.get("symbol"), "option_type": t.get("option_type"),
           "strike": t.get("strike_price"), "mode": t.get("trade_mode")}
    try:
        print(f"[fast-exit] {reason} closed #{tid} {t.get('symbol')} "
              f"{t.get('option_type')}{t.get('strike_price')} "
              f"@ {exit_px:.2f} (ltp {price:.2f}, {source})", flush=True)
    except Exception:
        pass
    return "closed", row


def check_once():
    """One REST poll pass (WS ka safety net). Returns {closed, skipped, checked}."""
    global _TRADES_CACHE, _LAST_SCAN
    closed, skipped = [], []
    try:
        from core.services.sl_monitor import market_open_ist
        if not market_open_ist():
            return {"closed": [], "skipped": "market-closed", "checked": 0}
    except Exception:
        pass
    # Naye/closed contracts ki WS subscription sync (throttled internally).
    try:
        _reconcile_subscriptions()
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
        kind, payload = _evaluate_and_exit(t, price, source)
        if kind == "closed":
            closed.append(payload)
        elif kind == "skip" and payload:
            skipped.append(payload)
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


# ------------------------------------------------------------------ tick stream
def _refresh_symbol_map(force=False):
    """fyers symbol -> trade id map (WS subscription + tick routing)."""
    global _SYM_MAP, _SYM_MAP_TS
    now = _now_ts()
    if not force and now - _SYM_MAP_TS < _TRADES_TTL:
        return _SYM_MAP
    m = {}
    try:
        from core.services.symbol_resolver import _fyers_symbol as _fsym
        for t in _open_trades():
            if str(t.get("option_type") or "").upper() not in ("CE", "PE"):
                continue
            try:
                fs = _fsym(t.get("symbol"), t.get("expiry_date"),
                           float(t.get("strike_price") or 0), t.get("option_type"))
            except Exception:
                fs = ""
            if fs:
                m[fs] = int(t.get("id") or 0)
    except Exception:
        m = {}
    _SYM_MAP = m
    _SYM_MAP_TS = now
    return m


def _trade_by_id(tid):
    for t in _open_trades():
        try:
            if int(t.get("id") or 0) == int(tid):
                return t
        except Exception:
            continue
    # cache stale ho sakta hai (trade abhi close hua) - force refresh ek baar
    global _TRADES_CACHE
    _TRADES_CACHE = (0.0, [])
    for t in _open_trades():
        try:
            if int(t.get("id") or 0) == int(tid):
                return t
        except Exception:
            continue
    return None


def _on_message(message):
    """Fyers WS tick -> usi tick par SL/TP decision (koi candle close wait nahi)."""
    try:
        msgs = message if isinstance(message, list) else [message]
        for m in msgs:
            if not isinstance(m, dict):
                continue
            sym = str(m.get("symbol") or m.get("fySymbol") or "")
            if not sym:
                continue
            v = _node_ltp(m)
            if v <= 0:
                continue
            _WS["ticks"] = int(_WS.get("ticks") or 0) + 1
            _WS["last_tick"] = _ist_now_str()
            tid = _SYM_MAP.get(sym)
            if not tid:
                continue
            t = _trade_by_id(tid)
            if not t:
                continue
            kind, payload = _evaluate_and_exit(t, v, "broker")
            if kind == "closed" and payload:
                _WS["exits"] = int(_WS.get("exits") or 0) + 1
                _LAST_SCAN.setdefault("closed", []).append(payload)
                try:
                    from core.services.auto_paper import _beat
                    _beat("fast-exit-tick")
                except Exception:
                    pass
    except Exception as e:
        _WS["last_error"] = f"tick: {e}"[:160]


def _ws_on_connect():
    global _WS_SOCKET
    _WS["connected"] = True
    _WS["connected_at"] = _ist_now_str()
    _WS["last_error"] = ""
    try:
        syms = list(_refresh_symbol_map(force=True).keys())
        if syms and _WS_SOCKET is not None:
            _WS_SOCKET.subscribe(symbols=syms, data_type="SymbolUpdate")
            _WS["subscribed"] = syms
    except Exception as e:
        _WS["last_error"] = f"subscribe: {e}"[:160]


def _ws_on_close(*_args):
    _WS["connected"] = False
    _WS["subscribed"] = []


def _ws_on_error(err):
    _WS["last_error"] = str(err)[:160]


def _reconcile_subscriptions():
    """Naye khule contract subscribe, band ho gaye unsubscribe (throttled)."""
    global _LAST_RECONCILE
    now = _now_ts()
    if now - _LAST_RECONCILE < _SUB_RECONCILE_S:
        return
    _LAST_RECONCILE = now
    want = set(_refresh_symbol_map(force=True).keys())
    if not _WS.get("connected") or _WS_SOCKET is None:
        return
    have = set(_WS.get("subscribed") or [])
    add, rem = want - have, have - want
    try:
        if rem:
            _WS_SOCKET.unsubscribe(symbols=list(rem), data_type="SymbolUpdate")
        if add:
            _WS_SOCKET.subscribe(symbols=list(add), data_type="SymbolUpdate")
        _WS["subscribed"] = sorted(want)
    except Exception as e:
        _WS["last_error"] = f"reconcile: {e}"[:160]


def start_tick_stream():
    """True tick-by-tick exit stream (Fyers WebSocket), fail-open.

    WS unavailable/token missing -> 0.75s REST poll hi chalta rehta hai, isliye
    exits kabhi ruk nahi sakte."""
    global _WS_SOCKET, _WS_BACKOFF
    if not _WS.get("enabled", True):
        return False
    with _LOCK:
        if _WS.get("started"):
            return True
        _WS["started"] = True

    def _run():
        global _WS_SOCKET, _WS_BACKOFF
        while True:
            try:
                from core.services.contract_pricer import _fyers_auth
                app_id, token = _fyers_auth()
                if not app_id or not token:
                    _WS["connected"] = False
                    _WS["last_error"] = "broker token nahi (WS off, REST poll on)"
                    time.sleep(30)
                    continue
                from fyers_apiv3.FyersWebsocket.data_ws import FyersDataSocket
                sock = FyersDataSocket(
                    f"{app_id}:{token}",
                    litemode=True, write_to_file=False, reconnect=True,
                    on_connect=_ws_on_connect, on_message=_on_message,
                    on_error=_ws_on_error, on_close=_ws_on_close,
                )
                try:
                    sock.background_flag = True   # daemon thread (clean shutdown)
                except Exception:
                    pass
                _WS_SOCKET = sock
                _WS_BACKOFF = 5.0
                sock.connect()          # returns immediately, thread start karta hai
                # socket thread zinda hai jab tak - wait karo (busy loop nahi)
                for _ in range(24 * 3600):
                    th = getattr(sock, "ws_thread", None)
                    if th is None or not th.is_alive():
                        break
                    time.sleep(5)
            except Exception as e:
                _WS["last_error"] = f"ws: {e}"[:160]
            finally:
                _WS["connected"] = False
                _WS_SOCKET = None
            time.sleep(_WS_BACKOFF)
            _WS_BACKOFF = min(_WS_BACKOFF * 2, 120.0)

    try:
        th = threading.Thread(target=_run, name="fast-exit-ws", daemon=True)
        th.start()
        return True
    except Exception:
        with _LOCK:
            _WS["started"] = False
        return False


def ws_status():
    """Diagnostics for /api/version: WS zinda hai ya REST fallback."""
    try:
        return {
            "started": bool(_WS.get("started")),
            "connected": bool(_WS.get("connected")),
            "ticks": int(_WS.get("ticks") or 0),
            "tick_exits": int(_WS.get("exits") or 0),
            "subscribed": len(_WS.get("subscribed") or []),
            "last_tick": _WS.get("last_tick") or "",
            "connected_at": _WS.get("connected_at") or "",
            "last_error": _WS.get("last_error") or "",
            "rest_fallback_s": float(_POLL_S),
        }
    except Exception:
        return {"started": False, "connected": False}



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
    """Daemon threads: (1) true tick stream (WS), (2) REST poll safety net."""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return True
        _STARTED = True

    # True tick-by-tick (Fyers WebSocket) - har exchange tick par decision.
    # Fail-open: token/WS na ho to neeche wala REST poll hi chalta rehta hai.
    try:
        start_tick_stream()
    except Exception:
        pass

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
