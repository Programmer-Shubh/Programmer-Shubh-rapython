"""Centralized NSE client - single source for all NSE data fetching.

Handles Akamai/bot protection robustly: session warmup, cookies, modern headers,
retries with backoff, rate limiting, response validation. Never bypasses security.

All NSE calls (spot, option-chain v3) go through here. Broker/execution never touches NSE.

Reference: PNSEA/AYNSE patterns + NSE v3 endpoints.

Endpoints used:
  - GET https://www.nseindia.com                 -> warmup, set cookies (nsit, nseappid, ak_bmsc)
  - GET https://www.nseindia.com/api/allIndices -> indices spot
  - GET https://www.nseindia.com/api/quote-equity?symbol=XYZ
  - GET https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=DD-MMM-YYYY
  - GET https://www.nseindia.com/api/option-chain-v3?type=Equity&symbol=RELIANCE&expiry=...
  - GET https://www.nseindia.com/api/option-chain-contract-info?symbol=NIFTY

Response validation: checks status, json shape, records/underlyingValue; handles 401/404/429.
"""
from __future__ import annotations

import json
import random
import time
import threading
from typing import Optional, Dict, Any, List

import requests

# ---- Config ----
_NSE_BASE = "https://www.nseindia.com"
_WARMUP_URL = f"{_NSE_BASE}/option-chain"
_API_ALL_INDICES = f"{_NSE_BASE}/api/allIndices"
_API_QUOTE_EQUITY = f"{_NSE_BASE}/api/quote-equity?symbol={{symbol}}"
_API_CONTRACT_INFO = f"{_NSE_BASE}/api/option-chain-contract-info?symbol={{symbol}}"
_API_OC_V3_INDICES = f"{_NSE_BASE}/api/option-chain-v3?type=Indices&symbol={{symbol}}&expiry={{expiry}}"
_API_OC_V3_EQUITY = f"{_NSE_BASE}/api/option-chain-v3?type=Equity&symbol={{symbol}}&expiry={{expiry}}"

# Legacy v2 fallback (sometimes less blocked)
_API_OC_V2_INDICES = f"{_NSE_BASE}/api/option-chain-indices?symbol={{symbol}}"
_API_OC_V2_EQUITY = f"{_NSE_BASE}/api/option-chain-equities?symbol={{symbol}}"

_INDICES_SET = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}

# Rate limiting: min interval between NSE calls
_MIN_INTERVAL_SEC = 0.35
_last_call_ts = 0.0
_lock = threading.Lock()
_session: Optional[requests.Session] = None
_session_ts = 0.0
_SESSION_TTL = 300  # refresh session every 5 min

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]


def _throttle():
    global _last_call_ts
    with _lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_SEC - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.08))
        _last_call_ts = time.monotonic()


def _build_headers() -> Dict[str, str]:
    ua = random.choice(_UA_POOL)
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.nseindia.com/option-chain",
        "Origin": "https://www.nseindia.com",
        "Connection": "keep-alive",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }


def _get_session(force_refresh: bool = False) -> requests.Session:
    global _session, _session_ts
    now = time.monotonic()
    if _session is not None and not force_refresh and (now - _session_ts) < _SESSION_TTL:
        return _session
    with _lock:
        if _session is not None and not force_refresh and (time.monotonic() - _session_ts) < _SESSION_TTL:
            return _session
        s = requests.Session()
        s.headers.update(_build_headers())
        # Warmup: hit multiple NSE pages to get cookies (nsit, nseappid, ak_bmsc, bm_sv)
        warmup_urls = [_WARMUP_URL, _NSE_BASE]
        for url in warmup_urls:
            try:
                r = s.get(url, timeout=4)
                if r.status_code == 200:
                    break
            except Exception:
                pass
        time.sleep(0.2)
        _session = s
        _session_ts = time.monotonic()
        return _session


