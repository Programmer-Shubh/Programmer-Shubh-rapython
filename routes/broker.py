from fastapi import APIRouter, Request
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


def _env_config(broker: str) -> dict:
    """Broker credentials from environment variables (survive Render redeploys
    + opencode edits, unlike the ephemeral SQLite DB which wipes on deploy).
    Naming: RATRADE_<BROKER>_<FIELD> e.g. RATRADE_DHAN_CLIENT_ID.
    Env values override DB values."""
    import os
    try:
        fields = (BROKER_DEFAULTS.get(broker, {}) or {}).get("fields", []) or []
    except Exception:
        fields = []
    out = {}
    for f in fields:
        v = os.environ.get(f"RATRADE_{broker.upper()}_{f.upper()}", "")
        if v:
            out[f] = v.strip()
    return out


def _get_config(broker: str) -> dict:
    db = Database.get_instance()
    cfg = {}
    row = db.fetch_one("SELECT setting_value FROM settings WHERE setting_key=?", [f"broker_{broker}"])
    if row and row.get("setting_value"):
        try:
            loaded = json.loads(row["setting_value"])
            # Strip already-saved values too (old trailing-space entries)
            cfg = {k: (v.strip() if isinstance(v, str) else v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}
        except Exception:
            cfg = {}
    # Env wins over DB so redeploys never lose credentials
    try:
        cfg.update(_env_config(broker))
    except Exception:
        pass
    return cfg


def _save_config(broker: str, config: dict):
    db = Database.get_instance()
    try:
        config = {k: (v.strip() if isinstance(v, str) else v) for k, v in (config or {}).items()}
    except Exception:
        pass
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
        try:
            from_env = sorted(_env_config(key).keys())
        except Exception:
            from_env = []
        brokers.append({
            "key": key,
            "name": info["name"],
            "icon": info["icon"],
            "color": info["color"],
            "desc": info["desc"],
            "fields": info["fields"],
            "configured": configured,
            "config": config,
            "from_env": from_env,
        })
    return {"brokers": brokers}


@router.post("/save-config")
def save_config(req: BrokerConfig):
    if req.broker not in BROKER_DEFAULTS:
        return {"success": False, "error": "Invalid broker"}
    config = req.config
    if req.broker == "fyers" and config.get("redirect_uri", "").strip() == "":
        config["redirect_uri"] = _default_redirect()
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


def _default_redirect() -> str:
    import os
    base = (os.environ.get("SELF_URL") or "https://ratrade.onrender.com").rstrip("/")
    return base + "/api/broker/fyers-callback"


@router.get("/fyers-callback")
async def fyers_callback(request: Request, code: str = "", state: str = ""):
    """Fyers v3 redirects here after login with ?auth_code=...&state=...
    (older docs say ?code=... - accept both). Exchange auth_code for
    access_token (24h) and store in DB. Register this exact URL in the Fyers
    app settings as Redirect URI."""
    from fastapi.responses import HTMLResponse
    try:
        qp = dict(request.query_params) if request is not None else {}
    except Exception:
        qp = {}
    # Fyers v3 sends auth_code; accept code too. Also surface s/error flags.
    auth_code = (code or "").strip() or (qp.get("auth_code") or "").strip()
    err_flag = (qp.get("error") or qp.get("s") or "").strip()
    config = _get_config("fyers")
    if not auth_code:
        detail = ""
        if err_flag:
            detail = f"<p>Fyers said: <code>{err_flag}</code> (access denied or app not approved?)</p>"
        return HTMLResponse("<h3>Fyers login failed: no auth code received.</h3>" + detail + "<p>Go back and click 'OAuth Login' again. Make sure the Redirect URI in your Fyers app settings EXACTLY matches: <code>/api/broker/fyers-callback</code> on your site (no extra / at end).</p>", status_code=400)
    if not config:
        return HTMLResponse("<h3>Fyers not configured.</h3><p>Set App ID + Secret in RaTrade Brokers tab first.</p>", status_code=400)
    fy = FyersV3(
        app_id=config.get("app_id", ""),
        secret_key=config.get("secret", ""),
        redirect_uri=config.get("redirect_uri", ""),
    )
    result = await fy.generate_token(auth_code)
    if result["success"]:
        config["access_token"] = fy.access_token
        if fy.refresh_token:
            config["refresh_token"] = fy.refresh_token
        _save_config("fyers", config)
        return HTMLResponse("<h3 style='color:green'>Fyers connected! Token saved for 24 hours.</h3><p>You can close this tab and return to RaTrade → Brokers.</p>")
    return HTMLResponse(f"<h3 style='color:red'>Fyers login failed.</h3><p>{result['error']}</p><p>Check App ID, Secret and Redirect URI match your Fyers app settings.</p>", status_code=400)


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
        # Don't auto-refresh on Connect - just verify token exists. Refresh is
        # explicit via Refresh Tokens button and auto on 401. This avoids
        # "Invalid Request" noise when Fyers refresh needs PIN or method mismatch.
        if config.get("access_token"):
            return {"success": True, "message": "Fyers connected (token valid - use Refresh Tokens if expired)"}
        if config.get("refresh_token"):
            # Try refresh once, but don't fail Connect if it errors - token may still be valid
            try:
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
                    return {"success": True, "message": "Fyers connected (token auto-refreshed)"}
            except Exception:
                pass
        return {"success": False, "error": "Fyers needs OAuth login or Access Token. Click 'Login with Fyers'."}

    if req.broker == "dhan":
        if not config.get("access_token") or not config.get("client_id"):
            return {"success": False, "error": "Dhan needs Client ID + Access Token. Paste both in Setup."}
        # REAL validation: fake "connected" is what caused confusion earlier.
        try:
            from core.services.broker_dhan_live import DhanLive
            dl = DhanLive(client_id=config.get("client_id", ""),
                          access_token=config.get("access_token", ""))
            v = await dl.validate()
            if not v.get("success") and "807" in str(v.get("raw", "")):
                # Expired: one renew attempt with the stored token before giving up
                rr = await dl.renew_token()
                if rr.get("success") and rr.get("access_token"):
                    config["access_token"] = rr["access_token"]
                    _save_config("dhan", config)
                    return {"success": True, "message": "Dhan connected! Expired token auto-renewed (no manual login)."}
        except Exception as e:
            return {"success": False, "error": f"Dhan check crashed: {str(e)[:150]}"}
        if v.get("success"):
            return {"success": True, "message": "Dhan connected! Token verified live with Dhan."}
        return {"success": False, "error": v.get("error")}

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
            # Renew with ACTIVE token first (no manual login); fall back to cached.
            try:
                from core.services.broker_dhan_live import DhanLive
                dl = DhanLive(client_id=config.get("client_id", ""),
                              access_token=config.get("access_token", ""))
                rr = await dl.renew_token()
                if rr.get("success") and rr.get("access_token"):
                    config["access_token"] = rr["access_token"]
                    _save_config("dhan", config)
                    results[key] = {"success": True, "message": "Token renewed (no manual login)"}
                else:
                    results[key] = {"success": bool(config.get("access_token")),
                                    "message": "Renew failed — manual login needed" if not config.get("access_token") else "Cached token kept (renew failed: %s)" % str(rr.get("error", ""))[:120]}
            except Exception as e:
                results[key] = {"success": bool(config.get("access_token")), "error": str(e)[:150]}
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


async def _estimate_margin(broker: str, cfg: dict, preview: dict,
                           symbol: str, opt: str, strike: float) -> dict:
    """Required margin preview per broker (dry-run only, never orders).
    Dhan: official margin calculator. Fyers/Angel: best-effort official
    endpoints. Shoonya: no public margin API -> unavailable note."""
    def _num(x):
        try:
            return round(float(x), 2)
        except Exception:
            return None

    def _pick_margin(obj):
        if not isinstance(obj, dict):
            return None
        for k in ("totalMargin", "total_margin", "totalmargin", "marginRequired",
                  "margin_required", "requiredMargin"):
            v = _num(obj.get(k))
            if v:
                return v
        span = _num(obj.get("spanMargin", obj.get("span_margin", 0))) or 0
        expo = _num(obj.get("exposureMargin", obj.get("exposure_margin", 0))) or 0
        if span or expo:
            return round(span + expo, 2)
        return None

    # Premium estimate for price-sensitive calculators
    px = 0.0
    try:
        from core.models.trade_model import TradeModel as _TM
        px = float(_TM().get_option_premium(
            symbol, opt, strike, preview.get("expiry", "")) or 0)
    except Exception:
        px = 0.0
    if px <= 0:
        try:
            from core.services.live_market_data import LiveMarketData as _LM
            from utils.helpers import model_premium as _mp
            spot = _LM().get_spot_price(symbol)
            px = float(_mp(spot or strike, strike, 7, opt, symbol=symbol)) if spot else 50.0
        except Exception:
            px = 50.0
    qty = int(preview.get("quantity", 0) or 0)
    side = str(preview.get("side", "BUY")).upper()

    if broker == "dhan":
        try:
            from core.services.broker_dhan_live import DhanLive
            dl = DhanLive(client_id=cfg.get("client_id", ""),
                          access_token=cfg.get("access_token", ""))
            res = await dl._request("POST", "/margincalculator", data={
                "dhanClientId": dl.client_id,
                "exchangeSegment": "NSE_FO",
                "transactionType": side,
                "quantity": qty,
                "productType": preview.get("product", "INTRADAY"),
                "securityId": str(preview.get("security_id", "")),
                "price": px,
            })
            if res.get("success"):
                m = _pick_margin(res.get("data") or {})
                if m:
                    return {"required": m, "note": "Dhan SPAN+exposure estimate"}
            return {"required": None,
                    "note": f"Dhan margin unavailable ({str(res.get('error', ''))[:100]})"}
        except Exception as e:
            return {"required": None, "note": f"Dhan margin error: {str(e)[:100]}"}

    if broker == "fyers":
        try:
            from core.services.broker_fyers import FyersV3
            c = cfg or {}
            fy = FyersV3(app_id=c.get("app_id", ""), secret_key=c.get("secret", ""),
                         redirect_uri=c.get("redirect_uri", ""),
                         access_token=c.get("access_token", ""),
                         refresh_token=c.get("refresh_token", ""))
            res = await fy._request("POST", "/api/v3/margins", data={
                "symbol": preview.get("symbol", ""), "qty": qty,
                "type": 1, "side": 1 if side == "BUY" else -1,
                "productType": "INTRADAY", "limitPrice": px, "stopPrice": 0,
                "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
            })
            if res.get("success"):
                m = _pick_margin(res.get("data") or {})
                if m:
                    return {"required": m, "note": "Fyers margin estimate"}
            return {"required": None,
                    "note": f"Fyers margin unavailable ({str(res.get('error', ''))[:100]})"}
        except Exception as e:
            return {"required": None, "note": f"Fyers margin error: {str(e)[:100]}"}

    if broker == "angel":
        try:
            from core.services.broker_angel_live import AngelLive
            c = cfg or {}
            an = AngelLive(api_key=c.get("api_key", ""), client_code=c.get("client_code", ""),
                           password=c.get("password", ""), totp_secret=c.get("totp_secret", ""))
            an.jwt = c.get("access_token", "")
            res = await an._post("/rest/secure/angelbroking/margin/v1/batch", {"positionList": [{
                "exchange": "NFO", "symboltoken": str(preview.get("symboltoken", "")),
                "tradingsymbol": preview.get("symbol", ""), "transactiontype": side,
                "quantity": str(qty), "price": str(px),
                "producttype": preview.get("product", "INTRADAY"),
                "triggerprice": "0",
            }]})
            if res.get("success"):
                data = res.get("data") or {}
                rows = data if isinstance(data, list) else data.get("data") or []
                row = rows[0] if rows else data
                m = _pick_margin(row if isinstance(row, dict) else {})
                if m:
                    return {"required": m, "note": "Angel margin estimate"}
            return {"required": None,
                    "note": f"Angel margin unavailable ({str(res.get('error', ''))[:100]})"}
        except Exception as e:
            return {"required": None, "note": f"Angel margin error: {str(e)[:100]}"}

    return {"required": None, "note": "Shoonya has no public margin API — check RMS/Span in app"}


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
        # Global strike alignment
        try:
            from utils.helpers import align_strike_price
            strike = align_strike_price(symbol, strike)
        except Exception:
            pass
        exp_hint = req.expiry or "weekly"
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
        # ---- Universal resolution: NEVER route an unvalidated expiry ----
        try:
            from core.services.symbol_resolver import resolve_contract
        except Exception as e:
            return {"error": f"Resolver missing: {str(e)[:120]}"}
        try:
            _res = await resolve_contract(broker, _get_config(broker) or {},
                                          symbol, exp_hint, strike, opt)
        except Exception as e:
            return {"error": f"{broker} resolution crashed: {str(e)[:150]}"}
        if not _res.get("ok"):
            _msg = str(_res.get("error", "contract not resolved"))
            _av = _res.get("available_expiries") or []
            if _av:
                _msg += f" Valid expiries: {', '.join(_av[:8])}"
            return {"error": f"{broker}: {_msg} - order NOT placed."}
        exp_ymd = _res.get("expiry_used", "")
        _refs = _res.get("refs", {})
        _tt = str(req.trade_type or "intraday").lower()
        if broker == "fyers":
            preview = {"broker": "fyers", "symbol": _refs.get("symbol", ""), "underlying": symbol,
                       "strike": strike, "expiry": exp_ymd, "side": txn,
                       "lots": lots, "lot_size": lot, "quantity": broker_qty,
                       "order_type": "MARKET", "product": "INTRADAY",
                       "source": _res.get("source", "")}
        elif broker == "dhan":
            _dlot = int(_refs.get("lot_size") or 0) or lot
            preview = {"broker": "dhan", "security_id": _refs.get("security_id", ""),
                       "resolved_symbol": _refs.get("symbol", ""), "underlying": symbol,
                       "strike": strike, "expiry": exp_ymd, "side": txn,
                       "lots": lots, "lot_size": _dlot, "quantity": lots * _dlot,
                       "order_type": "MARKET",
                       "product": "MARGIN" if _tt == "positional" else "INTRADAY",
                       "source": _res.get("source", "")}
            broker_qty = lots * _dlot
            lot = _dlot
        elif broker == "angel":
            preview = {"broker": "angel", "symbol": _refs.get("tradingsymbol", ""),
                       "symboltoken": _refs.get("symboltoken", ""), "underlying": symbol,
                       "strike": strike, "expiry": exp_ymd, "side": txn,
                       "lots": lots, "lot_size": lot, "quantity": broker_qty,
                       "order_type": "MARKET",
                       "product": "MARGIN" if _tt == "positional" else "INTRADAY",
                       "source": _res.get("source", "")}
        elif broker == "shoonya":
            _slot = int(_refs.get("lot_size") or 0) or lot
            preview = {"broker": "shoonya", "symbol": _refs.get("tsym", ""),
                       "token": _refs.get("token", ""), "underlying": symbol,
                       "strike": strike, "expiry": exp_ymd, "side": txn,
                       "lots": lots, "lot_size": _slot, "quantity": lots * _slot,
                       "order_type": "MKT",
                       "product": "M" if _tt == "positional" else "I",
                       "source": _res.get("source", "")}
            broker_qty = lots * _slot
            lot = _slot
        if req.dry_run:
            try:
                _req_exp = str(exp_hint or "")[:10]
                if _req_exp and exp_ymd and _req_exp != exp_ymd[:10]:
                    preview["expiry_requested"] = _req_exp
                    preview["expiry_note"] = (f"Requested {_req_exp} does not exist — "
                                              f"snapped to live expiry {exp_ymd[:10]}")
                # Strike snap note (e.g., 7740 -> 7700)
                try:
                    if "strike-snapped" in str(preview.get("source","")):
                        _res_sym = str(preview.get("resolved_symbol") or preview.get("symbol") or "")
                        _used = None
                        for part in _res_sym.replace("PE","-PE").replace("CE","-CE").split("-"):
                            try:
                                _used = float(part.strip())
                                if 100 <= _used <= 100000:
                                    break
                            except: continue
                        if _used and abs(_used - float(strike)) > 0.01:
                            preview["strike_requested"] = float(strike)
                            preview["strike_used"] = _used
                            preview["strike_note"] = f"Requested strike {int(float(strike))} not available — snapped to {int(_used)}"
                except: pass
                preview["margin"] = await _estimate_margin(
                    broker, _get_config(broker) or {}, preview, symbol, opt, strike)
            except Exception:
                preview["margin"] = {"required": None, "note": "margin unavailable"}
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
            res = await fy.place_order(preview["symbol"], txn, broker_qty, order_type="MARKET")
            if not res.get("success"):
                return {"error": f"Fyers rejected order: {res.get('error') or res.get('data')}",
                        "preview": preview, "broker_response": res.get("data", {})}
            order_id = str((res.get("data") or {}).get("order_id", ""))
            broker_ref = preview["symbol"]
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