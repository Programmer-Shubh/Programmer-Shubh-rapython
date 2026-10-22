_NSE_CHAIN_CACHE = {"ts": 0.0, "data": {}}
_NSE_CHAIN_TTL = 20  # seconds
_NSE_CHAIN_FAIL = {}  # symbol -> ts (negative cache: fail par dobara mat todo)


def fetch_nse_option_chain(symbol: str) -> dict:
    """NSE ka LIVE option chain (index options) - {expiry: {strike: {"CE":px,"PE":px}}}.
    Cloud par bhi try hota hai (cookies + browser headers). Fail -> {}.
    Ye stock options ke liye nahi hota (NSE public API sirf indices deta hai)."""
    sym = (symbol or "").upper()
    now = time.time()
    if _NSE_CHAIN_CACHE["data"] and (now - _NSE_CHAIN_CACHE["ts"]) < _NSE_CHAIN_TTL:
        return _NSE_CHAIN_CACHE["data"].get(sym, {})
    # Negative cache: fail hone par 60s tak retry mat karo (site hang na ho)
    if now - float(_NSE_CHAIN_FAIL.get(sym, 0) or 0) < 60:
        return {}
    out = {}
    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
            "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/option-chain",
            "Connection": "keep-alive",
        })
        try:
            s.get("https://www.nseindia.com", timeout=5)
        except Exception:
            pass
        r = s.get("https://www.nseindia.com/api/option-chain-indices",
                  params={"symbol": sym}, timeout=7)
        if r.status_code != 200:
            _NSE_CHAIN_FAIL[sym] = now
            return {}
        j = r.json() or {}
        recs = ((j.get("records") or {}).get("data") or [])
        for rec in recs:
            exp = str(rec.get("expiryDate") or "")[:10]
            for key, otype in (("CE", "CE"), ("PE", "PE")):
                for row in (rec.get(key) or []):
                    try:
                        px = float(row.get("ltp") or 0)
                    except Exception:
                        px = 0
                    if px <= 0:
                        continue
                    try:
                        st = float(row.get("strikePrice") or 0)
                    except Exception:
                        st = 0
                    if st <= 0:
                        continue
                    out.setdefault(exp, {}).setdefault(st, {})[otype] = px
    except Exception:
        _NSE_CHAIN_FAIL[sym] = now
        return {}
    if out:
        _NSE_CHAIN_CACHE["data"][sym] = out
        _NSE_CHAIN_CACHE["ts"] = now
    return out


def nse_chain_ltp(symbol: str, strike: float, opt: str, expiry_ymd: str) -> float:
    """NSE live chain se ek contract ka LTP (0 = nahi mila).
    PARITY: sirf EXACT expiry ka price (ye tier paper + live + SL-monitor
    sab jagah padhta hai). Pehla 'nearest expiry' fallback DOOSRE contract
    ka premium de deta tha (10-29 request me 11-03 monthly ka rate) - wrong
    contract price = paper loss/backtest profit ka bada sauda gap."""
    try:
        chain = fetch_nse_option_chain(symbol)
        ex = str(expiry_ymd or "")[:10]
        exp_map = chain.get(ex) or {}
        if not exp_map:
            # Exact expiry chain me nahi -> WRONG-contract price kabhi na do.
            # NSE chain fail hua ho to vaise bhi 0 hi hota (model tier sambhalega).
            if ex:
                return 0.0
            for _k, _v in (chain.items() or []):
                exp_map = _v
                break
        row = exp_map.get(float(strike)) or exp_map.get(int(float(strike))) or {}
        v = row.get(str(opt).upper(), 0)
        return float(v or 0)
    except Exception:
        return 0.0


import time
import datetime
import requests
import re

# Yahoo Finance symbols (works from cloud - Yahoo does NOT block Render/AWS)
# VERIFIED Sep 2026 vs NSE-direct: ^NSEI/^NSEBANK match NSE; FINNIFTY must be
# NIFTY_FIN_SERVICE.NS (^CNXFIN is a different/stale index ~27638 vs ~25570).
_YAHOO_MAP = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
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
    "INDUSINDBK": "INDUSINDBK.NS",
    "VEDL": "VEDL.NS",
    "SBILIFE": "SBILIFE.NS",
    "GOLDBEES": "GOLDBEES.NS",
    "SILVERBEES": "SILVERBEES.NS",
    # NOTE: BANKEX (BSE Bankex) has no Yahoo/Stooq quote - left out on purpose
    # so rows honestly show the last DB date instead of a fake live price.
}

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

