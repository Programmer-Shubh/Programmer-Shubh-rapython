import os
import json
from datetime import datetime

os.environ.setdefault("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "ratrade.db"))

from core.models.database import Database
from core.models.bhavcopy_model import BhavcopyModel
from core.services.live_market_data import LiveMarketData

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def run_scan():
    db = Database.get_instance()
    db.init_schema()
    bhav = BhavcopyModel()
    live = LiveMarketData()
    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
    signals = []
    for symbol in symbols:
        dates = bhav.get_dates(symbol)
        if not dates:
            continue
        latest = dates[0]
        expiries = bhav.get_expiries(symbol, latest)
        if not expiries:
            continue
        chain = bhav.get_option_chain(symbol, latest, expiries[0])
        spot = live.get_spot_price(symbol)
        if not chain or spot <= 0:
            continue
        total_ce_oi = sum(r.get("oi", 0) for r in chain if r["option_type"] == "CE")
        total_pe_oi = sum(r.get("oi", 0) for r in chain if r["option_type"] == "PE")
        pcr = total_pe_oi / max(total_ce_oi, 1)
        signal = "NEUTRAL"
        if pcr > 1.5:
            signal = "BULLISH"
        elif pcr < 0.7:
            signal = "BEARISH"
        signals.append({"symbol": symbol, "spot": spot, "pcr": round(pcr, 2), "signal": signal})
    log_file = os.path.join(LOG_DIR, f"scan_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(log_file, "w") as f:
        json.dump(signals, f, indent=2)
    print(f"[{datetime.now()}] Scan: {len(signals)} symbols")
    for s in signals:
        print(f"  {s['symbol']}: {s['signal']} (PCR={s['pcr']}, Spot={s['spot']})")
    return signals


if __name__ == "__main__":
    run_scan()
