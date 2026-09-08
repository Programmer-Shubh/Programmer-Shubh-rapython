from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, Dict, Any
import json
import asyncio
from core.models.database import Database
from core.services.broker_fyers import FyersV3
from core.services.broker_dhan import DhanHQ

router = APIRouter()

BROKER_DEFAULTS = {
    "shoonya": {"name": "Shoonya (Finvasia)", "icon": "bi-lightning", "color": "text-danger", "fields": ["uid", "pwd", "vc", "apikey", "secret_code", "secret", "actid"], "desc": "User ID + Password + Vendor Code + API Key"},
    "dhan": {"name": "Dhan", "icon": "bi-bank", "color": "text-primary", "fields": ["client_id", "access_token", "refresh_token"], "desc": "Client ID + Access Token + Refresh Token"},
    "fyers": {"name": "Fyers", "icon": "bi-lightning-charge", "color": "text-warning", "fields": ["app_id", "secret", "access_token", "refresh_token", "redirect_uri"], "desc": "App ID + Secret + OAuth Token"},
    "angel": {"name": "Angel One", "icon": "bi-graph-up-arrow", "color": "text-success", "fields": ["client_code", "password", "api_key", "totp_secret"], "desc": "Client Code + Password + API Key"},
}

BROKER_FIELD_LABELS = {
    "shoonya": {"uid": "User ID", "pwd": "Password", "vc": "Vendor Code", "apikey": "API Key", "secret_code": "Secret Code", "secret": "TOTP Secret (2FA)", "actid": "Account ID"},
    "dhan": {"client_id": "Client ID", "access_token": "Access Token", "refresh_token": "Refresh Token"},
    "fyers": {"app_id": "App ID", "secret": "App Secret", "access_token": "Access Token", "refresh_token": "Refresh Token", "redirect_uri": "Redirect URI"},
    "angel": {"client_code": "Client Code", "password": "Password", "api_key": "API Key", "totp_secret": "TOTP Secret"},
}


class BrokerConfig(BaseModel):
    broker: str
    config: Dict[str, Any] = {}


class ConnectRequest(BaseModel):
    broker: str


class AuthRequest(BaseModel):
    broker: str
    code: str = ""


class LiveOrderRequest(BaseModel):
    symbol: str
    option_type: str = "CE"
    transaction_type: str = "BUY"
    quantity: int = 1
    strike: float = 0
    expiry: str = ""
    stop_loss: float = 1500.0
    take_profit: float = 3000.0
    broker: str = ""
    dry_run: bool = False
    trade_type: str = "intraday"


class LiveToggleRequest(BaseModel):
    enabled: bool = False


def _live_enabled() -> bool:
    try:
        db = Database.get_instance()
        row = db.fetch_one("SELECT setting_value FROM settings WHERE setting_key='live_trading_enabled'")
        return (row.get("setting_value") if row else "0") == "1"
    except Exception:
        return False


def _real_token(broker: str) -> bool:
    """A 'real' token excludes the app's simulated SIM-/ANG- tokens."""
    tok = (_get_config(broker) or {}).get("access_token", "")
    return bool(tok) and not str(tok).startswith(("SIM-", "ANG-"))


