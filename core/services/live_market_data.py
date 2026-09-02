import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from core.models.database import Database
try:
    from core.services.free_data import fetch_google_spot
except Exception:
    fetch_google_spot = lambda s: 0

_LIVE_CACHE = {}
_LIVE_CACHE_TTL = 2  # fast streaming: 2 sec
_CHAIN_CACHE = {}
_CHAIN_CACHE_TTL = 2


def _with_timeout(fn, timeout, *args):
    """Run fn(*args) in a daemon thread; return its result or None if it exceeds timeout.
    Prevents NSE/StocksRin/nselib hangs from freezing the request (which caused 503/data-not-fetch)."""
    import threading
    box = {}
    def _run():
        try:
            box["r"] = fn(*args)
        except Exception:
            box["r"] = None
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("r")

# --- 3 fast alternatives (NSE allIndices + NSE quote + StocksRin) - No NiftyTrader, No Yahoo ---
def _fetch_nse_quote_spot(symbol: str):
    try:
        from core.services.nse_client import nse_fetch_spot
        d = nse_fetch_spot(symbol, timeout=5)
        if d and d.get("spot"):
            return d
    except Exception:
        pass
    return None


def _fetch_nse_indices_spot(symbol: str):
    try:
        from core.services.nse_client import nse_fetch_spot
        d = nse_fetch_spot(symbol, timeout=5)
        # nse_fetch_spot handles both indices (allIndices) and stocks (quote-equity)
        if d and d.get("spot") and symbol.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            return d
        return None if symbol.upper() not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY") else d
    except Exception:
        pass
    return None

def _fetch_nselib_spot(symbol: str):
    """nselib price_volume_data — reliable for stocks but can hang (no internal timeout), so bound it."""
    try:
        from nselib.capital_market import price_volume_data
        import datetime
        sd = (datetime.datetime.now() - datetime.timedelta(days=5)).strftime("%d-%m-%Y")
        ed = datetime.datetime.now().strftime("%d-%m-%Y")
        df = _with_timeout(price_volume_data, 2.5, symbol, from_date=sd, to_date=ed)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            cl = str(last.get("ClosePrice", last.get("LastPrice", 0)) or "0").replace(",", "").strip()
            try:
                v = float(cl)
                if v > 0:
                    return {"spot": v, "change": 0, "high": v, "low": v, "source": "nselib"}
            except:
                pass
    except Exception:
        pass
    return None

def _fetch_truedata_spot(symbol: str):
    """TrueData (truedata.in) — authorized NSE vendor, <50ms if configured."""
    try:
        import os
        td_user = os.environ.get("TRUEDATA_USERNAME", "")
        td_key = os.environ.get("TRUEDATA_API_KEY", "")
        if not td_user and not td_key:
            return None
        base = os.environ.get("TRUEDATA_BASE_URL", "https://api.truedata.in")
        headers = {"Accept": "application/json"}
        if td_key:
            headers["Authorization"] = f"Bearer {td_key}"
        params = {"symbol": symbol, "interval": "1m"}
        if td_user:
            params.update({"username": td_user, "password": os.environ.get("TRUEDATA_PASSWORD", "")})
        r = requests.get(f"{base}/getMarketData", params=params, headers=headers, timeout=2)
        if r.status_code == 200:
            j = r.json()
            spot = float(j.get("last") or j.get("ltp") or j.get("spot") or (j.get("data", {}) or {}).get("last", 0) or 0)
            if spot > 0:
                return {"spot": spot, "change": float(j.get("change_percent", 0) or 0), "high": float(j.get("high", 0) or spot), "low": float(j.get("low", 0) or spot), "source": "truedata"}
    except Exception:
        pass
    return None

def _fetch_stocksrin_spot(symbol: str):
    """StocksRin spot - https://stocksrin.com (F&O analytics). Real-time if public endpoint available, else scrape quote page."""
    # Try multiple possible StocksRin endpoints with very short timeout (fast fail 1.2s)
    for url in [
        f"https://stocksrin.com/api/spot/{symbol.upper()}",
        f"https://stocksrin.com/api/quote/{symbol.upper()}",
        f"https://stocksrin.com/api/v1/spot?symbol={symbol.upper()}",
    ]:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=1.2)
            if r.status_code == 200:
                try:
                    j = r.json()
                    spot = float(j.get("spot") or j.get("last") or j.get("price") or j.get("ltp") or (j.get("data", {}) or {}).get("spot", 0) or 0)
                    if spot > 0:
                        return {"spot": spot, "change": 0, "high": spot, "low": spot, "source": "stocksrin"}
                except Exception:
                    pass
                # Scrape embedded JSON if not pure JSON
                m = re.search(r'"spot"\s*:\s*([0-9]+\.?[0-9]*)', r.text)
                if m:
                    try:
                        v = float(m.group(1))
                        if v > 0:
                            return {"spot": v, "change": 0, "high": v, "low": v, "source": "stocksrin"}
                    except Exception:
                        pass
        except Exception:
            continue
