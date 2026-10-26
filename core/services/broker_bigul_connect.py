"""Bigul Connect (Bonanza) client-API login automation.

Official docs (read & verified): https://connect.bigul.co/docs

- Client API environment : base URL https://capi.bigul.co , source "B2C"
- Login API              : POST /auth/connect/login
                           headers: x-api-key, x-api-secret
                           body  : {"source":"B2C","clientCode":"..","totp":"6-digit"}
                           resp  : {"status":true,"data":{"token":"JWT..."}}
- Token                  : valid until 12:30 AM daily -> roz auto-login se generate
- Validate (profile)     : POST /auth/connect/get-client-profile?clientCode=&source=
                           headers: x-api-key, x-api-secret, Authorization: JWT
- IP whitelist           : API dashboard me server IP whitelist hona zaroori hai;
                           warna 403 "requesting IP address is not authorized".

Login automation jaise Fyers: credentials (api_key + api_secret + client_id +
totp_secret) setup me save hain to login() khud TOTP generate karke session
token laata hai - mobile OTP/manual steps ki koi zarurat nahi.
"""
import pyotp


ROOT = "https://capi.bigul.co"
SOURCE = "B2C"
LOGIN_PATH = "/auth/connect/login"
PROFILE_PATH = "/auth/connect/get-client-profile"


def _totp(secret: str) -> str:
    """TOTP secret se abhi ka 6-digit code (spaces tolerate)."""
    return pyotp.TOTP(str(secret or "").strip().replace(" ", "")).now()


class BigulConnect:
    """Bigul Connect REST client (sirf auth + validation; orders baad me add
    hone par iske upar hi banaenge)."""

    def __init__(self, api_key: str = "", api_secret: str = "", client_code: str = "",
                 totp_secret: str = "", base_url: str = "", token: str = ""):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.client_code = (client_code or "").strip()
        self.totp_secret = (totp_secret or "").strip()
        self.base = (str(base_url or "").strip().rstrip("/") or ROOT)
        self.token = (token or "").strip()

    async def login(self) -> dict:
        """Official TOTP login: token generate karo (valid till 12:30 AM)."""
        if not (self.api_key and self.api_secret and self.client_code):
            return {"success": False,
                    "error": "Bigul: x-api-key + x-api-secret + clientCode required (Bigul Connect dashboard se)"}
        try:
            totp = _totp(self.totp_secret) if self.totp_secret else ""
        except Exception as e:
            return {"success": False, "error": f"Bigul TOTP failed: {str(e)[:150]}"}
        if not totp:
            return {"success": False, "error": "Bigul: totp_secret missing (Authenticator secret)"}
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "x-api-key": self.api_key, "x-api-secret": self.api_secret}
        payload = {"source": SOURCE, "clientCode": self.client_code, "totp": totp}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(self.base + LOGIN_PATH, json=payload, headers=headers)
            try:
                body = r.json()
            except Exception:
                body = {"raw": (r.text or "")[:300]}
            if r.status_code == 200 and body.get("status") is True:
                data = body.get("data") or {}
                self.token = str(data.get("token") or "")
                if self.token:
                    return {"success": True, "token": self.token,
                            "message": body.get("message", "TOTP verified successfully")}
            err = str(body.get("error") or body.get("message") or body)[:250]
            if r.status_code == 403 or "not authorized" in err.lower():
                err += " (Bigul Connect dashboard me server IP whitelist karo; Render/static-IP par khud IP add karo)"
            return {"success": False, "error": err}
        except Exception as e:
            return {"success": False, "error": f"Bigul login request failed: {str(e)[:150]}"}

    async def validate(self) -> dict:
        """Token live validate: client-profile call (401/403/expired = fail)."""
        if not self.token:
            return {"success": False, "error": "No token"}
        if not (self.api_key and self.api_secret):
            return {"success": False, "error": "Bigul: x-api-key/x-api-secret missing for validation"}
        headers = {"Accept": "application/json", "x-api-key": self.api_key,
                   "x-api-secret": self.api_secret, "Authorization": self.token}
        try:
            import httpx
            url = (f"{self.base}{PROFILE_PATH}"
                   f"?clientCode={self.client_code}&source={SOURCE}")
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, json={}, headers=headers)
            try:
                body = r.json()
            except Exception:
                body = {"raw": (r.text or "")[:300]}
            if r.status_code == 200 and body.get("status") is True:
                return {"success": True, "profile": body.get("data"), "error": ""}
            return {"success": False,
                    "error": str(body.get("message") or body.get("error") or body)[:200]}
        except Exception as e:
            return {"success": False, "error": f"Bigul validate failed: {str(e)[:150]}"}
