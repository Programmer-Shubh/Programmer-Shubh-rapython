"""Broker token keep-alive (auto refresh) - the 'auto fetch' automation.

Fyers tokens expire daily. What runs automatically here:
1. Every cycle (and 60s after boot): validate the stored access token.
2. If the token is dead (or about to die) but a refresh token + PIN are
   saved, refresh it and persist the NEW token in the DB - so the app
   never needs a manual browser login again.
3. Bigul / Angel: re-login with the saved credentials when the session
   died (TOTP is generated locally, no user action needed).

DB is the live truth (env/dashboard = seed). This pairs with the
DB-wins precedence in routes/broker._get_config.
"""
import threading
import time

_LAST = {"ts": 0, "result": {}}
_LOCK = threading.Lock()

# Token is refreshed when it expires within this window (seconds).
_REFRESH_MARGIN = 45 * 60


def _db_cfg(name):
    try:
        import json as _js
        from core.models.database import Database
        row = Database.get_instance().fetch_one(
            "SELECT setting_value FROM settings WHERE setting_key=?", [f"broker_{name}"])
        cfg = _js.loads(row["setting_value"]) if row and row.get("setting_value") else {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _save_cfg(name, cfg):
    try:
        import json as _js
        from core.models.database import Database
        db = Database.get_instance()
        row = db.fetch_one("SELECT setting_key FROM settings WHERE setting_key=?", [f"broker_{name}"])
        if row:
            db.execute("UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key=?",
                       [_js.dumps(cfg), f"broker_{name}"])
        else:
            db.execute("INSERT INTO settings (setting_key, setting_value) VALUES (?, ?)",
                       [f"broker_{name}", _js.dumps(cfg)])
        return True
    except Exception:
        return False


def _broker_disconnected(name):
    """User ne 'Disconnect' daba diya -> env/db creds revive NAHI hone chahiye."""
    try:
        from core.models.database import Database
        row = Database.get_instance().fetch_one(
            "SELECT setting_value FROM settings WHERE setting_key=?",
            [f"broker_disconnected_{name}"])
        return str((row or {}).get("setting_value") or "") == "1"
    except Exception:
        return False


def _fyers_refresh(cfg):
    """Refresh the Fyers token (needs app_id + secret + refresh_token).
    Returns (ok, new_access_token, detail)."""
    try:
        import asyncio as _aio
        from core.services.broker_fyers import FyersV3
        if not (cfg.get("app_id") and cfg.get("secret") and cfg.get("refresh_token")):
            return False, "", "missing app_id/secret/refresh_token"
        fy = FyersV3(app_id=cfg.get("app_id", ""), secret_key=cfg.get("secret", ""),
                     redirect_uri=cfg.get("redirect_uri", ""),
                     access_token=cfg.get("access_token", ""),
                     refresh_token=cfg.get("refresh_token", ""),
                     pin=cfg.get("pin", ""))
        r = _aio.run(fy.refresh_access_token())
        if r.get("success") and fy.access_token:
            return True, fy.access_token, "refreshed"
        return False, "", str(r.get("error") or "refresh failed")[:150]
    except Exception as e:
        return False, "", str(e)[:150]


def _fyers_valid(cfg):
    """Live ping: is the stored token still accepted by Fyers?"""
    try:
        import asyncio as _aio
        from core.services.broker_fyers import FyersV3
        if not cfg.get("access_token"):
            return False, "no token"
        fy = FyersV3(app_id=cfg.get("app_id", ""), secret_key=cfg.get("secret", ""),
                     redirect_uri=cfg.get("redirect_uri", ""),
                     access_token=cfg.get("access_token", ""),
                     refresh_token=cfg.get("refresh_token", ""),
                     pin=cfg.get("pin", ""))
        r = _aio.run(fy.validate())
        if r.get("success"):
            return True, "valid"
        return False, str(r.get("error") or "invalid")[:150]
    except Exception as e:
        return False, str(e)[:120]


def _bigul_login(cfg):
    """Bigul/Angel password+TOTP login -> new access token ('' on failure)."""
    try:
        import asyncio as _aio
        from core.services.broker_angel_live import AngelLive
        if not (cfg.get("api_key") and cfg.get("client_id") and cfg.get("password")):
            return ""
        an = AngelLive(api_key=cfg.get("api_key", ""), client_code=cfg.get("client_id", ""),
                       password=cfg.get("password", ""), totp_secret=cfg.get("totp_secret", ""))
        r = _aio.run(an.login())
        if r.get("success"):
            return str(an.jwt or "")
        return ""
    except Exception:
        return ""


def sync_once():
    """One keep-alive pass over every linked broker.

    Fyers : validate -> refresh (refresh_token + PIN) -> persist new token.
    Bigul : re-login with saved creds when the session is gone.
    """
    res = {"fyers": "", "bigul": "", "detail": ""}
    # ---- Fyers (main broker) ----
    try:
        if _broker_disconnected("fyers"):
            res["fyers"] = "disconnected (skipped)"
        else:
            cfg = _db_cfg("fyers")
            if cfg.get("access_token"):
                ok, detail = _fyers_valid(cfg)
                if ok:
                    res["fyers"] = "token valid"
                else:
                    ok2, new_tok, det2 = _fyers_refresh(cfg)
                    if ok2:
                        cfg["access_token"] = new_tok
                        _save_cfg("fyers", cfg)
                        res["fyers"] = "token refreshed (auto)"
                    else:
                        res["fyers"] = f"needs login ({det2 or detail})"
            elif cfg.get("refresh_token"):
                ok2, new_tok, det2 = _fyers_refresh(cfg)
                if ok2:
                    cfg["access_token"] = new_tok
                    _save_cfg("fyers", cfg)
                    res["fyers"] = "token refreshed (auto)"
                else:
                    res["fyers"] = f"needs login ({det2})"
            else:
                res["fyers"] = "not configured"
    except Exception as e:
        res["fyers"] = f"error: {str(e)[:100]}"
    # ---- Bigul ----
    try:
        if _broker_disconnected("bigul"):
            res["bigul"] = "disconnected (skipped)"
        else:
            bcfg = _db_cfg("bigul")
            if bcfg.get("client_id") and bcfg.get("password"):
                jwt = _bigul_login(bcfg)
                if jwt:
                    bcfg["access_token"] = jwt
                    _save_cfg("bigul", bcfg)
                    res["bigul"] = "session renewed (auto)"
                else:
                    res["bigul"] = "login failed (check creds)"
            else:
                res["bigul"] = "not configured"
    except Exception as e:
        res["bigul"] = f"error: {str(e)[:100]}"
    with _LOCK:
        _LAST["ts"] = time.time()
        _LAST["result"] = dict(res)
    return res


def status():
    try:
        with _LOCK:
            return {"age_s": round(time.time() - _LAST["ts"], 1) if _LAST["ts"] else -1,
                    **dict(_LAST["result"])}
    except Exception:
        return {}


def start_background(interval_s=30 * 60):
    def _loop():
        try:
            time.sleep(60)
        except Exception:
            return
        while True:
            try:
                r = sync_once()
                try:
                    print(f"[token-keeper] {r}", flush=True)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                time.sleep(interval_s)
            except Exception:
                break
    try:
        th = threading.Thread(target=_loop, name="token-keeper", daemon=True)
        th.start()
        return True
    except Exception:
        return False
