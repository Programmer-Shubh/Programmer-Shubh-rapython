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
# Observable worker state (heartbeat for /auto-paper-status)
_STATE = {"threads": {}, "last_daily": {}, "last_live": {}}
# Bandwidth saver caches: history refetch was the top per-run eater
# (120d Yahoo fetch per strategy per worker run + per heartbeat).
_HIST_CACHE = {}  # (sym, start, end) -> (ts, hist); TTL 6h
_HIST_TTL = 6 * 3600
_INTRA_CACHE = {}  # sym -> (ts, bars); TTL 15 min (shared across strategies)
_INTRA_TTL = 900


def _beat(name):
    try:
        import datetime as _dt
        with _LOCK:
            _STATE["threads"][name] = _dt.datetime.utcnow().strftime("%H:%M:%S")
    except Exception:
        pass


def worker_state():
    try:
        from core.services.sl_monitor import market_open_ist as _mkt
        _open = bool(_mkt())
    except Exception:
        _open = None
    try:
        import datetime as _dt
        _now = (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        _now = ""
    with _LOCK:
        st = {"threads": dict(_STATE["threads"]),
              "last_daily": dict(_STATE["last_daily"]),
              "last_live": dict(_STATE["last_live"])}
    st["server_ist"] = _now
    st["market_open"] = _open
    return st


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
                r = place_for_strategy(s.get("id"))
                placed.extend(r.get("placed", []))
                notes.extend(r.get("notes", []))
            except Exception:
                continue
        return {"placed": placed, "skipped": "", "notes": notes[:10]}
    except Exception as e:
        return {"placed": [], "skipped": f"error: {e}"[:150]}


def _today_signal(sid, s, today):
    """True if the strategy's own indicators fire an entry dated TODAY on
    REAL history (backtest engine, same legs/config). Guards:
    - needs 30+ real DB bars (never signals on synthetic fallback);
    - 180s cap (worker must not hang).
    Returns (fires: bool, note: str)."""
    try:
        import json as _js
        import concurrent.futures as _cf
        sym = str(s.get("symbol") or "").upper()
        if not sym:
            return False, "no symbol"
        try:
            # Full fetch chain (DB -> nsepython -> Yahoo -> Stooq -> AV/TD),
            # NOT DB-only: production DB had 12 stale dates while Yahoo has
            # 1y - DB-only gate starved every strategy forever.
            # Bandwidth saver: day-cache (6h TTL) - same symbol fetched once,
            # not on every worker/heartbeat run.
            import datetime as _dt
            _end = _dt.datetime.strptime(today, "%Y-%m-%d").date()
            _start = (_end - _dt.timedelta(days=120)).strftime("%Y-%m-%d")
            _ck = (sym, _start, today)
            _ch = _HIST_CACHE.get(_ck)
            if _ch and time.time() - _ch[0] < _HIST_TTL:
                _hist = _ch[1]
            else:
                from core.services.historical_fetcher import fetch_historical
                _hist = fetch_historical(sym, _start, today)
                try:
                    _HIST_CACHE[_ck] = (time.time(), _hist)
                    if len(_HIST_CACHE) > 30:
                        _HIST_CACHE.pop(next(iter(_HIST_CACHE)))
                except Exception:
                    pass
            if not _hist or len(_hist) < 30:
                return False, "no real history (synthetic par signal nahi)"
        except Exception:
            return False, "history unreadable"
        try:
            legs = _js.loads(s.get("legs") or "[]")
        except Exception:
            legs = []
        if not legs:
            return False, "no legs"
        try:
            adv = _js.loads(s.get("advanced_options") or "{}")
        except Exception:
            adv = {}
        if not isinstance(adv, dict):
            adv = {}
        adv = dict(adv)
        adv.setdefault("trade_mode", "intraday")
        adv.setdefault("timeframe", "1d")
        try:
            risk = _js.loads(s.get("risk_management") or "{}")
        except Exception:
            risk = {}
        if not isinstance(risk, dict):
            risk = {}

        def _run():
            from routes.strategy_builder import _run_backtest_core, BacktestRequest
            req = BacktestRequest(
                symbol=sym, symbols=[sym],
                start_date=(min(h.get("trade_date", today) for h in _hist[-60:]) if _hist else today),
                end_date=today, indicators=[],
                legs=[], advanced={}, risk={})
            # Real saved config (indicators + legs), not blanks
            try:
                inds = _js.loads(s.get("indicators") or "[]")
            except Exception:
                inds = []
            req.indicators = inds if isinstance(inds, list) else []
            req.legs = legs if isinstance(legs, list) else []
            req.advanced = adv
            req.risk = risk
            return _run_backtest_core(req)

        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            try:
                res = _ex.submit(_run).result(timeout=180)
            except Exception as e:
                return False, f"engine timeout/fail: {e}"[:100]
        try:
            tl = ((res or {}).get("metrics") or {}).get("trade_list") or []
        except Exception:
            tl = []
        try:
            if any(str(t.get("entry_date", ""))[:10] == today for t in tl):
                return True, "aaj ka signal mila"
        except Exception:
            pass
        return False, "aaj koi signal nahi"
    except Exception as e:
        return False, f"signal-check fail: {e}"[:120]


def _intraday_signal(sid, s, today):
    """Live 15m-bar signal check: runs the strategy's own indicators on
    fresh intraday bars and returns the legs that fired TODAY.
    Returns (fired_legs_or_None, note). Never raises."""
    import json as _js
    try:
        sym = str(s.get("symbol") or "").upper()
        if not sym:
            return None, "no symbol"
        try:
            # Bandwidth saver: 15m bars shared across strategies, 15-min TTL
            # (was: fresh fetch per strategy per 5-min run).
            _tf0 = str((adv or {}).get("timeframe") or "15m").lower()
            if _tf0 not in ("1m", "5m", "15m", "30m", "1h"):
                _tf0 = "15m"
            _ick = (sym, _tf0)
            _ic = _INTRA_CACHE.get(_ick)
            if _ic and time.time() - _ic[0] < _INTRA_TTL:
                bars = _ic[1]
            else:
                from core.services.scanner import OptionScanner
                _tf = _tf0
                bars = OptionScanner()._fetch_intraday_bars(sym, _tf)
                try:
                    _INTRA_CACHE[_ick] = (time.time(), bars)
                    if len(_INTRA_CACHE) > 20:
                        _INTRA_CACHE.pop(next(iter(_INTRA_CACHE)))
                except Exception:
                    pass
        except Exception:
            bars = []
        if not bars or len(bars) < 40:
            return None, "no fresh 15m bars"
        # No forming-bar signals: aakhri (adhuri) candle hatao - backtest bhi
        # sirf closed bars par signal banata hai. Iske bina phantom-signal par
        # live entry hoti jo backtest me kabhi nahi hoti (divergence ka kaaran).
        try:
            bars = list(bars[:-1]) if len(bars) > 41 else list(bars)
        except Exception:
            pass
        if not bars or len(bars) < 40:
            return None, "no fresh 15m bars"
        try:
            legs = _js.loads(s.get("legs") or "[]")
        except Exception:
            legs = []
        if not legs:
            return None, "no legs"
        if any(str(l.get("option_type", "")).upper() == "AUTO" for l in legs):
            return None, "AUTO legs need manual direction"
        try:
            adv = _js.loads(s.get("advanced_options") or "{}")
        except Exception:
            adv = {}
        if not isinstance(adv, dict):
            adv = {}
        adv = dict(adv)
        # Saved config parity: backtest jis trade_mode/timeframe par chala,
        # paper bhi usi par (pehle hamesha intraday/15m force hota tha ->
        # positional-1d backtest vs intraday paper kabhi match nahi hota tha).
        # Sirf missing ho to intraday/15m default.
        _saved_mode = str(adv.get("trade_mode") or "").lower()
        if _saved_mode not in ("intraday", "positional", "btst"):
            adv["trade_mode"] = "intraday"
        _saved_tf = str(adv.get("timeframe") or "").lower()
        if _saved_tf not in ("1m", "5m", "15m", "30m", "1h", "1d"):
            adv["timeframe"] = "15m"
        try:
            risk = _js.loads(s.get("risk_management") or "{}")
        except Exception:
            risk = {}
        if not isinstance(risk, dict):
            risk = {}
        try:
            inds = _js.loads(s.get("indicators") or "[]")
        except Exception:
            inds = []

        def _run():
            from core.services.backtest_engine import BacktestEngine
            eng = BacktestEngine(is_live=False)
            return eng.run(bars[-120:], sym, today, today,
                           inds if isinstance(inds, list) else [],
                           [], [], legs if isinstance(legs, list) else [],
                           adv, risk, is_live=False)

        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            try:
                res = _ex.submit(_run).result(timeout=120)
            except Exception as e:
                return None, f"engine timeout/fail: {e}"[:100]
        try:
            tl = ((res or {}).get("metrics") or {}).get("trade_list") or []
        except Exception:
            tl = []
        fired = []
        try:
            for t in tl:
                if str(t.get("entry_date", ""))[:10] != today:
                    continue
                _o = str(t.get("option_type") or "").upper()
                _p = str(t.get("position") or "").upper()
                for leg in legs:
                    if (str(leg.get("option_type") or "").upper() == _o
                            and str(leg.get("transaction") or "").upper() == _p
                            and leg not in fired):
                        fired.append(leg)
                        break
        except Exception:
            pass
        if fired:
            return fired, f"{len(fired)} leg(s) fired on 15m"
        return None, "no 15m signal right now"
    except Exception as e:
        return None, f"signal-check fail: {e}"[:120]


def _execute_legs(s, legs, db, tm, today):
    """Shared leg executor: live ATM + place_trade per leg. Returns (placed, notes)."""
    import json as _js
    from core.services.live_market_data import LiveMarketData
    from utils.helpers import get_strike_step
    placed, notes = [], []
    sid = s.get("id")
    try:
        rm = _js.loads(s.get("risk_management") or "{}")
    except Exception:
        rm = {}
    sl = float(rm.get("daily_stop_loss") or 1500)
    tp = float(rm.get("daily_take_profit") or 3000)
    sym = str(s.get("symbol") or "").upper()
    if not sym:
        return placed, ["no symbol - skipped"]
    try:
        spot = LiveMarketData().get_spot_price(sym) or 0
    except Exception:
        spot = 0
    if spot <= 0:
        return placed, [f"{s.get('name')}: no live spot - skipped"]
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
                               "rolled_to": res.get("rolled_to", ""),
                               "strike_snapped": res.get("strike_snapped") or None})
            elif isinstance(res, dict) and res.get("error"):
                notes.append(f"{s.get('name')}: {str(res['error'])[:100]}")
                break
        except Exception as e:
            notes.append(f"{s.get('name')}: leg failed {e}"[:120])
            break
    return placed, notes


