import json
import os
from datetime import datetime
from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import Optional
from core.services.email_alert_parser import EmailAlertParser
from core.services.risk_manager import RiskManager

router = APIRouter()
parser = EmailAlertParser()
risk = RiskManager()

ALERTS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "email_alerts.json")
TRADES_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "email_trades.json")


def _load_alerts():
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_alerts(alerts):
    os.makedirs(os.path.dirname(ALERTS_FILE), exist_ok=True)
    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts[-200:], f, indent=2)


def _load_trades():
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_trades(trades):
    os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
    with open(TRADES_FILE, "w") as f:
        json.dump(trades[-500:], f, indent=2)


class AlertConfig(BaseModel):
    sl_pct: Optional[float] = 2.0
    tp_pct: Optional[float] = 4.0
    max_trades: Optional[int] = 5
    daily_loss_limit: Optional[float] = 5000
    trailing: Optional[str] = "lock"
    lock_at: Optional[float] = 1.0


class EmailSignal(BaseModel):
    text: str = ""
    subject: str = ""
    source: str = "auto"


@router.post("/ingest")
async def ingest_email(req: EmailSignal):
    combined = f"{req.subject} {req.text}".strip()
    if not combined:
        return {"error": "Empty email content"}
    parsed = parser.parse(combined, req.source)
    if not parsed:
        return {"error": "Could not parse trading signal from email", "raw": combined[:200]}
    risk_check = risk.can_trade()
    if not risk_check["allowed"]:
        return {"error": risk_check["reason"], "parsed": parsed}
    alerts = _load_alerts()
    alert_entry = {
        "id": len(alerts) + 1,
        "source": parsed.get("source", "unknown"),
        "symbol": parsed.get("symbol", ""),
        "action": parsed.get("action", "BUY"),
        "option_type": parsed.get("option_type", "CE"),
        "strike": parsed.get("strike"),
        "expiry": parsed.get("expiry"),
        "quantity": parsed.get("quantity"),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "raw": combined[:500],
        "status": "received",
    }
    alerts.append(alert_entry)
    _save_alerts(alerts)
    trades = _load_trades()
    trade_id = len(trades) + 1
    entry_price = 0
    sl_tp = risk.calculate_sl_tp(100.0, parsed.get("option_type", "CE"))
    trade_entry = {
        "id": trade_id,
        "alert_id": alert_entry["id"],
        "source": parsed.get("source", "unknown"),
        "symbol": parsed.get("symbol", ""),
        "option_type": parsed.get("option_type", "CE"),
        "strike": parsed.get("strike"),
        "action": parsed.get("action", "BUY"),
        "entry_price": entry_price,
        "sl": sl_tp["sl"],
        "target": sl_tp["tp"],
        "quantity": parsed.get("quantity", 1),
        "status": "pending",
        "pnl": None,
        "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exit_time": None,
        "exit_price": None,
    }
    trades.append(trade_entry)
    _save_trades(trades)
    risk.register_trade({"id": str(trade_id), "symbol": parsed.get("symbol"), "entry_price": entry_price})
    return {
        "success": True,
        "trade_id": trade_id,
        "parsed": parsed,
        "sl": sl_tp["sl"],
        "target": sl_tp["tp"],
        "risk_status": risk.get_status(),
    }


@router.post("/process-email")
async def process_email_webhook(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    text = body.get("text", "") or body.get("body", "") or body.get("message", "")
    subject = body.get("subject", "")
    source = body.get("source", "auto")
    if not text and not subject:
        raw = json.dumps(body)
        text = raw
    combined = f"{subject} {text}".strip()
    parsed = parser.parse(combined, source)
    if not parsed:
        return {"error": "Could not parse signal", "raw": combined[:200]}
    risk_check = risk.can_trade()
    if not risk_check["allowed"]:
        return {"error": risk_check["reason"], "parsed": parsed}
    alerts = _load_alerts()
    alert_entry = {
        "id": len(alerts) + 1,
        "source": parsed.get("source", "unknown"),
        "symbol": parsed.get("symbol", ""),
        "action": parsed.get("action", "BUY"),
        "option_type": parsed.get("option_type", "CE"),
        "strike": parsed.get("strike"),
        "expiry": parsed.get("expiry"),
        "quantity": parsed.get("quantity"),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "raw": combined[:500],
        "status": "received",
    }
    alerts.append(alert_entry)
    _save_alerts(alerts)
    trades = _load_trades()
    trade_id = len(trades) + 1
    entry_price = 0
    sl_tp = risk.calculate_sl_tp(100.0, parsed.get("option_type", "CE"))
    trade_entry = {
        "id": trade_id,
        "alert_id": alert_entry["id"],
        "source": parsed.get("source", "unknown"),
        "symbol": parsed.get("symbol", ""),
        "option_type": parsed.get("option_type", "CE"),
        "strike": parsed.get("strike"),
        "action": parsed.get("action", "BUY"),
        "entry_price": entry_price,
        "sl": sl_tp["sl"],
        "target": sl_tp["tp"],
        "quantity": parsed.get("quantity", 1),
        "status": "pending",
        "pnl": None,
        "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exit_time": None,
        "exit_price": None,
    }
    trades.append(trade_entry)
    _save_trades(trades)
    risk.register_trade({"id": str(trade_id), "symbol": parsed.get("symbol"), "entry_price": entry_price})
    return {
        "success": True,
        "trade_id": trade_id,
        "action": parsed.get("action"),
        "symbol": parsed.get("symbol"),
        "strike": parsed.get("strike"),
        "option_type": parsed.get("option_type"),
        "source": parsed.get("source"),
    }


@router.get("/list")
async def list_alerts():
    return {"alerts": _load_alerts()[-50:]}


@router.get("/trades")
async def list_trades():
    return {"trades": _load_trades()[-50:]}


@router.get("/risk")
async def get_risk():
    return risk.get_status()


@router.post("/config")
async def update_config(cfg: AlertConfig):
    risk.update_config(cfg.dict(exclude_none=True))
    return {"success": True, "config": risk.config}


@router.post("/reset")
async def reset_daily():
    risk.reset_daily()
    return {"success": True, "message": "Daily state reset"}
