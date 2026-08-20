import asyncio
import json
import logging
import time
import urllib.parse
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


class FyersV3Error(Exception):
    pass


class FyersV3:
    BASE_URL = "https://api.fyers.in"
    AUTH_URL = "https://api.fyers.in/api/v2/generate-authcode"
    TOKEN_URL = "https://api.fyers.in/api/v2/token"
    MAX_RETRIES = 3

    def __init__(
        self,
        app_id: str,
        secret_key: str,
        redirect_uri: str,
        access_token: str = None,
        refresh_token: str = None,
    ):
        self.app_id = app_id
        self.secret_key = secret_key
        self.redirect_uri = redirect_uri
        self.access_token = access_token
        self.refresh_token = refresh_token
        self._token_expiry: float = 0

    def get_auth_url(self) -> str:
        params = {
            "client_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": "state_value",
        }
        query = urllib.parse.urlencode(params)
        return f"{self.AUTH_URL}?{query}"

    async def generate_token(self, auth_code: str) -> dict:
        payload = {
            "grant_type": "authorization_code",
            "appId": self.app_id,
            "secretKey": self.secret_key,
            "code": auth_code,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self.TOKEN_URL, json=payload)
            body = resp.json()
            if resp.status_code == 200 and body.get("access_token"):
                self.access_token = body["access_token"]
                self.refresh_token = body.get("refresh_token")
                self._token_expiry = time.time() + body.get("expires_in", 86400)
                logger.info("Fyers token generated successfully")
                return {"success": True, "data": body, "error": ""}
            return {"success": False, "data": body, "error": body.get("message", "Token generation failed")}
        except httpx.RequestError as exc:
            logger.error("Fyers token generation error: %s", exc)
            return {"success": False, "data": {}, "error": str(exc)}

    async def refresh_access_token(self) -> dict:
        if not self.refresh_token:
            return {"success": False, "data": {}, "error": "No refresh token available"}
        payload = {
            "grant_type": "refresh_token",
            "appId": self.app_id,
            "secretKey": self.secret_key,
            "refresh_token": self.refresh_token,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self.TOKEN_URL, json=payload)
            body = resp.json()
            if resp.status_code == 200 and body.get("access_token"):
                self.access_token = body["access_token"]
                self.refresh_token = body.get("refresh_token", self.refresh_token)
                self._token_expiry = time.time() + body.get("expires_in", 86400)
                logger.info("Fyers token refreshed successfully")
                return {"success": True, "data": body, "error": ""}
            return {"success": False, "data": body, "error": body.get("message", "Token refresh failed")}
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
        headers = {
            "Authorization": self.access_token,
            "Content-Type": "application/json",
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
                        headers["Authorization"] = self.access_token
                        continue
                    return {
                        "success": False,
                        "data": body,
                        "error": "Token expired. Re-authenticate.",
                    }

                if resp.status_code >= 400 or body.get("code", 0) != 0:
                    error_msg = body.get("message", body.get("error", str(body)))
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
        fy_order_type_map = {
            "MARKET": 1,
            "LIMIT": 2,
            "SL": 3,
            "SL-M": 4,
        }
        payload = {
            "symbol": symbol,
            "qty": quantity,
            "type": fy_order_type_map.get(order_type.upper(), 1),
            "side": 1 if transaction_type.upper() == "BUY" else -1,
            "productType": "INTRADAY",
            "limitPrice": price if order_type.upper() == "LIMIT" else 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        result = await self._request("POST", "/orders/sync", data=payload)
        if result["success"]:
            result["data"] = {"order_id": result["data"].get("id", "")}
        return result

    async def modify_order(
        self, order_id: str, quantity: int = None, price: float = None
    ) -> dict:
        payload = {"id": order_id}
        if quantity is not None:
            payload["qty"] = quantity
        if price is not None:
            payload["limitPrice"] = price
        return await self._request("PUT", "/orders/sync", data=payload)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("DELETE", "/orders/sync", data={"id": order_id})

    async def get_positions(self) -> dict:
        return await self._request("GET", "/positions")

    async def get_holdings(self) -> dict:
        return await self._request("GET", "/holdings")

    async def get_funds(self) -> dict:
        return await self._request("GET", "/funds")

    async def get_option_chain(self, symbol: str, expiry: str) -> dict:
        return await self._request(
            "GET", f"/option-chain?symbol={symbol}&expiry={expiry}"
        )