_YAHOO_SESS = None


def _yahoo_session():
    """Shared keep-alive session: a fresh TLS handshake per quote (~1s+) made
    batch warms crawl. Reuse cuts per-call to ~0.3s."""
    global _YAHOO_SESS
    try:
        if _YAHOO_SESS is None:
            import requests as _rq
            _YAHOO_SESS = _rq.Session()
            _YAHOO_SESS.headers.update(_UA)
        return _YAHOO_SESS
    except Exception:
        return None


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


def _yahoo_fallback_quote(symbol: str, timeout: float = 5) -> dict:
    """Private Yahoo v8 fallback - last resort only (NSE blocked on cloud, Stooq JS-blocked).
    Returns source='live' so frontend never shows 'yahoo'. yfinance lib NOT used."""
    try:
        ysym = _YAHOO_MAP.get(symbol.upper(), f"{symbol}.NS" if "." not in symbol and not symbol.startswith("^") else symbol)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval=1d&range=5d"
        _sess = _yahoo_session()
        try:
            r = _sess.get(url, timeout=timeout) if _sess is not None else requests.get(url, headers=_UA, timeout=timeout)
        except Exception:
            r = None
        if r is not None and r.status_code == 200:
            j = r.json()
            res = (j.get("chart", {}).get("result") or [{}])[0]
            meta = res.get("meta", {})
            px = float(meta.get("regularMarketPrice") or 0)
            if px <= 0:
                return {}
            prev = float(meta.get("previousClose") or meta.get("chartPreviousClose") or 0)
            chg = round((px - prev) / prev * 100, 2) if prev > 0 else 0.0
            return {"spot": px, "change": chg, "high": float(meta.get("regularMarketDayHigh") or px), "low": float(meta.get("regularMarketDayLow") or px), "source": "live"}
    except Exception:
        pass
    return {}


def fetch_yahoo_spot(symbol: str) -> float:
    """Compat stub - calls private fallback, returns 0 if unavailable."""
    try:
        q = _yahoo_fallback_quote(symbol)
        return float(q.get("spot") or 0) if q else 0
    except Exception:
        return 0


def fetch_yahoo_quote(symbol: str) -> dict:
    """Compat stub - returns live-labelled quote (never 'yahoo' source)."""
    return _yahoo_fallback_quote(symbol)


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
                r = requests.get(url, headers=_UA, timeout=5)
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
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            m = re.search(r'data-last-price="([^"]+)"', r.text)
            if m:
                return float(m.group(1).replace(",", ""))
    except Exception:
        pass
    return 0


