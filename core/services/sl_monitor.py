"""In-process SL/TP monitor: closes open paper/live trades whose Stop-Loss or
Target is breached, using the same rupee-level math as the backtest engine.

Why in-process: the GitHub cron runs scripts/auto_trade.py against a fresh
empty checkout DB, so it NEVER sees production trades. This runs against the
app's real DB: (a) a daemon thread every 30s in market hours, (b) throttled
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


def run_once(min_interval=_MIN_INTERVAL, market_hours=True):
    """Scan + close breached trades. Returns {closed, skipped}."""
    global _LAST_RUN
    try:
        with _LOCK:
            if time.time() - _LAST_RUN < min_interval:
                return {"closed": [], "skipped": "throttled"}
            _LAST_RUN = time.time()
        if market_hours and not market_open_ist():
            return {"closed": [], "skipped": "market-closed"}
        fn = _check_fn()
        if not fn:
            return {"closed": [], "skipped": "no-check-fn"}
        closed = fn() or []
        # Arb pair stops (time/spread) ride along the same scan
        try:
            from core.services import arb_engine as _ae
            _arb = _ae.check_arb_stops() or []
            if _arb:
                closed = list(closed) + [{"id": None, "reason": "arb:" + str(a.get("reason", "")),
                                          "arb": a} for a in _arb]
        except Exception:
            pass
        return {"closed": closed, "skipped": ""}
    except Exception as e:
        return {"closed": [], "skipped": f"error: {e}"[:150]}


def start_background(interval_s=30):
    """Daemon thread: SL/TP scan every `interval_s` in market hours."""
    def _loop():
        while True:
            try:
                time.sleep(interval_s)
                r = run_once(market_hours=True)
                if r.get("closed"):
                    try:
                        print(f"[sl-monitor] auto-closed: {r['closed']}", flush=True)
                    except Exception:
                        pass
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
