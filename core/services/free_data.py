import time
import datetime
import requests
import re


def fetch_google_spot(symbol: str) -> float:
    """Google Finance quote page for spot (scrape) - fallback only."""
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
