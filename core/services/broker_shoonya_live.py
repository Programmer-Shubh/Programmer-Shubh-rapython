"""Shoonya/Finvasia Noren API live client (verified against official API docs).

Auth: POST https://api.shoonya.com/NorenWClientTP/QuickAuth
      {uid, pwd:SHA256(password), factor2:TOTP, vc, appkey:SHA256(uid|api_key),
       imei, source:API} -> {stat:Ok, susertoken, actid, ...}.
Resolve: POST SearchScrip {uid, exch:NFO, stext} (needs session).
Orders: POST PlaceOrder {uid, actid, exch:NFO, tsym, qty, prc, prd M/I,
      trantype B/S, prctyp MKT, ret DAY} -> {stat:Ok, norenordno}.
"""
import hashlib
import logging

import httpx

logger = logging.getLogger(__name__)

ROOT = "https://api.shoonya.com/NorenWClientTP"


def _sha256(s: str) -> str:
    import hashlib as _h
    return _h.sha256(str(s or "").encode()).hexdigest()


def _totp(secret: str) -> str:
    import pyotp
    return pyotp.TOTP(str(secret).strip().replace(" ", "")).now()


class ShoonyaLive:
    def __init__(self, uid: str, password: str, totp_secret: str = "",
                 vendor_code: str = "", api_key: str = "", imei: str = "abc123"):
        self.uid = (uid or "").strip()
        self.password = password or ""
        self.totp_secret = totp_secret or ""
        self.vc = vendor_code or ""
        self.api_key = api_key or ""
        self.imei = imei or "abc123"
        self.susertoken = ""
        self.actid = ""

    async def login(self) -> dict:
        if not (self.uid and self.password and self.vc and self.api_key):
            return {"success": False,
                    "error": "Shoonya: uid + password + vendor code + api key required"}
        try:
            factor2 = _totp(self.totp_secret) if self.totp_secret else ""
        except Exception as e:
            return {"success": False, "error": f"Shoonya TOTP failed: {e}"}
        import json as _j
        payload = {"source": "API", "apkversion": "1.0.0", "uid": self.uid,
                   "pwd": _sha256(self.password), "factor2": factor2,
                   "vc": self.vc, "appkey": _sha256(f"{self.uid}|{self.api_key}"),
                   "imei": self.imei}
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{ROOT}/QuickAuth",
                                 data={"jData": _j.dumps(payload), "jKey": ""})
            try:
                body = r.json()
            except Exception:
                body = {}
            if isinstance(body, dict) and body.get("stat") == "Ok" and body.get("susertoken"):
                self.susertoken = body["susertoken"]
                self.actid = body.get("actid", self.uid)
                return {"success": True, "susertoken": self.susertoken, "actid": self.actid}
            detail = ""
            try:
                detail = str(body.get("emsg") or body)[:200] if isinstance(body, dict) else str(body)[:200]
            except Exception:
                detail = "unknown"
            return {"success": False, "error": f"Shoonya rejected login: {detail or 'check uid/password/TOTP/vendor-code/api-key'}"}
        except Exception as e:
            return {"success": False, "error": f"Shoonya login failed: {str(e)[:150]}"}

    async def _api(self, endpoint: str, payload: dict) -> dict:
        import json as _j
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{ROOT}/{endpoint}",
                                 data={"jData": _j.dumps(payload), "jKey": self.susertoken})
            try:
                body = r.json()
            except Exception:
                body = {}
            if isinstance(body, dict) and body.get("stat") == "Ok":
                return {"success": True, "data": body, "error": ""}
            return {"success": False, "data": body,
                    "error": str((body or {}).get("emsg") or body)[:250]}
        except Exception as e:
            return {"success": False, "data": {}, "error": str(e)[:200]}

    async def resolve_fo(self, underlying: str, expiry_ymd: str,
                         strike: float, option_type: str) -> dict:
        """Resolve NFO tsym via SearchScrip. tsym shape: UNDERLYING+DDMMMYY+C/P+STRIKE."""
        import datetime as _dt
        d = _dt.datetime.strptime(expiry_ymd[:10], "%Y-%m-%d").date()
        mon = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"][d.month - 1]
        frag = f"{d.day:02d}{mon}{d.strftime('%y')}"
        tail = f"{'C' if str(option_type or 'CE').upper().startswith('C') else 'P'}{int(float(strike))}"
        res = await self._api("SearchScrip", {"uid": self.uid, "exch": "NFO",
                                              "stext": f"{underlying.upper()} {frag} {tail[0]}"})
        if not res.get("success"):
            return {"error": f"Shoonya search failed: {res.get('error')}"}
        vals = (res.get("data") or {}).get("values") or []
        want_end = tail.upper()
        cands = []
        for v in vals if isinstance(vals, list) else []:
            try:
                tsym = str(v.get("tsym", "")).upper()
                if frag in tsym.replace(" ", "") and tsym.replace(" ", "").endswith(want_end):
                    cands.append(v)
            except Exception:
                continue
        if not cands:
            return {"error": f"Shoonya: no NFO contract like *{frag}*{want_end} ({len(vals) if isinstance(vals, list) else 0} hits)"}
        best = cands[0]
        try:
            return {"tsym": best["tsym"], "token": str(best.get("token", "")),
                    "lot_size": int(best.get("ls") or 0), "source": "shoonya-search"}
        except Exception as e:
            return {"error": f"Shoonya match parse failed: {e}"}

    async def place_order(self, tsym: str, side: str, qty: int,
                          product: str = "M") -> dict:
        """product M=NRML (positional) / I=MIS (intraday)."""
        res = await self._api("PlaceOrder", {
            "uid": self.uid, "actid": self.actid or self.uid, "exch": "NFO",
            "tsym": tsym, "qty": str(int(qty)), "dscqty": "0", "prc": "0",
            "prd": product, "trantype": "B" if str(side or "BUY").upper() == "BUY" else "S",
            "prctyp": "MKT", "ret": "DAY", "ordersource": "API",
        })
        if res.get("success"):
            data = res.get("data") or {}
            res["data"] = {"order_id": str(data.get("norenordno", "")), "raw": data}
        return res