def _fetch_nse_option_chain_live(symbol: str):
    """Delegate to centralized nse_client (v3 -> v2, session/cookies/retries/validation)."""
    try:
        from core.services.nse_client import nse_fetch_option_chain
        data = nse_fetch_option_chain(symbol, expiry=None, timeout=6)
        if data and data.get("rows"):
            # Normalize to live_market_data expected shape (atm field)
            # nse_client already computes atm, pcr, ce_total_oi etc.
            return data
    except Exception:
        pass
    return None

def _fetch_yahoo_spot(symbol: str):
    """Yahoo Finance chart API - reliable free realtime (works from Render, ~400ms).
    Returns live NSE spot for stocks (.NS) + indices (^NSEI/^NSEBANK)."""
    try:
        m = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "NIFTY_FIN_SERVICE.NS", "MIDCPNIFTY": "NIFTY_MID_SELECT.NS"}
        y = m.get(symbol.upper(), symbol.upper() + ".NS")
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{y}", headers={"User-Agent": "Mozilla/5.0"}, timeout=2)
        if r.status_code == 200:
            j = r.json()
            res = j.get("chart", {}).get("result", [])
            if res:
                meta = res[0].get("meta", {})
                spot = float(meta.get("regularMarketPrice") or meta.get("previousClose") or 0)
                if spot > 0:
                    prev = float(meta.get("previousClose") or spot)
                    ch = ((spot - prev) / prev * 100) if prev else 0
                    return {"spot": spot, "change": ch, "high": float(meta.get("regularMarketDayHigh") or spot), "low": float(meta.get("regularMarketDayLow") or spot), "source": "yahoo"}
    except Exception:
        pass
    return None


def _spots_worker(syms, max_workers, q):
    try:
        from core.services.live_market_data import LiveMarketData as _LM
        lm = _LM()
        res = {}

        def _one(s):
            try:
                return s, lm.fetch_live_from_nse(s)
            except Exception:
                return s, None
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
        with _TPE(max_workers=max_workers) as ex:
            futs = {ex.submit(_one, s): s for s in syms}
            for fut in _ac(futs, timeout=12):
                try:
                    sym, data = fut.result()
                    if data and data.get("spot"):
                        res[sym] = data
                except Exception:
                    continue
        q.put(res)
    except Exception:
        try:
            q.put({})
        except Exception:
            pass


def _chains_worker(syms, q):
    try:
        from core.services.live_market_data import LiveMarketData as _LM
        lm = _LM()
        res = {}
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
        with _TPE(max_workers=len(syms)) as ex:
            futs = {ex.submit(lm.fetch_live_option_chain, s): s for s in syms}
            for fut in _ac(futs, timeout=10):
                try:
                    sym = futs[fut]
                    data = fut.result() if not fut.exception() else None
                    if data and data.get("rows"):
                        res[sym] = data
                except Exception:
                    continue
        q.put(res)
    except Exception:
        try:
            q.put({})
        except Exception:
            pass


