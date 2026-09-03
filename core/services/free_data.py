import time
import datetime
import requests
import re

# Yahoo Finance symbols (works from cloud - Yahoo does NOT block Render/AWS)
_YAHOO_MAP = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "^CNXFIN",
    "MIDCPNIFTY": "^NSEMDCP50",
    "SENSEX": "^BSESN",
    "RELIANCE": "RELIANCE.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "TCS": "TCS.NS",
    "INFY": "INFY.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "ITC": "ITC.NS",
    "SBIN": "SBIN.NS",
    "AXISBANK": "AXISBANK.NS",
    "KOTAKBANK": "KOTAKBANK.NS",
    "LT": "LT.NS",
    "BAJFINANCE": "BAJFINANCE.NS",
}

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


def fetch_yahoo_spot(symbol: str) -> float:
    """Yahoo Finance v8 chart API - PRIMARY cloud source (works on Render)."""
    try:
        ysym = _YAHOO_MAP.get(symbol.upper(), f"{symbol}.NS" if "." not in symbol and not symbol.startswith("^") else symbol)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval=1d&range=1d"
        r = requests.get(url, headers=_UA, timeout=8)
        if r.status_code == 200:
            j = r.json()
            res = (j.get("chart", {}).get("result") or [{}])[0]
            meta = res.get("meta", {})
            px = float(meta.get("regularMarketPrice") or 0)
            if px > 0:
                return px
    except Exception:
        pass
    return 0


def fetch_yahoo_quote(symbol: str) -> dict:
    """Yahoo quote with change%/high/low - for dashboard cards."""
    try:
        ysym = _YAHOO_MAP.get(symbol.upper(), f"{symbol}.NS" if "." not in symbol and not symbol.startswith("^") else symbol)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval=1d&range=5d"
        r = requests.get(url, headers=_UA, timeout=8)
        if r.status_code == 200:
            j = r.json()
            res = (j.get("chart", {}).get("result") or [{}])[0]
            meta = res.get("meta", {})
            px = float(meta.get("regularMarketPrice") or 0)
            if px <= 0:
                return {}
            prev = float(meta.get("previousClose") or meta.get("chartPreviousClose") or 0)
            chg = round((px - prev) / prev * 100, 2) if prev > 0 else 0.0
            return {
                "spot": px,
                "change": chg,
                "high": float(meta.get("regularMarketDayHigh") or px),
                "low": float(meta.get("regularMarketDayLow") or px),
                "source": "yahoo",
            }
    except Exception:
        pass
    return {}


def fetch_stooq_spot(symbol: str) -> float:
    """Stooq CSV quote - 2nd fallback (cloud-friendly)."""
    try:
        ssym = _STOOQ_MAP.get(symbol.upper())
        if not ssym:
            return 0
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
    """Cloud-friendly live spot: Yahoo -> Stooq -> Google. Returns {spot,change,high,low,source} or {}."""
    q = fetch_yahoo_quote(symbol)
    if q and q.get("spot"):
        return q
    s = fetch_stooq_spot(symbol)
    if s and s > 0:
        return {"spot": s, "change": 0.0, "high": s, "low": s, "source": "stooq"}
    g = fetch_google_spot(symbol)
    if g and g > 0:
        return {"spot": g, "change": 0.0, "high": g, "low": g, "source": "google"}
    return {}


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