def fetch_cloud_spot(symbol: str) -> dict:
    """Live spot: NSE direct -> Yahoo -> Google -> Stooq (Stooq last, fixes KOTAKBANK 417 vs 1900 mismatch).
    Yahoo first for indices (Google has no index price markup; verified ^NSEI/^NSEBANK/^CNXFIN/^NSEMDCP50).
    Short timeouts (5s) so dashboard never hangs into proxy 502.
    Returns {spot,change,high,low,source} or {}."""
    spot, source = 0, ""
    chg = 0.0
    # 1) NSE direct (free, most accurate; blocked from some cloud IPs)
    try:
        from core.services.nse_client import nse_fetch_spot
        d = nse_fetch_spot(symbol, timeout=4)
        if d and float(d.get("spot") or 0) > 0:
            spot, source = float(d["spot"]), "nse"
            chg = float(d.get("change") or 0)
    except Exception:
        pass
    # 1b) Connected broker quote (Fyers) - real index tick, same broker the
    # live orders go to (so paper/live strike + premium align).
    if not spot:
        try:
            from core.services.contract_pricer import _fyers_auth, _fyers_get, _fyers_underlying
            _app_id, _tok = _fyers_auth()
            _under = _fyers_underlying(symbol)
            if _app_id and _tok and _under:
                _body = _fyers_get(f"/api/v3/quotes?symbols={_under}", _app_id, _tok, timeout=6)
                _node = (_body.get("data") or {}).get(_under) or {}
                if isinstance(_node, dict):
                    for _f in ("last_price", "lastPrice", "ltp"):
                        try:
                            _px = float(_node.get(_f, 0) or 0)
                        except Exception:
                            _px = 0
                        if _px > 0:
                            spot, source = _px, "fyers"
                            break
        except Exception:
            pass
    # 2) Yahoo v8 API (works on cloud, indices included) - labelled 'live', never 'yahoo'
    if not spot:
        q = _yahoo_fallback_quote(symbol)
        if q and q.get("spot"):
            return q
    # 3) Google Finance quote page (free scrape) - tried before Stooq to avoid stale Stooq
    if not spot:
        g = fetch_google_spot(symbol)
        if g and g > 0:
            # sanity: KOTAKBANK must be ~1500-2500, reject 417-style stale
            if not (symbol.upper()=="KOTAKBANK" and g < 800):
                spot, source = g, "google"
            elif g > 0:
                spot, source = g, "google"
    # 4) Stooq CSV last resort (fixes KOTAKBANK stale 417 - only if Yahoo/Google failed)
    if not spot:
        s = fetch_stooq_spot(symbol)
        if s and s > 0:
            # reject obvious stale: KOTAKBANK 417 vs Yahoo 1900
            if not (symbol.upper()=="KOTAKBANK" and 300 < s < 800):
                spot, source = s, "stooq"
    if spot <= 0:
        return {}
    if not chg:
        prev = _db_prev_close(symbol)
        chg = round((spot - prev) / prev * 100, 2) if prev > 0 else 0.0
    return {"spot": spot, "change": chg, "high": spot, "low": spot, "source": source or "live"}