class LiveMarketData:
    def __init__(self):
        self.db = Database.get_instance()

    def get_spot_price(self, symbol: str) -> float:
        # 1) Live cache (2 sec TTL - instant)
        try:
            live = self.get_live_spot(symbol)
            if live and live.get("spot"):
                return float(live["spot"])
        except Exception:
            pass
        # 2) DB fallback (instant) before network calls for speed
        row = self.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=(SELECT MAX(trade_date) FROM bhavcopy_data WHERE symbol=?) AND option_type IS NULL",
            [symbol, symbol],
        )
        if row and row["close_price"] and float(row["close_price"]) > 0:
            return float(row["close_price"])
        row2 = None
        try:
            row2 = self.db.fetch_one(
                "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type='CE' AND trade_date=(SELECT MAX(trade_date) FROM bhavcopy_data WHERE symbol=?) ORDER BY ABS(strike_price - (SELECT AVG(strike_price) FROM bhavcopy_data WHERE symbol=? AND option_type='CE')) LIMIT 1",
                [symbol, symbol, symbol],
            )
        except Exception:
            pass
        return float(row2["close_price"]) if row2 and row2["close_price"] else (float(row["close_price"]) if row and row["close_price"] else 0)

    def get_option_ltp(self, symbol: str, strike: float, option_type: str) -> float:
        # 1) Live chain first (real NSE LTP)
        try:
            chain = self.get_live_chain_cached(symbol.upper())
            if not chain:
                chain = _with_timeout(_fetch_nse_option_chain_live, 2.0, symbol.upper())
            if chain and chain.get("rows"):
                for r in chain["rows"]:
                    if float(r.get("strike",0)) == float(strike):
                        v = float(r.get("ce_ltp",0) if option_type=="CE" else r.get("pe_ltp",0))
                        if v > 0:
                            return v
        except Exception:
            pass
        row = self.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND strike_price=? AND option_type=? AND trade_date=(SELECT MAX(trade_date) FROM bhavcopy_data WHERE symbol=?)",
            [symbol, strike, option_type, symbol],
        )
        return float(row["close_price"]) if row else None

    def fetch_live_from_nse(self, symbol: str):
        """Spot via Yahoo first (reliable, 3s, works from Render)."""
        for fn, tout in [(_fetch_truedata_spot, 1.8), (_fetch_nse_quote_spot, 1.8), (_fetch_nse_indices_spot, 1.8), (_fetch_stocksrin_spot, 1.5)]:
            try:
                d = _with_timeout(fn, tout, symbol)
                if d:
                    src = "NSE" if d["source"]=="yahoo" else d["source"]
                    return {"spot": d["spot"], "formatted": f"INR {d['spot']:,.2f}", "change": d.get("change", 0), "high": d.get("high", 0) or d["spot"], "low": d.get("low", 0) or d["spot"], "source": src}
            except Exception:
                pass
        try:
            g = _with_timeout(fetch_google_spot, 1.2, symbol)
            if g and g > 0:
                return {"spot": float(g), "formatted": f"INR {float(g):,.2f}", "change": 0, "high": float(g), "low": float(g), "source": "google"}
        except Exception:
            pass
        return None

    def get_live_spot(self, symbol: str):
        now = time.time()
        if symbol in _LIVE_CACHE and now - _LIVE_CACHE[symbol]["ts"] < _LIVE_CACHE_TTL:
            return _LIVE_CACHE[symbol]["data"]
        data=None
        for attempt in range(3):
            data = self.fetch_live_from_nse(symbol)
            if data: break
            try: time.sleep(0.6*(attempt+1))
            except: pass
        if not data:
            # DB fallback: last bhavcopy close
            try:
                from core.models.database import Database
                row=Database.get_instance().fetch_one("SELECT close_price FROM bhavcopy_data WHERE symbol=? ORDER BY trade_date DESC LIMIT 1",[symbol.upper()])
                if row: data={"spot":float(row["close_price"]),"formatted":f"INR {float(row['close_price']):,.2f}","change":0,"high":float(row["close_price"]),"low":float(row["close_price"]),"source":"db-fallback"}
            except: pass
        if data:
            _LIVE_CACHE[symbol] = {"ts": now, "data": data}
        return data

    def get_live_spots_cached(self, symbols):
        now = time.time()
        result = {}
        for sym in symbols:
            if sym in _LIVE_CACHE and now - _LIVE_CACHE[sym]["ts"] < _LIVE_CACHE_TTL:
                result[sym] = _LIVE_CACHE[sym]["data"]
        return result

    def get_live_spots_parallel(self, symbols, max_workers: int = 10):
        """Parallel live spots for 50 F&O symbols. Subprocess hard-killed at 12s + Yahoo realtime."""
        now = time.time()
        result = {}
        fresh = []
        for sym in symbols:
            if sym in _LIVE_CACHE and now - _LIVE_CACHE[sym]["ts"] < _LIVE_CACHE_TTL:
                result[sym] = _LIVE_CACHE[sym]["data"]
            else:
                fresh.append(sym)
        if not fresh:
            return result
        import multiprocessing as _mp
        q = _mp.Queue()
        p = _mp.Process(target=_spots_worker, args=(fresh, max_workers, q))
        p.start()
        p.join(12)
        if p.is_alive():
            p.terminate()
            p.join(2)
        else:
            try:
                res = q.get_nowait()
                for sym, data in res.items():
                    if data and data.get("spot"):
                        _LIVE_CACHE[sym] = {"ts": now, "data": data}
                        result[sym] = data
            except Exception:
                pass
        return result

    def get_live_chain_cached(self, symbol: str):
        now = time.time()
        sym = symbol.upper()
        if sym in _CHAIN_CACHE and now - _CHAIN_CACHE[sym]["ts"] < _CHAIN_CACHE_TTL:
            return _CHAIN_CACHE[sym]["data"]
        return None

    def get_live_chains_parallel(self, symbols):
        now = time.time()
        result = {}
        fresh = []
        for sym in symbols:
            if sym in _CHAIN_CACHE and now - _CHAIN_CACHE[sym]["ts"] < _CHAIN_CACHE_TTL:
                result[sym] = _CHAIN_CACHE[sym]["data"]
            else:
                fresh.append(sym)
        if not fresh:
            return result
        import multiprocessing as _mp
        q = _mp.Queue()
        p = _mp.Process(target=_chains_worker, args=(fresh, q))
        p.start()
        p.join(10)
        if p.is_alive():
            p.terminate()
            p.join(2)
        else:
            try:
                res = q.get_nowait()
                for sym, data in res.items():
                    if data and data.get("rows"):
                        _CHAIN_CACHE[sym] = {"ts": now, "data": data}
                        result[sym] = data
            except Exception:
                pass
        return result

    def fetch_option_chain_nse(self, symbol: str):
        return self.fetch_live_option_chain(symbol)

    def fetch_live_option_chain(self, symbol: str):
        """Live option chain: NSE live -> DB -> synthetic."""
        # 0) Try NSE live option chain first (real LTP, OI)
        try:
            live = _with_timeout(_fetch_nse_option_chain_live, 2.5, symbol)
            if live and live.get("rows"):
                return live
        except Exception:
            pass
        # 1) Try DB chain next
        try:
            from core.models.bhavcopy_model import BhavcopyModel
            bhav = BhavcopyModel()
            dates = bhav.get_dates(symbol)
            if dates:
                expiries = bhav.get_expiries(symbol, dates[0])
                if expiries:
                    chain = bhav.get_option_chain(symbol, dates[0], expiries[0])
                    if chain and len(chain) >= 4:
                        spot = self.get_spot_price(symbol)
                        if spot <= 0:
                            sd = self.fetch_live_from_nse(symbol)
                            spot = float(sd["spot"]) if sd else 0
                        from utils.helpers import get_strike_step
                        step = get_strike_step(symbol)
                        atm = round(spot / step) * step if spot > 0 else 0
                        ce = {r["strike_price"]: r for r in chain if r["option_type"] == "CE"}
                        pe = {r["strike_price"]: r for r in chain if r["option_type"] == "PE"}
                        all_strikes = sorted(set(list(ce.keys()) + list(pe.keys())))
                        rows = []
                        for s in all_strikes:
                            rows.append({"strike": s, "distance": int(s - atm) if atm else 0,
                                         "ce_ltp": ce.get(s, {}).get("close_price", 0), "ce_oi": ce.get(s, {}).get("oi", 0), "ce_vol": ce.get(s, {}).get("volume", 0), "ce_iv": 0,
                                         "pe_ltp": pe.get(s, {}).get("close_price", 0), "pe_oi": pe.get(s, {}).get("oi", 0), "pe_vol": pe.get(s, {}).get("volume", 0), "pe_iv": 0})
                        if rows:
                            return {"symbol": symbol, "spot": spot, "atm": atm, "rows": rows, "source": "bhavcopy", "timestamp": "", "max_pain": 0, "pcr": None}
        except Exception:
            pass
        # 2) Synthetic chain via Black-Scholes (instant, always works)
        try:
            spot = self.get_spot_price(symbol)
            if spot <= 0:
                sd = self.fetch_live_from_nse(symbol)
                spot = float(sd["spot"]) if sd else 0
            if spot > 0:
                from utils.helpers import get_strike_step, black_scholes
                step = get_strike_step(symbol)
                atm = round(spot / step) * step
                dte = 7 / 365.0
                rows = []
                for offset in range(-8, 9):
                    strike = atm + offset * step
                    ce_prem = black_scholes(spot, strike, dte, 0.20, "CE")
                    pe_prem = black_scholes(spot, strike, dte, 0.20, "PE")
                    rows.append({"strike": strike, "distance": int(strike - atm),
                                 "ce_ltp": round(ce_prem, 2), "ce_oi": 0, "ce_vol": 0, "ce_iv": 20,
                                 "pe_ltp": round(pe_prem, 2), "pe_oi": 0, "pe_vol": 0, "pe_iv": 20})
                if rows:
                    return {"symbol": symbol, "spot": spot, "atm": atm, "rows": rows, "source": "synthetic", "timestamp": "", "max_pain": 0, "pcr": None}
        except Exception:
            pass
        return None
