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


def sync_once():
    """Validate candidates, sync the winner to DB, renew to extend life."""
    res = {"kept": "", "renewed": False, "detail": ""}
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
            res["detail"] = "no valid Dhan token anywhere - manual reconnect needed"
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
