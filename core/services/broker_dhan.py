import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


class DhanHQError(Exception):
    pass


class DhanHQ:
    BASE_URL = "https://api.dhan.co"
    MAX_RETRIES = 3

    def __init__(self, client_id: str, access_token: str, refresh_token: str = None):
        self.client_id = client_id
        self.access_token = access_token
        self.refresh_token = refresh_token
        self._token_expiry: float = 0

    def _generate_signature(self, payload: str = "") -> str:
        timestamp = str(int(time.time()))
        message = self.client_id + timestamp + payload
        return hmac.new(
            self.access_token.encode(), message.encode(), hashlib.sha256
        ).hexdigest()

    def _build_headers(self, payload: str = "") -> dict:
        return {
            "Content-Type": "application/json",
            "access-token": self.access_token,
            "client-id": self.client_id,
            "signature": self._generate_signature(payload),
            "timestamp": str(int(time.time())),
        }

    async def _request(
        self, method: str, path: str, data: dict = None, retries: int = MAX_RETRIES
    ) -> dict:
        url = f"{self.BASE_URL}{path}"
        payload = json.dumps(data) if data else ""
        headers = self._build_headers(payload)

        for attempt in range(1, retries + 1):
            try:
                logger.info(
                    "DhanHQ %s %s attempt=%d/%d", method, path, attempt, retries
                )
                async with httpx.AsyncClient(timeout=30) as client:
                    if method == "GET":
                        resp = await client.get(url, headers=headers)
                    elif method == "POST":
                        resp = await client.post(url, headers=headers, content=payload)
                    elif method == "PUT":
                        resp = await client.put(url, headers=headers, content=payload)
                    elif method == "DELETE":
                        resp = await client.delete(url, headers=headers)
                    else:
                        raise DhanHQError(f"Unsupported method: {method}")

                body = resp.json()

                if resp.status_code == 401:
                    logger.warning("DhanHQ token expired or invalid")
                    return {
                        "success": False,
                        "data": body,
                        "error": "Token expired or invalid. Please re-authenticate.",
                    }

                if resp.status_code >= 400:
                    error_msg = body.get("message", body.get("error", str(body)))
                    logger.error(
                        "DhanHQ %s %s returned %d: %s",
                        method, path, resp.status_code, error_msg,
                    )
                    if attempt < retries and resp.status_code >= 500:
                        wait = 2 ** attempt
                        logger.info("Retrying in %ds...", wait)
                        await asyncio.sleep(wait)
                        continue
                    return {"success": False, "data": body, "error": error_msg}

                logger.info("DhanHQ %s %s succeeded", method, path)
                return {"success": True, "data": body, "error": ""}

            except httpx.TimeoutException:
                logger.warning(
                    "DhanHQ %s %s timed out attempt=%d", method, path, attempt
                )
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return {
                    "success": False,
                    "data": {},
                    "error": f"Request timed out after {retries} attempts",
                }
            except httpx.RequestError as exc:
                logger.error("DhanHQ %s %s request error: %s", method, path, exc)
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
        trigger_price: float = 0,
        exchange: str = "NSE_FO",
    ) -> dict:
        payload = {
            "dhanClientId": self.client_id,
            "transactionType": transaction_type.upper(),
            "exchangeSegment": exchange,
            "productType": "INTRADAY",
            "orderType": order_type.upper(),
            "symbol": symbol,
            "quantity": str(quantity),
            "price": str(price),
            "triggerPrice": str(trigger_price),
            "validity": "DAY",
        }
        result = await self._request("POST", "/orders", data=payload)
        if result["success"]:
            result["data"] = {"order_id": result["data"].get("orderId", "")}
        return result

    async def modify_order(
        self, order_id: str, quantity: int = None, price: float = None
    ) -> dict:
        payload = {"orderId": order_id}
        if quantity is not None:
            payload["quantity"] = str(quantity)
        if price is not None:
            payload["price"] = str(price)
        return await self._request("PUT", f"/orders/{order_id}", data=payload)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("DELETE", f"/orders/{order_id}")

    async def cancel_all_orders(self) -> dict:
        return await self._request("DELETE", "/orders/cancel-all")

    async def get_order_status(self, order_id: str) -> dict:
        return await self._request("GET", f"/orders/{order_id}")

    async def get_positions(self) -> dict:
        return await self._request("GET", "/positions")

    async def get_holdings(self) -> dict:
        return await self._request("GET", "/holdings")

    async def get_funds(self) -> dict:
        return await self._request("GET", "/funds")

    async def get_option_chain(self, symbol: str, expiry: str) -> dict:
        params = f"?symbol={symbol}&expiry={expiry}"
        return await self._request("GET", f"/option-chain{params}")
