"""Angel One SmartAPI live client (verified against official SmartAPI docs).

Auth: POST /rest/auth/angelbroking/user/v1/loginByPassword
      {clientcode, password, totp} + headers (X-PrivateKey api_key, ...)
      -> data {jwtToken, refreshToken, feedToken}.
Orders: POST /rest/secure/angelbroking/order/v1/placeOrder (NFO)
      {variety NORMAL, tradingsymbol, symboltoken, BUY/SELL, MARKET,
       CARRYFORWARD/MARGIN, DAY, price 0, quantity}.
NFO tradingsymbol: UNDERLYING+DDMMMYY+STRIKE+CE/PE e.g. FINNIFTY28OCT2527600PE
Token resolved via searchScrip at order time (no master download needed).
"""
import logging
import time

import httpx

logger = logging.getLogger(__name__)

ROOT = "https://apiconnect.angelone.in"
LOGIN = "/rest/auth/angelbroking/user/v1/loginByPassword"
PLACE = "/rest/secure/angelbroking/order/v1/placeOrder"
BOOK = "/rest/secure/angelbroking/order/v1/getOrderBook"
SEARCH = "/rest/secure/angelbroking/order/v1/searchScrip"


def _totp(secret: str) -> str:
    import pyotp
    return pyotp.TOTP(str(secret).strip().replace(" ", "")).now()


def angel_tsym(underlying: str, expiry_ymd: str, strike: float, option_type: str) -> str:
    """NFO trading symbol e.g. NIFTY15SEP2624800CE."""
    import datetime as _dt
    d = _dt.datetime.strptime(expiry_ymd[:10], "%Y-%m-%d").date()
    mon = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"][d.month - 1]
    return f"{underlying.upper()}{d.day:02d}{mon}{d.strftime('%y')}{int(float(strike))}{option_type.upper()}"


class AngelLive:
    def __init__(self, api_key: str, client_code: str, password: str, totp_secret: str = ""):
        self.api_key = api_key or ""
        self.client_code = client_code or ""
        self.password = password or ""
        self.totp_secret = totp_secret or ""
        self.jwt = ""
        self.refresh_token = ""
        self.feed_token = ""

    def _headers(self, authed: bool = True) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self.api_key,
        }
        if authed and self.jwt:
            h["Authorization"] = f"Bearer {self.jwt}"
        return h

    async def login(self) -> dict:
        if not (self.api_key and self.client_code and self.password):
            return {"success": False, "error": "Angel: api_key + client_code + password required"}
        try:
            totp = _totp(self.totp_secret) if self.totp_secret else ""
        except Exception as e:
            return {"success": False, "error": f"Angel TOTP failed: {e}"}
        payload = {"clientcode": self.client_code, "password": self.password, "totp": totp}
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(ROOT + LOGIN, json=payload, headers=self._headers(False))
            try:
                body = r.json()
            except Exception:
                body = {}
            if r.status_code == 200 and body.get("status") is True:
                data = body.get("data") or {}
                self.jwt = data.get("jwtToken", "")
                self.refresh_token = data.get("refreshToken", "")
                self.feed_token = data.get("feedToken", "")
                if self.jwt:
                    return {"success": True, "jwt": self.jwt,
                            "refresh_token": self.refresh_token, "feed_token": self.feed_token}
            return {"success": False,
                    "error": str(body.get("message") or body.get("errorcode") or body)[:250]}
        except Exception as e:
            return {"success": False, "error": f"Angel login request failed: {str(e)[:150]}"}

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(ROOT + path, json=payload, headers=self._headers(True))
            try:
                body = r.json()
            except Exception:
                body = {"raw": (r.text or "")[:300]}
            if r.status_code == 200 and body.get("status") is True:
                return {"success": True, "data": body.get("data", {}), "error": ""}
            return {"success": False, "data": body.get("data", {}),
                    "error": str(body.get("message") or body.get("errorcode") or body)[:250]}
        except Exception as e:
            return {"success": False, "data": {}, "error": str(e)[:200]}

    async def resolve_fo(self, underlying: str, expiry_ymd: str,
                         strike: float, option_type: str) -> dict:
        """Resolve NFO {tradingsymbol, symboltoken} via searchScrip."""
        tsym = angel_tsym(underlying, expiry_ymd, strike, option_type)
        res = await self._post(SEARCH, {"exchange": "NFO", "searchscrip": tsym})
        if not res.get("success"):
            return {"error": f"Angel searchScrip failed: {res.get('error')}"}
        data = res.get("data") or []
        items = data if isinstance(data, list) else data.get("data") or []
        want = tsym.upper()
        for it in items if isinstance(items, list) else []:
            try:
                if str(it.get("tradingsymbol", "")).upper() == want and it.get("symboltoken"):
                    return {"tradingsymbol": it["tradingsymbol"],
                            "symboltoken": str(it["symboltoken"]),
                            "lotsize": int(it.get("lotsize") or 0),
                            "source": "angel-search"}
            except Exception:
                continue
        return {"error": f"Angel: {tsym} not found in NFO search ({len(items) if isinstance(items, list) else 0} hits)"}

    async def place_order(self, tradingsymbol: str, symboltoken: str, side: str,
                          qty: int, product: str = "CARRYFORWARD") -> dict:
        payload = {
            "variety": "NORMAL",
            "tradingsymbol": tradingsymbol,
            "symboltoken": str(symboltoken),
            "transactiontype": str(side or "BUY").upper(),
            "exchange": "NFO",
            "ordertype": "MARKET",
            "producttype": product,
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(qty)),
        }
        res = await self._post(PLACE, payload)
        if res.get("success"):
            data = res.get("data") or {}
            oid = data.get("orderid") or data.get("uniqueorderid") or ""
            res["data"] = {"order_id": str(oid), "raw": data}
        return res

    async def order_book(self) -> dict:
        return await self._post(BOOK, {})