def _request_with_retry(
    url: str,
    *,
    timeout: float = 4,
    max_retries: int = 1,
    expect_json: bool = True,
) -> Optional[requests.Response]:
    """GET with retries, 401 session refresh, 429 backoff. Returns Response or None."""
    last_exc = None
    for attempt in range(max_retries + 1):
        _throttle()
        sess = _get_session(force_refresh=(attempt > 0))
        # rotate UA per retry
        sess.headers.update({"User-Agent": random.choice(_UA_POOL)})
        try:
            r = sess.get(url, timeout=timeout)
            # 401 -> session expired, retry with fresh session
            if r.status_code == 401:
                if attempt < max_retries:
                    # force refresh next loop
                    _get_session(force_refresh=True)
                    time.sleep(0.6 * (attempt + 1))
                    continue
                return None
            if r.status_code == 429:
                # rate limited
                if attempt < max_retries:
                    time.sleep(1.2 * (attempt + 1) + random.uniform(0, 0.5))
                    continue
                return None
            if r.status_code in (403, 404):
                # For 404 on v3 (expiry may be wrong), don't retry same URL - caller handles fallback
                return r
            if r.status_code != 200:
                if attempt < max_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return r
            if expect_json:
                # Validate JSON and non-empty
                try:
                    j = r.json()
                except Exception:
                    if attempt < max_retries:
                        time.sleep(0.4)
                        continue
                    return None
                # NSE sometimes returns 200 with empty/filtered; let caller validate
                return r
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(0.6 * (attempt + 1))
                continue
            return None
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(0.4)
                continue
            return None
    return None


# ---- Public API ----

def nse_get_expiries(symbol: str, timeout: float = 6) -> List[str]:
    """Fetch expiries for symbol via contract-info. Returns list like ['09-Oct-2025', ...] or []."""
    sym = symbol.upper().strip()
    url = _API_CONTRACT_INFO.format(symbol=sym)
    r = _request_with_retry(url, timeout=timeout)
    if r is None or r.status_code != 200:
        return []
    try:
        j = r.json()
        exps = j.get("expiryDates") or j.get("records", {}).get("expiryDates") or []
        return [str(x) for x in exps if x]
    except Exception:
        return []