def place_for_strategy(sid, signal_check="daily"):
    """Place paper trades for ONE strategy now (used by worker + save hook).
    signal_check: 'daily' (backtest gate), 'intraday' (live 15m gate, only
    fired legs), 'done' (caller already gated). Same open/max-day guards, so
    Save is idempotent. Returns {placed, notes}."""
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
        s = db.fetch_one("SELECT * FROM strategies WHERE id=?", [sid])
        if not s:
            return {"placed": placed, "notes": ["strategy not found"]}
        # Paused/draft strategies stay stopped - only active ones trade.
        if str(s.get("status") or "") != "active":
            return {"placed": placed, "notes": [f"{s.get('name')}: status {s.get('status')} (paused) - skipped"]}
        try:
            rm0 = _js.loads(s.get("risk_management") or "{}")
        except Exception:
            rm0 = {}
        try:
            max_day = max(1, int((rm0 or {}).get("max_trades_per_day") or 3))
        except Exception:
            max_day = 3
        # Continuous running: re-enter while nothing open, up to max/day.
        # (SL/TP exit ke baad dobara lagana nahi padta - worker khud lagata hai.)
        try:
            has_open = db.fetch_one(
                "SELECT id FROM paper_trades WHERE strategy_id=? AND status='open' LIMIT 1", [sid])
            if has_open:
                return {"placed": placed, "notes": [f"{s.get('name')}: open position running - skipped"]}
            n_today = db.fetch_one(
                "SELECT COUNT(*) as c FROM paper_trades WHERE strategy_id=? AND entry_date=?", [sid, today])
            if n_today and int(n_today.get("c", 0) or 0) >= max_day:
                return {"placed": placed, "notes": [f"{s.get('name')}: max {max_day}/day reached - skipped"]}
        except Exception:
            pass
        try:
            legs = _js.loads(s.get("legs") or "[]")
        except Exception:
            legs = []
        if not legs:
            return {"placed": placed, "notes": [f"{s.get('name')}: no legs - skipped"]}
        if any(str(l.get("option_type", "")).upper() == "AUTO" for l in legs):
            return {"placed": placed, "notes": [f"{s.get('name')}: AUTO legs need manual direction - skipped"]}
        # Signal gate: 'daily' runs the backtest gate, 'intraday' runs the
        # live-15m gate (only fired legs), 'done' skips (caller gated).
        use_legs = legs
        if signal_check == "daily":
            try:
                _fires, _why = _today_signal(sid, s, today)
                if not _fires:
                    return {"placed": placed, "notes": [f"{s.get('name')}: {_why} - skipped"]}
            except Exception:
                pass
        elif signal_check == "intraday":
            try:
                _fired, _why = _intraday_signal(sid, s, today)
                if not _fired:
                    return {"placed": placed, "notes": [f"{s.get('name')}: {_why} - skipped"]}
                use_legs = _fired
            except Exception:
                pass
        _pl, _nt = _execute_legs(s, use_legs, db, tm, today)
        placed.extend(_pl)
        notes.extend(_nt)
    except Exception as e:
        notes.append(f"strategy {sid} failed: {e}"[:120])
    return {"placed": placed, "notes": notes}


