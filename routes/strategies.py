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
        return _hl.md5(_js.dumps({
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


def _result_matches_config(res: dict, symbol, start_date, end_date,
                           indicators, legs) -> bool:
    """Frontend-bheja result kya isi saved config se bana hai? (params echo
    vs saved config). Mismatch par attach mat karo - galat result card par
    dikhega. Returns True only on clear match."""
    try:
        if not isinstance(res, dict):
            return False
        m = res.get("metrics") or {}
        if not isinstance(m, dict) or "total_trades" not in m:
            return False
        p = res.get("params") or {}
        if not isinstance(p, dict):
            return False
        syms = p.get("symbols") or ([res.get("symbol")] if res.get("symbol") else [])
        if str(symbol or "").upper() not in [str(x or "").upper() for x in syms]:
            return False
        if str(p.get("start_date") or "")[:10] != str(start_date or "")[:10]:
            return False
        if str(p.get("end_date") or "")[:10] != str(end_date or "")[:10]:
            return False
        plegs = p.get("legs") or []
        if len(plegs) != len(legs or []):
            return False
        for _pl, _sl in zip(plegs, legs or []):
            if not isinstance(_pl, dict) or not isinstance(_sl, dict):
                return False
            if str(_pl.get("option_type") or "").upper() != str(_sl.get("option_type") or "").upper():
                return False
            _st = str(_sl.get("transaction") or _sl.get("position") or "").lower()
            if str(_pl.get("transaction") or "").lower() != _st:
                return False
        pinds = set()
        for _iv in (p.get("indicators") or []):
            pinds.add(str(_iv or ""))
        sinds = set()
        for _iv in (indicators or []):
            sinds.add(str((_iv.get("id") if isinstance(_iv, dict) else _iv) or ""))
        if pinds != sinds:
            return False
        return True
    except Exception:
        return False


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
    # Saved-result attach/clear: frontend ne abhi-chalaya result bheja ho AUR
    # wo isi config se bana ho to save ke saath result bhi save (Backtest
    # Detail phir dobara run nahi karega). Config badli ho aur koi result na
    # aaya ho to purana saved result saaf (stale card nahi dikhega).
    try:
        _new_hash = _canonical_config_hash(
            req.symbol, req.start_date, req.end_date, req.timeframe,
            req.indicators, req.entry_conditions, req.exit_conditions,
            req.legs, req.advanced_options, req.risk_management)
    except Exception:
        _new_hash = ""
    _attach_res, _clear_stored = None, False
    try:
        if isinstance(req.last_backtest, dict) and _result_matches_config(
                req.last_backtest, req.symbol, req.start_date, req.end_date,
                req.indicators, req.legs):
            _attach_res = req.last_backtest
        elif req.id:
            # No valid attach (bheja hi nahi ya mismatch): config badli ho to
            # purana saved result saaf - stale card kabhi nahi dikhega.
            _old = db.fetch_one(
                "SELECT last_backtest_hash FROM strategies WHERE id=?", [req.id])
            _old_hash = str((_old or {}).get("last_backtest_hash") or "")
            if _old_hash and _new_hash and _old_hash != _new_hash:
                _clear_stored = True
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
    elif _clear_stored:
        data["last_backtest"] = None
        data["last_backtest_hash"] = None
        data["last_backtest_at"] = None
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
    return resp


@router.delete("/{strat_id}")
def delete_strategy(strat_id: int):
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE id=?", [strat_id])
    return {"status": "deleted", "id": strat_id}


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
        # roz naya run nahi. Save-time hash bhi raw dates par hai).
        _cfg_hash = _canonical_config_hash(
            row.get("symbol"), row.get("start_date"), row.get("end_date"),
            row.get("timeframe"), indicators, entry_conditions,
            exit_conditions, legs, advanced, risk)
        if not refresh and _cfg_hash:
            try:
                if str(row.get("last_backtest_hash") or "") == _cfg_hash and row.get("last_backtest"):
                    _stored = _js.loads(row["last_backtest"])
                    if isinstance(_stored, dict) and (_stored.get("metrics") or _stored.get("trade_list") is not None):
                        _stored["stored"] = True
                        _stored["stored_at"] = row.get("last_backtest_at") or ""
                        _stored["strategy_id"] = strat_id
                        _stored["strategy_name"] = row.get("name") or ""
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
        except Exception:
            pass
        # Naya run save karo taaki agli baar WAHI result mile (error save nahi).
        if isinstance(res, dict) and not res.get("error") and (res.get("metrics") or res.get("trade_list") is not None):
            try:
                from datetime import datetime as _dtn, timedelta as _tdn, timezone as _tzn
                _now_s = (_dtn.now(_tzn(_tdn(hours=5, minutes=30)))).strftime("%Y-%m-%d %H:%M")
            except Exception:
                _now_s = ""
            try:
                db.execute(
                    "UPDATE strategies SET last_backtest=?, last_backtest_hash=?, last_backtest_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    [_js.dumps(res, default=str), _cfg_hash, _now_s, strat_id],
                )
                res["stored_at"] = _now_s
            except Exception:
                pass
        return res
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": f"Backtest failed: {e}"[:250]}