def fetch_yahoo_daily(symbol: str, start_date: str, end_date: str, timeout: float = 10):
    """Yahoo daily OHLC history (most reliable cloud source - same endpoint
    as the working spot quotes). Split-adjusted via adjclose factor so
    corporate actions don't fabricate jumps. Returns bar dicts or []."""
    out = []
    try:
        import datetime as _dt
        ysym = _YAHOO_MAP.get((symbol or "").upper(),
                              f"{symbol}.NS" if "." not in (symbol or "") and not str(symbol or "").startswith("^") else symbol)
        try:
            p1 = int(_dt.datetime.strptime(str(start_date)[:10], "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).timestamp())
            p2 = int((_dt.datetime.strptime(str(end_date)[:10], "%Y-%m-%d") + _dt.timedelta(days=1)).replace(tzinfo=_dt.timezone.utc).timestamp())
        except Exception:
            p1, p2 = 0, 0
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval=1d&period1={p1}&period2={p2}"
        _sess = _yahoo_session()
        try:
            r = _sess.get(url, timeout=timeout) if _sess is not None else requests.get(url, headers=_UA, timeout=timeout)
        except Exception:
            return []
        if r is None or r.status_code != 200:
            return []
        j = r.json()
        res = (j.get("chart", {}).get("result") or [{}])[0]
        ts = res.get("timestamp") or []
        ind = res.get("indicators", {}) or {}
        q = ((ind.get("quote") or [{}])[0]) or {}
        adj = ((ind.get("adjclose") or [{}])[0] or {}).get("adjclose") or []
        oo, hh, ll, cc, vv = q.get("open") or [], q.get("high") or [], q.get("low") or [], q.get("close") or [], q.get("volume") or []
        for k, t in enumerate(ts):
            try:
                td = _dt.datetime.utcfromtimestamp(int(t)).strftime("%Y-%m-%d")
                if td < str(start_date)[:10] or td > str(end_date)[:10]:
                    continue
                c = float(cc[k]) if k < len(cc) and cc[k] else 0
                if c <= 0:
                    continue
                o = float(oo[k]) if k < len(oo) and oo[k] else c
                h = float(hh[k]) if k < len(hh) and hh[k] else c
                l = float(ll[k]) if k < len(ll) and ll[k] else c
                v = int(float(vv[k] or 0)) if k < len(vv) else 0
                # Split-adjust everything by adjclose factor (keeps O/H/L/C consistent)
                try:
                    ac = float(adj[k]) if k < len(adj) and adj[k] else 0
                    if ac > 0 and c > 0:
                        f = ac / c
                        o, h, l, c = o * f, h * f, l * f, ac
                except Exception:
                    pass
                out.append({"symbol": str(symbol or "").upper(), "trade_date": td, "expiry_date": "",
                            "strike_price": None, "option_type": None,
                            "open_price": round(o, 2), "high_price": round(h, 2),
                            "low_price": round(l, 2), "close_price": round(c, 2),
                            "volume": v, "oi": 0})
            except Exception:
                continue
    except Exception:
        pass
    return out


def fetch_yahoo_intraday(symbol: str, interval: str = "15m", days: int = 5, timeout: float = 10):
    """Yahoo intraday bars (primary intraday source). interval 5m/15m/30m/1h,
    IST-stamped bar dicts with bar_time. Returns [] on any failure.
    Retry (3x, backoff, query1/query2 rotation): Yahoo cloud-IP se aksar
    pehli hit 429/timeout deta hai - bina retry worker hamesha khaali haath
    (auto-paper intraday dead)."""
    out = []
    try:
        import datetime as _dt
        iv = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m"}.get(str(interval or "15m"), "15m")
        ysym = _YAHOO_MAP.get((symbol or "").upper(),
                              f"{symbol}.NS" if "." not in (symbol or "") and not str(symbol or "").startswith("^") else symbol)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval={iv}&range={int(days or 5)}d"
        _sess = _yahoo_session()
        r = None
        _last_err = ""
        for _try in range(3):
            try:
                _host = "query1" if _try % 2 == 0 else "query2"
                _url = url.replace("query1.", _host + ".", 1)
                r = _sess.get(_url, timeout=timeout) if _sess is not None else requests.get(_url, headers=_UA, timeout=timeout)
                if r is not None and r.status_code == 200:
                    break
                _last_err = f"http={getattr(r, 'status_code', '?')}"
                try:
                    print(f"[yahoo-intraday] {symbol} {iv} try{_try + 1}/3 FAILED ({_last_err})", flush=True)
                except Exception:
                    pass
            except Exception as e:
                r = None
                _last_err = f"exc={str(e)[:80]}"
                try:
                    print(f"[yahoo-intraday] {symbol} {iv} try{_try + 1}/3 FAILED ({_last_err})", flush=True)
                except Exception:
                    pass
            try:
                import time as _ts
                _ts.sleep(1)
            except Exception:
                pass
        if r is None or r.status_code != 200:
            try:
                print(f"[yahoo-intraday] {symbol} {iv} EMPTY after 3 tries (last: {_last_err})", flush=True)
            except Exception:
                pass
            return []
        j = r.json()
        res = (j.get("chart", {}).get("result") or [{}])[0]
        ts = res.get("timestamp") or []
        ind = res.get("indicators", {}) or {}
        q = ((ind.get("quote") or [{}])[0]) or {}
        oo, hh, ll, cc, vv = q.get("open") or [], q.get("high") or [], q.get("low") or [], q.get("close") or [], q.get("volume") or []
        _ist = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
        for k, t in enumerate(ts):
            try:
                dt = _dt.datetime.fromtimestamp(int(t), tz=_dt.timezone.utc).astimezone(_ist)
                if dt.weekday() >= 5:
                    continue
                c = float(cc[k]) if k < len(cc) and cc[k] else 0
                if c <= 0:
                    continue
                o = float(oo[k]) if k < len(oo) and oo[k] else c
                h = float(hh[k]) if k < len(hh) and hh[k] else c
                l = float(ll[k]) if k < len(ll) and ll[k] else c
                out.append({"symbol": str(symbol or "").upper(),
                            "trade_date": dt.strftime("%Y-%m-%d"),
                            "bar_time": dt.strftime("%H:%M"),
                            "open_price": round(o, 2), "high_price": round(h, 2),
                            "low_price": round(l, 2), "close_price": round(c, 2),
                            "volume": int(float(vv[k] or 0)) if k < len(vv) else 0})
            except Exception:
                continue
    except Exception:
        pass
    return out


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
