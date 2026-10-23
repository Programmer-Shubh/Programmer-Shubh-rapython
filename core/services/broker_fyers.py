import asyncio
import json
import logging
import re
import time
import urllib.parse
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


class FyersV3Error(Exception):
    pass


def _is_wrong_host(error_msg: str) -> bool:
    """api.fyers.in (v2 host) answers 'Invalid Request, please provide valid
    method' for v3 paths - it means 'wrong host', not 'bad request'."""
    m = str(error_msg or "").lower()
    return "valid method" in m or "invalid request" in m


def _resp_ok(status_code: int, body: dict) -> bool:
    """Fyers v3 success shape = {"s":"ok","code":200,...}; errors carry
    {"s":"error","code":-99/-352,...}. HTTP 200 alone is NOT success here."""
    if not isinstance(body, dict):
        return False
    if status_code != 200:
        return False
    s = str(body.get("s") or "").strip().lower()
    if s:
        return s in ("ok", "success")
    code = body.get("code")
    return code in (200, "200", 0, "0", None)


class FyersV3:
    # v3 REST endpoints live on api-t1.fyers.in (api.fyers.in is the v2 host
    # and answers 'Invalid Request, please provide valid method' for /api/v3/*)
    BASE_URL = "https://api-t1.fyers.in"
    FALLBACK_BASE_URL = "https://api.fyers.in"
    # OAuth 2.0 (v3, current): authcode -> validate-authcode (appIdHash).
    # FIX: token endpoints MUST be api-t1 host (official fyers_apiv3 v3.1.15 uses api-t1 for all v3)
    AUTH_URL = "https://api-t1.fyers.in/api/v3/generate-authcode"
    TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
    REFRESH_URL = "https://api-t1.fyers.in/api/v3/validate-refresh-token"
    MAX_RETRIES = 3
    # Fyers App ID format = "<appId>-<appType>" (e.g. ABCDEFGH-100 for web apps).
    # Without the suffix the API answers "invalid clientId" on the login page.
    DEFAULT_APP_TYPE = "100"

    def __init__(
        self,
        app_id: str,
        secret_key: str,
        redirect_uri: str,
        access_token: str = None,
        refresh_token: str = None,
        pin: str = None,
    ):
        self.app_id = self.normalize_app_id(app_id)
        self.secret_key = secret_key
        self.redirect_uri = redirect_uri
        self.access_token = access_token
        self.refresh_token = refresh_token
        # Fyers trading PIN = 4 digits (used by validate-refresh-token).
        # Strip spaces/extra chars so a pasted "1234 " still works.
        self.pin = re.sub(r"\D", "", str(pin or ""))
        if self.pin and len(self.pin) != 4:
            logger.warning("Fyers PIN length %d (expected 4 digits)", len(self.pin))
        self._token_expiry: float = 0
        # True when an access token was refreshed mid-flight (caller must persist it)
        self.refreshed: bool = False

    @classmethod
    def normalize_app_id(cls, app_id: str) -> str:
        """Accept 'ABCDEFGH' or 'ABCDEFGH-100' -> always 'ABCDEFGH-100'.
        Fyers MyAPI dashboard shows the id WITH the appType suffix; users often
        paste only the first part, which the API rejects as 'invalid clientId'."""
        aid = str(app_id or "").strip()
        if not aid:
            return ""
        if re.search(r"-\d+$", aid):
            return aid
        return f"{aid}-{cls.DEFAULT_APP_TYPE}"

    def app_id_hash(self) -> str:
        import hashlib
        return hashlib.sha256(f"{self.app_id}:{self.secret_key}".encode()).hexdigest()

    def _fallback_url(self, path: str) -> str:
        return f"{self.FALLBACK_BASE_URL}{path}"

    def get_auth_url(self) -> str:
        params = {
            "client_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": "ratrade_login",
        }
        query = urllib.parse.urlencode(params)
        return f"{self.AUTH_URL}?{query}"

    async def generate_token(self, auth_code: str) -> dict:
        import urllib.parse as _up
        auth_code = _up.unquote((auth_code or "").strip())
        payload = {
            "grant_type": "authorization_code",
            "appIdHash": self.app_id_hash(),
            "code": auth_code,
        }
        # Try primary (api-t1) then fallback (api) - Fyers docs mix hosts
        urls = [self.TOKEN_URL, "https://api.fyers.in/api/v3/validate-authcode", "https://api-t1.fyers.in/api/v3/validate-authcode"]
        last_body = {}
        for url in urls:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
                try:
                    body = resp.json()
                except Exception:
                    body = {"raw": (resp.text or "")[:500]}
                last_body = body
                if resp.status_code == 200 and body.get("access_token"):
                    self.access_token = body["access_token"]
                    self.refresh_token = body.get("refresh_token")
                    self._token_expiry = time.time() + body.get("expires_in", 86400)
                    logger.info("Fyers token generated successfully (v3 OAuth) via %s", url)
                    return {"success": True, "data": body, "error": ""}
                # invalid auth code is not retryable - but try next host if 400 with method error
                msg = str(body.get("message") or body.get("error") or body)[:300]
                if "method" in msg.lower() and url != urls[-1]:
                    continue
                if body.get("access_token"):
                    return {"success": True, "data": body, "error": ""}
                return {"success": False, "data": body, "error": msg or "Token generation failed (check App ID, Secret, Redirect URI match Fyers app settings). Code single-use, expires in 60s - start fresh OAuth."}
            except httpx.RequestError as exc:
                logger.error("Fyers token generation error %s: %s", url, exc)
                last_body = {"error": str(exc)}
                continue
        return {"success": False, "data": last_body, "error": str(last_body.get("message") or last_body.get("error") or last_body)[:300] or "Token generation failed"}

    async def refresh_access_token(self, pin: str = "") -> dict:
        if not self.refresh_token:
            return {"success": False, "data": {}, "error": "No refresh token available — Login with Fyers again"}
        payload = {
            "grant_type": "refresh_token",
            "appIdHash": self.app_id_hash(),
            "refresh_token": self.refresh_token,
        }
        pin = str(pin or self.pin or "").strip()
        if pin:
            payload["pin"] = pin
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self.REFRESH_URL, json=payload)
            body = resp.json()
            if resp.status_code == 200 and body.get("access_token"):
                self.access_token = body["access_token"]
                self.refresh_token = body.get("refresh_token", self.refresh_token)
                self._token_expiry = time.time() + body.get("expires_in", 86400)
                self.refreshed = True
                logger.info("Fyers token refreshed successfully")
                return {"success": True, "data": body, "error": ""}
            msg = body.get("message", "")
            if "pin" in msg.lower():
                msg += " (Fyers needs your trading PIN for refresh — else login again)"
            # SEBI/ECN guideline: Fyers ne validate-refresh-token API disable
            # kar di hai -> ye USER-ka-nahi-FYERS-ka status hai. Trading tab
            # tak chalti hai jab tak stored access token valid hai; press
            # Connect/OAuth login karke naya token liya jaa sakta hai.
            _mlow = str(msg).lower()
            if ("sebi" in _mlow or "regulation" in _mlow
                    or "disabled" in _mlow and "refresh" in _mlow):
                msg = ("Fyers ne refresh-token API band ki hai (SEBI guideline) "
                       "- ye Fyers ka notice hai, app ka fault nahi. "
                       "Trading tab tak chalti rahegi jab tak ye token valid hai; "
                       "token khatam hone par Brokers tab me 'Connect' (OAuth "
                       "login) karke naya token le lena.")
                return {"success": False, "data": body,
                        "error": msg, "sebi_disabled": True}
            return {"success": False, "data": body, "error": msg or "Token refresh failed — Login with Fyers again"}
        except httpx.RequestError as exc:
            logger.error("Fyers token refresh error: %s", exc)
            return {"success": False, "data": {}, "error": str(exc)}

    async def _request(
        self, method: str, path: str, data: dict = None, retries: int = MAX_RETRIES
    ) -> dict:
        if not self.access_token:
            return {"success": False, "data": {}, "error": "No access token. Authenticate first."}

        if self._token_expiry and time.time() > self._token_expiry:
            logger.warning("Fyers access token expired, attempting refresh")
            refresh_result = await self.refresh_access_token()
            if not refresh_result["success"]:
                return {
                    "success": False,
                    "data": {},
                    "error": "Token expired and refresh failed. Re-authenticate.",
                }

        url = f"{self.BASE_URL}{path}"
        # Fyers v3 auth header: {app_id}:{access_token}
        auth_val = f"{self.app_id}:{self.access_token}" if self.app_id else self.access_token
        headers = {
            "Authorization": auth_val,
            "Content-Type": "application/json",
            "version": "3",
        }

        for attempt in range(1, retries + 1):
            try:
                logger.info("Fyers %s %s attempt=%d/%d", method, path, attempt, retries)
                async with httpx.AsyncClient(timeout=30) as client:
                    if method == "GET":
                        resp = await client.get(url, headers=headers)
                    elif method == "POST":
                        resp = await client.post(url, headers=headers, json=data)
                    elif method == "PUT":
                        resp = await client.put(url, headers=headers, json=data)
                    elif method == "DELETE":
                        resp = await client.delete(url, headers=headers)
                    else:
                        raise FyersV3Error(f"Unsupported method: {method}")

                body = resp.json()

                if resp.status_code == 401:
                    logger.warning("Fyers token expired or invalid")
                    refresh_result = await self.refresh_access_token()
                    if refresh_result["success"]:
                        # Fyers v3 header must stay "<app_id>:<access_token>"
                        headers["Authorization"] = (
                            f"{self.app_id}:{self.access_token}"
                            if self.app_id else self.access_token
                        )
                        continue
                    return {
                        "success": False,
                        "data": body,
                        "error": "Token expired. Re-authenticate.",
                    }

                if not _resp_ok(resp.status_code, body):
                    error_msg = body.get("message", body.get("error", str(body)))
                    # Wrong host answers 'Invalid Request, please provide valid
                    # method' for v3 paths (api.fyers.in vs api-t1.fyers.in).
                    if _is_wrong_host(error_msg) and url != self._fallback_url(path):
                        logger.warning("Fyers %s: host mismatch, retrying %s",
                                       path, self._fallback_url(path))
                        url = self._fallback_url(path)
                        continue
                    logger.error(
                        "Fyers %s %s returned %d: %s",
                        method, path, resp.status_code, error_msg,
                    )
                    if attempt < retries and resp.status_code >= 500:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return {"success": False, "data": body, "error": error_msg}

                logger.info("Fyers %s %s succeeded", method, path)
                return {"success": True, "data": body, "error": ""}

            except httpx.TimeoutException:
                logger.warning("Fyers %s %s timed out attempt=%d", method, path, attempt)
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return {
                    "success": False,
                    "data": {},
                    "error": f"Request timed out after {retries} attempts",
                }
            except httpx.RequestError as exc:
                logger.error("Fyers %s %s request error: %s", method, path, exc)
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return {"success": False, "data": {}, "error": str(exc)}

        return {"success": False, "data": {}, "error": "Max retries exceeded"}

    async def place_order(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        price: float = 0,
        exchange: str = "NSE",
    ) -> dict:
        """REAL order to Fyers v3. STRICT spec (user-confirmed):
        - type: 1 = LIMIT, 2 = MARKET, 3 = SL, 4 = SL-M (pehle INVERTED tha -
          MARKET ko 1 bhej rahe the -> Fyers usse LIMIT samajhta tha
          limitPrice=0 par -> order broker me rest/reject, kabhi execute nahi)
        - side: 1 = BUY, -1 = SELL (strict int)
        - qty: strict int, lot-size multiple (caller ki zimmedari), >= 1
        - Poori response (code + message) error me detail-log hoti hai taaki
          order rejection ka ASLI reason turant dikhe."""
        fy_order_type_map = {
            "LIMIT": 1,
            "MARKET": 2,
            "SL": 3,
            "SL-M": 4,
        }
        try:
            qty_i = int(quantity)
        except Exception:
            return {"success": False, "data": {}, "error": f"Invalid qty: {quantity!r} (int hona chahiye)"}
        if qty_i < 1:
            return {"success": False, "data": {}, "error": f"Invalid qty: {qty_i} (< 1)"}
        _ot = str(order_type or "MARKET").upper()
        _txn = str(transaction_type or "BUY").upper()
        _px = float(price or 0)
        if _ot == "LIMIT" and _px <= 0:
            return {"success": False, "data": {}, "error": "LIMIT order ke liye price>0 chahiye (market-order bhejo ya price do)"}
        payload = {
            "symbol": symbol,
            "qty": qty_i,
            "type": int(fy_order_type_map.get(_ot, 2)),
            "side": 1 if _txn == "BUY" else -1,
            "productType": "INTRADAY",
            "limitPrice": _px if _ot == "LIMIT" else 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        logger.info("Fyers place_order payload: %s", json.dumps(payload))
        try:
            result = await self._request("POST", "/api/v3/orders/sync", data=payload)
        except Exception as e:
            # API call crash (network/loop) - exact reason log + return, phir
            # kabhi "silent no-order" na lage.
            logger.error("Fyers place_order CRASH %s: %s", symbol, e, exc_info=True)
            return {"success": False, "data": {}, "error": f"Fyers place_order crashed: {str(e)[:200]}"}
        if result["success"]:
            _d = result.get("data") or {}
            result["data"] = {"order_id": _d.get("id", ""), "raw": _d}
            logger.info("Fyers order SENT: %s -> order_id=%s", symbol, _d.get("id", ""))
        else:
            # Poora broker response log (code/message jaise -300, margin,
            # invalid symbol) - rejection ka real reason yahan dikhega.
            _body = result.get("data") or {}
            logger.error(
                "Fyers ORDER REJECTED %s %s qty=%s: status_msg=%r | code=%s | body=%s",
                _txn, symbol, qty_i,
                str(result.get("error") or "")[:300],
                _body.get("code"), json.dumps(_body)[:500],
            )
        return result

    async def modify_order(
        self, order_id: str, quantity: int = None, price: float = None
    ) -> dict:
        payload = {"id": order_id}
        if quantity is not None:
            payload["qty"] = quantity
        if price is not None:
            payload["limitPrice"] = price
        return await self._request("PUT", "/api/v3/orders/sync", data=payload)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("DELETE", "/api/v3/orders/sync", data={"id": order_id})

    async def get_positions(self) -> dict:
        return await self._request("GET", "/api/v3/positions")

    async def get_order_book(self) -> dict:
        """Order book (live orders + fills). Used to verify the real fill
        price after placing an order (averagePrice)."""
        return await self._request("GET", "/api/v3/orders")

    async def get_holdings(self) -> dict:
        return await self._request("GET", "/api/v3/holdings")

    async def get_funds(self) -> dict:
        return await self._request("GET", "/api/v3/funds")

    async def get_profile(self) -> dict:
        """Cheap read-only call used to verify a token for real (readiness
        ping) - a stored token alone does not mean 'connected'."""
        return await self._request("GET", "/api/v3/profile")

    async def validate(self) -> dict:
        """True/False with reason: token alive or not."""
        if not self.access_token:
            return {"success": False, "error": "No access token. OAuth login required."}
        prof = await self.get_profile()
        if prof.get("success"):
            data = prof.get("data") or {}
            name = data.get("display_name") or data.get("name") or ""
            return {"success": True, "error": "", "detail": f"token valid ({name})" if name else "token valid"}
        return {"success": False, "error": str(prof.get("error") or "token invalid")[:200]}

    async def get_option_chain(self, symbol: str, expiry: str) -> dict:
        return await self._request(
            "GET", f"/option-chain?symbol={symbol}&expiry={expiry}"
        )
