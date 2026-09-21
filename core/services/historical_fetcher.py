import datetime
import requests
import re
import json
from typing import List, Dict
# Sources: DB -> Stooq -> openchart (marketcalls) -> tvDatafeed (TradingView).
# NSE-direct fetchers (nselib/jugaad/nsepython/archives) REMOVED — NSE blocks
# them (empty/archive responses); openchart+tvDatafeed serve instead.

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

def _parse_dates(start_date: str, end_date: str):
    try:
        s = datetime.datetime.strptime(start_date, "%Y-%m-%d")
        e = datetime.datetime.strptime(end_date, "%Y-%m-%d")
        return s, e
    except Exception:
        e = datetime.datetime.now()
        s = e - datetime.timedelta(days=90)
        return s, e

def _clean_num(v) -> float:
    """Parse NSE number like '2,613.10' or 2613.10."""
    if v is None: return 0
    s = str(v).replace(",", "").replace("\u20b9", "").strip()
    try: return float(s)
    except: return 0

def _fetch_truedata_historical(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    """TrueData (truedata.in): Authorized NSE/BSE/MCX vendor — requires TRUEDATA_USERNAME/PASSWORD or TRUEDATA_API_KEY."""
    try:
        import os
        td_user = os.environ.get("TRUEDATA_USERNAME", "")
        td_pass = os.environ.get("TRUEDATA_PASSWORD", "")
        td_key = os.environ.get("TRUEDATA_API_KEY", "")
        if not td_user and not td_key:
            return []
        # TrueData REST: https://api.truedata.in/getHistoricalData?symbol=NIFTY&from=2025-01-01&to=2025-01-10
        # Falls back to authorized NSE feed if configured
        headers = {"Accept": "application/json"}
        if td_key:
            headers["Authorization"] = f"Bearer {td_key}"
        base = os.environ.get("TRUEDATA_BASE_URL", "https://api.truedata.in")
        for ep in [f"{base}/getHistoricalData", f"{base}/api/historical", "https://www.truedata.in/api/historical"]:
            try:
                params = {"symbol": symbol, "from": start_date, "to": end_date, "interval": "1d"}
                if td_user: params.update({"username": td_user, "password": td_pass})
                r = requests.get(ep, params=params, headers=headers, timeout=8)
                if r.status_code != 200: continue
                data = r.json() if "application/json" in r.headers.get("Content-Type","") else None
                if not data: continue
                rows = data if isinstance(data, list) else data.get("data") or data.get("historical") or data.get("candles") or []
                if len(rows) < 5: continue
                out = []
                for c in rows:
                    td = str(c.get("date") or c.get("Date") or c.get("time") or "")[:10]
                    if td < start_date or td > end_date: continue
                    cl = _clean_num(c.get("close") or c.get("Close") or 0)
                    if cl <= 0: continue
                    out.append({"symbol": symbol, "trade_date": td, "open_price": _clean_num(c.get("open") or cl), "high_price": _clean_num(c.get("high") or cl), "low_price": _clean_num(c.get("low") or cl), "close_price": round(cl,2), "volume": int(_clean_num(c.get("volume") or "0")), "oi": 0})
                if len(out) >= 5:
                    return out
            except:
                continue
    except Exception:
        pass
    return []

def _fetch_stocksrin_historical(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    """StocksRin — option chain + historical data platform."""
    try:
        # StocksRin has historical API for F&O data
        urls = [
            f"https://stocksrin.com/api/historical/{symbol}?from={start_date}&to={end_date}",
            f"https://stocksrin.com/api/history/{symbol}?from={start_date}&to={end_date}",
        ]
        for url in urls:
            try:
                r = requests.get(url, headers=_HEADERS, timeout=6)
                if r.status_code != 200: continue
                try:
                    data = r.json()
                    rows = data if isinstance(data, list) else data.get("data") or data.get("historical") or data.get("candles") or []
                    if rows and len(rows) >= 5:
                        out = []
                        for c in rows:
                            td = str(c.get("date") or c.get("Date") or c.get("trade_date") or "")[:10]
                            if td < start_date or td > end_date: continue
                            cl = float(c.get("close") or c.get("Close") or c.get("close_price") or 0)
                            if cl <= 0: continue
                            out.append({"symbol": symbol, "trade_date": td, "open_price": float(c.get("open") or c.get("Open") or cl), "high_price": float(c.get("high") or c.get("High") or cl), "low_price": float(c.get("low") or c.get("Low") or cl), "close_price": cl, "volume": int(c.get("volume") or 0), "oi": 0})
                        if len(out) >= 5:
                            return out
                except Exception:
                    pass
                # Try scraping embedded JSON
                m = re.search(r'"historical"\s*:\s*(\[.*?\])', r.text)
                if m:
                    try:
                        hist = json.loads(m.group(1))
                        out = []
                        for h in hist:
                            td = str(h.get("date",""))[:10]
                            if td < start_date or td > end_date: continue
                            cl = float(h.get("close",0))
                            if cl <= 0: continue
                            out.append({"symbol": symbol, "trade_date": td, "open_price": float(h.get("open",cl)), "high_price": float(h.get("high",cl)), "low_price": float(h.get("low",cl)), "close_price": cl, "volume": 0, "oi": 0})
                        if len(out) >= 5:
                            return out
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception:
        pass
    return []

def _fetch_google_finance(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    try:
        url = f"https://www.google.com/finance/getprices?q={symbol}&x=NSE&i=86400&p=6M&f=d,o,h,l,c,v"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code==200 and "COLUMNS=" in r.text:
            lines=r.text.strip().split("\n")
            data_start=0
            for i,l in enumerate(lines):
                if l.startswith("COLUMNS="):
                    data_start=i+1
                    break
            out=[]
            base_ts=None
            for line in lines[data_start:]:
                if not line or line.startswith("TIMEZONE"): continue
                parts=line.split(",")
                if len(parts)<6: continue
                try:
                    d_str=parts[0]
                    if d_str.startswith("a"):
                        base_ts=int(d_str[1:])
                        ts=base_ts
                    else:
                        if base_ts is None: continue
                        ts=base_ts+int(d_str)*86400
                    td=datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                    if td<start_date or td>end_date: continue
                    c=float(parts[1]); o=float(parts[2]); h=float(parts[3]); l=float(parts[4]); vol=int(float(parts[5]))
                    if c<=0: continue
                    out.append({"symbol":symbol,"trade_date":td,"open_price":round(o,2),"high_price":round(h,2),"low_price":round(l,2),"close_price":round(c,2),"volume":vol,"oi":0})
                except Exception:
                    continue
            if len(out)>=10:
                return out
    except Exception:
        pass
    return []

def _generate_synthetic_data(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    """Realistic synthetic OHLCV - instant (<5ms), seed varies daily so same indicator setting != same trade every day."""
    _SPOTS = {
        "NIFTY": 24500, "BANKNIFTY": 51200, "FINNIFTY": 22800, "MIDCPNIFTY": 14800,
        "RELIANCE": 2850, "HDFCBANK": 1780, "ICICIBANK": 1250, "TCS": 3950,
        "INFY": 1580, "ITC": 470, "SBIN": 780, "TATAMOTORS": 980,
        "BAJFINANCE": 6800, "KOTAKBANK": 1820, "LT": 3650, "AXISBANK": 1150,
        "WIPRO": 560, "ONGC": 280, "TATASTEEL": 145, "SUNPHARMA": 1780,
        "ADANIENT": 3200, "HINDUNILVR": 2500, "BHARTIARTL": 1650, "M&M": 2900,
        "MARUTI": 12500, "NTPC": 350, "POWERGRID": 310, "HCLTECH": 1700,
        "JSWSTEEL": 880, "COALINDIA": 480, "DRREDDY": 6200, "CIPLA": 1500,
        "SBILIFE": 1550, "BPCL": 650, "GRASIM": 2300, "TECHM": 1650,
        "EICHERMOT": 4800, "BRITANNIA": 5200, "HINDALCO": 620, "VEDL": 450,
        "INDUSINDBK": 1450, "NESTLEIND": 25000, "BAJAJFINSV": 1750, "HEROMOTOCO": 4900,
        "APOLLOHOSP": 6300, "UPL": 550, "ULTRACEMCO": 11000, "SHREECEM": 28000,
    }
    import random, hashlib
    s = _SPOTS.get(symbol.upper(), 5000)
    try:
        sd = datetime.datetime.strptime(start_date, "%Y-%m-%d")
        ed = datetime.datetime.strptime(end_date, "%Y-%m-%d")
    except Exception:
        ed = datetime.datetime.now()
        sd = ed - datetime.timedelta(days=65)
    # Stable hash + date so pattern rotates daily (fixes same trade repeating) and indicator change matters
    seed = int(hashlib.md5(f"{symbol.upper()}|{start_date}|{end_date}".encode()).hexdigest()[:8], 16) ^ 0x5EED
    random.seed(seed)
    price = s
    records = []
    d = sd
    # Slight trend bias per symbol so win% not always 50-50 and not always losing
    trend = (seed % 7 - 3) * 0.001  # -0.003 to +0.003 daily drift bias
    while d <= ed:
        if d.weekday() < 5:
            daily_drift = random.uniform(-0.015, 0.015) + trend
            o = price
            c = price * (1 + daily_drift)
            h = max(o, c) * (1 + abs(random.uniform(0, 0.005)))
            l = min(o, c) * (1 - abs(random.uniform(0, 0.005)))
            vol = random.randint(80000, 600000)
            records.append({
                "symbol": symbol.upper(), "trade_date": d.strftime("%Y-%m-%d"),
                "open_price": round(o, 2), "high_price": round(h, 2),
                "low_price": round(l, 2), "close_price": round(c, 2),
                "volume": vol, "oi": 0,
            })
            price = c
        d += datetime.timedelta(days=1)
    return records

def _fetch_db_historical(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    """Instant local cache: bhavcopy_data spot rows (seeded by background refresh). No network."""
    try:
        from core.models.database import Database
        db = Database.get_instance()
        rows = db.fetch_all(
            "SELECT trade_date, open_price, high_price, low_price, close_price, volume FROM bhavcopy_data "
            "WHERE symbol=? AND option_type IS NULL AND trade_date>=? AND trade_date<=? ORDER BY trade_date ASC",
            [symbol.upper(), start_date, end_date],
        )
        if rows and len(rows) >= 5:
            out = []
            for r in rows:
                cl = float(r.get("close_price") or 0)
                if cl <= 0:
                    continue
                out.append({
                    "symbol": symbol.upper(),
                    "trade_date": str(r.get("trade_date")),
                    "open_price": float(r.get("open_price") or cl),
                    "high_price": float(r.get("high_price") or cl),
                    "low_price": float(r.get("low_price") or cl),
                    "close_price": cl,
                    "volume": int(r.get("volume") or 0),
                    "oi": 0,
                })
            if len(out) >= 5:
                return out
    except Exception:
        pass
    return []


def _fetch_tvDatafeed_historical(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    """tvDatafeed-enhanced: TradingView in-memory DataFrame, no CSV. pip install tvdatafeed-enhanced."""
    try:
        # Try tvDatafeed first (correct package: tvdatafeed-enhanced, repo rongardF/tvdatafeed)
        # Single attempt, 150 bars (fast, no Render timeout). No retry loop - caller has 8s budget.
        try:
            from tvDatafeed import TvDatafeed, Interval
            from concurrent.futures import ThreadPoolExecutor
            df = None
            try:
                tv = TvDatafeed()
                def _do_hist():
                    try:
                        return tv.get_hist(symbol=symbol, exchange='NSE', interval=Interval.in_daily, n_bars=150)
                    except Exception:
                        return tv.get_hist(symbol=symbol, exchange='NSE', interval='1d', n_bars=150)
                with ThreadPoolExecutor(max_workers=1) as _ex:
                    _fut = _ex.submit(_do_hist)
                    try:
                        df = _fut.result(timeout=12)
                    except Exception:
                        df = None
            except Exception:
                df = None
            if df is not None and not df.empty:
                # tvDatafeed returns DataFrame with columns: symbol, open, high, low, close, volume, datetime
                # Normalize to our format
                out=[]
                for idx, row in df.iterrows():
                    td = str(idx)[:10] if hasattr(idx, 'strftime') else str(row.get('datetime',''))[:10]
                    # Try to get date from index
                    try:
                        td = idx.strftime('%Y-%m-%d') if hasattr(idx, 'strftime') else str(td)[:10]
                    except: td=str(td)[:10]
                    if td < start_date or td > end_date: continue
                    cl=float(row.get('close', row.get('Close',0)) or 0)
                    if cl<=0: continue
                    out.append({"symbol":symbol,"trade_date":td,"open_price":float(row.get('open',row.get('Open',cl))),"high_price":float(row.get('high',row.get('High',cl))),"low_price":float(row.get('low',row.get('Low',cl))),"close_price":round(cl,2),"volume":int(row.get('volume',row.get('Volume',0)) or 0),"oi":0})
                if len(out)>=5:
                    return out
        except Exception:
            pass
        # Fallback to openchart (also TradingView in-memory)
        try:
            import openchart
            # openchart also provides DataFrame
            df = openchart.get_history(symbol, interval='1d', start=start_date, end=end_date)
            if df is not None and not df.empty:
                out=[]
                for idx, row in df.iterrows():
                    td=str(idx)[:10]
                    try: td=idx.strftime('%Y-%m-%d')
                    except: pass
                    if td < start_date or td > end_date: continue
                    cl=float(row.get('close',0) or 0)
                    if cl<=0: continue
                    out.append({"symbol":symbol,"trade_date":td,"open_price":float(row.get('open',cl)),"high_price":float(row.get('high',cl)),"low_price":float(row.get('low',cl)),"close_price":round(cl,2),"volume":int(row.get('volume',0) or 0),"oi":0})
                if len(out)>=5:
                    return out
        except Exception:
            pass
    except Exception:
        pass
    return []

def _fetch_openchart_historical(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    """openchart (marketcalls/openchart NSEData): NSE charting API via search +
    historical. Indices -> IDX segment, stocks -> EQ. May return [] where NSE
    blocks charting API (then tvDatafeed fallback serves)."""
    try:
        from openchart import NSEData
        import datetime as _dt
        from concurrent.futures import ThreadPoolExecutor
        _IDX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
        seg = "IDX" if symbol.upper() in _IDX else "EQ"
        try:
            sd = _dt.datetime.strptime(start_date, "%Y-%m-%d")
            ed = _dt.datetime.strptime(end_date, "%Y-%m-%d")
        except Exception:
            return []
        api = NSEData()

        def _do():
            return api.historical(symbol.upper(), segment=seg, start=sd, end=ed, interval="1d")

        df = None
        try:
            with ThreadPoolExecutor(max_workers=1) as _ex:
                df = _ex.submit(_do).result(timeout=15)
        except Exception:
            df = None
        if df is None or getattr(df, "empty", True):
            return []
        out = []
        for idx, row in df.iterrows():
            try:
                td = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            except Exception:
                td = str(idx)[:10]
            if td < start_date or td > end_date:
                continue
            cl = float(row.get("close", row.get("Close", 0)) or 0)
            if cl <= 0:
                continue
            out.append({"symbol": symbol.upper(), "trade_date": td,
                        "open_price": round(float(row.get("open", row.get("Open", cl)) or cl), 2),
                        "high_price": round(float(row.get("high", row.get("High", cl)) or cl), 2),
                        "low_price": round(float(row.get("low", row.get("Low", cl)) or cl), 2),
                        "close_price": round(cl, 2),
                        "volume": int(row.get("volume", row.get("Volume", 0)) or 0), "oi": 0})
        out.sort(key=lambda r: r["trade_date"])
        if len(out) >= 5:
            return out
    except Exception:
        pass
    return []

def _fetch_stooq_historical(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    """Stooq daily CSV in-memory (no file write) - cloud-friendly, free, no key. Replaces Yahoo."""
    try:
        import io, csv as _csv
        import datetime as _dt
        _STOOQ = {"NIFTY": "^nsei", "BANKNIFTY": "^nsebank", "FINNIFTY": "^cnxfin", "MIDCPNIFTY": "^nsemidcap50", "SENSEX": "^sensex"}
        ssym = _STOOQ.get(symbol.upper(), f"{symbol.lower()}.in")
        sd = _dt.datetime.strptime(start_date, "%Y-%m-%d").strftime("%Y%m%d")
        ed = _dt.datetime.strptime(end_date, "%Y-%m-%d").strftime("%Y%m%d")
        url = f"https://stooq.com/q/d/l/?s={ssym}&d1={sd}&d2={ed}&i=d"
        r = requests.get(url, headers=_HEADERS, timeout=8)
        if r.status_code != 200 or "Date" not in r.text:
            return []
        reader = _csv.DictReader(io.StringIO(r.text))
        out = []
        for row in reader:
            try:
                td = str(row.get("Date", ""))[:10]
                if td < start_date or td > end_date:
                    continue
                o = _clean_num(row.get("Open")); h = _clean_num(row.get("High"))
                l = _clean_num(row.get("Low")); cl = _clean_num(row.get("Close"))
                vol = int(_clean_num(row.get("Volume")))
                if cl <= 0:
                    continue
                out.append({"symbol": symbol, "trade_date": td, "open_price": round(o or cl, 2), "high_price": round(h or cl, 2), "low_price": round(l or cl, 2), "close_price": round(cl, 2), "volume": vol, "oi": 0})
            except Exception:
                continue
        if len(out) >= 5:
            return out
    except Exception:
        pass
    return []

def _last_trading_day():
    d = datetime.date.today() - datetime.timedelta(days=1)
    while d.weekday() >= 5: d -= datetime.timedelta(days=1)
    return d

def fetch_historical(symbol: str, start_date: str, end_date: str, allow_synthetic: bool = False) -> List[Dict]:
    """Real 6-month local archive. end_date clamped to last completed trading day. DB -> Stooq -> openchart -> tvDatafeed."""
    allow_synthetic=False
    symbol = symbol.upper()
    # Clamp end_date to last completed trading day (avoid today ongoing session)
    try:
        ed = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
        ltd = _last_trading_day()
        if ed > ltd: end_date = ltd.strftime("%Y-%m-%d")
    except: pass
    # 1) Instant local cache (6-month archive) - serves backtest in <10ms
    try:
        db_data = _fetch_db_historical(symbol, start_date, end_date)
        if db_data and len(db_data) >= 5:
            return db_data
    except Exception:
        pass
    # 2) External sources — 8s budget (Yahoo removed): Stooq -> openchart -> tvDatafeed
    import time as _t
    _deadline = _t.time() + 8
    for fetcher in [_fetch_stooq_historical, _fetch_openchart_historical, _fetch_tvDatafeed_historical]:
        try:
            if _t.time() > _deadline:
                break
            data = fetcher(symbol, start_date, end_date)
            if data and len(data) >= 5:
                # Cache it for next time (instant backtest thereafter)
                try:
                    from core.models.bhavcopy_model import BhavcopyModel
                    BhavcopyModel().import_data(data)
                except Exception:
                    pass
                return data
        except Exception:
            continue
    return []
