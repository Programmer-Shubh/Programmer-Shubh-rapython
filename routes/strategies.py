from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List
from core.models.database import Database

router = APIRouter()


class StrategyRequest(BaseModel):
    id: Optional[int] = None
    name: str = "My Strategy"
    symbol: str = "BANKNIFTY"
    start_date: str = ""
    end_date: str = ""
    timeframe: str = "daily"
    description: str = ""
    indicators: list = []
    entry_conditions: list = []
    exit_conditions: list = []
    legs: list = []
    advanced_options: dict = {}
    risk_management: dict = {}
    status: str = "active"
    # Frontend apna abhi-chalaya backtest result bhej sakta hai taaki save ke
    # saath hi result bhi save ho - Backtest Detail phir dobara run nahi karega.
    last_backtest: Optional[dict] = None


def _norm_legs_for_hash(legs):
    """Hash/compare ke liye legs normalize (backtest endpoint jaisa)."""
    import copy as _cp
    out = []
    try:
        for _l in (_cp.deepcopy(legs) or []):
            if isinstance(_l, dict):
                _l.setdefault("lots", 1)
                _l.setdefault("expiry", "weekly")
                _l.setdefault("strike_selection", "atm")
                _l.setdefault("otm_distance", 0)
                out.append({k: _l.get(k) for k in
                            ("option_type", "transaction", "position", "lots",
                             "strike_selection", "otm_distance", "expiry",
                             "delta_target", "offset") if _l.get(k) is not None})
    except Exception:
        pass
    return out


def _canonical_config_hash(symbol, start_date, end_date, timeframe,
                           indicators, entry_conditions, exit_conditions,
                           legs, advanced, risk) -> str:
    """Saved-config ka stable hash (RAW dates - taaki repeat click par wahi
    saved result mile, roz naya run na ho). backtest endpoint + save dono
    yahi use karte hain."""
    import json as _js
    import hashlib as _hl
    try:
        adv = dict(advanced or {})
        if timeframe and not adv.get("timeframe"):
            adv["timeframe"] = timeframe
        try:
            from core.services.backtest_engine import ENGINE_VERSION as _ev
        except Exception:
            _ev = "v1"
        return _hl.md5(_js.dumps({
            "engine": _ev,
            "symbol": (symbol or "NIFTY"),
            "start_date": (start_date or "")[:10],
            "end_date": (end_date or "")[:10],
            "timeframe": (timeframe or ""),
            "indicators": indicators or [],
            "entry_conditions": entry_conditions or [],
            "exit_conditions": exit_conditions or [],
            "legs": _norm_legs_for_hash(legs),
            "advanced": adv,
            "risk": risk or {},
        }, sort_keys=True, default=str).encode()).hexdigest()
    except Exception:
        return ""


def _stored_engine_stale(stored: dict) -> bool:
    """Saved result PURANE engine version ka hai? Engine-version tag na ho
    (tagging se pehle ka purana saved result) ya current se alag -> stale.
    Buggy v2 ka 100%-win fake result aise hi pakda jaata hai."""
    try:
        from core.services.backtest_engine import ENGINE_VERSION as _ev
    except Exception:
        _ev = "v1"
    try:
        return str((stored or {}).get("engine_version") or "") != str(_ev)
    except Exception:
        return True


def _result_matches_config(res: dict, symbol, start_date, end_date,
                           indicators, legs, risk=None, advanced=None,
                           reasons: list = None) -> bool:
    """Frontend-bheja result kya isi saved config se bana hai? (params echo
    vs saved config). Mismatch par attach mat karo - galat result card par
    dikhega. Returns True only on clear match. reasons (list) di ho to har
    mismatch ki wajah usme (Hindi-English, user ko dikhane layak)."""
    def _why(t):
        try:
            if reasons is not None:
                reasons.append(str(t)[:160])
        except Exception:
            pass
    try:
        if not isinstance(res, dict):
            _why("result khaali hai - pehle Run Backtest karo")
            return False
        m = res.get("metrics") or {}
        if not isinstance(m, dict) or "total_trades" not in m:
            _why("result me metrics nahi - pehle Run Backtest karo")
            return False
        p = res.get("params") or {}
        if not isinstance(p, dict):
            _why("result me params-echo nahi (purana run?) - dobara Run Backtest karo")
            return False
        syms = p.get("symbols") or ([res.get("symbol")] if res.get("symbol") else [])
        if str(symbol or "").upper() not in [str(x or "").upper() for x in syms]:
            _why(f"symbol alag hai (run: {syms} vs save: {symbol}) - wahi symbol par run karke save karo")
            return False
        if str(p.get("start_date") or "")[:10] != str(start_date or "")[:10]:
            _why(f"start date alag hai (run: {str(p.get('start_date') or '')[:10]} vs save: {str(start_date or '')[:10]})")
            return False
        if str(p.get("end_date") or "")[:10] != str(end_date or "")[:10]:
            _why(f"end date alag hai (run: {str(p.get('end_date') or '')[:10]} vs save: {str(end_date or '')[:10]})")
            return False
        plegs = p.get("legs") or []
        if len(plegs) != len(legs or []):
            _why(f"legs ki ginti alag hai (run: {len(plegs)} vs save: {len(legs or [])})")
            return False
        for _pl, _sl in zip(plegs, legs or []):
            if not isinstance(_pl, dict) or not isinstance(_sl, dict):
                _why("leg ka format alag hai - dobara run karke save karo")
                return False
            if str(_pl.get("option_type") or "").upper() != str(_sl.get("option_type") or "").upper():
                _why("option_type (CE/PE) alag hai - run wali legs par save karo")
                return False
            _st = str(_sl.get("transaction") or _sl.get("position") or "").lower()
            if str(_pl.get("transaction") or "").lower() != _st:
                _why("buy/sell direction alag hai - run wali legs par save karo")
                return False
        pinds = set()
        for _iv in (p.get("indicators") or []):
            pinds.add(str(_iv or ""))
        sinds = set()
        for _iv in (indicators or []):
            sinds.add(str((_iv.get("id") if isinstance(_iv, dict) else _iv) or ""))
        if pinds != sinds:
            _why(f"indicators alag hain (run: {sorted(pinds)} vs save: {sorted(sinds)})")
            return False
        # Risk/settings bhi match hone chahiye (SL/TP/mode badla ho to purana
        # result attach mat karo). Saved risk/advanced vs result params echo.
        try:
            _rk = dict(risk or {})
            _ad = dict(advanced or {})

            def _f(x):
                try:
                    return float(x)
                except Exception:
                    return None

            _psl, _ssl = _f(p.get("sl")), _f(_rk.get("daily_stop_loss"))
            if _psl is not None and _ssl is not None and abs(_psl - _ssl) > 0.01:
                _why(f"stop-loss alag hai (run: {_psl} vs save: {_ssl})")
                return False
            _ptp, _stp = _f(p.get("tp")), _f(_rk.get("daily_take_profit"))
            if _ptp is not None and _stp is not None and abs(_ptp - _stp) > 0.01:
                _why(f"target alag hai (run: {_ptp} vs save: {_stp})")
                return False
            try:
                _pm = int(p.get("max_trades_per_day") or 0)
                _sm = int(_rk.get("max_trades_per_day") or 0)
                if _pm and _sm and _pm != _sm:
                    _why(f"max trades/day alag hai (run: {_pm} vs save: {_sm})")
                    return False
            except Exception:
                pass
            for _k in ("trade_mode", "timeframe", "entry_time", "exit_time"):
                _pv = str(p.get(_k) or "")
                _sv = str(_ad.get(_k) or "")
                if _pv and _sv and _pv != _sv:
                    _why(f"{_k} alag hai (run: {_pv} vs save: {_sv})")
                    return False
        except Exception:
            _why("risk/settings compare nahi hua - dobara run karke save karo")
            return False
        return True
    except Exception:
        _why("match-check crash - dobara run karke save karo")
        return False


