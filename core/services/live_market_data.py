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
