"""In-process SL/TP monitor: closes open paper/live trades whose Stop-Loss or
Target is breached, using the same rupee-level math as the backtest engine.

Why in-process: the GitHub cron runs scripts/auto_trade.py against a fresh
empty checkout DB, so it NEVER sees production trades. This runs against the
app's real DB: (a) a daemon thread every 120s in market hours, (b) throttled
inside the portfolio endpoints so an exit reflects the moment you look.

Throttled (min 20s between full scans, thread-safe): portfolio polls often,
scans are cheap (pricer caches 10s) and idempotent (only status='open').
"""
import importlib.util
import os
import threading
import time

_LAST_RUN = 0.0
_LOCK = threading.Lock()
_MIN_INTERVAL = 20


def _check_fn():
    """Load scripts/auto_trade.check_stoploss_target (single math source)."""
    try:
        base = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "auto_trade.py")
        base = os.path.abspath(base)
        spec = importlib.util.spec_from_file_location("ratrade_auto_trade", base)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.check_stoploss_target
    except Exception:
        return None


def market_open_ist():
    try:
        from datetime import datetime, timedelta, timezone
        ist = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist)
        if now.weekday() >= 5:
            return False
        mins = now.hour * 60 + now.minute
        return 9 * 60 <= mins <= 15 * 60 + 45
    except Exception:
        return True


ENTRY_CUTOFF_MIN = 15 * 60  # 3:00 PM IST - iske baad NAYI entry nahi


def entry_open_ist(_now=None):
    """Nayi ENTRY allowed? Market khula + 3:00 PM se pehle (user rule).
    3:00 PM ke baad KISI nayi short/sell position nahi khulti. Jo trades
    3:00 PM se PEHLE khule hain, wo 3:15 PM tak hold hote hain aur 3:15
    PM par square-off hote hain (close_intraday_trades).
    EXITS (close/SL/TP/strategy-exit/square-off) hamesha khule rehte hain -
    ye gate sirf naye orders rokta hai. _now: test hook (IST datetime)."""
    try:
        if not market_open_ist():
            return False
        from datetime import timedelta, timezone
        if _now is None:
            from datetime import datetime as _dt
            ist = timezone(timedelta(hours=5, minutes=30))
            _now = _dt.now(ist)
        mins = _now.hour * 60 + _now.minute
        return mins < ENTRY_CUTOFF_MIN
    except Exception:
        return False


def entry_block_reason():
    return ("3:00 PM ke baad nayi entry band hai - kal market khulne par order "
            "lagao (exit/SL/Target hamesha chalu rahenge, 3:15 PM tak square-off hota hai)")


def run_once(min_interval=_MIN_INTERVAL, market_hours=True):
    """Scan + close breached trades. Returns {closed, skipped, skipped_trades}.
    Strategy exit_time EOD exit market gate se PEHLE chalta hai (market band
    hone ke baad bhi - wahi kal ka 15:29-entry bug tha)."""
    global _LAST_RUN
    try:
        with _LOCK:
            if time.time() - _LAST_RUN < min_interval:
                return {"closed": [], "skipped": "throttled", "skipped_trades": []}
            _LAST_RUN = time.time()
        # (b) Strategy exit_time exit: market hours se independent (EOD exit
        # market band ke baad bhi hona chahiye).
        # (c) 15:15 intraday square-off: background thread se (page khole
        # bina) - positional KEVAL lage rahe hain. Dono gate se pehle.
        _strat_closed = []
        _eod_closed = []
        try:
            from core.models.trade_model import TradeModel as _TM
            _tm = _TM()
            _strat_closed = _tm.close_strategy_exit_time_trades() or []
            _eod_closed = _tm.close_intraday_trades() or []
        except Exception:
            pass
        _pre_closed = list(_strat_closed) + list(_eod_closed)
        if market_hours and not market_open_ist():
            return {"closed": _pre_closed,
                    "skipped": "market-closed" if not _pre_closed else "",
                    "skipped_trades": []}
        fn = _check_fn()
        if not fn:
            return {"closed": _pre_closed, "skipped": "no-check-fn",
                    "skipped_trades": []}
        res = fn() or {}
        if isinstance(res, dict):
            closed = list(res.get("closed", []) or [])
            _skipped_trades = list(res.get("skipped", []) or [])
        else:
            closed = list(res or [])
            _skipped_trades = []
        closed = _pre_closed + closed
        # Arb pair stops (time/spread) ride along the same scan
        try:
            from core.services import arb_engine as _ae
            _arb = _ae.check_arb_stops() or []
            if _arb:
                closed = list(closed) + [{"id": None, "reason": "arb:" + str(a.get("reason", "")),
                                          "arb": a} for a in _arb]
        except Exception:
            pass
        return {"closed": closed, "skipped": "", "skipped_trades": _skipped_trades}
    except Exception as e:
        return {"closed": [], "skipped": f"error: {e}"[:150]}


def start_background(interval_s=30):
    """Daemon thread: SL/TP scan every `interval_s` in market hours.
    First scan runs immediately on boot (no blind window after deploys)."""
    def _scan():
        try:
            from core.services.auto_paper import _beat
            _beat("sl-monitor")
        except Exception:
            pass
        try:
            r = run_once(market_hours=True)
            if r.get("closed"):
                try:
                    print(f"[sl-monitor] auto-closed: {r['closed']}", flush=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _loop():
        _scan()
        while True:
            try:
                time.sleep(interval_s)
                _scan()
            except Exception:
                try:
                    time.sleep(interval_s)
                except Exception:
                    break
    try:
        th = threading.Thread(target=_loop, name="sl-monitor", daemon=True)
        th.start()
        return True
    except Exception:
        return False
