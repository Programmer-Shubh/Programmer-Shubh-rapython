"""
TrueData (truedata.in) — Authorized NSE/BSE/MCX vendor.
Optional: requires TRUEDATA_USERNAME/TRUEDATA_PASSWORD or TRUEDATA_API_KEY env vars.
If not configured, all functions return None/[] gracefully.
"""
import os
import requests
from typing import List, Dict

_BASE = os.environ.get("TRUEDATA_BASE_URL", "https://api.truedata.in")

def _auth_params():
    user = os.environ.get("TRUEDATA_USERNAME", "")
    pwd = os.environ.get("TRUEDATA_PASSWORD", "")
    key = os.environ.get("TRUEDATA_API_KEY", "")
    headers = {"Accept": "application/json"}
    params = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if user:
        params.update({"username": user, "password": pwd})
    return headers, params, bool(user or key)

def _is_configured() -> bool:
    _, _, ok = _auth_params()
    return ok

def get_spot(symbol: str) -> float:
    """Live spot via TrueData REST."""
    try:
        if not _is_configured():
            return 0
        headers, params, _ = _auth_params()
        params.update({"symbol": symbol, "interval": "1m"})
        r = requests.get(f"{_BASE}/getMarketData", params=params, headers=headers, timeout=3)
        if r.status_code == 200:
            j = r.json()
            spot = float(j.get("last", 0) or j.get("ltp", 0) or j.get("spot", 0) or (j.get("data", {}) or {}).get("last", 0) or 0)
            if spot > 0:
                return spot
    except Exception:
        pass
    return 0

def get_historical(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    """Historical OHLC via TrueData."""
    try:
        if not _is_configured():
            return []
        headers, params, _ = _auth_params()
        params.update({"symbol": symbol, "from": start_date, "to": end_date, "interval": "1d"})
        r = requests.get(f"{_BASE}/getHistoricalData", params=params, headers=headers, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json() if "application/json" in r.headers.get("Content-Type", "") else None
        if not data:
            return []
        rows = data if isinstance(data, list) else data.get("data") or data.get("historical") or []
        out = []
        for c in rows:
            td = str(c.get("date") or c.get("time") or "")[:10]
            cl = float(c.get("close", 0) or c.get("Close", 0) or 0)
            if cl <= 0 or td < start_date or td > end_date:
                continue
            out.append({"symbol": symbol, "trade_date": td, "open_price": float(c.get("open", cl) or cl), "high_price": float(c.get("high", cl) or cl), "low_price": float(c.get("low", cl) or cl), "close_price": round(cl, 2), "volume": int(c.get("volume", 0) or 0), "oi": 0})
        return out if len(out) >= 5 else []
    except Exception:
        pass
    return []

def get_option_chain(symbol: str, expiry: str = "") -> List[Dict]:
    """Option chain via TrueData (if available)."""
    try:
        if not _is_configured():
            return []
        headers, params, _ = _auth_params()
        params.update({"symbol": symbol, "expiry": expiry})
        r = requests.get(f"{_BASE}/getOptionChain", params=params, headers=headers, timeout=5)
        if r.status_code == 200:
            j = r.json()
            rows = j.get("data") or j.get("chain") or []
            if rows:
                return rows
    except Exception:
        pass
    return []
