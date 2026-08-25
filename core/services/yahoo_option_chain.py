import requests
import json
from typing import List, Dict

_YAHOO_MAP = {
    "NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "^CNXFINANCE", "MIDCPNIFTY": "^NSEMDCP50",
}


def fetch_yahoo_option_chain(symbol: str) -> dict:
    """Fetch live option chain from Yahoo Finance (free, no key)."""
    try:
        ysym = _YAHOO_MAP.get(symbol.upper(), f"{symbol.upper()}.NS")
        # Yahoo v7 options endpoint
        url = f"https://query1.finance.yahoo.com/v7/finance/options/{ysym}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("optionChain", {}).get("result", [])
        if not result:
            return None
        res = result[0]
        spot = res.get("quote", {}).get("regularMarketPrice", 0) or res.get("quote", {}).get("previousClose", 0)
        if not spot or spot <= 0:
            return None
        # Get nearest expiry
        expirations = res.get("expirationDates", [])
        options = res.get("options", [])
        if not options:
            return None
        opt = options[0]  # nearest expiry
        calls = opt.get("calls", [])
        puts = opt.get("puts", [])
        step = _get_step(symbol)
        atm = round(spot / step) * step
        # Build rows
        ce_map = {}
        pe_map = {}
        for c in calls:
            strike = c.get("strike", 0)
            ce_map[strike] = {
                "ltp": c.get("lastPrice", 0) or c.get("close", 0),
                "oi": c.get("openInterest", 0),
                "vol": c.get("volume", 0),
                "iv": c.get("impliedVolatility", 0),
            }
        for p in puts:
            strike = p.get("strike", 0)
            pe_map[strike] = {
                "ltp": p.get("lastPrice", 0) or p.get("close", 0),
                "oi": p.get("openInterest", 0),
                "vol": p.get("volume", 0),
                "iv": p.get("impliedVolatility", 0),
            }
        all_strikes = sorted(set(list(ce_map.keys()) + list(pe_map.keys())))
        rows = []
        for strike in all_strikes:
            ce = ce_map.get(strike, {})
            pe = pe_map.get(strike, {})
            rows.append({
                "strike": strike,
                "distance": int(strike - atm),
                "ce_ltp": round(ce.get("ltp", 0), 2),
                "ce_oi": ce.get("oi", 0),
                "ce_vol": ce.get("vol", 0),
                "ce_iv": round(ce.get("iv", 0) * 100, 1) if ce.get("iv") else 0,
                "pe_ltp": round(pe.get("ltp", 0), 2),
                "pe_oi": pe.get("oi", 0),
                "pe_vol": pe.get("vol", 0),
                "pe_iv": round(pe.get("iv", 0) * 100, 1) if pe.get("iv") else 0,
            })
        from datetime import datetime
        exp_ts = expirations[0] if expirations else 0
        exp_str = datetime.utcfromtimestamp(exp_ts).strftime("%Y-%m-%d") if exp_ts else ""
        return {
            "symbol": symbol.upper(),
            "spot": round(spot, 2),
            "atm": atm,
            "rows": rows,
            "source": "yahoo",
            "timestamp": exp_str,
            "max_pain": atm,
            "pcr": None,
        }
    except Exception:
        return None


def _get_step(symbol: str) -> float:
    steps = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 50, "SENSEX": 100}
    return steps.get(symbol.upper(), 50)