_RESULTS_KEEP = 10


def _ensure_results_table():
    """strategy_results table pakki karo (purani DB files me init_schema ke
    baad bani table missing ho sakti hai) - har history/restore/archive se pehle."""
    try:
        from core.models.database import Database as _DB
        db = _DB.get_instance()
        try:
            _pg = db._is_postgres()
        except Exception:
            _pg = False
        if _pg:
            db.execute("CREATE TABLE IF NOT EXISTS strategy_results (id SERIAL PRIMARY KEY,"
                       " strategy_id INTEGER NOT NULL, run_id TEXT, config_hash TEXT,"
                       " stored_at TEXT, trades INTEGER DEFAULT 0,"
                       " net_pnl DOUBLE PRECISION DEFAULT 0, win_rate DOUBLE PRECISION DEFAULT 0,"
                       " result_json TEXT, created_at TIMESTAMPTZ DEFAULT NOW())")
            db.execute("CREATE INDEX IF NOT EXISTS idx_stratres_sid ON strategy_results(strategy_id)")
        else:
            db.execute("CREATE TABLE IF NOT EXISTS strategy_results (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                       " strategy_id INTEGER NOT NULL, run_id TEXT, config_hash TEXT,"
                       " stored_at TEXT, trades INTEGER DEFAULT 0,"
                       " net_pnl REAL DEFAULT 0, win_rate REAL DEFAULT 0,"
                       " result_json TEXT, created_at TEXT DEFAULT (datetime('now')))")
            db.execute("CREATE INDEX IF NOT EXISTS idx_stratres_sid ON strategy_results(strategy_id)")
    except Exception:
        pass


def _archive_result(strategy_id, res, config_hash="", stored_at=""):
    """Append-only version history (max 10/strategy): har save naya version,
    purana kabhi overwrite nahi hota. Same run_id dobara archive nahi."""
    import json as _js
    try:
        _ensure_results_table()
    except Exception:
        pass
    try:
        db = Database.get_instance()
        try:
            _rid = str((res or {}).get("run_id") or "")
            if _rid:
                _ex = db.fetch_one(
                    "SELECT id FROM strategy_results WHERE strategy_id=? AND run_id=? LIMIT 1",
                    [strategy_id, _rid])
                if _ex:
                    return int(_ex.get("id") or 0)
        except Exception:
            pass
        m = (res or {}).get("metrics") or {}
        _aid = db.execute(
            "INSERT INTO strategy_results (strategy_id, run_id, config_hash,"
            " stored_at, trades, net_pnl, win_rate, result_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [strategy_id, str((res or {}).get("run_id") or ""), str(config_hash or ""),
             str(stored_at or ""), int(m.get("total_trades") or 0),
             float(m.get("net_pnl") or 0), float(m.get("win_rate") or 0),
             _js.dumps(res, default=str)])
        try:
            _old = db.fetch_all(
                "SELECT id FROM strategy_results WHERE strategy_id=? ORDER BY id DESC",
                [strategy_id]) or []
            for _r in _old[_RESULTS_KEEP:]:
                try:
                    db.execute("DELETE FROM strategy_results WHERE id=?", [_r["id"]])
                except Exception:
                    pass
        except Exception:
            pass
        return int(_aid or 0)
    except Exception:
        return 0


def _bt_summary(row) -> dict:
    """List/cards ke liye halka summary (poora JSON nahi - list halki rahe)."""
    import json as _js
    try:
        raw = row.get("last_backtest") or ""
        if not raw:
            return {}
        _r = _js.loads(raw) if isinstance(raw, str) else raw
        _m = (_r or {}).get("metrics") or {}
        if not isinstance(_m, dict) or "total_trades" not in _m:
            return {}
        return {"trades": int(_m.get("total_trades") or 0),
                "win_rate": float(_m.get("win_rate") or 0),
                "net_pnl": float(_m.get("net_pnl") or 0),
                "run_id": (_r or {}).get("run_id") or "",
                # Cards ko batao saved result purane engine ka hai (red chip)
                "stale_engine": _stored_engine_stale(_r),
                "stored_at": row.get("last_backtest_at") or ""}
    except Exception:
        return {}


def _ist_today():
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")


def auto_rollover_expired():
    """Background self-healing: every ACTIVE strategy whose end_date passed
    is shifted to the next weekly expiry automatically - no button press
    needed, paper/live trading keeps running. Returns rolled count."""
    try:
        db = Database.get_instance()
        rows = db.fetch_all(
            "SELECT id, symbol, end_date FROM strategies WHERE status='active'"
        )
        today = _ist_today()
        n = 0
        for r in rows or []:
            try:
                end = str(r.get("end_date") or "")[:10]
                if not end or end >= today:
                    continue
                new_end = next_weekly_expiry(r.get("symbol") or "")
                if new_end and new_end >= today:
                    db.execute(
                        "UPDATE strategies SET end_date=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        [new_end, r["id"]],
                    )
                    n += 1
            except Exception:
                continue
        return n
    except Exception:
        return 0


def _apply_expiry_status(r):
    """Backend source of truth: end_date < today (IST) => display expired.

    Stored `status` is left untouched; frontend uses `display_status` /
    `expired` so a past end_date can never render as runnable Active."""
    try:
        end = str(r.get("end_date") or "")[:10]
        if end and end < _ist_today():
            r["expired"] = True
            r["display_status"] = "expired"
        else:
            r["expired"] = False
            r["display_status"] = r.get("status") or "active"
    except Exception:
        r["expired"] = False
        r["display_status"] = r.get("status") or "active"
    return r