def live_signal_pass(min_interval=240):
    """Background live evaluation (every ~5 min, market hours): each ACTIVE
    strategy's indicators are evaluated on FRESH 15m candles; fired legs
    place paper trades immediately (max/day + open guards hold)."""
    global _LAST_RUN
    try:
        from core.models.database import Database
        db = Database.get_instance()
        try:
            rows = db.fetch_all(
                "SELECT * FROM strategies WHERE status='active' ORDER BY updated_at DESC"
            )
        except Exception:
            return {"placed": [], "notes": ["no-strategies-table"]}
        placed, notes = [], []
        for s in rows or []:
            try:
                r = place_for_strategy(s.get("id"), signal_check="intraday")
                placed.extend(r.get("placed", []))
                notes.extend(r.get("notes", [])[:2])
            except Exception:
                continue
        return {"placed": placed, "notes": notes[:12]}
    except Exception as e:
        return {"placed": [], "notes": [f"error: {e}"[:150]]}


def dry_run_status():
    """No trades placed. Reports per ACTIVE strategy: due or skip-reason.
    Used to diagnose empty history without touching anything."""
    out = []
    try:
        import json as _js
        from core.models.database import Database
        db = Database.get_instance()
        today = _ist_today()
        try:
            rows = db.fetch_all(
                "SELECT id, name, symbol, status, end_date, legs FROM strategies ORDER BY updated_at DESC"
            )
        except Exception as e:
            return {"error": f"strategies unreadable: {e}"[:150], "today": today}
        for s in rows or []:
            try:
                sid = s.get("id")
                st = str(s.get("status") or "")
                if st != "active":
                    out.append({"id": sid, "name": s.get("name"), "decision": "skip",
                                "reason": f"status={st} (Auto Trade ON karo)"})
                    continue
                if str(s.get("status") or "") != "active":
                    out.append({"id": sid, "name": s.get("name"), "decision": "skip",
                                "reason": f"status={s.get('status')} (paused = stopped)"})
                    continue
                try:
                    legs = _js.loads(s.get("legs") or "[]")
                except Exception:
                    legs = []
                if not legs:
                    out.append({"id": sid, "name": s.get("name"), "decision": "skip",
                                "reason": "no legs saved"})
                    continue
                if any(str(l.get("option_type", "")).upper() == "AUTO" for l in legs):
                    out.append({"id": sid, "name": s.get("name"), "decision": "skip",
                                "reason": "AUTO legs need manual direction"})
                    continue
                try:
                    has_open = db.fetch_one(
                        "SELECT id FROM paper_trades WHERE strategy_id=? AND status='open' LIMIT 1", [sid])
                    if has_open:
                        out.append({"id": sid, "name": s.get("name"), "decision": "wait",
                                    "reason": f"open position #{has_open['id']} running"})
                        continue
                    try:
                        _rmd = _js.loads(s.get("risk_management") or "{}")
                        _mx = max(1, int((_rmd or {}).get("max_trades_per_day") or 3))
                    except Exception:
                        _mx = 3
                    _nt = db.fetch_one(
                        "SELECT COUNT(*) as c FROM paper_trades WHERE strategy_id=? AND entry_date=?", [sid, today])
                    if _nt and int(_nt.get("c", 0) or 0) >= _mx:
                        out.append({"id": sid, "name": s.get("name"), "decision": "wait",
                                    "reason": f"max {_mx}/day reached"})
                        continue
                except Exception as e:
                    out.append({"id": sid, "name": s.get("name"), "decision": "skip",
                                "reason": f"db check failed: {e}"[:120]})
                    continue
                out.append({"id": sid, "name": s.get("name"), "decision": "DUE",
                            "reason": f"{len(legs)} leg(s) will place on next worker run"})
            except Exception:
                continue
        return {"today": today, "strategies": out}
    except Exception as e:
        return {"error": str(e)[:150]}


