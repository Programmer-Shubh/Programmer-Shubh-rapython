import asyncio
import base64
import hashlib
import hmac
import json
import logging
import struct
import time
from datetime import datetime

import httpx

from core.models.database import Database

logger = logging.getLogger(__name__)


def generate_totp(secret_b32: str, period: int = 30) -> str:
    """RFC 6238 TOTP from a base32 secret (no external dependency)."""
    if not secret_b32:
        return ""
    try:
        key = base64.b32decode(secret_b32.replace(" ", "").upper())
    except Exception:
        return ""
    counter = int(time.time() // period)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[19] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000
    return str(code).zfill(6)


class TokenManager:
    def __init__(self):
        self._db = Database.get_instance()

    def _load(self, broker: str) -> dict:
        row = self._db.fetch_one(
            "SELECT setting_value FROM settings WHERE setting_key=?", [f"broker_{broker}"]
        )
        if row and row.get("setting_value"):
            try:
                return json.loads(row["setting_value"])
            except Exception:
                return {}
        return {}

    def _save(self, broker: str, config: dict):
        existing = self._db.fetch_one(
            "SELECT setting_key FROM settings WHERE setting_key=?", [f"broker_{broker}"]
        )
        if existing:
            self._db.execute(
                "UPDATE settings SET setting_value=?, updated_at=datetime('now') WHERE setting_key=?",
                [json.dumps(config), f"broker_{broker}"],
            )
        else:
            self._db.execute(
                "INSERT INTO settings (setting_key, setting_value) VALUES (?, ?)",
                [f"broker_{broker}", json.dumps(config)],
            )

    async def refresh_fyers(self, cfg: dict) -> dict:
        if not (cfg.get("app_id") and cfg.get("secret") and cfg.get("refresh_token")):
            return {"success": False, "error": "Missing app_id/secret/refresh_token"}
        payload = {
            "grant_type": "refresh_token",
            "appId": cfg["app_id"],
            "secretKey": cfg["secret"],
            "refresh_token": cfg["refresh_token"],
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post("https://api.fyers.in/api/v2/token", json=payload)
            body = resp.json()
            if resp.status_code == 200 and body.get("access_token"):
                cfg["access_token"] = body["access_token"]
                if body.get("refresh_token"):
                    cfg["refresh_token"] = body["refresh_token"]
                cfg["token_valid_until"] = time.time() + body.get("expires_in", 86400)
                self._save("fyers", cfg)
                return {"success": True, "message": "Fyers token refreshed"}
            return {"success": False, "error": body.get("message", "Fyers refresh failed")}
        except httpx.RequestError as exc:
            return {"success": False, "error": str(exc)}

    async def refresh_dhan(self, cfg: dict) -> dict:
        if not (cfg.get("client_id") and cfg.get("refresh_token")):
            return {"success": False, "error": "Missing client_id/refresh_token"}
        payload = {
            "client_id": cfg["client_id"],
            "client_secret": cfg.get("access_token", ""),
            "refresh_token": cfg["refresh_token"],
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.dhan.co/oauth2/token/refresh", json=payload
                )
            body = resp.json()
            if resp.status_code in (200, 201) and body.get("accessToken"):
                cfg["access_token"] = body["accessToken"]
                if body.get("refreshToken"):
                    cfg["refresh_token"] = body["refreshToken"]
                cfg["token_valid_until"] = time.time() + body.get("expiresIn", 86400)
                self._save("dhan", cfg)
                return {"success": True, "message": "Dhan token refreshed"}
            return {"success": False, "error": body.get("message", "Dhan refresh failed")}
        except httpx.RequestError as exc:
            return {"success": False, "error": str(exc)}

    async def refresh_shoonya(self, cfg: dict) -> dict:
        if not (cfg.get("uid") and cfg.get("pwd") and cfg.get("vc") and cfg.get("apikey")):
            return {"success": False, "error": "Missing uid/pwd/vc/apikey"}
        totp = generate_totp(cfg.get("secret", ""))
        payload = {
            "uid": cfg["uid"],
            "pwd": cfg["pwd"],
            "vc": cfg["vc"],
            "apikey": cfg["apikey"],
            "factor2": totp or cfg.get("secret_code", ""),
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.shoonya.com/NorenWClientTP/QuickAuth", json=payload
                )
            body = resp.json()
            if body.get("stat") == "Ok" and body.get("susertoken"):
                cfg["access_token"] = body["susertoken"]
                cfg["token_valid_until"] = time.time() + 12 * 3600
                self._save("shoonya", cfg)
                return {"success": True, "message": "Shoonya token generated"}
            return {"success": False, "error": body.get("emsg", "Shoonya login failed")}
        except httpx.RequestError as exc:
            return {"success": False, "error": str(exc)}

    async def refresh_angel(self, cfg: dict) -> dict:
        if not (cfg.get("client_code") and cfg.get("password") and cfg.get("api_key")):
            return {"success": False, "error": "Missing client_code/password/api_key"}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": cfg["api_key"],
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://apiconnect.angelbroking.com/rest/auth/angelbroking/user/v1/loginByPassword",
                    json={"clientcode": cfg["client_code"], "password": cfg["password"]},
                    headers=headers,
                )
            body = resp.json()
            if body.get("status") and body.get("data", {}).get("jwtToken"):
                jwt = body["data"]["jwtToken"]
                totp = generate_totp(cfg.get("totp_secret", ""))
                if totp:
                    headers["Authorization"] = f"Bearer {jwt}"
                    resp2 = await client.post(
                        "https://apiconnect.angelbroking.com/rest/auth/angelbroking/user/v1/verifyTOTP",
                        json={"totp": totp, "clientcode": cfg["client_code"]},
                        headers=headers,
                    )
                    body2 = resp2.json()
                    if body2.get("status") and body2.get("data", {}).get("jwtToken"):
                        jwt = body2["data"]["jwtToken"]
                cfg["access_token"] = jwt
                cfg["token_valid_until"] = time.time() + 86400
                self._save("angel", cfg)
                return {"success": True, "message": "Angel token refreshed"}
            return {"success": False, "error": body.get("message", "Angel login failed")}
        except httpx.RequestError as exc:
            return {"success": False, "error": str(exc)}

    async def refresh_broker(self, broker: str) -> dict:
        cfg = self._load(broker)
        if not cfg:
            return {"success": False, "error": "Not configured"}
        handlers = {
            "fyers": self.refresh_fyers,
            "dhan": self.refresh_dhan,
            "shoonya": self.refresh_shoonya,
            "angel": self.refresh_angel,
        }
        handler = handlers.get(broker)
        if not handler:
            return {"success": False, "error": "Unknown broker"}
        return await handler(cfg)

    async def refresh_all(self) -> dict:
        results = {}
        for broker in ["shoonya", "dhan", "fyers", "angel"]:
            results[broker] = await self.refresh_broker(broker)
        return results

    def token_status(self) -> dict:
        status = {}
        for broker in ["shoonya", "dhan", "fyers", "angel"]:
            cfg = self._load(broker)
            has = bool(cfg.get("access_token") or (cfg.get("client_id") and cfg.get("refresh_token")))
            valid_until = cfg.get("token_valid_until")
            expiry = ""
            if valid_until and float(valid_until) > 0:
                expiry = datetime.fromtimestamp(float(valid_until)).strftime("%Y-%m-%d %H:%M:%S")
            status[broker] = {"valid": bool(has), "expires_at": expiry}
        return status