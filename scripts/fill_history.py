"""Fill 3-month NSE history (spot OHLC) into local DB for backtest.
Uses tvDatafeed (only working free source here). Safe to re-run
(import_data dedupes by symbol/date/expiry/strike/option).
Usage:  PYTHONPATH=. python scripts/fill_history.py [months] [SYM1 SYM2 ...]
"""
import sys
import datetime

SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
           "RELIANCE", "TCS", "HDFCBANK", "INFY", "AXISBANK",
           "BAJAJFINSV", "BHARTIARTL", "BPCL", "SBILIFE"]


def main():
    months = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 3
    syms = [s.upper() for s in sys.argv[2:] if not s.isdigit()] or SYMBOLS
    from core.services.historical_fetcher import _fetch_tvDatafeed_historical, _last_trading_day
    from core.models.bhavcopy_model import BhavcopyModel
    from core.models.database import Database
    end = _last_trading_day().strftime("%Y-%m-%d")
    start = (_last_trading_day() - datetime.timedelta(days=int(months * 31))).strftime("%Y-%m-%d")
    print(f"Filling {start} -> {end} for {len(syms)} symbols", flush=True)
    bhav = BhavcopyModel()
    for sym in syms:
        try:
            before = Database.get_instance().fetch_one(
                "SELECT COUNT(*) c FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL AND trade_date BETWEEN ? AND ?",
                [sym, start, end])
            data = _fetch_tvDatafeed_historical(sym, start, end) or []
            n = bhav.import_data(data) if data else 0
            after = Database.get_instance().fetch_one(
                "SELECT COUNT(*) c, MIN(trade_date) mn, MAX(trade_date) mx FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL AND trade_date BETWEEN ? AND ?",
                [sym, start, end])
            print(f"[{sym}] got={len(data)} imported={n} spot_rows={after['c'] if after else 0} {after['mn'] if after else ''}->{after['mx'] if after else ''}", flush=True)
        except Exception as e:
            print(f"[{sym}] FAILED: {str(e)[:150]}", flush=True)


if __name__ == "__main__":
    main()
