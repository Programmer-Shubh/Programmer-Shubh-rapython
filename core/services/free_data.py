import time
import datetime
import requests
import re

# NSE symbols suitable for direct NSE fetch (nse_client) - primary source.
# NOTE: BANKEX (BSE Bankex) has no Stooq quote - left out on purpose
# so rows honestly show the last DB date instead of a fake live price.

# Stooq symbols (2nd fallback, also cloud-friendly)
_STOOQ_MAP = {
    "NIFTY": "^nsei",
    "BANKNIFTY": "^nsebank",
    "SENSEX": "^sensex",
    "RELIANCE": "reliance.in",
    "HDFCBANK": "hdfcbank.in",
    "TCS": "tcs.in",
    "INFY": "infy.in",
}

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _db_prev_close(symbol: str) -> float:
    """Previous DB close for change% calc (free, local)."""
    try:
        from core.models.database import Database
        rows = Database.get_instance().fetch_all(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 2",
            [symbol.upper()],
        )
        if rows and len(rows) >= 1 and rows[0]["close_price"]:
            return float(rows[0]["close_price"])
    except Exception:
        pass
    return 0


def fetch_stooq_spot(symbol: str) -> float:
    """Stooq CSV quote - PRIMARY free source (cloud-friendly, no key)."""
    try:
        ssym = _STOOQ_MAP.get(symbol.upper())
        candidates = []
        if ssym:
            candidates.append(ssym)
        # Generic NSE-stock fallback: RELIANCE -> reliance.in
        if not (symbol.upper().startswith("^") or symbol.upper() in _STOOQ_MAP):
            candidates.append(f"{symbol.lower()}.in")
        for ssym in candidates:
            try:
                url = f"https://stooq.com/q/l/?s={ssym}&f=sd2t2ohlcv&h&e=csv"
                r = requests.get(url, headers=_UA, timeout=8)
                if r.status_code == 200 and "N/D" not in r.text:
                    lines = r.text.strip().split("\n")
                    if len(lines) >= 2:
                        parts = lines[1].split(",")
                        if len(parts) >= 7:
                            c = float(parts[6])
                            if c > 0:
                                return c
            except Exception:
                continue
    except Exception:
        pass
    return 0


def fetch_google_spot(symbol: str) -> float:
    """Google Finance quote page for spot (scrape) - 3rd fallback."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = f"https://www.google.com/finance/quote/{symbol}:NSE"
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            m = re.search(r'data-last-price="([^"]+)"', r.text)
            if m:
                return float(m.group(1).replace(",", ""))
    except Exception:
        pass
    return 0


def fetch_cloud_spot(symbol: str) -> dict:
    """Cloud-friendly live spot, all free, no key: NSE direct -> Stooq -> Google.
    Change% vs DB prev close. Returns {spot,change,high,low,source} or {}."""
    spot, source = 0, ""
    # 1) NSE direct (free, most accurate; blocked from some cloud IPs)
    try:
        from core.services.nse_client import nse_fetch_spot
        d = nse_fetch_spot(symbol, timeout=4)
        if d and float(d.get("spot") or 0) > 0:
            spot, source = float(d["spot"]), "nse"
    except Exception:
        pass
    # 2) Stooq CSV (free, cloud-friendly)
    if not spot:
        s = fetch_stooq_spot(symbol)
        if s and s > 0:
            spot, source = s, "stooq"
    # 3) Google Finance quote page (free scrape)
    if not spot:
        g = fetch_google_spot(symbol)
        if g and g > 0:
            spot, source = g, "google"
    if spot <= 0:
        return {}
    prev = _db_prev_close(symbol)
    chg = round((spot - prev) / prev * 100, 2) if prev > 0 else 0.0
    return {"spot": spot, "change": chg, "high": spot, "low": spot, "source": source}


def fetch_google_historical(symbol: str, start_date: str, end_date: str):
    """Google getprices for historical fallback."""
    try:
        url = f"https://www.google.com/finance/getprices?q={symbol}&x=NSE&i=86400&p=6M&f=d,o,h,l,c,v"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200 and "COLUMNS=" in r.text:
            lines = r.text.strip().split("\n")
            data_start = 0
            for i, l in enumerate(lines):
                if l.startswith("COLUMNS="):
                    data_start = i + 1
                    break
            out = []
            base_ts = None
            for line in lines[data_start:]:
                if not line or line.startswith("TIMEZONE"):
                    continue
                parts = line.split(",")
                if len(parts) < 6:
                    continue
                try:
                    d_str = parts[0]
                    if d_str.startswith("a"):
                        base_ts = int(d_str[1:])
                        ts = base_ts
                    else:
                        if base_ts is None: continue
                        ts = base_ts + int(d_str) * 86400
                    td = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                    if td < start_date or td > end_date:
                        continue
                    c = float(parts[1]); o = float(parts[2]); h = float(parts[3]); l = float(parts[4]); vol = int(float(parts[5]))
                    if c <= 0: continue
                    out.append({"symbol": symbol, "trade_date": td, "expiry_date": "", "strike_price": None, "option_type": None, "open_price": round(o,2), "high_price": round(h,2), "low_price": round(l,2), "close_price": round(c,2), "volume": vol, "oi": 0})
                except Exception:
                    continue
            if len(out) >= 5:
                return out
    except Exception:
        pass
    return []
