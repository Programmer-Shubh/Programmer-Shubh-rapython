"""Fyers AUTO-LOGIN (daily no-login): Fyers access token roz expire hota hai
aur company ne refresh-token API SEBI guideline me disable kar rakha hai.
Isliye ye module POORA login automate karta hai - jaise Mobile app kar raha
hota hai - bina browser/OTP-on-phone ke:

  1) send_login_otp (_v3 -> _v2 fallback): fy_id triggers its session
  2) verify_otp: TOTP secret (user ne Fyers profile me 2FA-TOTP enable kiya
     aur uska secret app ko diya) se LOCAL OTP generate hota hai
  3) verify_pin_v2: 4-digit trading PIN
  4) /api/v3/token (Bearer) -> 308 redirect URL me auth_code
  5) /api/v3/validate-authcode (appIdHash) -> ACCESS TOKEN

Token AIKO persist ho jata hai (routes/broker._save_config) - daily bina
login ke trading chalti rahegi.

Setup jarooriat (Brokers tab Fyers card):
  - fy_id        (FX12345)
  - totp_secret  (2FA TOTP ka secret - Fyers profile se)
  - pin          (4-digit)
  - app_id + secret + redirect_uri (pehle se hai)
  Env: RATRADE_FYERS_FY_ID / RATRADE_FYERS_TOTP_SECRET / RATRADE_FYERS_PIN
"""
import base64
import hashlib
import json
import logging

import httpx

logger = logging.getLogger(__name__)

_VAGATOR = "https://api-t2.fyers.in/vagator/v2"
_TOKEN_URLS = [
    "https://api-t1.fyers.in/api/v3/token",
    "https://api.fyers.in/api/v2/token",        # purana host fallback
]
_VALIDATE_URLS = [
    "https://api-t1.fyers.in/api/v3/validate-authcode",
    "https://api.fyers.in/api/v2/validate-authcode",
]

_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "text/plain",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
}


def _b64(v) -> str:
    return base64.b64encode(str(v or "").encode()).decode()


def _totp_now(secret: str) -> str:
    import pyotp
    s = str(secret or "").replace(" ", "").upper()
    if not s:
        raise ValueError("totp_secret khali")
    return pyotp.TOTP(s).now()


def missing_creds(cfg: dict) -> list:
    """Auto-login ke liye KIS-KIS cheez ki supplied kammi hai (UI message)."""
    out = []
    if not str(cfg.get("fy_id") or "").strip():
        out.append("fy_id")
    if not str(cfg.get("totp_secret") or "").strip():
        out.append("totp_secret")
    if not str(cfg.get("pin") or "").strip():
        out.append("pin")
    if not str(cfg.get("app_id") or "").strip():
        out.append("app_id")
    if not str(cfg.get("secret") or "").strip():
        out.append("secret")
    return out


async def auto_login(cfg: dict) -> dict:
    """POORA headless Fyers login. Returns
    {success, access_token, refresh_token, error, detail}."""
    cfg = dict(cfg or {})
    miss = missing_creds(cfg)
    if miss:
        return {"success": False, "error":
                (f"Fyers auto-login ke liye ye cheezein chahiye: {', '.join(miss)} "
                 f"- Brokers tab Fyers card me bharo (TOTP secret Fyers profile "
                 f"ke 2FA-TOTP section se milta hai)")}
    fy_id = str(cfg.get("fy_id")).strip()
    app_id_full = str(cfg.get("app_id")).strip()          # XXXXXXX-100
    app_id_bare = app_id_full.split("-")[0]               # XXXXXXX
    app_type = "100"
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=30, follow_redirects=True) as c:
            # 1) send_login_otp (v3 phir v2)
            rkey = None
            _last = ""
            for path in ("/send_login_otp_v3", "/send_login_otp_v2"):
                resp = await c.post(_VAGATOR + path, content=json.dumps(
                    {"fy_id": _b64(fy_id), "app_id": "2"}))
                if resp.status_code == 200:
                    body = resp.json()
                    rkey = body.get("request_key") or (body.get("data") or {}).get("request_key")
                    if rkey:
                        break
                _last = (resp.text or "")[:200]
            if not rkey:
                return {"success": False, "error":
                        f"Fyers login step-1 (OTP trigger) fail: {_last}"}

            # 2) verify_otp — LOCAL TOTP (koi phone OTP nahi)
            resp = await c.post(_VAGATOR + "/verify_otp", content=json.dumps(
                {"request_key": rkey, "otp": _totp_now(cfg.get("totp_secret"))}))
            if resp.status_code != 200:
                return {"success": False, "error":
                        f"Fyers OTP verify fail: {(resp.text or '')[:200]} "
                        f"(TOTP secret galat/expired ho sakta hai)"}
            rkey = resp.json().get("request_key")

            # 3) verify PIN
            resp = await c.post(_VAGATOR + "/verify_pin_v2", content=json.dumps(
                {"request_key": rkey, "identity_type": "pin",
                 "identifier": _b64(cfg.get("pin"))}))
            if resp.status_code != 200:
                return {"success": False, "error":
                        f"Fyers PIN verify fail: {(resp.text or '')[:200]}"}
            sess_tok = ((resp.json().get("data") or {}).get("access_token") or "")

            # 4) app-token exchange -> 308 redirect with auth_code
            auth_code = ""
            _last = ""
            for turl in _TOKEN_URLS:
                resp = await c.post(turl, content=json.dumps({
                    "fyers_id": fy_id, "app_id": app_id_bare,
                    "redirect_uri": str(cfg.get("redirect_uri") or "").strip(),
                    "appType": app_type, "code_challenge": "",
                    "state": "None", "scope": "", "nonce": "",
                    "response_type": "code", "create_cookie": True,
                }), headers={"Authorization": f"Bearer {sess_tok}",
                             "Content-Type": "application/json; charset=UTF-8"})
                try:
                    url = (resp.json() or {}).get("Url", "")
                except Exception:
                    url = ""
                if resp.status_code == 308 and url:
                    try:
                        import urllib.parse as _up
                        auth_code = _up.parse_qs(_up.urlparse(url).query).get("auth_code", [""])[0]
                    except Exception:
                        auth_code = ""
                    if auth_code:
                        break
                _last = (resp.text or "")[:200]
            if not auth_code:
                return {"success": False, "error":
                        f"Fyers auth-code step fail: {_last} "
                        f"(Redirect URI app settings me EXACT match hona chahiye)"}

            # 5) access_token
            a_hash = hashlib.sha256(f"{app_id_full}:{cfg.get('secret')}".encode()).hexdigest()
            for vurl in _VALIDATE_URLS:
                resp = await c.post(vurl, json={
                    "grant_type": "authorization_code",
                    "appIdHash": a_hash, "code": auth_code})
                if resp.status_code == 200:
                    body = resp.json()
                    tok = body.get("access_token")
                    if tok:
                        logger.info("Fyers AUTO-LOGIN ok (daily headless)")
                        return {"success": True,
                                "access_token": tok,
                                "refresh_token": body.get("refresh_token", ""),
                                "error": ""}
            return {"success": False, "error":
                    f"Fyers validate-authcode fail: {(resp.text or '')[:200]}"}
    except Exception as e:
        logger.error("Fyers auto-login crashed: %s", e, exc_info=True)
        return {"success": False, "error": f"auto-login crash: {str(e)[:180]}"}