def start_background(interval_s=600):
    """Daemon thread: auto paper-trade scan every `interval_s` (default 10min,
    so a closed position re-enters intraday without manual clicks)."""
    def _onescan():
        try:
            _beat("daily-worker")
            r = run_once()
            with _LOCK:
                _STATE["last_daily"] = {"placed": len(r.get("placed", [])),
                                        "notes": (r.get("notes", []) or [])[:3],
                                        "skipped": r.get("skipped", "")}
            if r.get("placed"):
                try:
                    print(f"[auto-paper] placed: {r['placed']}", flush=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _loop():
        _onescan()
        while True:
            try:
                time.sleep(interval_s)
                _onescan()
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


def start_live_background(interval_s=300):
    """Daemon thread: live 15m signal evaluation every `interval_s`."""
    def _onescan():
        try:
            _beat("live-worker")
            r = {"placed": [], "notes": []}
            try:
                from core.services.sl_monitor import market_open_ist as _mkt
                if _mkt():
                    r = live_signal_pass()
            except Exception:
                pass
            with _LOCK:
                _STATE["last_live"] = {"placed": len((r or {}).get("placed", [])),
                                       "notes": ((r or {}).get("notes", []) or [])[:3]}
            if (r or {}).get("placed"):
                try:
                    print(f"[live-signals] placed: {r['placed']}", flush=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _loop():
        try:
            time.sleep(60)
        except Exception:
            return
        _onescan()
        while True:
            try:
                time.sleep(interval_s)
                _onescan()
            except Exception:
                break
    try:
        th = threading.Thread(target=_loop, name="live-signals", daemon=True)
        th.start()
        return True
    except Exception:
        return False
