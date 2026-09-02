"""Bulk import 2019-2026 bhavcopy for deep backtest (Quantman 5Y)."""
import datetime, time, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.services.historical_fetcher import _fetch_nselib_historical, _fetch_jugaad_historical
from core.models.bhavcopy_model import BhavcopyModel

SYMBOLS = ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","RELIANCE","HDFCBANK","ICICIBANK","TCS","INFY","ITC","SBIN","AXISBANK","KOTAKBANK","LT","HINDUNILVR","BHARTIARTL","M&M","MARUTI","BAJFINANCE","WIPRO","ONGC","SUNPHARMA","ULTRACEMCO","NTPC","POWERGRID","TATAMOTORS","TATASTEEL","HCLTECH","JSWSTEEL","COALINDIA","DRREDDY","CIPLA","ADANIENT","SBILIFE","BPCL","GRASIM","TECHM","DIVISLAB","EICHERMOT","BRITANNIA"]
START = "2019-01-01"
END = datetime.date.today().strftime("%Y-%m-%d")
CHUNK_DAYS = 90

def import_range(symbol, s, e):
    for fn in (_fetch_nselib_historical, _fetch_jugaad_historical):
        try:
            data = fn(symbol, s, e)
            if data and len(data) >= 5:
                BhavcopyModel().import_data(data)
                print(f"  {symbol} {s}->{e}: {len(data)} via {fn.__name__}")
                return len(data)
        except Exception as ex:
            print(f"  {symbol} {fn.__name__} fail: {ex}")
    return 0

if __name__ == "__main__":
    bhav = BhavcopyModel()
    d1 = datetime.datetime.strptime(START, "%Y-%m-%d").date()
    d2 = datetime.datetime.strptime(END, "%Y-%m-%d").date()
    for sym in SYMBOLS:
        cur = d1
        total = 0
        while cur <= d2:
            nxt = min(cur + datetime.timedelta(days=CHUNK_DAYS-1), d2)
            n = import_range(sym, cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d"))
            total += n
            cur = nxt + datetime.timedelta(days=1)
            time.sleep(0.5)
        print(f"{sym} done total {total}")
    print("5Y import complete")
