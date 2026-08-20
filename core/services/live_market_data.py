import requests
from core.models.database import Database


class LiveMarketData:
    def __init__(self):
        self.db = Database.get_instance()

    def get_spot_price(self, symbol: str) -> float:
        row = self.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=(SELECT MAX(trade_date) FROM bhavcopy_data WHERE symbol=?) AND option_type IS NULL",
            [symbol, symbol],
        )
        if row:
            return float(row["close_price"])
        row = self.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type='CE' AND trade_date=(SELECT MAX(trade_date) FROM bhavcopy_data WHERE symbol=?) ORDER BY ABS(strike_price - (SELECT AVG(strike_price) FROM bhavcopy_data WHERE symbol=? AND option_type='CE')) LIMIT 1",
            [symbol, symbol, symbol],
        )
        return float(row["close_price"]) if row else 0

    def get_option_ltp(self, symbol: str, strike: float, option_type: str) -> float:
        row = self.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND strike_price=? AND option_type=? AND trade_date=(SELECT MAX(trade_date) FROM bhavcopy_data WHERE symbol=?)",
            [symbol, strike, option_type, symbol],
        )
        return float(row["close_price"]) if row else None

    def fetch_live_from_nse(self, symbol: str) -> dict:
        try:
            url = f"https://www.nseindia.com/api/equity-stockIndices?index={symbol}%2050"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.nseindia.com/market-data/live-equity-market",
            }
            session = requests.Session()
            session.get("https://www.nseindia.com", headers=headers, timeout=5)
            resp = session.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data and len(data["data"]) > 0:
                    for item in data["data"]:
                        if "lastPrice" in item:
                            return {"spot": item["lastPrice"], "change": item.get("pChange", 0), "high": item.get("dayHigh", 0), "low": item.get("dayLow", 0)}
        except Exception:
            pass
        return None

    def fetch_option_chain_nse(self, symbol: str) -> dict:
        try:
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/market-data/option-chain",
            }
            session = requests.Session()
            session.get("https://www.nseindia.com", headers=headers, timeout=5)
            resp = session.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def fetch_live_option_chain(self, symbol: str) -> dict:
        try:
            symbol_map = {"NIFTY": "nifty", "BANKNIFTY": "banknifty", "FINNIFTY": "finnifty", "MIDCPNIFTY": "midcpnifty"}
            ephem = symbol_map.get(symbol.upper(), symbol.lower())
            home_url = f"https://www.niftytrader.in/nse-option-chain/{ephem}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.niftytrader.in/",
            }
            session = requests.Session()
            home_resp = session.get(home_url, headers=headers, timeout=10)
            if home_resp.status_code != 200:
                return None
            import re as _re
            build_id_match = _re.search(r'"buildId":"([a-zA-Z0-9_]+)"', home_resp.text)
            if not build_id_match:
                return None
            build_id = build_id_match.group(1)
            data_url = f"https://www.niftytrader.in/_next/data/{build_id}/nse-option-chain/{ephem}.json"
            data_resp = session.get(data_url, headers=headers, timeout=15)
            if data_resp.status_code != 200:
                return None
            data = data_resp.json()
            page_props = data.get("pageProps", {})
            spot_data = page_props.get("initialSpot", {})
            chain_rows = page_props.get("initialOptionChainData", [])
            rows = []
            spot_price = float(spot_data.get("last_trade_price", 0))
            for r in chain_rows:
                strike = r.get("strike_price", 0)
                distance = int(strike - spot_price)
                row = {
                    "strike": strike,
                    "distance": distance,
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
            atm_strike = round(spot_price / 50) * 50
            return {
                "symbol": symbol,
                "spot": spot_price,
                "atm": atm_strike,
                "rows": rows,
                "source": "niftytrader",
                "timestamp": spot_data.get("timestamp", ""),
                "max_pain": spot_data.get("max_pain", 0),
                "pcr": None,
            }
        except Exception:
            pass
        return None
