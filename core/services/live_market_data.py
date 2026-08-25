import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from core.models.database import Database
try:
    from core.services.free_data import fetch_yahoo_spot, fetch_google_spot
except Exception:
    fetch_yahoo_spot = lambda s: 0
    fetch_google_spot = lambda s: 0

_LIVE_CACHE = {}
_LIVE_CACHE_TTL = 5  # NSE-like streaming: refresh every 5 sec
_CHAIN_CACHE = {}
_CHAIN_CACHE_TTL = 5

_SYMBOL_MAP = {"NIFTY": "nifty", "BANKNIFTY": "banknifty", "FINNIFTY": "finnifty", "MIDCPNIFTY": "midcpnifty"}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.niftytrader.in/",
}


class LiveMarketData:
    def __init__(self):
        self.db = Database.get_instance()

    def get_spot_price(self, symbol: str) -> float:
        # Try live cache first (NSE-like streaming)
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
        # Free website fallback: Yahoo/Google (no bhavcopy needed)
        try:
            y = fetch_yahoo_spot(symbol)
            if y and y > 0:
                return float(y)
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

    def _fetch_chain_page(self, symbol: str) -> dict:
        ephem = _SYMBOL_MAP.get(symbol.upper(), symbol.lower())
        home_url = f"https://www.niftytrader.in/nse-option-chain/{ephem}"
        session = requests.Session()
        session.headers.update(_HEADERS)
        home_resp = session.get(home_url, timeout=12)
        if home_resp.status_code != 200:
            return None
        build_id_match = re.search(r'"buildId":"([a-zA-Z0-9_]+)"', home_resp.text)
        if not build_id_match:
            return None
        build_id = build_id_match.group(1)
        data_url = f"https://www.niftytrader.in/_next/data/{build_id}/nse-option-chain/{ephem}.json"
        data_resp = session.get(data_url, timeout=15)
        if data_resp.status_code != 200:
            return None
        return data_resp.json()

    def fetch_live_from_nse(self, symbol: str) -> dict:
        """Fetch live spot + market data for a symbol from niftytrader.in (single source)."""
        try:
            data = self._fetch_chain_page(symbol)
            if not data:
                return None
            page_props = data.get("pageProps", {})
            spot_data = page_props.get("initialSpot", {})
            spot = float(spot_data.get("last_trade_price", 0) or 0)
            if spot <= 0:
                return None
            change = float(spot_data.get("change", 0) or 0)
            return {
                "spot": spot,
                "formatted": f"INR {spot:,.2f}",
                "change": change,
                "high": float(spot_data.get("high", 0) or spot),
                "low": float(spot_data.get("low", 0) or spot),
                "source": "niftytrader.in",
            }
        except Exception:
            return None

    def get_live_spot(self, symbol: str) -> dict:
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

    def get_live_chain_cached(self, symbol: str) -> dict:
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
                for fut in as_completed(futures, timeout=20):
                    sym = futures[fut]
                    data = fut.result() if not fut.exception() else None
                    if data and data.get("rows"):
                        _CHAIN_CACHE[sym] = {"ts": now, "data": data}
                        result[sym] = data
        return result

    def fetch_option_chain_nse(self, symbol: str) -> dict:
        """Fetch live option chain from niftytrader.in (single source)."""
        return self.fetch_live_option_chain(symbol)

    def fetch_live_option_chain(self, symbol: str) -> dict:
        try:
            data = self._fetch_chain_page(symbol)
            if not data:
                return None
            page_props = data.get("pageProps", {})
            spot_data = page_props.get("initialSpot", {})
            chain_rows = page_props.get("initialOptionChainData", [])
            rows = []
            spot_price = float(spot_data.get("last_trade_price", 0) or 0)
            for r in chain_rows:
                strike = float(r.get("strike_price", 0) or 0)
                row = {
                    "strike": strike,
                    "distance": int(strike - spot_price),
                    "ce_ltp": r.get("calls_ltp", 0),
                    "ce_oi": r.get("calls_oi", 0),
                    "ce_vol": r.get("calls_volume", 0),
                    "ce_iv": r.get("calls_iv", 0),
                    "pe_ltp": r.get("puts_ltp", 0),
                    "pe_oi": r.get("puts_oi", 0),
                    "pe_vol": r.get("puts_volume", 0),
                    "pe_iv": r.get("puts_iv", 0),
                }
                rows.append(row)
            if rows:
                atm_strike = round(spot_price / 50) * 50
                return {
                    "symbol": symbol,
                    "spot": spot_price,
                    "atm": atm_strike,
                    "rows": rows,
                    "source": "niftytrader.in",
                    "timestamp": spot_data.get("timestamp", ""),
                    "max_pain": spot_data.get("max_pain", 0),
                    "pcr": None,
                }
        except Exception:
            pass
        return None
