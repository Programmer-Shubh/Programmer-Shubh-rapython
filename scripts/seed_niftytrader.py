import random
from datetime import datetime, timedelta
from core.models.database import Database
from core.models.bhavcopy_model import BhavcopyModel
from core.services.live_market_data import LiveMarketData

SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
EXTRA = ["RELIANCE", "HDFCBANK", "ICICIBANK", "TCS", "INFY", "ITC", "SBIN"]


def _dates_back(days: int) -> str:
    d = datetime.now() - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _trade_dates(days: int) -> list:
    dates = []
    d = datetime.now()
    while len(dates) < days:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return dates


def _seed_spot(symbol: str, spot: float, date: str):
    db = Database.get_instance()
    existing = db.fetch_one(
        "SELECT id FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND option_type IS NULL",
        [symbol, date],
    )
    if existing:
        return
    db.execute(
        """INSERT INTO bhavcopy_data
           (symbol, trade_date, expiry_date, strike_price, option_type,
            open_price, high_price, low_price, close_price, volume, oi)
           VALUES (?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)""",
        [symbol, date, spot, spot, spot, spot, 0, 0],
    )


def _seed_history(symbol: str, spot: float):
    """Generate ~250 rows of synthetic daily closes for the scanner."""
    db = Database.get_instance()
    has_history = db.fetch_one(
        "SELECT COUNT(*) as c FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL",
        [symbol],
    )
    if has_history and has_history["c"] > 210:
        return
    dates = _trade_dates(260)
    base = spot
    price = base
    rnd = random.Random(symbol)
    steps = []
    for _ in dates:
        price = price * (1 + rnd.uniform(-0.012, 0.012))
        steps.append(round(price, 2))
    last_spot = max(spot, base)
    scale = last_spot / steps[-1] if steps else 1
    steps = [round(s * scale, 2) for s in steps]
    rows = []
    seen = set()
    for dt, close in zip(dates, steps):
        key = (symbol, dt)
        if key in seen:
            continue
        seen.add(key)
        o = close * (1 + rnd.uniform(-0.004, 0.004))
        hi = max(o, close) * (1 + rnd.uniform(0, 0.006))
        lo = min(o, close) * (1 - rnd.uniform(0, 0.006))
        vol = int(rnd.uniform(5_000_000, 30_000_000))
        rows.append({
            "symbol": symbol, "trade_date": dt, "expiry_date": None,
            "strike_price": None, "option_type": None,
            "open_price": round(o, 2), "high_price": round(hi, 2),
            "low_price": round(lo, 2), "close_price": close,
            "volume": vol, "oi": 0,
        })
    if rows:
        BhavcopyModel().import_data(rows)


def _seed_chain(symbol: str, chain: dict):
    """Store today's live option chain from niftytrader.in into bhavcopy_data."""
    if not chain or not chain.get("rows"):
        return
    db = Database.get_instance()
    date = datetime.now().strftime("%Y-%m-%d")
    expiry = chain.get("timestamp", "") or ""
    rows = []
    seen = set()
    for r in chain["rows"]:
        strike = float(r["strike"])
        for opt, ltp_k, oi_k, vol_k in (("CE", "ce_ltp", "ce_oi", "ce_vol"), ("PE", "pe_ltp", "pe_oi", "pe_vol")):
            ltp = float(r.get(ltp_k, 0) or 0)
            key = (strike, opt)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "symbol": symbol, "trade_date": date, "expiry_date": expiry,
                "strike_price": strike, "option_type": opt,
                "open_price": ltp, "high_price": ltp, "low_price": ltp,
                "close_price": ltp, "volume": int(r.get(vol_k, 0) or 0),
                "oi": int(r.get(oi_k, 0) or 0),
            })
    if rows:
        BhavcopyModel().import_data(rows)


def seed_from_niftytrader(force: bool = False):
    """Populate bhavcopy_data with live data from niftytrader.in if empty."""
    db = Database.get_instance()
    count = db.fetch_one("SELECT COUNT(*) as c FROM bhavcopy_data")
    has_chain = db.fetch_one(
        "SELECT COUNT(*) as c FROM bhavcopy_data WHERE option_type IS NOT NULL"
    )
    already = False
    if count and count["c"] > 5000:
        # data already present
        if force or not has_chain or not has_chain["c"]:
            pass
        else:
            return

    live = LiveMarketData()
    print("[seed] fetching live data from niftytrader.in ...")
    chains = {}
    for sym in SYMBOLS:
        d = live.fetch_live_from_nse(sym)
        c = live.fetch_live_option_chain(sym)
        spot = (d or {}).get("spot") or (c or {}).get("spot") or 0
        if spot:
            _seed_spot(sym, spot, datetime.now().strftime("%Y-%m-%d"))
        if c:
            chains[sym] = c
            _seed_chain(sym, c)
        # mock history for non-index symbols too, anchored between NIFTY/BANKNIFTY scaling
    for sym in EXTRA:
        base = live.get_spot_price("NIFTY")
        if not base:
            base = 24000
        # rough anchor: derive a plausible base price, seeded near last close if exists
        last = db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1",
            [sym],
        )
        anchor = float(last["close_price"]) if last else (base * (0.55 if sym == "HDFCBANK" else 0.15))
        _seed_history(sym, anchor)

    for sym in SYMBOLS:
        last = db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1",
            [sym],
        )
        anchor = float(last["close_price"]) if last else live.get_spot_price(sym) or 24000
        _seed_history(sym, anchor)

    new_count = db.fetch_one("SELECT COUNT(*) as c FROM bhavcopy_data")
    print(f"[seed] done. DB rows: {new_count['c'] if new_count else 0}")


if __name__ == "__main__":
    seed_from_niftytrader()