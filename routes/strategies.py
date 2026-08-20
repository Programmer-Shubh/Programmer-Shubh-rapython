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


@router.get("/list")
def list_strategies():
    db = Database.get_instance()
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
        db.execute(f"UPDATE strategies SET {sets}, updated_at=datetime('now') WHERE id=?", vals)
        return {"id": req.id, "status": "updated"}
    else:
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        vals = [1] + list(data.values())
        row_id = db.execute(
            f"INSERT INTO strategies (user_id, {cols}) VALUES (?, {placeholders})",
            vals,
        )
        return {"id": row_id, "status": "created"}


@router.delete("/{strat_id}")
def delete_strategy(strat_id: int):
    db = Database.get_instance()
    db.execute("DELETE FROM strategies WHERE id=?", [strat_id])
    return {"status": "deleted", "id": strat_id}