@router.get("/list")
def list_strategies():
    db = Database.get_instance()
    try:
        auto_rollover_expired()  # self-heal on every view
    except Exception:
        pass
    try:
        # Piggyback auto-paper: Render free tier par background threads so
        # jate hain - user ke site kholte hi due trades apne-aap lagenge.
        # Daemon thread (response nahi rukta); run_once khud throttled
        # (20 min) + market-gated hai, to safe hai.
        import threading as _th

        def _ap_piggy():
            try:
                from core.services.auto_paper import run_once as _ap_run
                _ap_run()
            except Exception:
                pass

        _th.Thread(target=_ap_piggy, name="auto-paper-piggy", daemon=True).start()
    except Exception:
        pass
    rows = db.fetch_all(
        "SELECT * FROM strategies WHERE user_id=1 ORDER BY updated_at DESC"
    )
    for r in rows:
        import json
        r["indicators"] = json.loads(r.get("indicators") or "[]")
        r["legs"] = json.loads(r.get("legs") or "[]")
        r["entry_conditions"] = json.loads(r.get("entry_conditions") or "[]")
        r["exit_conditions"] = json.loads(r.get("exit_conditions") or "[]")
        r["advanced_options"] = json.loads(r.get("advanced_options") or "{}")
        r["risk_management"] = json.loads(r.get("risk_management") or "{}")
        # Saved backtest: poora JSON list me nahi (payload bhari), sirf
        # summary - card par result pehle se dikhega, dobara run nahi.
        try:
            r["bt"] = _bt_summary(r)
            for _drop in ("last_backtest", "last_backtest_hash"):
                try:
                    r.pop(_drop, None)
                except Exception:
                    pass
        except Exception:
            pass
        _apply_expiry_status(r)
        r["live_enabled"] = _strat_live_enabled(db, r.get("id"))
    return {"strategies": rows, "count": len(rows)}


@router.get("/{strat_id}")
def get_strategy(strat_id: int):
    db = Database.get_instance()
    row = db.fetch_one("SELECT * FROM strategies WHERE id=?", [strat_id])
    if not row:
        return {"error": "Strategy not found"}
    import json
    row["indicators"] = json.loads(row.get("indicators") or "[]")
    row["legs"] = json.loads(row.get("legs") or "[]")
    row["entry_conditions"] = json.loads(row.get("entry_conditions") or "[]")
    row["exit_conditions"] = json.loads(row.get("exit_conditions") or "[]")
    row["advanced_options"] = json.loads(row.get("advanced_options") or "{}")
    row["risk_management"] = json.loads(row.get("risk_management") or "{}")
    try:
        row["bt"] = _bt_summary(row)
        for _drop in ("last_backtest", "last_backtest_hash"):
            try:
                row.pop(_drop, None)
            except Exception:
                pass
    except Exception:
        pass
    _apply_expiry_status(row)
    row["live_enabled"] = _strat_live_enabled(db, strat_id)
    return row


