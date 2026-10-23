from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import Optional, Dict, Any
import json
import asyncio
from core.models.database import Database
from core.services.broker_fyers import FyersV3

router = APIRouter()

# ---------------- Static-IP egress proxy (broker IP pinning) ----------------

class EgressConfig(BaseModel):
    enabled: bool = True
    url: str = ""
    token: str = ""
    ip: str = ""


@router.get("/egress")
def egress_get():
    """Proxy config status (token masked) + kahan se aa raha hai (env/db)."""
    try:
        from core.services.egress_proxy import status
        return {"success": True, "egress": status()}
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


@router.post("/egress")
def egress_save(req: EgressConfig):
    """UI se proxy save/settings-table me (env vars zyada priority hote hain)."""
    try:
        url = (req.url or "").strip().rstrip("/")
        ip = (req.ip or "").strip()
        token = (req.token or "").strip()
        if req.enabled and not (url.startswith(("http://", "https://")) and token and ip):
            return {"success": False, "error": "Proxy URL (https://...), token aur IPv6 teeno bharo (ya enabled off chhod do)"}
        payload = {"enabled": bool(req.enabled), "url": url, "token": token, "ip": ip}
        db = Database.get_instance()
        existing = db.fetch_one("SELECT setting_key FROM settings WHERE setting_key='egress_proxy'")
        if existing:
            db.execute("UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key=?",
                       [json.dumps(payload), "egress_proxy"])
        else:
            db.execute("INSERT INTO settings (setting_key, setting_value) VALUES (?, ?)",
                       ["egress_proxy", json.dumps(payload)])
        from core.services.egress_proxy import status
        return {"success": True, "message": "Egress proxy saved", "egress": status()}
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


@router.post("/egress-test")
async def egress_test():
    """Proxy sahi chal raha hai? Snipy free plan sirf broker-domains allow
    karta hai (ipify/outside host 403) - isliye test target api.kite.trade
    (snipy ke apne doc-sample jaisa "take the red pill" 200 body)."""
    try:
        import httpx
        out = {}
        try:
            from core.services.egress_proxy import cfg, httpx_kwargs
            c = cfg()
            kw = httpx_kwargs()
            out["proxy_on"] = bool(c.get("configured"))
            out["proxy_url"] = c.get("url", "")
            out["whitelisted_ip"] = c.get("ip", "")
            if kw:
                async with httpx.AsyncClient(timeout=25, **kw) as cl:
                    r = await cl.get("https://api.kite.trade/")
                out["proxy_ok"] = (r.status_code == 200 and
                                   "kite" in (r.text or "").lower())
                if not out["proxy_ok"]:
                    out["proxy_detail"] = f"HTTP {r.status_code} {(r.text or '')[:120]}"
            else:
                out["proxy_ok"] = False
                out["proxy_detail"] = "proxy off"
        except Exception as e:
            out["proxy_ok"] = False
            out["proxy_error"] = str(e)[:200]
        try:
            async with httpx.AsyncClient(timeout=15) as cl:
                r2 = await cl.get("https://api.ipify.org?format=json")
            out["direct_ip"] = ((r2.json() or {}).get("ip") if r2.status_code == 200 else "") or ""
        except Exception as e:
            out["direct_error"] = str(e)[:200]
        return {"success": True, **out}
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}



BROKER_DEFAULTS = {
    "fyers": {"name": "Fyers", "icon": "bi-currency-exchange", "color": "text-danger", "fields": ["app_id", "secret", "redirect_uri", "access_token", "refresh_token", "pin", "fy_id", "totp_secret"], "desc": "App ID + Secret + TOTP secret - daily token AUTO-login (Historical OHLC + LIVE orders)"},
    "bigul": {"name": "Bigul", "icon": "bi-bullseye", "color": "text-warning", "fields": ["client_id", "password", "api_key", "totp_secret"], "desc": "Mobile se login — OTP auto-fetch, token auto-connect"},
}

# API-only brokers (UI me nahi dikhte) - direct API calls ke liye.
BROKER_INTERNAL = {
    "angel": {"name": "Angel One", "icon": "bi-bullseye", "color": "text-info", "fields": ["api_key", "client_code", "password", "totp_secret"], "desc": "SmartAPI"},
    "shoonya": {"name": "Shoonya", "icon": "bi-bullseye", "color": "text-info", "fields": ["uid", "pwd", "vc", "apikey", "secret"], "desc": "Noren"},
}

# Live-order priority: FYERS is the MAIN broker (#1), Bigul next.
BROKER_ORDER = ("fyers", "bigul", "angel", "shoonya")