def nse_fetch_option_chain_v3(
    symbol: str,
    expiry: Optional[str] = None,
    timeout: float = 6,
) -> Optional[Dict[str, Any]]:
    """Fetch option chain via NSE v3 endpoint. If expiry is None, uses nearest expiry.
    Returns normalized dict or None on failure (404/empty/timeout).
    """
    sym = symbol.upper().strip()
    is_index = sym in _INDICES_SET

    # Resolve expiry if not given
    exp = expiry
    if not exp:
        exps = nse_get_expiries(sym, timeout=4)
        if not exps:
            return None
        exp = exps[0]

    tmpl = _API_OC_V3_INDICES if is_index else _API_OC_V3_EQUITY
    url = tmpl.format(symbol=sym, expiry=exp)
    r = _request_with_retry(url, timeout=timeout, max_retries=2)
    if r is None:
        return None
    if r.status_code == 404:
        # expiry format mismatch or no chain for that expiry - try without expiry fallback to v2
        return None
    if r.status_code != 200:
        return None
    try:
        j = r.json()
    except Exception:
        return None

    # Validate shape
    rec = j.get("records") or {}
    data = rec.get("data") or j.get("data") or j.get("filtered", {}).get("data") or []
    if not data:
        return None

    # underlyingValue may be in records or top-level
    spot = rec.get("underlyingValue")
    if spot is None:
        spot = j.get("underlyingValue")
    try:
        spot = float(spot) if spot is not None else 0
    except Exception:
        spot = 0

    expiries = rec.get("expiryDates") or j.get("expiryDates") or []
    # Build rows
    rows: List[Dict[str, Any]] = []
    ce_total_oi = 0
    pe_total_oi = 0
    for d in data:
        # v3 may filter by expiry already, but ensure
        if d.get("expiryDate") and exp and d.get("expiryDate") != exp:
            continue
        strike = d.get("strikePrice")
        if strike is None:
            strike = d.get("strike")
        try:
            strike = float(strike)
        except Exception:
            continue
        if strike <= 0:
            continue
        ce = d.get("CE") or {}
        pe = d.get("PE") or {}
        ce_oi = int(ce.get("openInterest") or 0)
        pe_oi = int(pe.get("openInterest") or 0)
        ce_total_oi += ce_oi
        pe_total_oi += pe_oi
        rows.append({
            "strike": strike,
            "expiryDate": d.get("expiryDate") or exp,
            "ce_ltp": float(ce.get("lastPrice") or ce.get("ltp") or 0),
            "ce_oi": ce_oi,
            "ce_oi_change": int(ce.get("changeinOpenInterest") or ce.get("changeInOpenInterest") or 0),
            "ce_vol": int(ce.get("totalTradedVolume") or ce.get("volume") or 0),
            "ce_iv": float(ce.get("impliedVolatility") or 0),
            "ce_bidQty": int(ce.get("bidQty") or 0),
            "ce_askQty": int(ce.get("askQty") or 0),
            "pe_ltp": float(pe.get("lastPrice") or pe.get("ltp") or 0),
            "pe_oi": pe_oi,
            "pe_oi_change": int(pe.get("changeinOpenInterest") or pe.get("changeInOpenInterest") or 0),
            "pe_vol": int(pe.get("totalTradedVolume") or pe.get("volume") or 0),
            "pe_iv": float(pe.get("impliedVolatility") or 0),
            "pe_bidQty": int(pe.get("bidQty") or 0),
            "pe_askQty": int(pe.get("askQty") or 0),
        })

    if not rows:
        return None

    rows.sort(key=lambda x: x["strike"])
    pcr = (pe_total_oi / ce_total_oi) if ce_total_oi > 0 else None

    # ATM
    atm = 0
    try:
        from utils.helpers import get_strike_step
        step = get_strike_step(sym)
        atm = round(spot / step) * step if spot and step else 0
        for rr in rows:
            rr["distance"] = int(rr["strike"] - atm) if atm else 0
    except Exception:
        pass

    return {
        "symbol": sym,
        "spot": spot,
        "atm": atm,
        "rows": rows,
        "expiries": expiries,
        "expiry": exp,
        "source": "nse_v3",
        "timestamp": rec.get("timestamp") or j.get("timestamp") or "",
        "pcr": round(pcr, 4) if pcr is not None else None,
        "ce_total_oi": ce_total_oi,
        "pe_total_oi": pe_total_oi,
    }


