"""Dhan token auto-renewal + valid-token-wins sync (the 'auto fetch').

Reality: Dhan issues tokens via web login only - no API mints a token
from thin air. What runs automatically here:
1. Every 6h (and 60s after boot): validate DB token AND env/dashboard
   tokens; whichever validates is synced INTO the DB (dashboard rotation
   heals itself even though env can't be rewritten from the app).
2. On the valid token: RenewToken extends its life without manual login,
   so an active token stays alive indefinitely instead of dying at midnight.

DB is the live truth (env/dashboard = seed). This pairs with the
DB-wins precedence in _get_config/_broker_cfg.
"""
import threading
import time

_LAST = {"ts": 0, "result": {}}
_LOCK = threading.Lock()


def _env_dhan():
    import os
    return {
        "client_id": (os.environ.get("RATRADE_DHAN_CLIENT_ID")
                      or os.environ.get("DHAN_CLIENT_ID") or "").strip(),
        "access_token": (os.environ.get("RATRADE_DHAN_ACCESS_TOKEN")
                         or os.environ.get("DHAN_ACCESS_TOKEN") or "").strip(),
    }


def _db_dhan():
    try:
        import json as _js
        from core.models.database import Database
        row = Database.get_instance().fetch_one(
            "SELECT setting_value FROM settings WHERE setting_key='broker_dhan'")
        cfg = _js.loads(row["setting_value"]) if row and row.get("setting_value") else {}
        if isinstance(cfg, dict):
            return {"client_id": str(cfg.get("client_id") or "").strip(),
                    "access_token": str(cfg.get("access_token") or "").strip()}
    except Exception:
        pass
    return {"client_id": "", "access_token": ""}


def _save_db_token(client_id, access_token):
    try:
        import json as _js
        from core.models.database import Database
        db = Database.get_instance()
        row = db.fetch_one("SELECT setting_key FROM settings WHERE setting_key='broker_dhan'")
        cfg = {"client_id": client_id, "access_token": access_token}
        if row:
            cur = db.fetch_one("SELECT setting_value FROM settings WHERE setting_key='broker_dhan'")
            try:
                old = _js.loads(cur["setting_value"]) if cur and cur.get("setting_value") else {}
                if isinstance(old, dict):
                    for k, v in old.items():
                        cfg.setdefault(k, v)
            except Exception:
                pass
            db.execute("UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key='broker_dhan'",
                       [_js.dumps(cfg)])
        else:
            db.execute("INSERT INTO settings (setting_key, setting_value) VALUES ('broker_dhan', ?)",
                       [_js.dumps(cfg)])
        return True
    except Exception:
        return False


def dhan_totp_login(client_id, pin, totp_secret):
    """Fully automatic Dhan login (official API): TOTP from saved secret +
    POST auth.dhan.co/app/generateAccessToken. Needs one-time TOTP setup on
    Dhan Web. Returns access_token or ''."""
    try:
        import pyotp
        import requests as _rq
        cid = str(client_id or "").strip()
        pin = str(pin or "").strip()
        sec = str(totp_secret or "").strip().replace(" ", "")
        if not (cid and len(pin) == 6 and sec):
            return ""
        totp = pyotp.TOTP(sec).now()
        r = _rq.post(
            "https://auth.dhan.co/app/generateAccessToken",
            params={"dhanClientId": cid, "pin": pin, "totp": totp},
            timeout=20)
        if r.status_code != 200:
            return ""
        try:
            body = r.json() or {}
        except Exception:
            return ""
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        tok = str((data or {}).get("accessToken") or (data or {}).get("access_token") or "")
        return tok
    except Exception:
        return ""


def angel_auto_login(cfg):
    """Angel full auto-login from saved api_key + client_code + password +
    totp_secret (SmartAPI login). Returns jwt or ''."""
    try:
        import asyncio as _aio
        from core.services.broker_angel_live import AngelLive
        if not (cfg.get("api_key") and cfg.get("client_code") and cfg.get("password")):
            return ""
        al = AngelLive(api_key=cfg.get("api_key", ""), client_code=cfg.get("client_code", ""),
                       password=cfg.get("password", ""), totp_secret=cfg.get("totp_secret", ""))
        try:
            r = _aio.run(al.login())
        except Exception:
            return ""
        return str((r or {}).get("jwt") or "")
    except Exception:
        return ""