BROKER_FIELD_LABELS = {
    "fyers": {"app_id": "App ID (MyAPI dashboard se, format XXXXXXXXX-100)", "secret": "Secret Key", "redirect_uri": "Redirect URI (Wahan sirf ratrade-iwi2.onrender.com/api/broker/fyers-callback hona chahiye)", "access_token": "Access Token (auto-fill ka rahega)", "refresh_token": "Refresh Token (auto-fill ka rahega)", "pin": "Fyers Trading PIN (4-digit, auto-login ke liye)", "fy_id": "Fyers Client ID (jaise ABCD1234 - daily auto-login ke liye)", "totp_secret": "TOTP 2FA Secret (Fyers profile > 2FA TOTP se - daily auto-login ke liye)"},
    "bigul": {"client_id": "Client ID", "password": "Password", "api_key": "API Key", "totp_secret": "TOTP Secret"},
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
    strategy_id: int = 0


class LiveToggleRequest(BaseModel):
    enabled: bool = False


def _auto_live_broker() -> str:
    """Real broker connected -> LIVE stays ON by itself (no toggle needed).
    Priority: Fyers (main broker) first, then Bigul."""
    try:
        for b in BROKER_ORDER:
            if _real_token(b):
                return b
    except Exception:
        pass
    return ""


def _live_enabled() -> bool:
    try:
        if _auto_live_broker():
            return True
        db = Database.get_instance()
        row = db.fetch_one("SELECT setting_value FROM settings WHERE setting_key='live_trading_enabled'")
        return (row.get("setting_value") if row else "0") == "1"
    except Exception:
        return False


def _real_token(broker: str) -> bool:
    """A 'real' token excludes the app's simulated SIM-/ANG- tokens."""
    tok = (_get_config(broker) or {}).get("access_token", "")
    return bool(tok) and not str(tok).startswith(("SIM-", "ANG-"))


def _is_disconnected(broker: str) -> bool:
    """User ne 'Disconnect' daba diya -> ye broker ab use nahi hoga, chahe
    env vars me creds maujood hon (env seed se wapas revive nahi hona chahiye)."""
    try:
        db = Database.get_instance()
        row = db.fetch_one("SELECT setting_value FROM settings WHERE setting_key=?",
                           [f"broker_disconnected_{broker}"])
        return str((row or {}).get("setting_value") or "") == "1"
    except Exception:
        return False


def _set_disconnected(broker: str, flag: bool):
    """Persist the disconnect flag (1 = disconnected, 0 = connected again)."""
    db = Database.get_instance()
    key = f"broker_disconnected_{broker}"
    try:
        n = db.execute("UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key=?",
                       ["1" if flag else "0", key])
    except Exception:
        n = 0
    if not n:
        try:
            row = db.fetch_one("SELECT setting_key FROM settings WHERE setting_key=?", [key])
            if row:
                db.execute("UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key=?",
                           ["1" if flag else "0", key])
            else:
                db.execute("INSERT INTO settings (setting_key, setting_value) VALUES (?, ?)",
                           [key, "1" if flag else "0"])
        except Exception:
            pass


def _clear_broker_row(broker: str):
    """Delete the stored credentials row for a broker."""
    try:
        db = Database.get_instance()
        db.execute("DELETE FROM settings WHERE setting_key=?", [f"broker_{broker}"])
    except Exception:
        pass


def _resolve_expiry(symbol: str, hint: str) -> str:
    """Return YYYY-MM-DD expiry: YYYY-MM-DD hint as-is; Weekly -> next expiry
    weekday (Tue for NSE F&O, Thu for SENSEX); Monthly (or stocks) -> last such
    weekday of month. (NSE moved expiries: NIFTY Tue, BANKNIFTY/FINNIFTY monthly
    last-Tue - verified against the broker scrip master Sep 2026.)"""
    import datetime as _dt
    import calendar as _cal
    hint = str(hint or "").strip()
    try:
        _d = _dt.datetime.strptime(hint[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        pass
    else:
        # Explicit date snap guard: NON-existent expiry date (10-29 Thursday
        # NIFTY jaisa phantom) par broker order/cquote REJECT - paper & live
        # same contract par price ho, isliye nearest real weekly par le jao.
        try:
            from core.services.contract_pricer import _snap_future_expiry as _snap
            return _snap(symbol, _d)
        except Exception:
            return _d
    today = (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).date()
    wd = 3 if symbol.upper() == "SENSEX" else 1  # Thu vs Tue
    weekly_idx = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"}
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
    real = [b for b in BROKER_ORDER if _real_token(b)]
    auto = _auto_live_broker()
    return {"enabled": _live_enabled(), "real_brokers": real, "auto": bool(auto),
            "auto_broker": auto,
            "note": "Fyers is the main broker (priority #1). Dry-run first to verify symbol/token."}


@router.post("/disconnect")
def disconnect_broker(req: ConnectRequest):
    """Broker ka session tod do: stored creds delete + disconnect flag ON
    (env vars se wapas revive nahi hoga). Re-enable ke liye Setup/Connect."""
    broker = (req.broker or "").lower()
    if broker not in BROKER_DEFAULTS and broker not in BROKER_INTERNAL:
        return {"success": False, "error": f"Unknown broker '{req.broker}'"}
    had = bool(_get_config(broker))
    _clear_broker_row(broker)
    _set_disconnected(broker, True)
    name = (BROKER_DEFAULTS.get(broker) or BROKER_INTERNAL.get(broker) or {}).get("name", broker)
    return {"success": True, "broker": broker, "disconnected": True,
            "had_session": had,
            "message": f"{name} disconnected. LIVE band (agar koi aur broker connected nahi hai). "
                       f"Re-connect ke liye Setup + Connect dabao."}


@router.post("/live-toggle")
def live_toggle(req: LiveToggleRequest):
    # UPDATE-first (never triggers the settings-table RETURNING-id path);
    # INSERT only when the row is missing (now safe via execute fallback).
    db = Database.get_instance()
    try:
        n = db.execute("UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key='live_trading_enabled'",
                       ["1" if req.enabled else "0"])
    except Exception:
        n = 0
    if not n:
        try:
            row = db.fetch_one("SELECT setting_key FROM settings WHERE setting_key='live_trading_enabled'")
            if not row:
                db.execute("INSERT INTO settings (setting_key, setting_value) VALUES ('live_trading_enabled', ?)",
                           ["1" if req.enabled else "0"])
            else:
                db.execute("UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key='live_trading_enabled'",
                           ["1" if req.enabled else "0"])
        except Exception as e:
            return {"error": f"live-toggle failed: {e}"[:200]}
    # A connected real broker keeps LIVE always-ON: explicit disable is
    # refused so trading never silently goes dark while connected.
    auto = _auto_live_broker()
    if auto and not req.enabled:
        return {"enabled": True, "auto": True, "auto_broker": auto,
                "note": f"LIVE stays ON - {auto} connected (disconnect broker to disable)"}
    return {"enabled": bool(req.enabled), "auto": bool(auto), "auto_broker": auto}


def _env_config(broker: str) -> dict:
    """Broker credentials from environment variables (survive Render redeploys
    + opencode edits, unlike the ephemeral SQLite DB which wipes on deploy).
    Naming: RATRADE_<BROKER>_<FIELD> e.g. RATRADE_FYERS_APP_ID.
    Also accepted: <BROKER>_<FIELD> (FYERS_APP_ID) and per-field aliases
    (RATRADE_FYERS_SECRET_ID / _SECRET_KEY / _APP_SECRET -> 'secret')."""
    import os
    # Forgiving aliases: common misspellings/naming variants still work.
    _ENV_ALIASES = {
        "redirect_uri": ["redirect_url", "callback_url"],
        "secret": ["secret_id", "secret_key", "app_secret"],
        "app_id": ["appid", "client_id"],
        "fy_id": ["fyers_id", "user_id", "client_id", "clientid"],
        "totp_secret": ["totp", "totp_key", "authsecret"],
    }
    try:
        fields = (BROKER_DEFAULTS.get(broker, {}) or {}).get("fields", []) or []
    except Exception:
        fields = []
    # Normalized lookup: strip whitespace + uppercase, so keys with
    # accidental spaces/case issues (e.g. pasted from chat) still match.
    _norm = {}
    for k, v in os.environ.items():
        try:
            _norm[str(k).strip().upper()] = v
        except Exception:
            pass
    _b = broker.upper()
    out = {}
    for f in fields:
        # 1) RATRADE_<BROKER>_<FIELD>  2) <BROKER>_<FIELD>  3) aliases
        cands = [f"RATRADE_{_b}_{f.upper()}", f"{_b}_{f.upper()}"]
        cands += [f"RATRADE_{_b}_{a.upper()}" for a in _ENV_ALIASES.get(f, [])]
        cands += [f"{_b}_{a.upper()}" for a in _ENV_ALIASES.get(f, [])]
        for c in cands:
            v = _norm.get(c, "")
            if v and str(v).strip():
                out[f] = str(v).strip()
                break
    return out


def _normalize_broker_cfg(broker: str, cfg: dict) -> dict:
    """Fyers App ID must be '<appId>-<appType>' (e.g. ABCDEFGH-100) - the API
    answers 'invalid clientId' without the suffix. Normalize everywhere so
    DB values, dashboard values and env values all behave the same."""
    if broker != "fyers" or not isinstance(cfg, dict):
        return cfg
    try:
        from core.services.broker_fyers import FyersV3
        aid = str(cfg.get("app_id") or "").strip()
        if aid:
            cfg["app_id"] = FyersV3.normalize_app_id(aid)
    except Exception:
        pass
    return cfg


# Fields where a Render env value is AUTHORITATIVE over the DB copy (creds).
# Tokens stay DB-first (the browser OAuth writes them there).
_ENV_PRIORITY_FIELDS = {
    "fyers": {"app_id", "secret", "redirect_uri", "pin", "fy_id", "totp_secret"},
}


def _get_config(broker: str) -> dict:
    # Disconnected broker: koi bhi cred (DB ya env) use nahi hoti - jab tak
    # user dobara Setup/Connect na kare.
    if _is_disconnected(broker):
        return {}
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
    # DB wins over env: UI connects + token-keeper renewals are the live
    # truth (env/dashboard values are only the first-boot seed). The keeper
    # syncs any valid env rotation INTO the DB by itself.
    # Exception: credential fields listed in _ENV_PRIORITY_FIELDS (Fyers
    # App ID/Secret) - the Render env is the source of truth for those, so
    # a rotated env value is never shadowed by a stale DB copy.
    try:
        env = _env_config(broker) or {}
        prio = _ENV_PRIORITY_FIELDS.get(broker) or set()
        for k, v in env.items():
            if k in prio:
                cfg[k] = v
            elif k not in cfg or not cfg.get(k):
                cfg[k] = v
    except Exception:
        pass
    return _normalize_broker_cfg(broker, cfg)


def _save_config(broker: str, config: dict):
    # Naya credential save = broker dobara connected (disconnect flag hatao).
    try:
        if _is_disconnected(broker):
            _set_disconnected(broker, False)
    except Exception:
        pass
    db = Database.get_instance()
    try:
        config = {k: (v.strip() if isinstance(v, str) else v) for k, v in (config or {}).items()}
    except Exception:
        pass
    config = _normalize_broker_cfg(broker, config)
    existing = db.fetch_one("SELECT setting_key FROM settings WHERE setting_key=?", [f"broker_{broker}"])
    if existing:
        db.execute("UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key=?", [json.dumps(config), f"broker_{broker}"])
    else:
        db.execute("INSERT INTO settings (setting_key, setting_value) VALUES (?, ?)", [f"broker_{broker}", json.dumps(config)])


def _fyers_client(config: dict):
    """Build a FyersV3 client from saved/env config (app id normalized).
    fy_id + totp_secret pass hote hain daily headless AUTO-LOGIN ke liye
    (refresh-token API SEBI-blocked hai)."""
    from core.services.broker_fyers import FyersV3
    cfg = _normalize_broker_cfg("fyers", dict(config or {}))
    return FyersV3(
        app_id=cfg.get("app_id", ""),
        secret_key=cfg.get("secret", ""),
        redirect_uri=(cfg.get("redirect_uri") or "").strip() or _default_redirect(),
        access_token=cfg.get("access_token", ""),
        refresh_token=cfg.get("refresh_token", ""),
        pin=cfg.get("pin", ""),
        fy_id=cfg.get("fy_id", ""),
        totp_secret=cfg.get("totp_secret", ""),
    )


def _is_configured(broker: str) -> bool:
    return bool(_get_config(broker))


@router.get("/bigul-auth-url")
def bigul_auth_url():
    return {"success": True, "url": "https://bigul.co/login"}

@router.get("/bigul-callback")
async def bigul_callback(request: Request):
    from fastapi.responses import HTMLResponse
    try:
        qp = dict(request.query_params)
    except Exception:
        qp = {}
    tok = (qp.get("access_token") or qp.get("code") or qp.get("token") or "").strip()
    if tok:
        cfg = _get_config("bigul") or {}
        cfg["access_token"] = tok
        _save_config("bigul", cfg)
        return HTMLResponse("<h3 style='color:green'>Bigul connected! Token auto-saved.</h3><p>Close this tab and return to RaTrade.</p>")
    return HTMLResponse("<h3>Bigul login — mobile OTP se login karo, phir token auto-fetch hoga. Agar token URL me aaye to wapas yahan redirect hoga.</h3><p>Manual: Bigul app se API token copy karke RaTrade Bigul Setup me paste karo, Auto-connect dabao.</p>")

@router.get("/angel-auth-url")
def angel_auth_url():
    """Returns the Angel One login URL for OAuth flow."""
    config = _get_config("angel")
    if not config or not config.get("client_code"):
        return {"success": False, "error": "Angel not configured. Set Client Code first."}
    return {"success": True, "url": "https://api.angelone.in/api/v2/oauth/authorize"}


@router.get("/angel-callback")
async def angel_callback(request: Request):
    """Auto-capture Angel One access_token after OAuth login via ?code= redirect."""
    from fastapi.responses import HTMLResponse
    try:
        qp = dict(request.query_params)
    except Exception:
        qp = {}
    code = (qp.get("code") or "").strip()
    if not code:
        return HTMLResponse("<h3 style='color:red'>No code received from Angel One.</h3><p>Login again or paste token <a href='/' style='color:#0d6efd'>back to RaTrade</a>.</p>", status_code=400)
    config = _get_config("angel")
    from core.services.broker_angel_live import AngelLive
    try:
        an = AngelLive(
            api_key=config.get("api_key", ""),
            client_code=config.get("client_code", ""),
            password=config.get("password", ""),
            totp_secret=config.get("totp_secret", "")
        )
        # Login with stored credentials + exchange OAuth code
        result = await an.login_by_oauth(code)
        if result["success"]:
            config["access_token"] = an.jwt
            config["refresh_token"] = an.refresh_token
            config["feed_token"] = an.feed_token
            _save_config("angel", config)
            return HTMLResponse("<h3 style='color:green'>Angel One connected! Token auto-saved.</h3><p>You can close this tab and return to RaTrade → Brokers.</p>")
        return HTMLResponse(f"<h3 style='color:red'>Angel One login failed.</h3><p>{result.get('error','Unknown error')}</p>", status_code=400)
    except Exception as e:
        return HTMLResponse(f"<h3 style='color:red'>Angel One login failed.</h3><p>{str(e)[:200]}</p>", status_code=400)


@router.post("/angel-callback")
async def angel_callback_post(req: dict):
    """POST version for frontend P() call — saves Angel token directly."""
    config = _get_config("angel")
    if isinstance(req, dict):
        if req.get("code"): config["access_token"] = req["code"]
        if req.get("access_token"): config["access_token"] = req["access_token"]
    _save_config("angel", config)
    return {"success": True, "message": "Angel token saved"}


@router.post("/auto-connect")
async def auto_connect_brokers():
    """Auto-connect all configured brokers (Fyers + Bigul) by validating
    tokens live. If a token is invalid, attempts refresh. Returns results."""
    results = []
    for key in ("fyers", "bigul"):
        config = _get_config(key)
        if not config:
            results.append({"broker": key, "status": "not_configured"})
            continue
        try:
            if key == "fyers":
                if not (config.get("app_id") and config.get("secret")):
                    results.append({"broker": key, "status": "failed",
                                    "error": "App ID + Secret missing (Setup ya env)"})
                    continue
                fy = _fyers_client(config)
                if fy.access_token:
                    v = await fy.validate()
                    if v.get("success"):
                        if fy.refreshed and fy.access_token:
                            config["access_token"] = fy.access_token
                            _save_config("fyers", config)
                        results.append({"broker": key, "status": "connected"})
                    elif fy.refresh_token:
                        rr = await fy.refresh_access_token()
                        if rr.get("success"):
                            config["access_token"] = fy.access_token
                            if fy.refresh_token:
                                config["refresh_token"] = fy.refresh_token
                            _save_config("fyers", config)
                            results.append({"broker": key, "status": "renewed"})
                        else:
                            results.append({"broker": key, "status": "needs_login",
                                            "error": str(rr.get("error",""))[:150]})
                    else:
                        results.append({"broker": key, "status": "needs_login",
                                        "error": str(v.get("error",""))[:150]})
                elif fy.refresh_token:
                    rr = await fy.refresh_access_token()
                    if rr.get("success"):
                        config["access_token"] = fy.access_token
                        if fy.refresh_token:
                            config["refresh_token"] = fy.refresh_token
                        _save_config("fyers", config)
                        results.append({"broker": key, "status": "renewed"})
                    else:
                        results.append({"broker": key, "status": "needs_login",
                                        "error": str(rr.get("error",""))[:150]})
                else:
                    results.append({"broker": key, "status": "needs_login",
                                    "error": "OAuth login required (browser)"})
            elif key == "bigul":
                from core.services.broker_angel_live import AngelLive
                an = AngelLive(api_key=config.get("api_key",""), client_code=config.get("client_id",""),
                               password=config.get("password",""), totp_secret=config.get("totp_secret",""))
                result = await an.login()
                if result.get("success"):
                    config["access_token"] = an.jwt
                    _save_config("bigul", config)
                    results.append({"broker": key, "status": "connected"})
                else:
                    results.append({"broker": key, "status": "failed", "error": result.get("error","")})
        except Exception as e:
            results.append({"broker": key, "status": "error", "error": str(e)[:150]})
    return {"success": True, "results": results}


@router.get("/broker-config")
def broker_config_get():
    """Get current broker config status for auto-connect."""
    configs = {}
    for key in BROKER_DEFAULTS:
        c = _get_config(key)
        configs[key] = {"configured": bool(c), "has_token": bool(c.get("access_token","")),
                        "disconnected": _is_disconnected(key)}
    return {"success": True, "configs": configs}


@router.get("/list")
def list_brokers():
    brokers = []
    # Order = priority: Fyers (#1, main broker) then Bigul.
    for key, info in BROKER_DEFAULTS.items():
        config = _get_config(key)
        configured = bool(config)
        disconnected = _is_disconnected(key)
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
            "connected": bool(configured and _real_token(key)),
            "disconnected": disconnected,
            "config": config,
            "from_env": from_env,
        })
    return {"brokers": brokers}


@router.post("/save-config")
def save_config(req: BrokerConfig):
    if req.broker not in BROKER_DEFAULTS and req.broker not in BROKER_INTERNAL:
        return {"success": False, "error": "Invalid broker"}
    config = req.config
    _save_config(req.broker, config)
    return {"success": True, "message": "Config saved"}


@router.post("/fyers-auth")
async def fyers_auth(req: AuthRequest):
    config = _get_config("fyers")
    if not config:
        return {"success": False, "error": "Fyers not configured. Setup first."}
    fy = _fyers_client(config)
    result = await fy.generate_token(req.code)
    if result["success"]:
        config["access_token"] = fy.access_token
        config["refresh_token"] = fy.refresh_token
        _save_config("fyers", config)
        return {"success": True, "message": "Fyers connected! Tokens auto-refresh daily."}
    return {"success": False, "error": result["error"]}


def _default_redirect() -> str:
    import os
    base = (os.environ.get("SELF_URL") or os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("RENDER_EXTERNAL_HOSTNAME") and f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}" or "https://ratrade-iwi2.onrender.com").rstrip("/")
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
    # Fyers v3 sends ?s=ok&code=200&auth_code=eyJ... (code=200 is HTTP status, NOT the auth code!)
    # Must prefer auth_code; plain `code` is numeric and will fail validation (code_len=3 bug).
    raw_code = (code or "").strip()
    raw_qp_code = (qp.get("code") or "").strip()
    raw_auth = (qp.get("auth_code") or "").strip()
    # Prefer JWT-looking value: contains '.' and long, otherwise use raw_auth
    auth_code = ""
    for cand in [raw_auth, raw_code, raw_qp_code]:
        if cand and ("." in cand or len(cand) > 20):
            auth_code = cand
            break
    if not auth_code:
        # fallback: if no JWT-looking, take longest non-empty (covers legacy ?code=<jwt>)
        candidates = [c for c in [raw_auth, raw_code, raw_qp_code] if c]
        if candidates:
            auth_code = max(candidates, key=len)
    # Detect Fyers error flag: s != ok
    s_flag = (qp.get("s") or "").strip().lower()
    if s_flag and s_flag not in ("ok", "success"):
        err_flag = (qp.get("message") or qp.get("error") or qp.get("s") or "").strip()
        dbg_qs = str(dict(qp))[:500] if qp else "empty"
        return HTMLResponse(f"<h3 style='color:red'>Fyers login rejected (s={s_flag}).</h3><p>{err_flag or 'User denied or app not approved'}</p><p style='font-size:11px;color:#6c757d'>Debug: {dbg_qs}</p><p>Expected Redirect URI: <code>{_default_redirect()}</code></p>", status_code=400)
    err_flag = (qp.get("error") or qp.get("message") or "").strip()
    # also capture state for debugging
    dbg_qs = str(dict(qp))[:500] if qp else "empty"
    config = _get_config("fyers")
    if not auth_code:
        detail = ""
        if err_flag:
            detail = f"<p>Fyers said: <code>{err_flag}</code> (access denied or app not approved?)</p>"
        detail += f"<p style='font-size:11px;color:#6c757d'>Debug: received params {dbg_qs}</p>"
        return HTMLResponse("<h3>Fyers login failed: no auth code received.</h3>" + detail + "<p>Go back and click 'OAuth Login' again. Make sure the Redirect URI in your Fyers app settings EXACTLY matches: <code>https://ratrade-iwi2.onrender.com/api/broker/fyers-callback</code> (no extra / at end). If you see 'blocked' on fyers.in, try mobile hotspot.</p>", status_code=400)
    if not config:
        return HTMLResponse("<h3>Fyers not configured.</h3><p>Set App ID + Secret in RaTrade Brokers tab first.</p>", status_code=400)
    ruri_cb = (config.get("redirect_uri") or "").strip() or _default_redirect()
    fy = _fyers_client({**config, "redirect_uri": ruri_cb})
    result = await fy.generate_token(auth_code)
    if result["success"]:
        config["access_token"] = fy.access_token
        if fy.refresh_token:
            config["refresh_token"] = fy.refresh_token
        _save_config("fyers", config)
        return HTMLResponse("<h3 style='color:green'>Fyers connected! Token saved for 24 hours.</h3><p>You can close this tab and return to RaTrade → Brokers.</p>")
    dbg = f"code_len={len(auth_code)}, hash_prefix={fy.app_id_hash()[:8]}..., params={dbg_qs}"
    return HTMLResponse(f"<h3 style='color:red'>Fyers login failed.</h3><p>{result['error']}</p><p style='font-size:11px;color:#6c757d'>Debug: {dbg}</p><p>Check App ID, Secret and Redirect URI match your Fyers app settings. Expected: <code>{ruri_cb}</code><br>Code single-use, 60s expiry - start FRESH OAuth, don't refresh this page.</p>", status_code=400)


@router.get("/fyers-auth-url")
def fyers_auth_url():
    config = _get_config("fyers")
    if not config:
        return {"success": False, "error": "Fyers not configured"}
    ruri = (config.get("redirect_uri") or "").strip() or _default_redirect()
    # use live host if env SELF_URL missing (covers old hosts)
    if "ratrade.onrender.com" in ruri and "ratrade-iwi2.onrender.com" not in ruri:
        try:
            import os
            if os.environ.get("RENDER_EXTERNAL_URL"):
                ruri = os.environ["RENDER_EXTERNAL_URL"].rstrip("/") + "/api/broker/fyers-callback"
        except: pass
    fy = _fyers_client({**config, "redirect_uri": ruri})
    return {"success": True, "url": fy.get_auth_url()}


@router.get("/fyers-history")
async def fyers_history(symbol: str = "NSE:NIFTY50-INDEX",
                        range_from: str = "", range_to: str = "",
                        resolution: str = "D", rows: int = 40):
    """Fyers API v3 Historical OHLC candles -> pandas DataFrame (IST datetime).
    Example: /api/broker/fyers-history?symbol=NSE:SBIN-EQ&range_from=2026-09-01&range_to=2026-10-20&resolution=5"""
    from core.services.fyers_data import FyersHistoryData, generate_access_token
    config = _get_config("fyers") or {}
    token = str(config.get("access_token", "") or "")
    if not config.get("app_id") or not token:
        return {"success": False,
                "error": "Fyers connected nahi hai - Brokers page par Setup + OAuth Login karo"}
    fh = FyersHistoryData(client_id=config.get("app_id", ""), access_token=token)
    resp = fh.history(symbol, range_from or "", range_to or "", resolution)
    if resp.get("s") != "ok":
        return {"success": False, "error": str(resp.get("message") or resp)[:220],
                "code": resp.get("code", "")}
    try:
        df = FyersHistoryData.to_dataframe(resp)
    except Exception as e:
        return {"success": False, "error": f"DataFrame banane me error: {str(e)[:150]}"}
    out = df.copy()
    recent = out.tail(max(1, min(int(rows or 40), len(out))))
    # numpy scalars (int64/float64) are NOT JSON serializable -> FastAPI 500.
    # to_dict(orient='records') boxes them into native python types.
    records = recent.to_dict(orient="records")
    clean_rows = []
    for rec in records:
        r = {k: (str(v) if k == "timestamp" else v) for k, v in rec.items()}
        clean_rows.append(r)
    return {"success": True, "symbol": symbol, "resolution": resolution,
            "range": [str(range_from)[:10], str(range_to)[:10]],
            "total_candles": int(len(out)),
            "columns": list(out.columns),
            "rows": clean_rows,
            }


@router.post("/connect")
async def connect_broker(req: ConnectRequest):
    if req.broker not in BROKER_DEFAULTS and req.broker not in BROKER_INTERNAL:
        return {"success": False, "error": "Invalid broker"}
    config = _get_config(req.broker)
    if not config:
        return {"success": False, "error": "No credentials configured. Click Setup first."}

    if req.broker == "fyers":
        # REAL validation: token is verified live against Fyers (profile ping)
        # so the UI never shows 'Connected' for a dead token.
        if not config.get("app_id") or not config.get("secret"):
            return {"success": False, "error": "Fyers App ID + Secret missing. Save Setup / env first."}
        fy = _fyers_client(config)
        if config.get("access_token"):
            v = await fy.validate()
            if v.get("success"):
                if fy.refreshed and fy.access_token:
                    config["access_token"] = fy.access_token
                    _save_config("fyers", config)
                return {"success": True, "message": "Fyers connected! Token verified live."}
            detail = str(v.get("error") or "")[:150]
            # Expired token -> try refresh (needs refresh_token, PIN optional)
            if config.get("refresh_token"):
                rr = await fy.refresh_access_token()
                if rr.get("success"):
                    config["access_token"] = fy.access_token
                    if fy.refresh_token:
                        config["refresh_token"] = fy.refresh_token
                    _save_config("fyers", config)
                    v2 = await fy.validate()
                    if v2.get("success"):
                        return {"success": True, "message": "Fyers connected! Token refreshed + verified."}
                    return {"success": False, "error": f"Token refreshed but verify failed: {str(v2.get('error',''))[:120]}"}
                return {"success": False, "error": f"{detail} | Refresh failed: {str(rr.get('error',''))[:120]}"}
            return {"success": False, "error": f"{detail} - Click 'OAuth Login' (browser) to connect again."}
        if config.get("refresh_token"):
            # Try refresh once, but don't fail Connect if it errors - token may still be valid
            fy2 = _fyers_client(config)
            result = await fy2.refresh_access_token()
            if result["success"]:
                config["access_token"] = fy2.access_token
                if fy2.refresh_token:
                    config["refresh_token"] = fy2.refresh_token
                _save_config("fyers", config)
                return {"success": True, "message": "Fyers connected (token auto-refreshed)"}
            return {"success": False, "error": f"Token refresh failed: {str(result.get('error',''))[:150]}"}
        return {"success": False, "error": "Fyers needs OAuth login. Click 'OAuth Login'."}

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


def _is_sebi_refresh_error(msg) -> bool:
    """Fyers 'refresh token api disabled (SEBI regulation)' style messages."""
    m = str(msg or "").lower()
    return ("sebi" in m or ("disabled" in m and "refresh" in m)
            or "regulation" in m)


@router.post("/refresh-tokens")
async def refresh_tokens():
    results = {}
    for key in BROKER_ORDER:
        config = _get_config(key)
        if not config:
            results[key] = {"success": False, "error": "Not configured"}
            continue
        if key == "fyers":
            fy = _fyers_client(config)
            if not fy.refresh_token:
                results[key] = {"success": False, "error": "No refresh token - OAuth login again"}
                continue
            result = await fy.refresh_access_token()
            if result["success"]:
                config["access_token"] = fy.access_token
                if fy.refresh_token:
                    config["refresh_token"] = fy.refresh_token
                _save_config("fyers", config)
                results[key] = {"success": True, "message": "Token refreshed"}
            elif result.get("sebi_disabled") or _is_sebi_refresh_error(result.get("error")):
                # Fyers ne refresh-token API band ki hai (SEBI guideline) -
                # ab seedhe HEADLESS AUTO-LOGIN (TOTP+PIN) try karo.
                try:

                    from core.services.fyers_auto_login import auto_login as _al
                    _alr = await _al(config)
                    if _alr.get("success") and _alr.get("access_token"):
                        config["access_token"] = _alr["access_token"]
                        if _alr.get("refresh_token"):
                            config["refresh_token"] = _alr["refresh_token"]
                        _save_config("fyers", config)
                        results[key] = {"success": True,
                                        "message": "AUTO-LOGIN ok (TOTP+PIN) — daily naya token, login ki zaroorat nahi"}
                    else:
                        results[key] = {"success": False,
                                        "error": (f"Refresh SEBI-blocked + AUTO-LOGIN fail: "
                                                  f"{str(_alr.get('error') or '')[:180]}. "
                                                  f"fy_id/totp_secret/pin Fyers card me bharo.")}
                except Exception as _ex:
                    results[key] = {"success": False,
                                    "error": f"Refresh SEBI-blocked, auto-login crash: {str(_ex)[:120]}"}
            else:
                results[key] = {"success": False, "error": result["error"]}
        elif key == "angel":
            # OAuth mode: access_token exists but no password
            if config.get("access_token") and not config.get("password"):
                results[key] = {"success": True, "message": "Token cached (OAuth mode)"}
            else:
                results[key] = {"success": bool(config.get("access_token")), "message": "Tokens cached"}
    return {"success": True, "results": results}


_TOKEN_CHECK_TS = {}
_TOKEN_CHECK_RES = {}


@router.get("/token-status")
async def token_status():
    """Real validity: the broker token is pinged live (60s cache) so the UI
    never shows 'Connected' for an expired token."""
    import time as _tt
    status = {}
    for key in BROKER_DEFAULTS:
        config = _get_config(key)
        tok = str(config.get("access_token", "") or "")
        if not (tok and not tok.startswith(("SIM-", "ANG-"))):
            status[key] = {"valid": False, "expires_at": "", "detail": "not connected"}
            continue
        if key == "fyers":
            # Live ping (profile) so 'Connected' never means 'token saved but dead'
            try:
                if _tt.time() - _TOKEN_CHECK_TS.get(key, 0) < 60 and key in _TOKEN_CHECK_RES:
                    status[key] = _TOKEN_CHECK_RES[key]
                    continue
                fy = _fyers_client(config)
                v = await fy.validate()
                res = {"valid": bool(v.get("success")), "expires_at": "",
                       "detail": str(v.get("detail") or v.get("error") or "")[:150]}
                if v.get("success") and fy.refreshed and fy.access_token:
                    config["access_token"] = fy.access_token
                    if fy.refresh_token:
                        config["refresh_token"] = fy.refresh_token
                    _save_config("fyers", config)
                    res["detail"] = "token valid (auto-refreshed)"
                _TOKEN_CHECK_TS[key] = _tt.time()
                _TOKEN_CHECK_RES[key] = res
                status[key] = res
            except Exception as e:
                status[key] = {"valid": False, "expires_at": "", "detail": str(e)[:120]}
        else:
            status[key] = {"valid": True, "expires_at": "", "detail": "saved (OAuth mode)"}
    return {"success": True, "status": status}


@router.post("/account")
async def view_account(req: ConnectRequest):
    if req.broker not in BROKER_DEFAULTS and req.broker not in BROKER_INTERNAL:
        return {"success": False, "error": "Invalid broker"}
    config = _get_config(req.broker)
    if not config:
        return {"success": False, "error": "Not connected. Click Setup first."}

    if req.broker == "fyers" and config.get("access_token"):
        fy = _fyers_client(config)
        funds = await fy.get_funds()
        positions = await fy.get_positions()
        holdings = await fy.get_holdings()
        if fy.refreshed and fy.access_token:
            config["access_token"] = fy.access_token
            if fy.refresh_token:
                config["refresh_token"] = fy.refresh_token
            _save_config("fyers", config)
        return {
            "success": True,
            "funds": funds.get("data", {}).get("fund_limit", [{}])[0] if funds.get("data", {}).get("fund_limit") else {"message": "Live fetch pending"},
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
    Fyers/Angel: best-effort official endpoints.
    Shoonya: no public margin API -> unavailable note."""
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


class MarginPreviewRequest(BaseModel):
    symbol: str
    option_type: str = "CE"
    transaction_type: str = "BUY"
    quantity: int = 1
    strike: float = 0
    expiry: str = ""
    trade_type: str = "intraday"
    mode: str = "paper"  # paper | live
    broker: str = ""


@router.post("/margin-preview")
async def margin_preview(req: MarginPreviewRequest):
    """Har order window me required margin dikhane ke liye.
    live + Fyers connected => official margin calculator.
    paper / no-broker => honest local estimate (BUY = premium×qty exact,
    SELL = ~12% underlying SPAN estimate). Kabhi order nahi lagata."""
    try:
        from utils.helpers import get_lot_size, get_strike_step, model_premium
        symbol = (req.symbol or "").upper()
        if not symbol:
            return {"error": "Symbol required"}
        opt = str(req.option_type or "CE").upper()
        txn = str(req.transaction_type or "BUY").upper()
        lots = max(1, int(req.quantity or 1))
        lot = get_lot_size(symbol)
        strike = float(req.strike or 0)
        if strike <= 0:
            try:
                from core.services.live_market_data import LiveMarketData as _LM
                spot0 = _LM().get_spot_price(symbol) or 0
                step0 = get_strike_step(symbol)
                strike = round(spot0 / step0) * step0 if spot0 and step0 else 0
            except Exception:
                strike = 0
        if strike <= 0:
            return {"error": "Strike required"}
        # Premium (local pricer, no order)
        px = 0.0
        try:
            from core.models.trade_model import TradeModel as _TM
            px = float(_TM().get_option_premium(symbol, opt, strike, req.expiry or "") or 0)
        except Exception:
            px = 0.0
        if px <= 0:
            try:
                from core.services.live_market_data import LiveMarketData as _LM2
                spot1 = _LM2().get_spot_price(symbol) or 0
                px = float(model_premium(spot1 or strike, strike, 7, opt, symbol=symbol)) if spot1 else 0.0
            except Exception:
                px = 0.0
        total_qty = lots * lot
        # LIVE + Fyers connected => official margin calculator
        if str(req.mode or "").lower() == "live":
            try:
                want = (req.broker or "").lower() or _auto_live_broker() or "fyers"
                if want == "fyers" and _real_token("fyers"):
                    from core.services.symbol_resolver import resolve_contract
                    _res = await resolve_contract("fyers", _get_config("fyers") or {},
                                                  symbol, req.expiry or "weekly", strike, opt)
                    if _res.get("ok"):
                        _tt = str(req.trade_type or "intraday").lower()
                        _pv = {"quantity": total_qty, "side": txn,
                               "symbol": (_res.get("refs") or {}).get("symbol", ""),
                               "expiry": _res.get("expiry_used", ""),
                               "product": "MARGIN" if _tt == "positional" else "INTRADAY"}
                        m = await _estimate_margin("fyers", _get_config("fyers") or {}, _pv, symbol, opt, strike)
                        if m.get("required"):
                            _naked = (txn == "SELL")
                            return {"required": m["required"], "source": "fyers-official",
                                    "premium": round(px, 2), "lot_size": lot, "total_qty": total_qty,
                                    "naked": _naked,
                                    "note": "Fyers SPAN+exposure (official)" + (" NAKED SELL: unlimited loss possible - hedge lagao" if _naked else "")}
            except Exception as e:
                return {"required": None, "premium": round(px, 2),
                        "note": f"Fyers margin unavailable: {str(e)[:100]}"}
        # Local estimate (paper ya no-broker): BUY exact, SELL ~SPAN
        if txn == "BUY":
            return {"required": round(px * total_qty, 2), "source": "estimate",
                    "premium": round(px, 2), "lot_size": lot, "total_qty": total_qty,
                    "naked": False,
                    "note": "BUY me premium×qty hi lagta hai (exact)"}
        base = strike or px
        return {"required": round(0.12 * base * total_qty, 2), "source": "estimate",
                "premium": round(px, 2), "lot_size": lot, "total_qty": total_qty,
                "naked": True,
                "note": "SELL estimate ~12% underlying (SPAN approx, broker se confirm karo). NAKED SELL: unlimited loss possible - hedge (spread) lagao"}
    except Exception as e:
        return {"error": f"Margin preview failed: {e}"[:200]}


def _round_tick(px: float, tick: float = 0.05) -> float:
    try:
        return round(round(float(px) / tick) * tick, 2)
    except Exception:
        return 0.0


async def _broker_limit_price(symbol: str, opt: str, strike: float, exp_ymd: str, txn: str):
    """LIMIT price = the SHARED contract LTP (contract_pricer).

    Yahi wahi pricer hai jo paper trade use karta hai (contract_pricer.
    entry_price) - isliye paper trade aur live trade ek hi price par land
    karte hain (same broker tick, same slippage model). None = price nahi
    mila (MARKET fallback)."""
    try:
        from core.services.contract_pricer import get_contract_ltp
        ltp = float(get_contract_ltp(symbol, strike, opt, exp_ymd, fresh=True) or 0)
    except Exception:
        ltp = 0.0
    if ltp <= 0:
        from core.models.trade_model import TradeModel
        from core.services.live_market_data import LiveMarketData
        from utils.helpers import model_premium as _mp
        premium = None
        try:
            premium = TradeModel().get_option_premium(symbol, opt, strike, exp_ymd)
        except Exception:
            premium = None
        if not premium or float(premium) <= 0:
            spot = LiveMarketData().get_spot_price(symbol)
            if not spot or spot <= 0:
                return None
            premium = _mp(spot, strike, 7, opt, symbol=symbol)
        try:
            ltp = float(premium)
        except Exception:
            ltp = 0.0
    if ltp <= 0:
        return None
    # Price EXACT LTP par (0.05 tick me rounded) - koi extra buffer nahi.
    return _round_tick(ltp)


def _angel_direct_ltp(symbol: str, strike: float, opt: str, expiry_ymd: str) -> float:
    """Angel/Bigul resolved contract ka REAL LTP (order se pehle)."""
    try:
        from core.services.contract_pricer import _angel_batch_ltp
        m = _angel_batch_ltp(symbol, [(float(strike), str(opt).upper())], expiry_ymd)
        for v in m.values():
            f = float(v or 0)
            if f > 0:
                return f
    except Exception:
        pass
    return 0.0


def _latest_paper_entry(strategy_id: int, symbol: str, opt: str) -> dict:
    """Latest PAPER entry of the same strategy+symbol+option (price parity
    check between paper and live). {} = no paper trade yet."""
    try:
        from core.models.database import Database
        db = Database.get_instance()
        if strategy_id:
            row = db.fetch_one(
                "SELECT entry_price, entry_date, strike_price, expiry_date, id "
                "FROM paper_trades WHERE strategy_id=? AND symbol=? AND option_type=? "
                "ORDER BY id DESC LIMIT 1",
                [int(strategy_id), str(symbol or "").upper(), str(opt or "").upper()])
        else:
            row = db.fetch_one(
                "SELECT entry_price, entry_date, strike_price, expiry_date, id "
                "FROM paper_trades WHERE symbol=? AND option_type=? "
                "ORDER BY id DESC LIMIT 1",
                [str(symbol or "").upper(), str(opt or "").upper()])
        if not row:
            return {}
        return {"entry_price": float(row.get("entry_price") or 0),
                "entry_date": str(row.get("entry_date") or ""),
                "strike": float(row.get("strike_price") or 0),
                "expiry": str(row.get("expiry_date") or ""),
                "trade_id": row.get("id")}
    except Exception:
        return {}

@router.post("/place-live")
async def place_live_order(req: LiveOrderRequest):
    """REAL live order execution (real money). Safety chain:
    1. dry_run=true -> only previews the exact broker payload, sends nothing.
    2. Global kill-switch: live_trading_enabled must be ON (Brokers tab).
    3. Broker must hold a REAL token (simulated SIM-/ANG- tokens rejected).
    4. Fyers is the main broker (priority #1); symbol auto-built.
    Quantity sent to broker = lots x lot_size (never raw lots).
    Fills are tracked trade_mode='live' with broker_order_id stored.
    The price used = the SHARED contract LTP (same pricer the paper trade
    uses), so paper and live of the same contract land on the same price.
    """
    try:
        from core.models.trade_model import TradeModel
        from core.services.transaction_costs import TransactionCosts
        from utils.helpers import get_lot_size, get_strike_step, model_premium
        from core.services.live_market_data import LiveMarketData
        symbol = (req.symbol or "").upper()
        if not symbol:
            return {"error": "Symbol required"}
        try:
            from utils.helpers import is_optionable as _isopt3
            if not _isopt3(symbol):
                return {"error": f"{symbol} par koi options nahi hain (F&O me nahi) - live order impossible"}
        except Exception:
            pass
        # 3:00 PM entry cutoff (user rule, REAL MONEY): iske baad live
        # order bhi nahi. Exits broker par hamescha allowed.
        # dry_run preview cutoff ke baad bhi allowed (kuch nahi bheja jata,
        # sirf REAL LTP + LIMIT price dikhana zaroori hai).
        try:
            from core.services.sl_monitor import entry_open_ist as _eok5
            from core.services.sl_monitor import entry_block_reason as _ebr5
            if not bool(_eok5()) and not req.dry_run:
                return {"error": _ebr5()}
        except Exception:
            pass
        # Official-lot gate (fail-closed, REAL MONEY): unverified lot par
        # live order BLOCK - galat lot par broker order kabhi na jaye.
        try:
            from utils.helpers import lot_guard_error as _lge4
            _lot_err4 = _lge4(symbol)
            if _lot_err4:
                return {"error": _lot_err4}
        except Exception:
            pass
        # Strategy expiry: auto-rollover to next weekly expiry by default so
        # live trading keeps running instead of blocking on a past end_date.
        _rolled_to = ""
        if req.strategy_id:
            # Per-strategy LIVE toggle guard: OFF strategy par koi real order
            # nahi jayega (dry-run preview allowed hai, confirm screen ke liye).
            if not req.dry_run:
                try:
                    from core.models.database import Database as _DB0
                    from routes.strategies import _strat_live_enabled as _sle2
                    if not _sle2(_DB0.get_instance(), req.strategy_id):
                        return {"error": "Is strategy ka Live Trade OFF hai - Trading Center ke saved strategy card par Live ON karo"}
                except Exception:
                    pass
            try:
                from core.models.database import Database as _DB2
                from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2
                _srow = _DB2.get_instance().fetch_one(
                    "SELECT name, end_date FROM strategies WHERE id=?", [req.strategy_id]
                )
                if _srow:
                    _end = str(_srow.get("end_date") or "")[:10]
                    _today = _dt2.now(_tz2(_td2(hours=5, minutes=30))).strftime("%Y-%m-%d")
                    if _end and _end < _today:
                        from routes.strategies import rollover_strategy as _roll2
                        _rolled_to = _roll2(req.strategy_id) or ""
            except Exception:
                pass
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
        # Hard floor: ₹1-2 lottery tickets (far-OTM) - dry_run aur real dono blocked
        try:
            from utils.helpers import check_min_premium as _cmp3
            from core.models.trade_model import TradeModel as _TM3
            _px3 = 0.0
            try:
                _px3 = float(_TM3().get_option_premium(symbol, opt, strike, req.expiry or "") or 0)
            except Exception:
                _px3 = 0.0
            if _px3 <= 0:
                try:
                    from core.services.live_market_data import LiveMarketData as _LM3
                    from utils.helpers import model_premium as _mp3
                    _sp3 = _LM3().get_spot_price(symbol) or 0
                    if _sp3:
                        _px3 = float(_mp3(_sp3, strike, 7, opt, symbol=symbol) or 0)
                except Exception:
                    pass
            _merr3 = _cmp3(_px3, opt) if _px3 > 0 else None
            if _merr3:
                return {"error": _merr3}
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
        broker = want or next((b for b in BROKER_ORDER if _real_token(b)), "")
        if not broker:
            return {"error": "No broker with a REAL token connected. Connect Fyers (main broker) or Bigul first - order NOT placed."}
        if broker not in BROKER_ORDER:
            return {"error": f"Live execution supports Fyers/Bigul (got {broker})."}
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
            # Strip any leaked HTML/block pages so alerts stay readable
            try:
                import re as _re2
                if "<" in _msg:
                    _msg = _re2.sub(r"<[^>]*>", " ", _msg)
                    _msg = _re2.sub(r"\s+", " ", _msg).strip()
                    if not _msg:
                        _msg = "broker blocked the request (network/WAF) — reconnect and retry"
                _msg = _msg[:200]
            except Exception:
                _msg = _msg[:200]
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
        elif broker in ("angel", "bigul"):
            preview = {"broker": broker, "symbol": _refs.get("tradingsymbol", ""),
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
        # Strategy-compatible LIMIT price (Angel/Bigul par LIMIT order taaki
        # app aur broker ki price na tedhi ho; Fyers MARKET bhejta hai).
        _limit_px = None
        try:
            _limit_px = await _broker_limit_price(symbol, opt, strike, exp_ymd, txn)
        except Exception:
            _limit_px = None
        if _limit_px:
            preview["limit_price"] = _limit_px
            for _b in ("angel", "bigul"):
                if preview.get("broker") == _b:
                    preview["order_type"] = "LIMIT"
        # The SHARED LTP (contract_pricer) is the single price source for
        # paper AND live. Recorded here so the UI can compare paper vs live.
        _ltp_price = 0.0
        _ltp_source = "none"
        try:
            from core.services.contract_pricer import price_source_detail
            _det = price_source_detail(symbol, strike, opt, exp_ymd)
            _ltp_price = float(_det.get("ltp") or 0)
            _ltp_source = str(_det.get("source") or "none")
        except Exception:
            pass
        if _ltp_price <= 0 and _limit_px:
            _ltp_price = float(_limit_px)
            _ltp_source = "limit-price"
        preview["ltp"] = _round_tick(_ltp_price) if _ltp_price > 0 else 0
        preview["price_source"] = _ltp_source
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
            # Client with fy_id/totp_secret: token expire ho to _request
            # khud headless AUTO-LOGIN kar lega (refresh API SEBI-blocked).
            fy = _fyers_client(cfg)
            res = await fy.place_order(preview["symbol"], txn, broker_qty, order_type="MARKET")
            if not res.get("success"):
                return {"error": f"Fyers rejected order: {res.get('error') or res.get('data')}",
                        "preview": preview, "broker_response": res.get("data", {})}
            order_id = str((res.get("data") or {}).get("order_id", ""))
            broker_ref = preview["symbol"]
            # Auto-login ya refresh mid-flight hua ho -> naya token SAVE
            # (warna agli call me wapas purana dead token use hoga).
            try:
                if fy.refreshed and fy.access_token:
                    cfg["access_token"] = fy.access_token
                    if fy.refresh_token:
                        cfg["refresh_token"] = fy.refresh_token
                    _save_config("fyers", cfg)
                    fy.refreshed = False
            except Exception:
                pass
            # Fill verify: real average price (or a clear rejection). FAILED
            # order par fake position NAHI likhenge.
            _fill_px = None
            for _i in range(5):
                try:
                    await asyncio.sleep(1.2)
                    _ob = await fy.get_order_book()
                    _rows = ((_ob or {}).get("data") or {}).get("orderBook") or []
                    for _r0 in (_rows if isinstance(_rows, list) else []):
                        if not isinstance(_r0, dict):
                            continue
                        if str(_r0.get("id") or _r0.get("orderId") or "") != str(order_id):
                            continue
                        try:
                            _avg = float(_r0.get("averagePrice") or _r0.get("average_price") or 0)
                        except Exception:
                            _avg = 0.0
                        if _avg > 0:
                            _fill_px = _round_tick(_avg)
                        _st = str(_r0.get("status") or _r0.get("orderStatus") or "").upper()
                        if _st in ("REJECTED", "FAILED", "CANCELLED", "CANCELED"):
                            _rsn = str(_r0.get("statusMessage") or _r0.get("message")
                                       or _r0.get("remarks") or _st)
                            return {"error": (f"Fyers order FAILED/REJECTED: {_rsn}. "
                                              f"Open position record NAHI hua (broker verify fail)."),
                                    "preview": preview, "broker_response": _r0}
                        if _st in ("EXECUTED", "TRADED", "COMPLETE", "COMPLETED", "FILLED"):
                            break
                except Exception:
                    continue
            if _fill_px:
                preview["fill_price"] = _fill_px
        elif broker in ("angel", "bigul"):
            _ac = _get_config(broker) or {}
            from core.services.broker_angel_live import AngelLive
            an = AngelLive(api_key=_ac.get("api_key", ""), client_code=_ac.get("client_code", ""),
                           password=_ac.get("password", ""), totp_secret=_ac.get("totp_secret", ""))
            an.jwt = _ac.get("access_token", "")
            res = await an.place_order(preview["symbol"], preview["symboltoken"], txn,
                                       broker_qty, product=preview.get("product", "INTRADAY"),
                                       order_type=("LIMIT" if _limit_px else "MARKET"),
                                       price=(_limit_px or 0))
            if not res.get("success"):
                return {"error": f"Angel rejected order: {res.get('error')}",
                        "preview": preview, "broker_response": res.get("data", {})}
            order_id = str((res.get("data") or {}).get("order_id", ""))
            broker_ref = preview["symbol"]
            # Angel/Bigul fill verify: orderbook me status + asli avg price.
            _fill_px = None
            for _i in range(5):
                try:
                    await asyncio.sleep(1.2)
                    _ob = await an.order_book()
                    _rows = ((_ob or {}).get("data") or {}).get("data") if isinstance(_ob, dict) else None
                    if not _rows:
                        continue
                    _row = None
                    for _r0 in _rows:
                        if str(_r0.get("orderid") or _r0.get("orderId") or "") == order_id:
                            _row = _r0
                            break
                    if not _row:
                        continue
                    _st = str(_row.get("orderstatus") or _row.get("orderStatus") or "").lower()
                    try:
                        _avg = float(_row.get("averageprice") or _row.get("averagePrice") or 0)
                    except Exception:
                        _avg = 0.0
                    if _avg > 0:
                        _fill_px = _round_tick(_avg)
                    if _st in ("rejected", "cancelled", "failed"):
                        return {"error": (f"{broker} order FAILED: "
                                          f"{_row.get('text') or _row.get('statusmessage') or _st}. "
                                          f"Open position record NAHI hua (broker verify fail)."),
                                "preview": preview, "broker_response": _row}
                    if _st in ("complete", "completed", "filled", "executed"):
                        break
                except Exception:
                    continue
            if _fill_px:
                preview["fill_price"] = _fill_px
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
        # Track the live fill locally (entry: broker real fill > SHARED pricer
        # price). The shared pricer (contract_pricer.entry_price) is the SAME
        # function the paper trade uses - so paper and live of one contract
        # land on the same price.
        _px_src = float(preview.get("fill_price") or 0)
        _price_source = "broker-fill"
        if _px_src > 0:
            adj = _round_tick(_px_src)
        else:
            adj = 0.0
            try:
                from core.services.contract_pricer import entry_price as _shared_entry
                _adj2, _src2 = _shared_entry(symbol, strike, opt, exp_ymd, side=txn, fresh=True)
                if _adj2 and float(_adj2) > 0:
                    adj = _round_tick(float(_adj2))
                    _price_source = _src2 or "shared-pricer"
            except Exception:
                adj = 0.0
            if adj <= 0:
                premium = None
                try:
                    premium = LiveMarketData().get_option_ltp(symbol, strike, opt, exp_ymd, fresh=True)
                except Exception:
                    premium = None
                if not premium or float(premium) <= 0:
                    try:
                        premium = TradeModel().get_option_premium(symbol, opt, strike, exp_ymd)
                    except Exception:
                        premium = None
                if not premium or float(premium) <= 0:
                    spot = LiveMarketData().get_spot_price(symbol)
                    if not spot or spot <= 0:
                        return {"error": f"Live premium/spot nahi mila {symbol} {strike}{opt} - bina price ke live fill record nahi hoga"}
                    premium = model_premium(spot, strike, 7, opt, symbol=symbol)
                premium = TransactionCosts.apply_fill_slippage(float(premium), txn, is_live=True)
                adj = _round_tick(float(premium))
                _price_source = "model"
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
            "strategy_id": req.strategy_id,
        })
        _lresp = {"success": True, "trade_id": tid, "broker_order_id": order_id,
                "broker": broker, "broker_symbol": broker_ref, "quantity": broker_qty,
                "entry_price": round(adj, 2), "mode": "live",
                "ltp": float(preview.get("ltp") or 0) or None,
                "price_source": _price_source,
                "expiry": exp_ymd, "strike": strike, "option_type": opt}
        # Paper-vs-live price check: same strategy + same symbol + same option
        # type ka latest PAPER entry - UI isko side-by-side dikhata hai.
        try:
            _paper_ref = _latest_paper_entry(req.strategy_id, symbol, opt)
            if _paper_ref:
                _lresp["paper_ref"] = _paper_ref
                _pl = float(_paper_ref.get("entry_price") or 0)
                if _pl > 0:
                    _lresp["paper_live_delta"] = round(adj - _pl, 2)
                    _lresp["paper_live_delta_pct"] = round((adj - _pl) / _pl * 100, 2) if _pl else 0.0
        except Exception:
            pass
        if _rolled_to:
            _lresp["rolled_to"] = _rolled_to
        return _lresp
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": f"Live order failed: {str(e)}"}