def nse_fetch_option_chain_v2(symbol: str, timeout: float = 6) -> Optional[Dict[str, Any]]:
    """Fallback: NSE v2 option-chain (no expiry param). Returns nearest-expiry rows."""
    sym = symbol.upper().strip()
    is_index = sym in _INDICES_SET
    url = (_API_OC_V2_INDICES if is_index else _API_OC_V2_EQUITY).format(symbol=sym)
    r = _request_with_retry(url, timeout=timeout, max_retries=1)
    if r is None or r.status_code != 200:
        return None
    try:
        j = r.json()
    except Exception:
        return None
    rec = j.get("records") or {}
    data = rec.get("data") or []
    if not data:
        data = j.get("filtered", {}).get("data") or []
    if not data:
        return None
    spot = rec.get("underlyingValue") or j.get("underlyingValue") or 0
    try:
        spot = float(spot) if spot else 0
    except Exception:
        spot = 0
    expiries = rec.get("expiryDates") or []
    nearest = expiries[0] if expiries else ""
    rows: List[Dict[str, Any]] = []
    ce_total_oi = 0
    pe_total_oi = 0
    for d in data:
        if nearest and d.get("expiryDate") != nearest:
            continue
        strike = d.get("strikePrice")
        try:
            strike = float(strike)
        except Exception:
            continue
        if strike <= 0:
            continue
        ce = d.get("CE") or {}
        pe = d.get("PE") or {}
        ce_oi = int(ce.get("openInterest") or 0)
        pe_oi = int(pe.get("openInterest") or 0)
        ce_total_oi += ce_oi
        pe_total_oi += pe_oi
        rows.append({
            "strike": strike,
            "expiryDate": d.get("expiryDate") or nearest,
            "ce_ltp": float(ce.get("lastPrice") or 0),
            "ce_oi": ce_oi,
            "ce_oi_change": int(ce.get("changeinOpenInterest") or 0),
            "ce_vol": int(ce.get("totalTradedVolume") or 0),
            "ce_iv": float(ce.get("impliedVolatility") or 0),
            "pe_ltp": float(pe.get("lastPrice") or 0),
            "pe_oi": pe_oi,
            "pe_oi_change": int(pe.get("changeinOpenInterest") or 0),
            "pe_vol": int(pe.get("totalTradedVolume") or 0),
            "pe_iv": float(pe.get("impliedVolatility") or 0),
        })
    if not rows:
        return None
    rows.sort(key=lambda x: x["strike"])
    pcr = (pe_total_oi / ce_total_oi) if ce_total_oi > 0 else None
    atm = 0
    try:
        from utils.helpers import get_strike_step
        step = get_strike_step(sym)
        atm = round(spot / step) * step if spot and step else 0
        for rr in rows:
            rr["distance"] = int(rr["strike"] - atm) if atm else 0
    except Exception:
        pass
    return {
        "symbol": sym,
        "spot": spot,
        "atm": atm,
        "rows": rows,
        "expiries": expiries,
        "expiry": nearest,
        "source": "nse_v2",
        "timestamp": rec.get("timestamp") or "",
        "pcr": round(pcr, 4) if pcr is not None else None,
        "ce_total_oi": ce_total_oi,
        "pe_total_oi": pe_total_oi,
    }


def nse_fetch_option_chain(symbol: str, expiry: Optional[str] = None, timeout: float = 6) -> Optional[Dict[str, Any]]:
    """Unified: try v3 (with expiry) -> v2 (nearest). Validates response."""
    # Prefer v3 when expiry known or resolvable
    v3 = nse_fetch_option_chain_v3(symbol, expiry=expiry, timeout=timeout)
    if v3 and v3.get("rows"):
        return v3
    v2 = nse_fetch_option_chain_v2(symbol, timeout=timeout)
    if v2 and v2.get("rows"):
        return v2
    return None


def nse_fetch_spot(symbol: str, timeout: float = 5) -> Optional[Dict[str, Any]]:
    """Fetch spot via NSE allIndices (indices) or quote-equity (stocks). Returns {spot, change, high, low, source} or None."""
    sym = symbol.upper().strip()
    if sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        # Use allIndices via centralized session
        try:
            r = _request_with_retry(_API_ALL_INDICES, timeout=timeout, max_retries=1)
            if r and r.status_code == 200:
                _map = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FINANCIAL SERVICES", "MIDCPNIFTY": "NIFTY MIDCAP SELECT"}
                target = _map.get(sym)
                for item in r.json().get("data", []):
                    if item.get("index") == target:
                        spot = float(item.get("last", 0))
                        if spot > 0:
                            return {"spot": spot, "change": float(item.get("percentChange", 0) or 0),
                                    "high": float(item.get("high", 0) or spot), "low": float(item.get("low", 0) or spot), "source": "nse"}
        except Exception:
            pass
        return None
    # Stocks: quote-equity
    url = _API_QUOTE_EQUITY.format(symbol=sym)
    r = _request_with_retry(url, timeout=timeout, max_retries=1)
    if r and r.status_code == 200:
        try:
            j = r.json()
            pi = j.get("priceInfo") or {}
            spot = float(pi.get("lastPrice") or pi.get("close") or 0)
            if spot > 0:
                return {"spot": spot, "change": float(pi.get("pChange", 0) or 0),
                        "high": float((pi.get("intraDayHighLow") or {}).get("max", spot) or spot),
                        "low": float((pi.get("intraDayHighLow") or {}).get("min", spot) or spot),
                        "source": "nse_quote"}
        except Exception:
            pass
    return None
