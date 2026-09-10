"""DhanHQ TRUE v2 API client for real order execution.

Real Dhan API (https://api.dhan.co/v2):
- Auth headers ONLY: access-token + client-id (+ Content-Type). No signature scheme.
- Place order: POST /v2/orders {dhanClientId, transactionType, exchangeSegment,
  productType, orderType, validity, securityId, quantity, price, triggerPrice}.
- F&O needs numeric securityId -> resolved via option-chain API (light) or the
  official scrip-master CSV (fallback), cached 24h in data/.
"""
import asyncio
import json
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

# Known index underlying codes for the option-chain API (scrip, segment)
UNDERLYING = {
    "NIFTY": (13, "IDX_I"),
    "BANKNIFTY": (25, "IDX_I"),
    "FINNIFTY": (27, "IDX_I"),
    "MIDCPNIFTY": (261, "IDX_I"),
    "SENSEX": (51, "IDX_I"),
}

WEEKLY_IDX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}


def _data_dir() -> str:
    for p in (os.path.join(os.path.dirname(__file__), "..", "..", "data"), "data"):
        try:
            os.makedirs(p, exist_ok=True)
            return p
        except Exception:
            continue
    return "."


class DhanLive:
    def __init__(self, client_id: str, access_token: str):
        self.client_id = client_id or ""
        self.access_token = access_token or ""

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "access-token": self.access_token,
            "client-id": self.client_id,
        }

    async def _request(self, method: str, path: str, data: dict = None, retries: int = 2) -> dict:
        url = f"{BASE_URL}{path}"
        payload = json.dumps(data) if data is not None else ""
        for attempt in range(1, retries + 1):
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    if method == "GET":
                        resp = await client.get(url, headers=self._headers())
                    elif method == "POST":
                        resp = await client.post(url, headers=self._headers(), content=payload)
                    elif method == "DELETE":
                        resp = await client.delete(url, headers=self._headers())
                    else:
                        return {"success": False, "data": {}, "error": f"Unsupported {method}"}
                try:
                    body = resp.json()
                except Exception:
                    body = {"raw": (resp.text or "")[:500]}
                if resp.status_code in (200, 201):
                    inner = body if isinstance(body, dict) else {}
                    if str(inner.get("status", "success")).lower() in ("success", "ok", "pending", "transit"):
                        return {"success": True, "data": inner.get("data", inner), "error": ""}
                    return {"success": False, "data": inner.get("data", inner),
                            "error": str(inner.get("remarks") or inner.get("message") or body)[:300]}
                if resp.status_code == 401:
                    return {"success": False, "data": body, "error": "Dhan token invalid/expired. Reconnect."}
                msg = ""
                try:
                    msg = str(body.get("message") or body.get("remarks") or body)[:300]
                except Exception:
                    msg = f"HTTP {resp.status_code}"
                if attempt < retries and resp.status_code >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return {"success": False, "data": body, "error": msg}
            except httpx.TimeoutException:
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return {"success": False, "data": {}, "error": "Dhan request timed out"}
            except httpx.RequestError as exc:
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return {"success": False, "data": {}, "error": str(exc)[:200]}
        return {"success": False, "data": {}, "error": "Max retries exceeded"}

    # ---------- orders ----------
    async def place_order(self, security_id: str, side: str, qty: int,
                          order_type: str = "MARKET", price: float = 0,
                          trigger_price: float = 0, product: str = "INTRADAY") -> dict:
        payload = {
            "dhanClientId": self.client_id,
            "transactionType": str(side or "BUY").upper(),
            "exchangeSegment": "NSE_FO",
            "productType": product,
            "orderType": str(order_type or "MARKET").upper(),
            "validity": "DAY",
            "securityId": str(security_id),
            "quantity": int(qty),
            "price": float(price or 0),
            "triggerPrice": float(trigger_price or 0),
        }
        res = await self._request("POST", "/orders", data=payload)
        if res.get("success"):
            data = res.get("data") or {}
            oid = data.get("orderId") or data.get("orderid") or data.get("id") or ""
            res["data"] = {"order_id": str(oid), "raw": data}
        return res

    async def renew_token(self) -> dict:
        """Renew access token WITHOUT manual web login, using the existing
        ACTIVE token: GET /v2/RenewToken {access-token, dhanClientId}.
        Returns {success, access_token?} — caller must save the new token."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{BASE_URL}/RenewToken",
                    headers={"access-token": self.access_token,
                             "dhanClientId": self.client_id,
                             "Content-Type": "application/json"},
                )
            try:
                body = resp.json()
            except Exception:
                body = {"raw": (resp.text or "")[:300]}
            if resp.status_code in (200, 201):
                inner = body if isinstance(body, dict) else {}
                new_tok = (inner.get("data") or inner).get("accessToken") if isinstance(inner.get("data"), dict) \
                    else inner.get("accessToken") or inner.get("access_token") or inner.get("token") or ""
                if new_tok:
                    self.access_token = new_tok
                    return {"success": True, "access_token": new_tok, "error": ""}
            msg = ""
            try:
                msg = str(body.get("message") or body.get("remarks") or body)[:250]
            except Exception:
                msg = f"HTTP {resp.status_code}"
            return {"success": False, "data": body, "error": msg or "Renew failed — token expired, manual login needed"}
        except Exception as e:
            return {"success": False, "data": {}, "error": str(e)[:200]}

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("DELETE", f"/orders/{order_id}")

    async def validate(self) -> dict:
        """Read-only token check (holdings). Returns success or exact Dhan
        error with fix guidance. Never places orders."""
        res = await self._request("GET", "/holdings")
        if res.get("success"):
            return {"success": True, "message": "Dhan token valid (holdings readable)."}
        err = str(res.get("error", ""))
        data = str(res.get("data", ""))
        blob = (err + " " + data)
        hint = ""
        if "807" in blob:
            hint = "Token EXPIRED: Dhan dashboard se naya Access Token banao aur paste karo."
        elif "808" in blob:
            hint = "Auth FAILED: Client ID + Access Token ka pair match nahi kar raha. Dono same Dhan account ke hone chahiye, bina space ke paste karo."
        elif "810" in blob:
            hint = "Client ID INVALID: dhanClientId dobara check karo."
        elif "806" in blob:
            hint = "Data APIs not subscribed: token Trading APIs wala hona chahiye."
        out = f"Dhan rejected token. {hint}" if hint else f"Dhan rejected token: {err[:200]}"
        return {"success": False, "error": out, "raw": data[:200]}

    async def get_order(self, order_id: str) -> dict:
        return await self._request("GET", f"/orders/{order_id}")

    # ---------- securityId resolution ----------
    async def resolve_fo(self, underlying: str, expiry_ymd: str,
                         strike: float, option_type: str, exact: bool = True) -> dict:
        """Return {security_id, lot_size, source} or {error}.
        exact=True: only the given expiry date matches. exact=False (Weekly/
        Monthly hint): fall back to nearest live expiry on/after today."""
        underlying = (underlying or "").upper()
        opt = "CE" if str(option_type or "CE").upper().startswith("C") else "PE"
        err1 = ""
        if underlying in UNDERLYING:
            try:
                r = await self._resolve_via_optionchain(underlying, expiry_ymd, strike, opt)
                if r.get("security_id"):
                    return r
                err1 = r.get("error", "")
            except Exception as e:
                err1 = str(e)[:150]
        try:
            r = await asyncio.to_thread(self._resolve_via_master, underlying, expiry_ymd, strike, opt, exact)
            if r.get("security_id"):
                return r
            err2 = r.get("error", "")
        except Exception as e:
            err2 = str(e)[:150]
        return {"error": f"Dhan securityId not resolved (chain: {err1 or 'n/a'}; master: {err2 or 'n/a'}). "
                         f"Verify expiry {expiry_ymd} exists for {underlying}."}

    async def _resolve_via_optionchain(self, underlying: str, expiry_ymd: str,
                                        strike: float, opt: str) -> dict:
        scrip, seg = UNDERLYING[underlying]
        exps = await self._request("POST", "/optionchain/expirylist",
                                   data={"UnderlyingScrip": scrip, "UnderlyingSeg": seg})
        if not exps.get("success"):
            return {"error": f"expiry-list failed: {exps.get('error')}"}
        exp_data = (exps.get("data") or {})
        # Official response: data = ["YYYY-MM-DD", ...] plain list
        exp_list = exp_data if isinstance(exp_data, list) else exp_data.get("data") or []
        exp_match = ""
        for e in exp_list if isinstance(exp_list, list) else []:
            es = str(e if isinstance(e, str) else e.get("expiry", e.get("date", "")))[:10]
            if es == expiry_ymd[:10]:
                exp_match = es
                break
        if not exp_match:
            return {"error": f"expiry {expiry_ymd[:10]} not in Dhan list ({len(exp_list) if isinstance(exp_list, list) else 0} expiries)"}
        oc = await self._request("POST", "/optionchain",
                                 data={"UnderlyingScrip": scrip, "UnderlyingSeg": seg, "Expiry": exp_match})
        if not oc.get("success"):
            return {"error": f"optionchain failed: {oc.get('error')}"}
        data = oc.get("data") or {}
        strikes = data.get("oc") or data.get("strikes") or {}
        for k, v in (strikes.items() if isinstance(strikes, dict) else []):
            try:
                if abs(float(str(k).replace(",", "")) - float(strike)) > 0.01:
                    continue
            except Exception:
                continue
            if not isinstance(v, dict):
                continue
            leg = v.get(opt.lower()) or v.get(opt) or {}
            for key in ("security_id", "securityId", "instrument_token", "token", "securityID"):
                if leg.get(key):
                    return {"security_id": str(leg[key]),
                            "lot_size": int(leg.get("lot_size") or leg.get("lotSize") or 0),
                            "source": "dhan-optionchain"}
        return {"error": f"strike {strike}{opt} not found in chain for {exp_match}"}

    def _resolve_via_master(self, underlying: str, expiry_ymd: str,
                             strike: float, opt: str, exact_only: bool = True) -> dict:
        """Stream the official scrip-master CSV, keep only matching NSE F&O rows.
        exact_only=False: nearest expiry on/after today is accepted (for
        Weekly/Monthly hints when the weekday rule drifts, e.g. holidays)."""
        import csv
        import datetime as _dt
        d = _dt.datetime.strptime(expiry_ymd[:10], "%Y-%m-%d").date()
        today = (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).date()
        cache_path = os.path.join(_data_dir(), f"dhan_master_{underlying}.json")
        now = time.time()
        try:
            if os.path.exists(cache_path) and now - os.path.getmtime(cache_path) < 24 * 3600:
                with open(cache_path) as f:
                    cached = json.load(f)
                for row in cached:
                    if self._master_match(row, d, strike, opt):
                        return {"security_id": row["sid"], "lot_size": row.get("lot", 0),
                                "source": "dhan-master-cache", "symbol": row.get("tsym", "")}
        except Exception:
            pass
        import requests
        try:
            resp = requests.get(MASTER_URL, timeout=60, stream=True,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return {"error": f"master download HTTP {resp.status_code}"}
            matches = []
            near = []  # (expiry_date, row) candidates for fallback
            seen_exp = set()
            lines = (ln.decode("utf-8", errors="replace") if isinstance(ln, (bytes, bytearray)) else ln
                     for ln in resp.iter_lines())
            reader = csv.DictReader(lines)
            for row in reader:
                try:
                    exch = str(row.get("SEM_EXM_EXCH_ID", "") or "").strip().upper()
                    seg = str(row.get("SEM_SEGMENT", "") or "").strip().upper()
                    itype = str(row.get("SEM_EXCH_INSTRUMENT_TYPE", "") or "").strip().upper()
                    # SEGMENT 'D' = NSE derivatives; also accept FO/FUT/OPT codes/types
                    seg_ok = (seg == "D" or "FO" in seg.replace("_", "")
                              or "FUT" in seg or "OPT" in seg or itype in ("OP", "FUT"))
                    if exch != "NSE" or not seg_ok:
                        continue
                    iname = str(row.get("SEM_INSTRUMENT_NAME", "") or row.get("SEM_INSTRUMENT", "") or "").strip().upper()
                    if iname and underlying not in (iname, iname.replace(" ", "")):
                        tsym0 = str(row.get("SEM_TRADING_SYMBOL", "") or "").upper().replace(" ", "")
                        # Trading symbols use dashes: NIFTY-Sep2026-24800-CE
                        if not (tsym0.startswith(underlying) or tsym0.split("-")[0] == underlying):
                            continue
                    norm = {
                        "sid": str(row.get("SEM_SMST_SECURITY_ID", "") or ""),
                        "tsym": str(row.get("SEM_TRADING_SYMBOL", "") or ""),
                        "exp": str(row.get("SEM_EXPIRY_DATE", "") or ""),
                        "strike": str(row.get("SEM_STRIKE_PRICE", "") or ""),
                        "otype": str(row.get("SEM_OPTION_TYPE", "") or ""),
                        "lot": int(float(row.get("SEM_LOT_UNITS", 0) or 0)),
                        "iname": iname,
                    }
                    if not norm["sid"]:
                        continue
                    try:
                        _ed = _dt.datetime.strptime(norm["exp"].strip()[:10], "%Y-%m-%d").date()
                        if _ed >= today:
                            seen_exp.add(str(_ed))
                    except Exception:
                        pass
                    if self._master_match(norm, d, strike, opt):
                        matches.append(norm)
                        if len(matches) >= 5:
                            break
                    elif not exact_only:
                        try:
                            ed = _dt.datetime.strptime(norm["exp"].strip()[:10], "%Y-%m-%d").date()
                        except Exception:
                            continue
                        if ed >= today:
                            try:
                                rstrike = float(str(norm["strike"] or "nan").replace(",", ""))
                            except Exception:
                                continue
                            if abs(rstrike - float(strike)) > 0.01:
                                continue
                            otype = str(norm["otype"] or "").upper()
                            tail = norm["tsym"].replace(" ", "").upper()
                            ok = (otype in ("CE", "CALL") or tail.endswith(("CE", "CALL"))) if opt == "CE" else \
                                 (otype in ("PE", "PUT") or tail.endswith(("PE", "PUT")))
                            if ok:
                                near.append((ed, norm))
                except Exception:
                    continue
            if not matches and near:
                near.sort(key=lambda x: x[0])
                matches = [near[0][1]]
            if matches:
                try:
                    with open(cache_path, "w") as f:
                        json.dump(matches, f)
                except Exception:
                    pass
                m = matches[0]
                src = "dhan-master" if self._master_match(m, d, strike, opt) else "dhan-master-nearest"
                return {"security_id": m["sid"], "lot_size": m.get("lot", 0),
                        "source": src, "symbol": m.get("tsym", ""),
                        "expiry": str(m.get("exp", ""))[:10]}
            return {"error": "no matching row in scrip master"
                     + (f". {underlying} expiries available: {', '.join(sorted(seen_exp)[:8])}" if seen_exp else "")}
        except Exception as e:
            return {"error": f"master failed: {str(e)[:150]}"}

    @staticmethod
    def _master_match(row: dict, exp_date, strike: float, opt: str) -> bool:
        try:
            tsym = str(row.get("tsym", "") or "").upper()
            exp_ok = False
            for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d-%m-%Y", "%Y%m%d"):
                try:
                    exp_ok = __import__("datetime").datetime.strptime(
                        str(row.get("exp", "")).strip()[:10], fmt).date() == exp_date
                    if exp_ok:
                        break
                except Exception:
                    continue
            if not exp_ok:
                # trading-symbol date fragment like 09SEP / 09SEP26
                frags = [exp_date.strftime("%d%b").upper(), exp_date.strftime("%d%b%y").upper(),
                         exp_date.strftime("%d-%b-%Y").upper()]
                exp_ok = any(f in tsym.replace(" ", "") for f in frags)
            if not exp_ok:
                return False
            try:
                rstrike = float(str(row.get("strike", "") or "nan").replace(",", ""))
                strike_ok = abs(rstrike - float(strike)) < 0.01
            except Exception:
                strike_ok = str(int(float(strike))) in tsym
            if not strike_ok:
                return False
            otype = str(row.get("otype", "") or "").upper()
            tail = tsym.replace(" ", "")
            if opt == "CE":
                return otype in ("CE", "CALL") or tail.endswith("CE") or tail.endswith("CALL")
            return otype in ("PE", "PUT") or tail.endswith("PE") or tail.endswith("PUT")
        except Exception:
            return False
