import time
import datetime
import requests
import re

YAHOO_MAP = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "^CNXFINANCE",   # fallback ^NSEI
    "MIDCPNIFTY": "^NSEMDCP50",
    "SENSEX": "^BSESN",
    "BANKEX": "^BSESN",
}

def _to_yahoo(symbol: str) -> str:
    s = symbol.upper().strip()
    if s in YAHOO_MAP:
        return YAHOO_MAP[s]
    # NSE stocks -> SYMBOL.NS
    return f"{s}.NS"

def _unix(date_str: str) -> int:
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    # Yahoo expects UTC midnight
    return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())

def fetch_yahoo_historical(symbol: str, start_date: str, end_date: str):
    """Fetch daily OHLC from Yahoo Finance (free, no key) and return OHLC records."""
    try:
        ysym = _to_yahoo(symbol)
        p1 = _unix(start_date)
        # end is exclusive, add 1 day
        p2 = _unix(end_date) + 86400
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?period1={p1}&period2={p2}&interval=1d&includePrePost=false"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code != 200:
            # Try alternative: query2
            url2 = f"https://query2.finance.yahoo.com/v8/finance/chart/{ysym}?period1={p1}&period2={p2}&interval=1d"
            resp = requests.get(url2, headers=headers, timeout=12)
            if resp.status_code != 200:
                return []
        j = resp.json()
        result = j.get("chart", {}).get("result", [])
        if not result:
            return []
        r0 = result[0]
        timestamps = r0.get("timestamp", [])
        quotes = r0.get("indicators", {}).get("quote", [{}])[0]
        opens = quotes.get("open", [])
        highs = quotes.get("high", [])
        lows = quotes.get("low", [])
        closes = quotes.get("close", [])
        volumes = quotes.get("volume", [])
        records = []
        for i, ts in enumerate(timestamps):
            try:
                td = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                if td < start_date or td > end_date:
                    continue
                o = float(opens[i] or 0) if i < len(opens) and opens[i] is not None else 0
                h = float(highs[i] or 0) if i < len(highs) and highs[i] is not None else 0
                l = float(lows[i] or 0) if i < len(lows) and lows[i] is not None else 0
                c = float(closes[i] or 0) if i < len(closes) and closes[i] is not None else 0
                v = int(volumes[i] or 0) if i < len(volumes) and volumes[i] is not None else 0
                if c <= 0:
                    continue
                # Yahoo sometimes gives 0 for o/h/l when c is valid, fallback to c
                if o <= 0: o = c
                if h <= 0: h = c
                if l <= 0: l = c
                records.append({
                    "symbol": symbol.upper(),
                    "trade_date": td,
                    "expiry_date": "",
                    "strike_price": None,
                    "option_type": None,
                    "open_price": round(o, 2),
                    "high_price": round(h, 2),
                    "low_price": round(l, 2),
                    "close_price": round(c, 2),
                    "volume": v,
                    "oi": 0,
                })
            except Exception:
                continue
        return records
    except Exception as e:
        # print(f"yahoo fetch failed {symbol}: {e}")
        return []

def fetch_yahoo_spot(symbol: str) -> float:
    """Fetch live spot via Yahoo quote (fast, free)."""
    try:
        ysym = _to_yahoo(symbol)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?period1={int(time.time())-86400}&period2={int(time.time())}&interval=1d"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            j = r.json()
            res = j.get("chart", {}).get("result", [])
            if res:
                meta = res[0].get("meta", {})
                price = meta.get("regularMarketPrice") or meta.get("previousClose") or 0
                if price and float(price) > 0:
                    return float(price)
                # fallback to last close
                closes = res[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                if closes:
                    for c in reversed(closes):
                        if c and float(c) > 0:
                            return float(c)
    except Exception:
        pass
    return 0

def fetch_google_spot(symbol: str) -> float:
    """Try Google Finance quote page for spot (scrape) - no API key."""
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

def fetch_historical_free(symbol: str, start_date: str, end_date: str):
    """Universal free fetch: Yahoo -> Google Finance (strategy_builder) -> NSE -> live drift. Returns records or []."""
    # 1) Yahoo (most reliable for historical)
    recs = fetch_yahoo_historical(symbol, start_date, end_date)
    if len(recs) >= 5:
        return recs
    # 2) Google Finance via existing strategy_builder helper (will be called by caller)
    # 3) Caller will try nselib / niftytrader
    return recs

def import_free_records(symbol: str, start_date: str, end_date: str) -> int:
    """Fetch free historical and import into DB for backtesting. Returns count."""
    recs = fetch_historical_free(symbol, start_date, end_date)
    if recs:
        from core.models.bhavcopy_model import BhavcopyModel
        bhav = BhavcopyModel()
        return bhav.import_data(recs)
    return 0