def _validate(client_id, token):
    """True if Dhan accepts the token (read-only holdings ping)."""
    try:
        import asyncio as _aio
        from core.services.broker_dhan_live import DhanLive
        if not token or token.startswith(("SIM-", "ANG-")) or not client_id:
            return False
        dl = DhanLive(client_id=client_id, access_token=token)
        try:
            r = _aio.run(dl.validate())
        except Exception:
            return False
        return bool((r or {}).get("success"))
    except Exception:
        return False


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
            db.execute(f"UPDATE settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key='broker_{name}'",
                       [_js.dumps(cfg)])
        else:
            db.execute(f"INSERT INTO settings (setting_key, setting_value) VALUES ('broker_{name}', ?)",
                       [_js.dumps(cfg)])
        return True
    except Exception:
        return False


def sync_once():
    """Full automation ladder (Dhan):
    1. existing valid token -> renew to extend;
    2. PIN + TOTP saved -> generate fresh via official API (no web login);
    3. else manual reconnect needed.
    Angel: password-login when creds saved. Winner synced to DB."""
    res = {"kept": "", "renewed": False, "generated": "", "detail": ""}
    try:
        dbc = _db_dhan()
        enc = _env_dhan()
        cands = []
        for src, c in (("db", dbc), ("env", enc)):
            t = c.get("access_token", "")
            if t and t not in [x[1] for x in cands]:
                cands.append((src, c.get("client_id", ""), t))
        winner = None
        for src, cid, tok in cands:
            try:
                if _validate(cid, tok):
                    winner = (src, cid, tok)
                    break
            except Exception:
                continue
        if not winner:
            # Ladder step 2: full auto-login via PIN + TOTP
            try:
                import os as _os
                _cfg = dict(dbc)
                _cid = _cfg.get("client_id") or enc.get("client_id")
                _pin = (_cfg.get("pin") or _os.environ.get("RATRADE_DHAN_PIN") or "").strip()
                _tsec = (_cfg.get("totp_secret") or _os.environ.get("RATRADE_DHAN_TOTP_SECRET") or "").strip()
                _nt = dhan_totp_login(_cid, _pin, _tsec) if (_cid and _pin and _tsec) else ""
                if _nt and _validate(_cid, _nt):
                    winner = ("totp-login", _cid, _nt)
                    res["generated"] = "fresh token via PIN+TOTP (fully automatic)"
            except Exception:
                pass
        # Angel full auto-login (password + TOTP creds)
        if not winner:
            try:
                _ac = _db_cfg("angel")
                _jwt = angel_auto_login(_ac)
                if _jwt:
                    _ac["access_token"] = _jwt
                    _save_cfg("angel", _ac)
                    res["kept"] = "angel auto-login ok"
                    with _LOCK:
                        _LAST["ts"] = time.time()
                        _LAST["result"] = res
                    return res
            except Exception:
                pass
        if not winner:
            res["detail"] = "no valid token and no PIN+TOTP saved - save Dhan PIN + TOTP once for full automation"
            with _LOCK:
                _LAST["ts"] = time.time()
                _LAST["result"] = res
            return res
        src, cid, tok = winner
        res["kept"] = f"{src} token valid"
        if tok != dbc.get("access_token"):
            _save_db_token(cid, tok)
            res["kept"] += " (synced to DB)"
        # Extend life without manual login
        try:
            import asyncio as _aio
            from core.services.broker_dhan_live import DhanLive
            dl = DhanLive(client_id=cid, access_token=tok)

            async def _rn():
                return await dl.renew_token()
            try:
                rr = _aio.run(_rn())
            except Exception:
                rr = {}
            if (rr or {}).get("success") and (rr or {}).get("access_token"):
                _save_db_token(cid, rr["access_token"])
                res["renewed"] = True
                res["kept"] += " + renewed"
        except Exception as e:
            res["detail"] = f"renew skipped: {e}"[:120]
        with _LOCK:
            _LAST["ts"] = time.time()
            _LAST["result"] = res
        return res
    except Exception as e:
        res["detail"] = str(e)[:150]
        return res


def status():
    try:
        with _LOCK:
            return {"age_s": round(time.time() - _LAST["ts"], 1) if _LAST["ts"] else -1,
                    **dict(_LAST["result"])}
    except Exception:
        return {}


def start_background(interval_s=6 * 3600):
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