@router.post("/save")
def save_strategy(req: StrategyRequest):
    import json
    db = Database.get_instance()
    data = {
        "name": req.name,
        "symbol": req.symbol,
        "start_date": req.start_date,
        "end_date": req.end_date,
        "timeframe": req.timeframe,
        "description": req.description,
        "indicators": json.dumps(req.indicators),
        "entry_conditions": json.dumps(req.entry_conditions),
        "exit_conditions": json.dumps(req.exit_conditions),
        "legs": json.dumps(req.legs),
        "advanced_options": json.dumps(req.advanced_options),
        "risk_management": json.dumps(req.risk_management),
        "status": req.status,
    }
    # Saved-result attach: frontend ne abhi-chalaya result bheja ho AUR wo
    # isi config se bana ho to save ke saath result bhi save (Backtest Detail
    # phir dobara run nahi karega).
    # NOTE (user demand): config badalne par purana saved result SAAF NAHI
    # hota - delete tak card par dikhega (kisi bhi computer par, DB me hai).
    # Backtest Detail nayi config par khud fresh run karke overwrite kar dega.
    try:
        _new_hash = _canonical_config_hash(
            req.symbol, req.start_date, req.end_date, req.timeframe,
            req.indicators, req.entry_conditions, req.exit_conditions,
            req.legs, req.advanced_options, req.risk_management)
    except Exception:
        _new_hash = ""
    _attach_res = None
    _attach_reasons = []
    try:
        if isinstance(req.last_backtest, dict) and _result_matches_config(
                req.last_backtest, req.symbol, req.start_date, req.end_date,
                req.indicators, req.legs, req.risk_management,
                req.advanced_options, reasons=_attach_reasons):
            _attach_res = req.last_backtest
    except Exception:
        pass
    try:
        from datetime import datetime as _dtn, timedelta as _tdn, timezone as _tzn
        _now_s = _dtn.now(_tzn(_tdn(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M")
    except Exception:
        _now_s = ""
    if _attach_res is not None:
        try:
            data["last_backtest"] = json.dumps(_attach_res, default=str)
            data["last_backtest_hash"] = _new_hash
            data["last_backtest_at"] = _now_s
        except Exception:
            pass
    if req.id:
        sets = ", ".join(f"{k}=?" for k in data)
        vals = list(data.values()) + [req.id]
        db.execute(f"UPDATE strategies SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?", vals)
        saved_id = req.id
        saved_status = "updated"
    else:
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        vals = [1] + list(data.values())
        row_id = db.execute(
            f"INSERT INTO strategies (user_id, {cols}) VALUES (?, {placeholders})",
            vals,
        )
        saved_id = row_id
        saved_status = "created"
    # Save => paper trade lagao (active status par hi; paused rukega).
    # Open/max-day/signal guards keep it idempotent; market-closed saves
    # wait for the worker.
    auto_trades = []
    try:
        if (req.status or "") == "active" and (req.legs or []):
            from core.services.sl_monitor import market_open_ist as _mkt
            if _mkt():
                from core.services.auto_paper import place_for_strategy as _pl
                auto_trades = (_pl(saved_id) or {}).get("placed", [])
    except Exception:
        pass
    resp = {"id": saved_id, "status": saved_status}
    if auto_trades:
        resp["auto_trades"] = auto_trades
    # Version history: attach hua result archive karo (purana kabhi na mite).
    try:
        if _attach_res is not None:
            _archive_result(saved_id, _attach_res, _new_hash, _now_s)
    except Exception:
        pass
    # Honest attach status: result juda ya nahi + wajah (frontend jhootha
    # "saved" na bole - mismatch par exact kaaran dikhe).
    try:
        resp["result_saved"] = bool(_attach_res is not None)
        if _attach_res is not None:
            try:
                resp["result_trades"] = int((_attach_res.get("metrics") or {}).get("total_trades") or 0)
            except Exception:
                resp["result_trades"] = 0
        else:
            resp["result_skip_reason"] = "; ".join(_attach_reasons[:2]) or "result match nahi hua"
    except Exception:
        pass
    return resp


def _strat_live_key(strat_id: int) -> str:
    return f"strat_live_enabled_{int(strat_id)}"


def _strat_live_enabled(db, strat_id: int) -> bool:
    """Live profile: sirf EXPLICIT '1' par. Pehle missing-row/crash par
    True (open) wapas aata tha - nahi chhune wali strategy bhi Live-ON
    maani jaati thi (galat orders + GHWAT LIVE tag). Ab fail-CLOSED: OFF."""
    try:
        row = db.fetch_one(
            "SELECT setting_value FROM settings WHERE setting_key=?",
            [_strat_live_key(strat_id)])
        if not row:
            return False
        return str(row.get("setting_value") or "") == "1"
    except Exception:
        return False


class LotsUpdateRequest(BaseModel):
    lots: int


class LiveToggleRequest(BaseModel):
    enabled: bool


@router.post("/{strat_id}/lots")
def update_strategy_lots(strat_id: int, req: LotsUpdateRequest):
    """Saved strategy ke saare legs ka lot size badlo (+/- buttons isse use
    karte hain). DB me legs JSON update hoti hai - live + paper trade dono
    isi se quantity nikaalte hain."""
    import json as _js
    db = Database.get_instance()
    row = db.fetch_one("SELECT * FROM strategies WHERE id=?", [strat_id])
    if not row:
        return {"error": "Strategy not found"}
    try:
        legs = _js.loads(row.get("legs") or "[]")
    except Exception:
        return {"error": "Legs data galat hai"}
    if not legs:
        return {"error": "No legs in this strategy"}
    lots = int(req.lots)
    if lots < 1:
        return {"error": "Lot size minimum 1 hona chahiye"}
    if lots > 200:
        return {"error": "Lot size maximum 200 tak hi ja sakta hai"}
    for leg in legs:
        if isinstance(leg, dict):
            leg["lots"] = lots
    db.execute(
        "UPDATE strategies SET legs=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        [_js.dumps(legs), strat_id])
    return {"status": "updated", "id": strat_id, "lots": lots}


@router.get("/{strat_id}/live-state")
def strategy_live_state(strat_id: int):
    db = Database.get_instance()
    row = db.fetch_one("SELECT id FROM strategies WHERE id=?", [strat_id])
    if not row:
        return {"error": "Strategy not found"}
    return {"id": strat_id, "live_enabled": _strat_live_enabled(db, strat_id)}


@router.post("/{strat_id}/live-toggle")
def strategy_live_toggle(strat_id: int, req: LiveToggleRequest):
    """Per-strategy LIVE trade ON/OFF. OFF par is strategy ka live order
    place nahi hoga (frontend + guard dono check karte hain)."""
    db = Database.get_instance()
    row = db.fetch_one("SELECT id FROM strategies WHERE id=?", [strat_id])
    if not row:
        return {"error": "Strategy not found"}
    key = _strat_live_key(strat_id)
    val = "1" if req.enabled else "0"
    existing = db.fetch_one("SELECT setting_key FROM settings WHERE setting_key=?", [key])
    if existing:
        db.execute("UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key=?",
                   [val, key])
    else:
        db.execute("INSERT INTO settings (setting_key, setting_value) VALUES (?, ?)", [key, val])
    return {"status": "ok", "id": strat_id, "live_enabled": req.enabled}


@router.post("/{strat_id}/paper-toggle")
def strategy_paper_toggle(strat_id: int, req: LiveToggleRequest):
    """Per-strategy Paper trading ON/OFF. Auto-paper worker sirf
    status='active' strategies par trade lagata hai, isliye:
    ON  -> status='active'  (auto-paper worker chal sakta hai)
    OFF -> status='paused'  (koi naya auto/paper-trade nahi lagega;
           open positions chalte rahenge)."""
    db = Database.get_instance()
    row = db.fetch_one("SELECT id FROM strategies WHERE id=?", [strat_id])
    if not row:
        return {"error": "Strategy not found"}
    new_status = "active" if req.enabled else "paused"
    db.execute(
        "UPDATE strategies SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        [new_status, strat_id])
    return {"status": "ok", "id": strat_id,
            "paper_enabled": req.enabled, "strategy_status": new_status}


@router.get("/{strat_id}/paper-state")
def strategy_paper_state(strat_id: int):
    db = Database.get_instance()
    row = db.fetch_one("SELECT id, status FROM strategies WHERE id=?", [strat_id])
    if not row:
        return {"error": "Strategy not found"}
    st = str((row or {}).get("status") or "active")
    return {"id": strat_id, "paper_enabled": st == "active", "status": st}


@router.delete("/{strat_id}")
def delete_strategy(strat_id: int):
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE id=?", [strat_id])
    return {"status": "deleted", "id": strat_id}


@router.post("/purge-symbol")
def purge_symbol(body: dict):
    """Ek symbol ka POORA data saaf karo: strategies + unke paper trades +
    unke saved results. Pehle dry-run (confirm missing/false) me sirf gin
    kar batata hai; confirm=true par delete + audit-log. MIDCPNIFTY jaise
    hataye gaye symbols ke purane rows ke liye."""
    import json as _js
    try:
        sym = str((body or {}).get("symbol") or "").strip().upper()
        if not sym:
            return {"error": "symbol do (e.g. {\"symbol\": \"MIDCPNIFTY\"})"}
        try:
            _ensure_results_table()
        except Exception:
            pass
        db = Database.get_instance()
        _sids = [int(r.get("id") or 0) for r in
                 (db.fetch_all("SELECT id FROM strategies WHERE symbol=?", [sym]) or [])]
        _open = db.fetch_one(
            "SELECT COUNT(*) c FROM paper_trades WHERE symbol=? AND status='open'", [sym])
        _closed = db.fetch_one(
            "SELECT COUNT(*) c FROM paper_trades WHERE symbol=? AND status='closed'", [sym])
        _res = db.fetch_one(
            "SELECT COUNT(*) c FROM strategy_results WHERE strategy_id IN "
            "(SELECT id FROM strategies WHERE symbol=?)", [sym])
        _info = {"symbol": sym, "strategies": len(_sids),
                 "open_trades": int((_open or {}).get("c") or 0),
                 "closed_trades": int((_closed or {}).get("c") or 0),
                 "saved_results": int((_res or {}).get("c") or 0)}
        if not body.get("confirm"):
            _info["deleted"] = False
            _info["hint"] = "Delete ke liye confirm=true bhejo"
            return _info
        _del = {"strategies": 0, "trades": 0, "results": 0}
        try:
            from utils.helpers import log_trade_delete as _ltd
        except Exception:
            _ltd = None
        for _tid in [int(r.get("id") or 0) for r in
                     (db.fetch_all("SELECT id FROM paper_trades WHERE symbol=?", [sym]) or [])]:
            try:
                if _ltd:
                    _row = db.fetch_one("SELECT * FROM paper_trades WHERE id=?", [_tid])
                    if _row:
                        _ltd(dict(_row), "purge-symbol:" + sym)
                db.execute("DELETE FROM paper_trades WHERE id=?", [_tid])
                _del["trades"] += 1
            except Exception:
                continue
        for _sid in _sids:
            try:
                try:
                    _rc = db.fetch_one(
                        "SELECT COUNT(*) c FROM strategy_results WHERE strategy_id=?", [_sid])
                    _rn = int((_rc or {}).get("c") or 0)
                    db.execute("DELETE FROM strategy_results WHERE strategy_id=?", [_sid])
                    _del["results"] += _rn
                except Exception:
                    pass
                db.execute("DELETE FROM strategies WHERE id=?", [_sid])
                _del["strategies"] += 1
            except Exception:
                continue
        _info["deleted"] = True
        _info["removed"] = _del
        return _info
    except Exception as e:
        return {"error": str(e)[:200]}


def _norm_exp(exp: str) -> str:
    """Normalize expiry to YYYY-MM-DD (NSE gives '29-Sep-2026')."""
    exp = str(exp or "").strip()
    if len(exp) == 10 and exp[4] == "-" and exp[7] == "-":
        return exp[:10]
    try:
        from datetime import datetime as _dt
        for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                return _dt.strptime(exp[:11], fmt).strftime("%Y-%m-%d")
            except Exception:
                continue
    except Exception:
        pass
    return ""


def next_weekly_expiry(symbol: str = "") -> str:
    """Next live weekly expiry (NSE when reachable, else today+7 IST)."""
    try:
        from core.services.contract_pricer import _nse_map
        exp, _m = _nse_map((symbol or "").upper())
        exp = _norm_exp(exp)
        if exp and exp >= _ist_today():
            return exp
    except Exception:
        pass
    try:
        from datetime import datetime as _dt, timedelta as _td
        return (_dt.strptime(_ist_today(), "%Y-%m-%d") + _td(days=7)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def rollover_strategy(strat_id: int):
    """Shift an expired strategy to the next weekly expiry so paper/live
    trading keeps running. Returns new end_date or ''."""
    try:
        db = Database.get_instance()
        row = db.fetch_one("SELECT symbol, end_date FROM strategies WHERE id=?", [strat_id])
        if not row:
            return ""
        new_end = next_weekly_expiry(row.get("symbol") or "")
        if not new_end:
            return ""
        db.execute(
            "UPDATE strategies SET end_date=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [new_end, strat_id],
        )
        return new_end
    except Exception:
        return ""


@router.get("/{strat_id}/trades")
def strategy_trades(strat_id: int, unlinked: int = 0):
    """Paper-trade detail for a saved strategy: open positions (live LTP +
    unrealized P&L) + closed trade history. Strictly strategy-wise:
    only trades with this strategy_id are counted in open/closed."""
    try:
        from core.models.trade_model import TradeModel
        tm = TradeModel()
        # Strategy header so modal can show name/symbol even if no trades
        strat_info = {}
        try:
            srow = tm.db.fetch_one(
                "SELECT id, name, symbol FROM strategies WHERE id=?", [strat_id]
            )
            if srow:
                strat_info = {
                    "id": srow.get("id"), "name": srow.get("name"),
                    "symbol": srow.get("symbol"),
                }
        except Exception:
            pass
        opens = []
        try:
            for p in tm.get_open_positions_with_pnl():
                t = p.get("trade", {}) or {}
                try:
                    if int(t.get("strategy_id") or 0) != int(strat_id):
                        continue
                except Exception:
                    continue
                opens.append({
                    "id": t.get("id"), "symbol": t.get("symbol"),
                    "transaction_type": t.get("transaction_type"),
                    "option_type": t.get("option_type"),
                    "strike": t.get("strike_price"),
                    "expiry_date": t.get("expiry_date"),
                    "quantity": t.get("quantity"),
                    "entry_price": t.get("entry_price"),
                    "current_price": p.get("current_price"),
                    "unrealized_pnl": p.get("unrealized_pnl"),
                    "unrealized_net": p.get("unrealized_net", p.get("unrealized_pnl")),
                    "est_exit_costs": p.get("est_exit_costs", 0),
                    "trade_mode": t.get("trade_mode"),
                    "status": t.get("status"),
                    "entry_date": t.get("entry_date", ""),
                    "entry_time": TradeModel.ist_hhmm(t.get("created_at", "")),
                })
        except Exception:
            pass
        closed = []
        try:
            # COALESCE: old rows with NULL strategy_id never leak into a strategy
            rows = tm.db.fetch_all(
                "SELECT * FROM paper_trades WHERE COALESCE(strategy_id,0)=? AND status<>'open' ORDER BY id DESC LIMIT 50",
                [int(strat_id)],
            )
            for t in rows or []:
                closed.append({
                    "id": t.get("id"), "symbol": t.get("symbol"),
                    "transaction_type": t.get("transaction_type"),
                    "option_type": t.get("option_type"),
                    "strike": t.get("strike_price"),
                    "entry_price": t.get("entry_price"),
                    "exit_price": t.get("exit_price"),
                    "pnl": t.get("pnl"),
                    "exit_reason": t.get("exit_status") or t.get("exit_reason"),
                    "trade_mode": t.get("trade_mode"),
                    "entry_date": t.get("entry_date", ""),
                    "entry_time": TradeModel.ist_hhmm(t.get("created_at", "")),
                    "exit_date": t.get("exit_date", ""),
                    "exit_time": TradeModel.ist_hhmm(t.get("updated_at", "")),
                })
        except Exception:
            pass
        try:
            open_pnl = round(sum(float(o.get("unrealized_pnl") or 0) for o in opens), 2)
        except Exception:
            open_pnl = 0
        try:
            closed_pnl = round(sum(float(c.get("pnl") or 0) for c in closed), 2)
        except Exception:
            closed_pnl = 0
        # Unlinked manual trades (strategy_id=0/NULL): shown separately so history
        # is never "missing" - honestly labeled, never attributed.
        manual = []
        if unlinked:
            try:
                mrows = tm.db.fetch_all(
                    "SELECT * FROM paper_trades WHERE COALESCE(strategy_id,0)=0 ORDER BY id DESC LIMIT 20",
                )
                for t in mrows or []:
                    manual.append({
                        "id": t.get("id"), "symbol": t.get("symbol"),
                        "transaction_type": t.get("transaction_type"),
                        "option_type": t.get("option_type"),
                        "strike": t.get("strike_price"),
                        "entry_price": t.get("entry_price"),
                        "exit_price": t.get("exit_price"),
                        "pnl": t.get("pnl"),
                        "status": t.get("status"),
                        "exit_reason": t.get("exit_status"),
                        "entry_date": t.get("entry_date", ""),
                        "entry_time": TradeModel.ist_hhmm(t.get("created_at", "")),
                        "exit_date": t.get("exit_date", ""),
                        "exit_time": TradeModel.ist_hhmm(t.get("updated_at", "")),
                    })
            except Exception:
                pass
        return {"strategy_id": strat_id, "strategy": strat_info,
                "open": opens, "closed": closed,
                "open_count": len(opens), "closed_count": len(closed),
                "open_pnl": open_pnl, "closed_pnl": closed_pnl,
                "total_pnl": round(open_pnl + closed_pnl, 2),
                "manual": manual, "manual_count": len(manual)}
    except Exception as e:
        return {"error": f"Detail failed: {e}"}


@router.post("/{strat_id}/rollover")
def rollover_endpoint(strat_id: int):
    new_end = rollover_strategy(strat_id)
    if not new_end:
        return {"error": "Rollover failed"}
    return {"strategy_id": strat_id, "end_date": new_end, "status": "rolled"}


@router.post("/{strat_id}/backtest")
def strategy_backtest(strat_id: int, refresh: int = 0):
    """Saved strategy ka backtest EXACT saved config par - form round-trip
    me kho jaane wale fields (entry/exit conditions, lots, mtd, checkbox
    indicators) se zero-metrics aata tha. Ye endpoint DB se sidha
    BacktestRequest banakar chalata hai.

    Wahi-result guarantee: pehli run ka poora result strategy ke saath DB me
    save hota hai (last_backtest). Config same rahe to dobara click par WAHI
    saved result turant milta hai (same run_id, same numbers) - naya run nahi.
    Config badli ho ya ?refresh=1 ho to naya run + save."""
    import json as _js
    try:
        db = Database.get_instance()
        row = db.fetch_one("SELECT * FROM strategies WHERE id=?", [strat_id])
        if not row:
            return {"error": "Strategy not found"}

        def _ld(v, fb):
            try:
                if v is None or v == "":
                    return fb
                return _js.loads(v) if isinstance(v, str) else (v or fb)
            except Exception:
                return fb

        legs = _ld(row.get("legs"), [])
        indicators = _ld(row.get("indicators"), [])
        entry_conditions = _ld(row.get("entry_conditions"), [])
        exit_conditions = _ld(row.get("exit_conditions"), [])
        advanced = _ld(row.get("advanced_options"), {})
        risk = _ld(row.get("risk_management"), {})
        # Top-level timeframe bhi advanced me mirror karo (form sirf
        # advanced.timeframe padhta tha, purani rows me top-level tha)
        try:
            if row.get("timeframe") and not advanced.get("timeframe"):
                advanced["timeframe"] = row.get("timeframe")
        except Exception:
            pass
        # Saved legs me lots/expiry missing ho to sane defaults
        try:
            for _l in (legs or []):
                if isinstance(_l, dict):
                    _l.setdefault("lots", 1)
                    _l.setdefault("expiry", "weekly")
                    _l.setdefault("strike_selection", "atm")
                    _l.setdefault("otm_distance", 0)
        except Exception:
            pass
        if not legs:
            return {"error": "Is strategy me koi leg nahi - pehle leg add karke save karo"}
        from routes.strategy_builder import BacktestRequest, _run_backtest_core
        # Empty dates (purani rows) -> last 60 days IST (stale hardcoded
        # 2026-08 default par INSUFFICIENT aata tha, button "dead" lagta tha).
        _sd = (row.get("start_date") or "")[:10]
        _ed = (row.get("end_date") or "")[:10]
        if not _sd or not _ed:
            try:
                from datetime import datetime as _dti, timedelta as _tdi, timezone as _tzi
                _ist = _tzi(_tdi(hours=5, minutes=30))
                _now = _dti.now(_ist).date()
                _ed = _ed or _now.strftime("%Y-%m-%d")
                _sd = _sd or (_now - _tdi(days=60)).strftime("%Y-%m-%d")
            except Exception:
                _sd = _sd or "2026-08-01"
                _ed = _ed or "2026-08-20"
        req = BacktestRequest(
            symbol=(row.get("symbol") or "NIFTY"),
            symbols=[],
            start_date=_sd,
            end_date=_ed,
            indicators=indicators or [],
            entry_conditions=entry_conditions or [],
            exit_conditions=exit_conditions or [],
            legs=legs,
            advanced=advanced or {},
            risk=risk or {},
        )
        # Config hash (RAW dates - stable: repeat click par wahi saved result,
        # roz naya run na ho. Save-time hash bhi raw dates par hai).
        _cfg_hash = _canonical_config_hash(
            row.get("symbol"), row.get("start_date"), row.get("end_date"),
            row.get("timeframe"), indicators, entry_conditions,
            exit_conditions, legs, advanced, risk)
        if not refresh and row.get("last_backtest"):
            # Backtest Detail (refresh=0): SAVED result TURANT wapas - chahe
            # config badli ho ya engine naya ho. Pehle hash-mismatch par
            # chupke se FULL fresh run hota tha - 45s client timeout + busy
            # errors + "saved result nahi dikhta" shikayat. Ab: badge ke saath
            # saved history hi dikhao; naya run sirf ↻ Naya run (refresh=1)
            # par. Purane engine ka result red badge me saaf bata do.
            try:
                _stored = _js.loads(row["last_backtest"]) if isinstance(row["last_backtest"], str) else dict(row["last_backtest"])
                if isinstance(_stored, dict) and (_stored.get("metrics") or _stored.get("trade_list") is not None):
                    _stored["stored"] = True
                    _stored["stored_at"] = row.get("last_backtest_at") or ""
                    _stored["strategy_id"] = strat_id
                    _stored["strategy_name"] = row.get("name") or ""
                    _stored["config_changed"] = bool(
                        _cfg_hash and str(row.get("last_backtest_hash") or "") != _cfg_hash)
                    _stored["stale_engine"] = _stored_engine_stale(_stored)
                    try:
                        _stored["took_ms"] = 5
                        _stored["cached"] = True
                    except Exception:
                        pass
                    return _stored
            except Exception:
                pass
        res = _run_backtest_core(req)
        try:
            res["strategy_id"] = strat_id
            res["strategy_name"] = row.get("name") or ""
            res["stored"] = False
            # Save-time engine tag (agar result me na ho - kisi aur path se
            # banas result ho): warna agli baar stale-mana jaayega.
            try:
                from core.services.backtest_engine import ENGINE_VERSION as _ev_save
            except Exception:
                _ev_save = "v1"
            res.setdefault("engine_version", _ev_save)
        except Exception:
            pass
        # Naya run save karo taaki agli baar WAHI result mile (error save nahi).
        # GUARD: 0-trade fresh run saved non-zero history NAHI mitata.
        _kept = False
        if isinstance(res, dict) and not res.get("error") and (res.get("metrics") or res.get("trade_list") is not None):
            try:
                if not _should_overwrite_saved(row.get("last_backtest"), res):
                    _kept = True
                    # Fresh 0-trade ne SAVED non-zero history ko overwrite
                    # nahi kiya - CLIENT ko bhi WAHI saved result bhejo
                    # (warna frontend me saved-result render hokar phir
                    # 0-trade fresh se overwrite ho jaata tha - "detail
                    # nahi dikha raha" bug).
                    try:
                        _sv_raw = row.get("last_backtest") or ""
                        _sv = _js.loads(_sv_raw) if isinstance(_sv_raw, str) else _sv_raw
                        if isinstance(_sv, dict) and (_sv.get("metrics") or _sv.get("trade_list") is not None):
                            _sv["stored"] = True
                            _sv["stored_at"] = row.get("last_backtest_at") or ""
                            _sv["strategy_id"] = strat_id
                            _sv["strategy_name"] = row.get("name") or ""
                            _sv["kept_saved"] = True
                            _sv["config_changed"] = True
                            return _sv
                    except Exception:
                        pass
                else:
                    from datetime import datetime as _dtn, timedelta as _tdn, timezone as _tzn
                    _now_s = (_dtn.now(_tzn(_tdn(hours=5, minutes=30)))).strftime("%Y-%m-%d %H:%M")
                    db.execute(
                        "UPDATE strategies SET last_backtest=?, last_backtest_hash=?, last_backtest_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        [_js.dumps(res, default=str), _cfg_hash, _now_s, strat_id],
                    )
                    res["stored_at"] = _now_s
                    try:
                        _archive_result(strat_id, res, _cfg_hash, _now_s)
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            if _kept:
                res["kept_saved"] = True
        except Exception:
            pass
        return res
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": f"Backtest failed: {e}"[:250]}


def _should_overwrite_saved(stored_raw, fresh_res) -> bool:
    """Fresh run saved result ko mitaye ya nahi? 0-trade fresh run kabhi
    non-zero saved history NAHI mitata (ce-sell-ppp ke 188 trades ek 0-trade
    run ne 2026-10-14 ko uda diye the - wahi bug) - LEKIN agar stored result
    PURANE engine version ka hai (staleness tag na ho ya alag ho) to 0-trade
    fresh bhi usse mitata hai: buggy v2 jaisi 100%-win fake history ko
    hamesha serve karna galat hai (naye engine ka honest 0-trade = data
    fetch fail tha tab bhi sahi correction). Baaki sab pehle jaisa."""
    try:
        try:
            from core.services.backtest_engine import ENGINE_VERSION as _ev
        except Exception:
            _ev = "v1"
        if not stored_raw:
            return True
        _s = stored_raw if isinstance(stored_raw, dict) else __import__("json").loads(stored_raw)
        _st = int(((_s or {}).get("metrics") or {}).get("total_trades") or 0)
        if _st <= 0:
            return True  # khone ko kuch nahi
        # Stale engine: tag missing (version-tagging se pehle ka result) ya
        # current se alag -> stored history known-stale hai, overwrite OK.
        _s_ev = str((_s or {}).get("engine_version") or "")
        if _s_ev != str(_ev):
            return True
        _ft = int(((fresh_res or {}).get("metrics") or {}).get("total_trades") or 0)
        return _ft > 0
    except Exception:
        return True


@router.get("/{strat_id}/saved-result")
def strategy_saved_result(strat_id: int):
    """Backtest Detail ka INSTANT path: strategy banate samay wali poori trade
    history (metrics + trade_list) DB se seedha - bina hash-gate, bina run,
    bina wait. Config badli ho tab bhi SAVED wahi dikhta hai (config_changed
    badge ke saath); naya chahiye to POST .../backtest?refresh=1."""
    import json as _js
    try:
        db = Database.get_instance()
        row = db.fetch_one("SELECT * FROM strategies WHERE id=?", [strat_id])
        if not row:
            return {"error": "Strategy not found"}
        raw = row.get("last_backtest") or ""
        if not raw:
            return {"error": "Is strategy ka saved result nahi hai - pehle Run Backtest karke Save karo"}
        try:
            res = _js.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            return {"error": "Saved result corrupt hai - Naya run karo"}
        if not isinstance(res, dict) or not (res.get("metrics") or res.get("trade_list") is not None):
            return {"error": "Saved result adhura hai - Naya run karo"}
        res["stored"] = True
        res["stored_at"] = row.get("last_backtest_at") or ""
        res["strategy_id"] = strat_id
        res["strategy_name"] = row.get("name") or ""
        # Purane engine ka saved result? (v2 ke fake 100%-win jaise) -
        # badge dikhao + note saaf bolo, warna user ko laga rahega sahi hai.
        res["stale_engine"] = _stored_engine_stale(res)
        if res["stale_engine"]:
            res["config_changed"] = True
            res["note"] = ((res.get("note") or "") +
                           " Ye result PURANE engine ka saved hai - ↻ Naya run dabao, honest fresh result dikhega.").strip()
        # Config badli ya nahi (badge ke liye - result phir bhi SAVED wala).
        try:
            def _ld(v, fb):
                try:
                    if v is None or v == "":
                        return fb
                    return _js.loads(v) if isinstance(v, str) else (v or fb)
                except Exception:
                    return fb
            _legs = _ld(row.get("legs"), [])
            _inds = _ld(row.get("indicators"), [])
            _ec = _ld(row.get("entry_conditions"), [])
            _xc = _ld(row.get("exit_conditions"), [])
            _adv = _ld(row.get("advanced_options"), {})
            _risk = _ld(row.get("risk_management"), {})
            _now_hash = _canonical_config_hash(
                row.get("symbol"), row.get("start_date"), row.get("end_date"),
                row.get("timeframe"), _inds, _ec, _xc, _legs, _adv, _risk)
            _old_hash = str(row.get("last_backtest_hash") or "")
            res["config_changed"] = bool(_now_hash and _old_hash and _now_hash != _old_hash)
        except Exception:
            res["config_changed"] = False
        return res
    except Exception as e:
        return {"error": f"Saved result failed: {e}"[:200]}


@router.get("/{strat_id}/results-history")
def strategy_results_history(strat_id: int, history_id: int = 0):
    """Purane saved results ki list (naya sabse upar) + ?history_id=N par
    poora result JSON (trade history samet) dekhne ke liye. history kabhi
    delete nahi hoti (max 10/strategy) - 188-trade loss dobara impossible."""
    import json as _js
    try:
        _ensure_results_table()
    except Exception:
        pass
    try:
        db = Database.get_instance()
        _srow = db.fetch_one("SELECT id FROM strategies WHERE id=?", [strat_id])
        if not _srow:
            return {"error": "Strategy not found"}
        if history_id:
            _v = db.fetch_one(
                "SELECT * FROM strategy_results WHERE id=? AND strategy_id=?",
                [history_id, strat_id])
            if not _v:
                return {"error": "Version nahi mila"}
            try:
                _full = _js.loads(_v.get("result_json") or "{}")
            except Exception:
                return {"error": "Version corrupt hai"}
            if not isinstance(_full, dict):
                return {"error": "Version corrupt hai"}
            _full["stored"] = True
            _full["stored_at"] = _v.get("stored_at") or ""
            _full["strategy_id"] = strat_id
            _full["version_id"] = int(_v.get("id") or 0)
            return _full
        rows = db.fetch_all(
            "SELECT id, run_id, stored_at, trades, net_pnl, win_rate"
            " FROM strategy_results WHERE strategy_id=? ORDER BY id DESC LIMIT 10",
            [strat_id]) or []
        return {"strategy_id": strat_id, "count": len(rows),
                "versions": [dict(r) for r in rows]}
    except Exception as e:
        return {"error": f"History failed: {e}"[:200]}


@router.post("/{strat_id}/restore-result")
def strategy_restore_result(strat_id: int, body: dict):
    """Purana version (history_id) ya browser-copy (result) ko wapas current
    saved result banao. Pehle CURRENT wala archive hota hai (undo safe).
    Symbol mismatch par reject (galat strategy me galat result nahi)."""
    import json as _js
    try:
        _ensure_results_table()
    except Exception:
        pass
    try:
        db = Database.get_instance()
        row = db.fetch_one("SELECT * FROM strategies WHERE id=?", [strat_id])
        if not row:
            return {"error": "Strategy not found"}
        body = body or {}
        res = None
        if body.get("history_id"):
            _v = db.fetch_one(
                "SELECT * FROM strategy_results WHERE id=? AND strategy_id=?",
                [int(body["history_id"]), strat_id])
            if not _v:
                return {"error": "Version nahi mila"}
            try:
                res = _js.loads(_v.get("result_json") or "{}")
            except Exception:
                return {"error": "Version corrupt hai"}
        elif isinstance(body.get("result"), dict):
            res = body["result"]
        else:
            return {"error": "history_id ya result do"}
        if not isinstance(res, dict) or not (res.get("metrics") or res.get("trade_list") is not None):
            return {"error": "Result adhura hai (metrics/trade_list nahi)"}
        _syms = ((res.get("params") or {}).get("symbols") or
                 ([res.get("symbol")] if res.get("symbol") else []))
        if _syms and str(row.get("symbol") or "").upper() not in \
                [str(x or "").upper() for x in _syms]:
            return {"error": f"Symbol mismatch (result: {_syms} vs strategy: {row.get('symbol')})"}
        # Pehle current archive (undo safe), phir restore + current-config hash
        # (badge imaandari se batayega config badli ya nahi).
        try:
            if row.get("last_backtest"):
                _cur = _js.loads(row["last_backtest"]) if isinstance(row["last_backtest"], str) else row["last_backtest"]
                if isinstance(_cur, dict):
                    _archive_result(strat_id, _cur, str(row.get("last_backtest_hash") or ""),
                                    str(row.get("last_backtest_at") or ""))
        except Exception:
            pass
        try:
            from datetime import datetime as _dtn, timedelta as _tdn, timezone as _tzn
            _now_s = (_dtn.now(_tzn(_tdn(hours=5, minutes=30)))).strftime("%Y-%m-%d %H:%M")
        except Exception:
            _now_s = ""
        try:
            def _ld(v, fb):
                try:
                    if v is None or v == "":
                        return fb
                    return _js.loads(v) if isinstance(v, str) else (v or fb)
                except Exception:
                    return fb
            _hash = _canonical_config_hash(
                row.get("symbol"), row.get("start_date"), row.get("end_date"),
                row.get("timeframe"), _ld(row.get("indicators"), []),
                _ld(row.get("entry_conditions"), []), _ld(row.get("exit_conditions"), []),
                _ld(row.get("legs"), []), _ld(row.get("advanced_options"), {}),
                _ld(row.get("risk_management"), {}))
        except Exception:
            _hash = ""
        try:
            db.execute(
                "UPDATE strategies SET last_backtest=?, last_backtest_hash=?, last_backtest_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                [_js.dumps(res, default=str), _hash, _now_s + " (restored)", strat_id])
            _archive_result(strat_id, res, _hash, _now_s + " (restored)")
        except Exception as e:
            return {"error": f"Restore save failed: {e}"[:200]}
        res["stored"] = True
        res["stored_at"] = _now_s + " (restored)"
        res["strategy_id"] = strat_id
        return res
    except Exception as e:
        return {"error": f"Restore failed: {e}"[:200]}


@router.post("/{strat_id}/paper-trade")
def strategy_paper_trade(strat_id: int, body: dict = {}):
    """Saved strategy se PAPER trade - BACKTEST SIGNAL-GATED (parity fix).

    Purana frontend button seedha live-ATM par saari legs laga deta tha
    (indicators/entry-conditions ignore) - isliye paper trades backtest
    result se kabhi match nahi hote the. Ab default me wahi signal gate
    chalta hai jo auto-worker use karta hai:
      - saved timeframe intraday (1m/5m/15m/30m/1h) => live intraday gate
      - saved timeframe daily (1d) => daily backtest gate
    Signal na bane to KOI blind entry nahi - wajah (notes) milti hai.
    Force manual entry chahiye to {"force": true} bhejo (purana seedha
    behavior, strike-step backend se sahi)."""
    import json as _js
    try:
        db = Database.get_instance()
        row = db.fetch_one("SELECT * FROM strategies WHERE id=?", [strat_id])
        if not row:
            return {"error": "Strategy not found"}
        try:
            _force = bool((body or {}).get("force"))
        except Exception:
            _force = False
        try:
            _adv = _js.loads(row.get("advanced_options") or "{}")
        except Exception:
            _adv = {}
        if not isinstance(_adv, dict):
            _adv = {}
        _tf = str(_adv.get("timeframe") or row.get("timeframe") or "").lower()
        _gate = "intraday" if _tf in ("1m", "5m", "15m", "30m", "1h") else "daily"
        from core.services.auto_paper import place_for_strategy as _pl
        if _force:
            # Manual override: gate skip, saari non-AUTO legs seedha lagao
            # (strike-step/expiry/rollover backend _execute_legs me sahi).
            try:
                _legs = _js.loads(row.get("legs") or "[]")
            except Exception:
                _legs = []
            _legs = [l for l in (_legs or [])
                     if str((l or {}).get("option_type") or "").upper() in ("CE", "PE")]
            if not _legs:
                return {"error": "Is strategy me koi CE/PE leg nahi (AUTO ko manual direction chahiye)",
                        "gate": _gate, "forced": True}
            from core.services.auto_paper import _execute_legs as _ex
            from core.models.trade_model import TradeModel as _TM
            from core.services.auto_paper import _ist_today as _today
            _placed, _notes = _ex(row, _legs, db, _TM(), _today())
            res = {"placed": _placed, "notes": _notes}
        else:
            res = _pl(strat_id, signal_check=_gate) or {}
        out = {"strategy_id": strat_id,
               "strategy_name": row.get("name") or "",
               "symbol": row.get("symbol") or "",
               "gate": _gate, "forced": bool(_force),
               "placed": res.get("placed", []),
               "notes": res.get("notes", [])}
        try:
            out["saved_bt"] = _bt_summary(row)
        except Exception:
            out["saved_bt"] = {}
        return out
    except Exception as e:
        return {"error": f"Paper trade failed: {e}"[:200]}
