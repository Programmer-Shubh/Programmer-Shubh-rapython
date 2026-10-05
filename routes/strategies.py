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
                    "trade_mode": t.get("trade_mode"),
                    "status": t.get("status"),
                    "entry_date": t.get("entry_date", ""),
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
                    "exit_date": t.get("exit_date", ""),
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