def _resolve_expiry(symbol: str, hint: str) -> str:
    """Return YYYY-MM-DD expiry: YYYY-MM-DD hint as-is; Weekly -> next expiry
    weekday (Tue for NSE F&O, Thu for SENSEX); Monthly (or stocks) -> last such
    weekday of month. (NSE moved expiries: NIFTY Tue, BANKNIFTY/FINNIFTY monthly
    last-Tue - verified against Dhan scrip master Sep 2026.)"""
    import datetime as _dt
    import calendar as _cal
    hint = str(hint or "").strip()
    try:
        return _dt.datetime.strptime(hint[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        pass
    today = (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).date()
    wd = 3 if symbol.upper() == "SENSEX" else 1  # Thu vs Tue
    weekly_idx = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
    hl = hint.lower()
    if hl.startswith("month"):
        pass  # monthly calc below
    elif hl.startswith("week") or symbol.upper() in weekly_idx:
        d = today + _dt.timedelta(days=1)
        while d.weekday() != wd:
            d += _dt.timedelta(days=1)
        return d.strftime("%Y-%m-%d")
    last = _dt.date(today.year, today.month, _cal.monthrange(today.year, today.month)[1])
    while last.weekday() != wd:
        last -= _dt.timedelta(days=1)
    if last < today:
        m = today.month + 1 if today.month < 12 else 1
        y = today.year if today.month < 12 else today.year + 1
        last = _dt.date(y, m, _cal.monthrange(y, m)[1])
        while last.weekday() != wd:
            last -= _dt.timedelta(days=1)
    return last.strftime("%Y-%m-%d")


def _expiry_is_explicit(hint: str) -> bool:
    import datetime as _dt
    try:
        _dt.datetime.strptime(str(hint or "")[:10], "%Y-%m-%d")
        return True
    except Exception:
        return False


def _fyers_symbol(symbol: str, expiry_ymd: str, strike: float, option_type: str) -> str:
    """Fyers F&O symbols (verified against official skill docs + community):
    monthly: NSE:NIFTY26JAN25500CE  ({YY}{MMM})
    weekly:  NSE:NIFTY2611325500CE  ({YY}{M}{dd}, M = 1-9/O/N/D single-char code).
    Monthly <=> expiry is the month's last Thursday, else weekly."""
    import datetime as _dt
    import calendar as _cal
    d = _dt.datetime.strptime(expiry_ymd[:10], "%Y-%m-%d").date()
    wd = 3 if symbol.upper() == "SENSEX" else 1  # monthly weekday: Thu vs Tue
    last = _dt.date(d.year, d.month, _cal.monthrange(d.year, d.month)[1])
    while last.weekday() != wd:
        last -= _dt.timedelta(days=1)
    yy = d.strftime("%y")
    if d == last:
        mon = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"][d.month - 1]
        code = f"{yy}{mon}"
    else:
        mcode = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6",
                 7: "7", 8: "8", 9: "9", 10: "O", 11: "N", 12: "D"}[d.month]
        code = f"{yy}{mcode}{d.day:02d}"
    return f"NSE:{symbol.upper()}{code}{int(float(strike))}{option_type.upper()}"


@router.get("/live-status")
def live_status():
    real = [b for b in ("fyers", "dhan") if _real_token(b)]
    return {"enabled": _live_enabled(), "real_brokers": real,
            "note": "Fyers/Dhan/Angel/Shoonya live supported. Dry-run first to verify broker symbol/token."}


@router.post("/live-toggle")
def live_toggle(req: LiveToggleRequest):
    db = Database.get_instance()
    row = db.fetch_one("SELECT setting_key FROM settings WHERE setting_key='live_trading_enabled'")
    if row:
        db.execute("UPDATE settings SET setting_value=?, updated_at=datetime('now') WHERE setting_key='live_trading_enabled'",
                   ["1" if req.enabled else "0"])
    else:
        db.execute("INSERT INTO settings (setting_key, setting_value) VALUES ('live_trading_enabled', ?)",
                   ["1" if req.enabled else "0"])
    return {"enabled": bool(req.enabled)}


def _get_config(broker: str) -> dict:
    db = Database.get_instance()
    row = db.fetch_one("SELECT setting_value FROM settings WHERE setting_key=?", [f"broker_{broker}"])
    if row and row.get("setting_value"):
        try:
            return json.loads(row["setting_value"])
        except Exception:
            return {}
    return {}


def _save_config(broker: str, config: dict):
    db = Database.get_instance()
    existing = db.fetch_one("SELECT setting_key FROM settings WHERE setting_key=?", [f"broker_{broker}"])
    if existing:
        db.execute("UPDATE settings SET setting_value=?, updated_at=datetime('now') WHERE setting_key=?", [json.dumps(config), f"broker_{broker}"])
    else:
        db.execute("INSERT INTO settings (setting_key, setting_value) VALUES (?, ?)", [f"broker_{broker}", json.dumps(config)])


def _is_configured(broker: str) -> bool:
    return bool(_get_config(broker))


@router.get("/list")
def list_brokers():
    brokers = []
    for key, info in BROKER_DEFAULTS.items():
        config = _get_config(key)
        configured = bool(config)
        brokers.append({
            "key": key,
            "name": info["name"],
            "icon": info["icon"],
            "color": info["color"],
            "desc": info["desc"],
            "fields": info["fields"],
            "configured": configured,
            "config": config,
        })
    return {"brokers": brokers}


@router.post("/save-config")
def save_config(req: BrokerConfig):
    if req.broker not in BROKER_DEFAULTS:
        return {"success": False, "error": "Invalid broker"}
    config = req.config
    if req.broker == "fyers" and config.get("redirect_uri", "").strip() == "":
        config["redirect_uri"] = "https://subh.infinityfreeapp.com/brokers/fyers-callback"
    _save_config(req.broker, config)
    return {"success": True, "message": "Config saved"}


@router.post("/fyers-auth")
async def fyers_auth(req: AuthRequest):
    config = _get_config("fyers")
    if not config:
        return {"success": False, "error": "Fyers not configured. Setup first."}
    fy = FyersV3(
        app_id=config.get("app_id", ""),
        secret_key=config.get("secret", ""),
        redirect_uri=config.get("redirect_uri", ""),
    )
    result = await fy.generate_token(req.code)
    if result["success"]:
        config["access_token"] = fy.access_token
        config["refresh_token"] = fy.refresh_token
        _save_config("fyers", config)
        return {"success": True, "message": "Fyers connected! Tokens auto-refresh daily."}
    return {"success": False, "error": result["error"]}


@router.get("/fyers-auth-url")
def fyers_auth_url():
    config = _get_config("fyers")
    if not config:
        return {"success": False, "error": "Fyers not configured"}
    fy = FyersV3(
        app_id=config.get("app_id", ""),
        secret_key=config.get("secret", ""),
        redirect_uri=config.get("redirect_uri", ""),
    )
    return {"success": True, "url": fy.get_auth_url()}


@router.post("/connect")
async def connect_broker(req: ConnectRequest):
    if req.broker not in BROKER_DEFAULTS:
        return {"success": False, "error": "Invalid broker"}
    config = _get_config(req.broker)
    if not config:
        return {"success": False, "error": "No credentials configured. Click Setup first."}

    if req.broker == "fyers":
        fy = FyersV3(
            app_id=config.get("app_id", ""),
            secret_key=config.get("secret", ""),
            redirect_uri=config.get("redirect_uri", ""),
            access_token=config.get("access_token", ""),
            refresh_token=config.get("refresh_token", ""),
        )
        if config.get("refresh_token"):
            result = await fy.refresh_access_token()
            if result["success"]:
                config["access_token"] = fy.access_token
                config["refresh_token"] = fy.refresh_token
                _save_config("fyers", config)
                return {"success": True, "message": "Fyers connected (token auto-refreshed)"}
        if config.get("access_token"):
            return {"success": True, "message": "Fyers connected (token valid)"}
        return {"success": False, "error": "Fyers needs OAuth login or Access Token. Click OAuth Login."}

    if req.broker == "dhan":
        if config.get("access_token"):
            return {"success": True, "message": "Dhan connected (token valid)"}
        return {"success": False, "error": "Dhan needs Access Token. Paste it in Setup."}

    if req.broker == "shoonya":
        # REAL Noren QuickAuth login (TOTP auto-generated from stored secret).
        # No simulated tokens: success stores the real session token.
        try:
            from core.services.broker_shoonya_live import ShoonyaLive
            sh = ShoonyaLive(uid=config.get("uid", ""), password=config.get("pwd", ""),
                             totp_secret=config.get("secret", "") or config.get("secret_code", ""),
                             vendor_code=config.get("vc", ""), api_key=config.get("apikey", ""))
            result = await sh.login()
        except Exception as e:
            return {"success": False, "error": f"Shoonya login crashed: {str(e)[:150]}"}
        if result.get("success"):
            config["access_token"] = sh.susertoken
            config["actid"] = sh.actid
            config.pop("refresh_token", None)
            _save_config("shoonya", config)
            return {"success": True, "message": "Shoonya connected! Real session token."}
        return {"success": False, "error": f"Shoonya login failed: {result.get('error')}. Check uid/password/TOTP-secret/vendor-code/api-key."}

    if req.broker == "angel":
        # REAL SmartAPI loginByPassword (TOTP auto-generated from stored secret).
        try:
            from core.services.broker_angel_live import AngelLive
            an = AngelLive(api_key=config.get("api_key", ""), client_code=config.get("client_code", ""),
                           password=config.get("password", ""), totp_secret=config.get("totp_secret", ""))
            result = await an.login()
        except Exception as e:
            return {"success": False, "error": f"Angel login crashed: {str(e)[:150]}"}
        if result.get("success"):
            config["access_token"] = an.jwt
            config["refresh_token"] = an.refresh_token
            config["feed_token"] = an.feed_token
            _save_config("angel", config)
            return {"success": True, "message": "Angel One connected! Real JWT session."}
        return {"success": False, "error": f"Angel login failed: {result.get('error')}. Check api-key/client-code/password/TOTP-secret."}

    return {"success": True, "message": f"{req.broker} connected (demo mode)"}


@router.post("/auto-connect")
async def auto_connect_all():
    results = []
    for key in BROKER_DEFAULTS:
        config = _get_config(key)
        if not config:
            results.append({"broker": key, "status": "not_configured"})
            continue
        if key == "fyers" and config.get("refresh_token"):
            fy = FyersV3(
                app_id=config.get("app_id", ""),
                secret_key=config.get("secret", ""),
                redirect_uri=config.get("redirect_uri", ""),
                access_token=config.get("access_token", ""),
                refresh_token=config.get("refresh_token", ""),
            )
            result = await fy.refresh_access_token()
            if result["success"]:
                config["access_token"] = fy.access_token
                config["refresh_token"] = fy.refresh_token
                _save_config("fyers", config)
                results.append({"broker": key, "status": "connected"})
            else:
                results.append({"broker": key, "status": "failed", "error": result["error"]})
        elif key in ("shoonya", "angel", "dhan"):
            # Already configured tokens present
            results.append({"broker": key, "status": "connected"})
        else:
            results.append({"broker": key, "status": "configured_but_no_token"})
    return {"success": True, "results": results}


@router.post("/refresh-tokens")
async def refresh_tokens():
    results = {}
    for key in BROKER_DEFAULTS:
        config = _get_config(key)
        if not config:
            results[key] = {"success": False, "error": "Not configured"}
            continue
        if key == "fyers":
            fy = FyersV3(
                app_id=config.get("app_id", ""),
                secret_key=config.get("secret", ""),
                redirect_uri=config.get("redirect_uri", ""),
                access_token=config.get("access_token", ""),
                refresh_token=config.get("refresh_token", ""),
            )
            result = await fy.refresh_access_token()
            if result["success"]:
                config["access_token"] = fy.access_token
                config["refresh_token"] = fy.refresh_token
                _save_config("fyers", config)
                results[key] = {"success": True, "message": "Token refreshed"}
            else:
                results[key] = {"success": False, "error": result["error"]}
        elif key == "dhan":
            results[key] = {"success": bool(config.get("access_token")), "message": "Token cached" if config.get("access_token") else "No token"}
        elif key in ("shoonya", "angel"):
            # Tokens already simulated and stored; return success
            results[key] = {"success": bool(config.get("access_token")), "message": "Tokens cached"}
        else:
            results[key] = {"success": True, "message": "OK (demo)"}
    return {"success": True, "results": results}


@router.get("/token-status")
def token_status():
    status = {}
    for key in BROKER_DEFAULTS:
        config = _get_config(key)
        valid = False
        if key == "fyers":
            valid = bool(config.get("access_token"))
        elif key == "dhan":
            valid = bool(config.get("access_token"))
        elif key in ("shoonya", "angel"):
            valid = bool(config.get("access_token"))
        else:
            valid = bool(config)
        status[key] = {"valid": valid, "expires_at": ""}
    return {"success": True, "status": status}


@router.post("/account")
async def view_account(req: ConnectRequest):
    if req.broker not in BROKER_DEFAULTS:
        return {"success": False, "error": "Invalid broker"}
    config = _get_config(req.broker)
    if not config:
        return {"success": False, "error": "Not connected. Click Setup first."}

    if req.broker == "fyers" and config.get("access_token"):
        fy = FyersV3(
            app_id=config.get("app_id", ""),
            secret_key=config.get("secret", ""),
            redirect_uri=config.get("redirect_uri", ""),
            access_token=config.get("access_token", ""),
            refresh_token=config.get("refresh_token", ""),
        )
        funds = await fy.get_funds()
        positions = await fy.get_positions()
        holdings = await fy.get_holdings()
        return {
            "success": True,
            "funds": funds.get("data", {}).get("fund_limit", [{}])[0] if funds.get("data", {}).get("fund_limit") else {"message": "Live fetch pending"},
            "positions": positions.get("data", []) if positions.get("data") else [],
            "holdings": holdings.get("data", []) if holdings.get("data") else [],
            "orders": [],
        }

    if req.broker == "dhan" and config.get("access_token"):
        dh = DhanHQ(
            client_id=config.get("client_id", ""),
            access_token=config.get("access_token", ""),
            refresh_token=config.get("refresh_token", ""),
        )
        funds = await dh.get_funds()
        positions = await dh.get_positions()
        holdings = await dh.get_holdings()
        return {
            "success": True,
            "funds": funds.get("data", {}) if funds.get("data") else {"message": "Live fetch pending"},
            "positions": positions.get("data", []) if positions.get("data") else [],
            "holdings": holdings.get("data", []) if holdings.get("data") else [],
            "orders": [],
        }

    if req.broker == "shoonya" and config.get("access_token"):
        # Simulated account data
        return {
            "success": True,
            "funds": {"available_margin": 100000, "used_margin": 20000},
            "positions": [{"symbol": "NIFTY", "quantity": 25, "average_price": 24500}],
            "holdings": [{"symbol": "RELIANCE", "quantity": 5, "average_price": 2500}],
            "orders": [],
        }

    if req.broker == "angel" and config.get("access_token"):
        # Simulated Angel account
        return {
            "success": True,
            "funds": {"available_margin": 50000, "used_margin": 10000},
            "positions": [{"symbol": "NIFTY", "quantity": 15}],
            "holdings": [{"symbol": "TCS", "quantity": 3}],
            "orders": [],
        }

    return {
        "success": True,
        "funds": {"available_margin": 0, "used_margin": 0},
        "positions": [],
        "holdings": [],
        "orders": [],
    }


@router.post("/place-live")
async def place_live_order(req: LiveOrderRequest):
    """REAL live order execution (real money). Safety chain:
    1. dry_run=true -> only previews the exact broker payload, sends nothing.
    2. Global kill-switch: live_trading_enabled must be ON (Brokers tab).
    3. Broker must hold a REAL token (simulated SIM-/ANG- tokens rejected).
    4. Fyers supported (symbol auto-built). Dhan needs securityId mapping.
    Quantity sent to broker = lots x lot_size (never raw lots).
    Fills are tracked trade_mode='live' with broker_order_id stored.
    """
    try:
        from core.models.trade_model import TradeModel
        from core.services.transaction_costs import TransactionCosts
        from utils.helpers import get_lot_size, get_strike_step, model_premium
        from core.services.live_market_data import LiveMarketData
        symbol = (req.symbol or "").upper()
        if not symbol:
            return {"error": "Symbol required"}
        lots = max(1, int(req.quantity or 1))
        lot = get_lot_size(symbol)
        broker_qty = lots * lot
        txn = str(req.transaction_type or "BUY").upper()
        if txn not in ("BUY", "SELL"):
            txn = "BUY"
        opt = str(req.option_type or "CE").upper()
        if opt not in ("CE", "PE"):
            opt = "CE"
        strike = float(req.strike or 0)
        if strike <= 0:
            spot = LiveMarketData().get_spot_price(symbol)
            step = get_strike_step(symbol)
            strike = round((spot or 0) / step) * step if spot and step else 0
            if strike <= 0:
                return {"error": "Strike required (spot unavailable for ATM calc)"}
        exp_ymd = _resolve_expiry(symbol, req.expiry or "weekly")
        # Broker choice
        want = (req.broker or "").lower()
        if want and want not in BROKER_DEFAULTS:
            return {"error": f"Unknown broker '{req.broker}'"}
        # (Simulated SIM-/ANG- tokens are rejected by _real_token below.)
        if want and not _real_token(want):
            return {"error": f"{want} has no REAL session (old simulated token?). Hit Connect in Brokers tab to login for real - order NOT placed."}
        broker = want or next((b for b in ("fyers", "dhan", "angel", "shoonya") if _real_token(b)), "")
        if not broker:
            return {"error": "No broker with a REAL token connected. Connect Fyers, Dhan, Angel or Shoonya first - order NOT placed."}
        if broker not in ("fyers", "dhan", "angel", "shoonya"):
            return {"error": f"Live execution supports Fyers/Dhan/Angel/Shoonya (got {broker})."}
        if broker == "fyers":
            fy_sym = _fyers_symbol(symbol, exp_ymd, strike, opt)
            preview = {"broker": "fyers", "symbol": fy_sym, "underlying": symbol,
                       "strike": strike, "expiry": exp_ymd, "side": txn,
                       "lots": lots, "lot_size": lot, "quantity": broker_qty,
                       "order_type": "MARKET", "product": "INTRADAY"}
        elif broker == "dhan":
            try:
                from core.services.broker_dhan_live import DhanLive
                _dl0 = DhanLive(client_id=(_get_config("dhan") or {}).get("client_id", ""),
                                access_token=(_get_config("dhan") or {}).get("access_token", ""))
                _res0 = await _dl0.resolve_fo(symbol, exp_ymd, strike, opt,
                                              exact=_expiry_is_explicit(req.expiry))
            except Exception as e:
                return {"error": f"Dhan resolution crashed: {str(e)[:150]}"}
            if not _res0.get("security_id"):
                return {"error": f"Dhan: {_res0.get('error', 'securityId not resolved')} - order NOT placed."}
            _dlot = int(_res0.get("lot_size") or 0) or lot
            _tt = str(req.trade_type or "intraday").lower()
            preview = {"broker": "dhan", "security_id": _res0["security_id"],
                       "resolved_symbol": _res0.get("symbol", ""), "underlying": symbol,
                       "strike": strike, "expiry": exp_ymd, "side": txn,
                       "lots": lots, "lot_size": _dlot, "quantity": lots * _dlot,
                       "order_type": "MARKET",
                       "product": "MARGIN" if _tt == "positional" else "INTRADAY",
                       "source": _res0.get("source", "")}
            broker_qty = lots * _dlot
            lot = _dlot
        elif broker == "angel":
            try:
                from core.services.broker_angel_live import AngelLive
                _ac0 = _get_config("angel") or {}
                _an0 = AngelLive(api_key=_ac0.get("api_key", ""), client_code=_ac0.get("client_code", ""),
                                 password=_ac0.get("password", ""), totp_secret=_ac0.get("totp_secret", ""))
                _an0.jwt = _ac0.get("access_token", "")
                _resA = await _an0.resolve_fo(symbol, exp_ymd, strike, opt)
            except Exception as e:
                return {"error": f"Angel resolution crashed: {str(e)[:150]}"}
            if not _resA.get("symboltoken"):
                return {"error": f"Angel: {_resA.get('error', 'symboltoken not resolved')} - order NOT placed."}
            _tt = str(req.trade_type or "intraday").lower()
            preview = {"broker": "angel", "symbol": _resA["tradingsymbol"],
                       "symboltoken": _resA["symboltoken"], "underlying": symbol,
                       "strike": strike, "expiry": exp_ymd, "side": txn,
                       "lots": lots, "lot_size": lot, "quantity": broker_qty,
                       "order_type": "MARKET",
                       "product": "MARGIN" if _tt == "positional" else "INTRADAY"}
        elif broker == "shoonya":
            try:
                from core.services.broker_shoonya_live import ShoonyaLive
                _sc0 = _get_config("shoonya") or {}
                _sh0 = ShoonyaLive(uid=_sc0.get("uid", ""), password=_sc0.get("pwd", ""),
                                   totp_secret=_sc0.get("secret", "") or _sc0.get("secret_code", ""),
                                   vendor_code=_sc0.get("vc", ""), api_key=_sc0.get("apikey", ""))
                _sh0.susertoken = _sc0.get("access_token", "")
                _sh0.actid = _sc0.get("actid", _sc0.get("uid", ""))
                _resS = await _sh0.resolve_fo(symbol, exp_ymd, strike, opt)
            except Exception as e:
                return {"error": f"Shoonya resolution crashed: {str(e)[:150]}"}
            if not _resS.get("tsym"):
                return {"error": f"Shoonya: {_resS.get('error', 'contract not resolved')} - order NOT placed."}
            _slot = int(_resS.get("lot_size") or 0) or lot
            _tt = str(req.trade_type or "intraday").lower()
            preview = {"broker": "shoonya", "symbol": _resS["tsym"],
                       "token": _resS.get("token", ""), "underlying": symbol,
                       "strike": strike, "expiry": exp_ymd, "side": txn,
                       "lots": lots, "lot_size": _slot, "quantity": lots * _slot,
                       "order_type": "MKT",
                       "product": "M" if _tt == "positional" else "I"}
            broker_qty = lots * _slot
            lot = _slot
        if req.dry_run:
            return {"success": True, "dry_run": True, "preview": preview,
                    "note": "Preview only - nothing sent to broker."}
        if not _live_enabled():
            return {"error": "LIVE trading is OFF (safety switch). Enable it in Brokers tab first - order NOT placed.",
                    "preview": preview}
        order_id = ""
        broker_ref = ""
        if broker == "fyers":
            cfg = _get_config("fyers")
            from core.services.broker_fyers import FyersV3
            fy = FyersV3(app_id=cfg.get("app_id", ""), secret_key=cfg.get("secret", ""),
                         redirect_uri=cfg.get("redirect_uri", ""),
                         access_token=cfg.get("access_token", ""),
                         refresh_token=cfg.get("refresh_token", ""))
            res = await fy.place_order(fy_sym, txn, broker_qty, order_type="MARKET")
            if not res.get("success"):
                return {"error": f"Fyers rejected order: {res.get('error') or res.get('data')}",
                        "preview": preview, "broker_response": res.get("data", {})}
            order_id = str((res.get("data") or {}).get("order_id", ""))
            broker_ref = fy_sym
        elif broker == "dhan":
            from core.services.broker_dhan_live import DhanLive
            dl = DhanLive(client_id=(_get_config("dhan") or {}).get("client_id", ""),
                          access_token=(_get_config("dhan") or {}).get("access_token", ""))
            res = await dl.place_order(preview["security_id"], txn, broker_qty,
                                       order_type="MARKET", product=preview.get("product", "INTRADAY"))
            if not res.get("success"):
                return {"error": f"Dhan rejected order: {res.get('error')}",
                        "preview": preview, "broker_response": res.get("data", {})}
            order_id = str((res.get("data") or {}).get("order_id", ""))
            broker_ref = preview["security_id"]
        elif broker == "angel":
            _ac = _get_config("angel") or {}
            from core.services.broker_angel_live import AngelLive
            an = AngelLive(api_key=_ac.get("api_key", ""), client_code=_ac.get("client_code", ""),
                           password=_ac.get("password", ""), totp_secret=_ac.get("totp_secret", ""))
            an.jwt = _ac.get("access_token", "")
            res = await an.place_order(preview["symbol"], preview["symboltoken"], txn,
                                       broker_qty, product=preview.get("product", "INTRADAY"))
            if not res.get("success"):
                return {"error": f"Angel rejected order: {res.get('error')}",
                        "preview": preview, "broker_response": res.get("data", {})}
            order_id = str((res.get("data") or {}).get("order_id", ""))
            broker_ref = preview["symbol"]
        elif broker == "shoonya":
            _sc = _get_config("shoonya") or {}
            from core.services.broker_shoonya_live import ShoonyaLive
            sh = ShoonyaLive(uid=_sc.get("uid", ""), password=_sc.get("pwd", ""),
                             totp_secret=_sc.get("secret", "") or _sc.get("secret_code", ""),
                             vendor_code=_sc.get("vc", ""), api_key=_sc.get("apikey", ""))
            sh.susertoken = _sc.get("access_token", "")
            sh.actid = _sc.get("actid", _sc.get("uid", ""))
            res = await sh.place_order(preview["symbol"], txn, broker_qty,
                                       product=preview.get("product", "I"))
            if not res.get("success"):
                return {"error": f"Shoonya rejected order: {res.get('error')}",
                        "preview": preview, "broker_response": res.get("data", {})}
            order_id = str((res.get("data") or {}).get("order_id", ""))
            broker_ref = preview["symbol"]
        # Track the live fill locally (entry at premium estimate)
        premium = None
        try:
            premium = TradeModel().get_option_premium(symbol, opt, strike, exp_ymd)
        except Exception:
            premium = None
        if not premium or premium <= 0:
            spot = LiveMarketData().get_spot_price(symbol)
            premium = model_premium(spot or strike, strike, 7, opt, symbol=symbol) if spot else 50.0
        adj = TransactionCosts.apply_fill_slippage(float(premium), txn, is_live=True)
        costs = TransactionCosts.calculate(adj * lots * lot, txn == "SELL", is_live=True)
        import datetime as _dt
        tm = TradeModel()
        tid = tm.insert_trade({
            "symbol": symbol, "option_type": opt, "strike_price": strike,
            "expiry_date": exp_ymd, "transaction_type": txn,
            "quantity": lots, "lot_size": lot, "entry_price": adj,
            "stop_loss": float(req.stop_loss or 0), "target": float(req.take_profit or 0),
            "total_cost": costs["total"],
            "entry_date": _dt.datetime.now().strftime("%Y-%m-%d"),
            "trade_mode": "live", "broker_order_id": order_id,
        })
        return {"success": True, "trade_id": tid, "broker_order_id": order_id,
                "broker": broker, "broker_symbol": broker_ref, "quantity": broker_qty,
                "entry_price": round(adj, 2), "mode": "live"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": f"Live order failed: {str(e)}"}