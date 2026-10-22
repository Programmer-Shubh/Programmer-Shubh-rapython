"""Fyers API v3 broker automation - Historical Data (OHLC candles).

Dhan automation jaisa hi: Client ID + Access Token se automated auth,
/efm/v3/history par candles, raw list -> pandas DataFrame, epoch -> readable
IST datetime, basic error-handling + rate-limit safety.

Symbols: 'NSE:NIFTY50-INDEX', 'NSE:SBIN-EQ', NFO options 'NSE:SBIN24500CE'
Docs: Fyers API v3 fyers-apiv3 SDK (SessionModel + FyersModel.history).
"""
import pandas as pd


def generate_access_token(client_id: str, secret_key: str, redirect_uri: str,
                          auth_code: str) -> dict:
    """Automation auth setup: authorization_code -> access_token (+ refresh).
    auth_code = login URL se mila ?auth_code=..."""
    from fyers_apiv3 import fyersModel
    session = fyersModel.SessionModel(
        client_id=client_id, secret_key=secret_key, redirect_uri=redirect_uri,
        response_type="code", grant_type="authorization_code")
    session.auth_code = auth_code
    resp = session.generate_token()
    if not resp or ((resp or {}).get("s") not in ("ok", "success")):
        return {"success": False, "error": str((resp or {}).get("message") or resp)[:200]}
    return {"success": True,
            "access_token": resp.get("access_token", ""),
            "refresh_token": resp.get("refresh_token", "")}


class FyersHistoryData:
    """Historical OHLC candles -> pandas DataFrame (IST datetime index)."""

    RESOLUTIONS = ("1", "2", "3", "5", "10", "15", "20", "30", "60",
                   "120", "240", "D", "1D", "1W", "1M")
    _MIN_GAP = 0.11  # 10 req/sec hadd; chhota margin rakho

    def __init__(self, client_id: str, access_token: str, log_path: str = ""):
        self.client_id = (client_id or "").strip()
        self.access_token = (access_token or "").strip()
        self.log_path = log_path
        self._last_call = 0.0
        self._fy = None

    @property
    def fy(self):
        if self._fy is None:
            from fyers_apiv3 import fyersModel
            self._fy = fyersModel.FyersModel(
                client_id=self.client_id, token=self.access_token,
                is_async=False, log_path=self.log_path)
        return self._fy

    # ---------- rate limit ----------
    def _throttle(self):
        import time
        gap = time.time() - self._last_call
        if gap < self._MIN_GAP:
            time.sleep(self._MIN_GAP - gap)
        self._last_call = time.time()

    # ---------- validation ----------
    @staticmethod
    def _validate(symbol, range_from, range_to, resolution):
        sym = (symbol or "").strip().upper()
        if ":" not in sym:
            return "Symbol 'EXCHANGE:NAME' format me do (jaise 'NSE:NIFTY50-INDEX' ya 'NSE:SBIN-EQ')"
        if not range_from or not range_to:
            return "range_from aur range_to chahiye (YYYY-MM-DD)"
        try:
            import datetime as _dt
            d1 = _dt.datetime.strptime(str(range_from), "%Y-%m-%d")
            d2 = _dt.datetime.strptime(str(range_to), "%Y-%m-%d")
            if d1 > d2:
                return "range_from range_to se pehle hona chahiye"
            if (d2 - d1).days > 720:
                return "Ek saath max ~720 din (resolution D par chhote chhunks me rakho)"
        except ValueError:
            return "Dates 'YYYY-MM-DD' format me do"
        res = str(resolution or "D").upper()
        if res.endswith("D") and res not in ("D", "1D"):
            res = res[1:]
        if res not in [r for r in FyersHistoryData.RESOLUTIONS]:
            return f"resolution in {FyersHistoryData.RESOLUTIONS} me do (got '{resolution}')"
        return ""

    # ---------- main ----------
    def history(self, symbol: str, range_from: str, range_to: str,
                resolution: str = "D", retries: int = 3) -> dict:
        """Raw API call + error mapping. Success: {'s':'ok','candles':[[ts,o,h,l,c,v],...],...}"""
        err = self._validate(symbol, range_from, range_to, resolution)
        if err:
            return {"s": "error", "message": err}
        payload = {"symbol": symbol.strip().upper(),
                   "resolution": str(resolution or "D").upper(),
                   "date_format": 1,  # 1 = YYYY-MM-DD input allowed
                   "range_from": str(range_from)[:10],
                   "range_to": str(range_to)[:10],
                   "cont_flag": "1"}
        import time
        last_err = ""
        for attempt in range(1, max(1, retries) + 1):
            self._throttle()
            try:
                resp = self.fy.history(data=payload)
            except Exception as e:
                last_err = f"SDK error: {str(e)[:150]}"
                time.sleep(0.6 * attempt)
                continue
            if not isinstance(resp, dict):
                last_err = f"non-dict response: {str(resp)[:120]}"
                time.sleep(0.6 * attempt)
                continue
            if resp.get("s") == "ok":
                return resp
            code = str(resp.get("code", "") or "")
            if code == "429" or "rate" in str(resp.get("message", "")).lower():
                time.sleep(1.2 * attempt)  # rate limit -> backoff
                last_err = "429 rate-limit (backoff)"
                continue
            if code == "411" or "invalid" in str(resp.get("message", "")).lower() and "token" in str(resp.get("message", "")).lower():
                return {"s": "error", "code": code,
                        "message": "Access token invalid/expired - Refresh Tokens dabao (Brokers page)"}
            if attempt == retries:
                return {"s": "error", "code": code, "message": str(resp.get("message") or json.dumps(resp)[:200])}
            last_err = str(resp.get("message") or code)
            time.sleep(0.6 * attempt)
        return {"s": "error", "message": last_err[:200]}

    @staticmethod
    def to_dataframe(resp: dict) -> pd.DataFrame:
        """Raw candles -> pandas DataFrame, epoch -> readable IST datetime.
        Columns: timestamp(IS, IST tz), open, high, low, close, volume."""
        if not resp or resp.get("s") != "ok":
            raise ValueError(str(resp.get("message") or "history response not ok"))
        candles = resp.get("candles") or []
        if not candles:
            raise ValueError("No candles (symbol/expiry/range check karo)")
        df = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
        df["timestamp"] = (pd.to_datetime(df["epoch"], unit="s", utc=True)
                              .dt.tz_convert("Asia/Kolkata"))
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]
        df = df.sort_values("timestamp").reset_index(drop=True)
        # same-timestamp duplicates: last wins (re-send candles guard)
        dup = df.duplicated(subset=["timestamp"], keep="last")
        if dup.any():
            df = df[~dup].reset_index(drop=True)
        return df

    def get_history_df(self, symbol: str, range_from: str, range_to: str,
                       resolution: str = "D", retries: int = 3) -> pd.DataFrame:
        """/api/... use: validate -> fetch -> DataFrame (500 rows me bonus? pura)."""
        resp = self.history(symbol, range_from, range_to, resolution, retries)
        if resp.get("s") != "ok":
            raise ValueError(str(resp.get("message") or "fetch failed"))
        return self.to_dataframe(resp)
