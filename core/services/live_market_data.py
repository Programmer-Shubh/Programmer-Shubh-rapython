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

# --- 3 fast alternatives (no NiftyTrader, no Yahoo) ---

def _fetch_nse_indices_spot(symbol: str):
    """NSE /api/allIndices — fastest for indices (200ms)."""
    try:
        _MAP = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FINANCIAL SERVICES", "MIDCPNIFTY": "NIFTY MIDCAP SELECT"}
        nse_name = _MAP.get(symbol.upper())
        if not nse_name:
            return None
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        s.get("https://www.nseindia.com", timeout=3)
        r = s.get("https://www.nseindia.com/api/allIndices", timeout=4)
        if r.status_code == 200:
            for item in r.json().get("data", []):
                if item.get("index") == nse_name:
                    spot = float(item.get("last", 0))
                    if spot > 0:
                        return {"spot": spot, "change": float(item.get("percentChange", 0) or 0),
                                "high": float(item.get("high", 0) or spot), "low": float(item.get("low", 0) or spot), "source": "nse"}
    except Exception:
        pass
    return None

def _fetch_nselib_spot(symbol: str):
    """nselib price_volume_data — reliable for stocks (1-2s)."""
    try:
        from nselib.capital_market import price_volume_data
        import datetime
        sd = (datetime.datetime.now() - datetime.timedelta(days=5)).strftime("%d-%m-%Y")
        ed = datetime.datetime.now().strftime("%d-%m-%Y")
        df = price_volume_data(symbol, from_date=sd, to_date=ed)
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

def _fetch_stocksrin_spot(symbol: str):
    """StocksRin spot — if available, else None (fast fallback)."""
    try:
        # Try StocksRin chain page for spot (may be behind auth, so quick fail)
        r = requests.get(f"https://stocksrin.com/api/spot/{symbol}", headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=2)
        if r.status_code == 200:
            j = r.json()
            spot = float(j.get("spot") or j.get("last") or j.get("price") or 0)
            if spot > 0:
                return {"spot": spot, "change": 0, "high": spot, "low": spot, "source": "stocksrin"}
    except Exception:
        pass
    return None


class LiveMarketData:
    def __init__(self):
        self.db = Database.get_instance()

    def get_spot_price(self, symbol: str) -> float:
        # 1) Live cache
        try:
            live = self.get_live_spot(symbol)
            if live and live.get("spot"):
                return float(live["spot"])
        except Exception:
            pass
        row = self.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=(SELECT MAX(trade_date) FROM bhavcopy_data WHERE symbol=?) AND option_type IS NULL",
            [symbol, symbol],
        )
        if row and row["close_price"] and float(row["close_price"]) > 0:
            return float(row["close_price"])
        row = self.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type='CE' AND trade_date=(SELECT MAX(trade_date) FROM bhavcopy_data WHERE symbol=?) ORDER BY ABS(strike_price - (SELECT AVG(strike_price) FROM bhavcopy_data WHERE symbol=? AND option_type='CE')) LIMIT 1",
            [symbol, symbol, symbol],
        )
        if row and row["close_price"] and float(row["close_price"]) > 0:
            return float(row["close_price"])
        for fn in [_fetch_nse_indices_spot, _fetch_nselib_spot]:
            try:
                d = fn(symbol)
                if d and d.get("spot", 0) > 0:
                    return float(d["spot"])
            except Exception:
                pass
        try:
            g = fetch_google_spot(symbol)
            if g and g > 0:
                return float(g)
        except Exception:
            pass
        return float(row["close_price"]) if row and row["close_price"] else 0

    def get_option_ltp(self, symbol: str, strike: float, option_type: str) -> float:
        row = self.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND strike_price=? AND option_type=? AND trade_date=(SELECT MAX(trade_date) FROM bhavcopy_data WHERE symbol=?)",
            [symbol, strike, option_type, symbol],
        )
        return float(row["close_price"]) if row else None

    def fetch_live_from_nse(self, symbol: str):
        """Spot via 3 fast alternatives: NSE indices -> nselib -> Google."""
        for fn in [_fetch_nse_indices_spot, _fetch_nselib_spot]:
            try:
                d = fn(symbol)
                if d:
                    return {"spot": d["spot"], "formatted": f"INR {d['spot']:,.2f}", "change": d.get("change", 0), "high": d.get("high", 0) or d["spot"], "low": d.get("low", 0) or d["spot"], "source": d["source"]}
            except Exception:
                pass
        try:
            g = fetch_google_spot(symbol)
            if g and g > 0:
                return {"spot": float(g), "formatted": f"INR {float(g):,.2f}", "change": 0, "high": float(g), "low": float(g), "source": "google"}
        except Exception:
            pass
        return None

    def get_live_spot(self, symbol: str):
        now = time.time()
        if symbol in _LIVE_CACHE and now - _LIVE_CACHE[symbol]["ts"] < _LIVE_CACHE_TTL:
            return _LIVE_CACHE[symbol]["data"]
        data = self.fetch_live_from_nse(symbol)
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
        if fresh:
            with ThreadPoolExecutor(max_workers=len(fresh)) as ex:
                futures = {ex.submit(self.fetch_live_option_chain, sym): sym for sym in fresh}
                for fut in as_completed(futures, timeout=10):
                    sym = futures[fut]
                    data = fut.result() if not fut.exception() else None
                    if data and data.get("rows"):
                        _CHAIN_CACHE[sym] = {"ts": now, "data": data}
                        result[sym] = data
        return result

    def fetch_option_chain_nse(self, symbol: str):
        return self.fetch_live_option_chain(symbol)

    def fetch_live_option_chain(self, symbol: str):
        """3 alternatives: nselib spot + DB -> nselib spot + synthetic -> nselib only. No NiftyTrader."""
        # Try DB chain first (fastest if bhavcopy exists)
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
        # Fallback: synthetic chain via Black-Scholes (instant, always works)
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
