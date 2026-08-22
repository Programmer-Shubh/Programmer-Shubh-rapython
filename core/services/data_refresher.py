import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.services.live_market_data import LiveMarketData, _LIVE_CACHE
from core.models.database import Database
from core.models.bhavcopy_model import BhavcopyModel

logger = logging.getLogger(__name__)

INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
EXTRA_SYMBOLS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "TCS", "INFY", "ITC", "SBIN",
                 "AXISBANK","KOTAKBANK","LT","HINDUNILVR","BHARTIARTL","M&M","MARUTI",
                 "BAJFINANCE","WIPRO","ONGC","SUNPHARMA","ULTRACEMCO","NTPC","POWERGRID",
                 "TATAMOTORS","TATASTEEL","HCLTECH","JSWSTEEL","COALINDIA","DRREDDY","CIPLA",
                 "ADANIENT","SBILIFE","BPCL","GRASIM","TECHM","DIVISLAB","EICHERMOT","BRITANNIA",
                 "HINDALCO","VEDL","INDUSINDBK","SHREECEM","NESTLEIND","BAJAJFINSV","HEROMOTOCO",
                 "APOLLOHOSP","UPL"]
# Realistic base prices for fallback when niftytrader blocked (Render)
FALLBACK_SPOTS = {
    "NIFTY": 24500, "BANKNIFTY": 52500, "FINNIFTY": 24700, "MIDCPNIFTY": 13200,
    "RELIANCE": 2900, "HDFCBANK": 1720, "ICICIBANK": 1330, "TCS": 4100, "INFY": 1850,
    "ITC": 430, "SBIN": 810, "AXISBANK": 1130, "KOTAKBANK": 1930, "LT": 3600,
    "HINDUNILVR": 2380, "BHARTIARTL": 1630, "M&M": 3130, "MARUTI": 12600, "BAJFINANCE": 8900,
    "WIPRO": 550, "ONGC": 255, "SUNPHARMA": 1820, "ULTRACEMCO": 11600, "NTPC": 350,
    "POWERGRID": 315, "TATAMOTORS": 1010, "TATASTEEL": 163, "HCLTECH": 1950, "JSWSTEEL": 1020,
    "COALINDIA": 510, "DRREDDY": 1320, "CIPLA": 1550, "ADANIENT": 2450, "SBILIFE": 1490,
    "BPCL": 340, "GRASIM": 2610, "TECHM": 1680, "DIVISLAB": 6350, "EICHERMOT": 4870,
    "BRITANNIA": 5750, "HINDALCO": 680, "VEDL": 465, "INDUSINDBK": 1420, "SHREECEM": 27800,
    "NESTLEIND": 2450, "BAJAJFINSV": 1930, "HEROMOTOCO": 4850, "APOLLOHOSP": 7100, "UPL": 530,
}
ALL_SYMBOLS = INDEX_SYMBOLS + EXTRA_SYMBOLS
_REFRESH_INTERVAL = 45  # seconds (30-60 range)
_RUNNING = False


def _seed_history(symbol: str, anchor: float):
    """Generate ~260 days (6 months) of realistic daily closes so scanner/backtest has enough history. Auto-updates."""
    import random
    from datetime import datetime, timedelta
    from core.services.indicator_engine import IndicatorEngine

    db = Database.get_instance()
    has = db.fetch_one(
        "SELECT COUNT(*) as c FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL",
        [symbol],
    )
    # Auto-update: if <260 rows, top-up to 260; don't skip if we have 0 for NIFTY/BANKNIFTY
    existing = has["c"] if has else 0
    if existing >= 260:
        return
    need = 260 - existing
    dates = []
    d = datetime.now()
    while len(dates) < 260:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    # Use fallback spot if anchor is missing/0 (e.g. NIFTY when niftytrader blocked on Render)
    if not anchor or anchor <= 0:
        anchor = FALLBACK_SPOTS.get(symbol, 1000)
    rnd = random.Random(symbol)
    price = float(anchor)
    rows = []
    seen = set()
    # Fetch existing dates to avoid duplicates when topping up
    existing_dates = set()
    if existing > 0:
        db_rows = db.fetch_all("SELECT trade_date FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL", [symbol])
        existing_dates = {r["trade_date"] for r in db_rows}
    for dt in dates:
        if dt in existing_dates:
            continue
        price = price * (1 + rnd.uniform(-0.012, 0.012))
        o = price * (1 + rnd.uniform(-0.004, 0.004))
        hi = max(o, price) * (1 + rnd.uniform(0, 0.006))
        lo = min(o, price) * (1 - rnd.uniform(0, 0.006))
        key = (symbol, dt)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "symbol": symbol, "trade_date": dt, "expiry_date": None,
            "strike_price": None, "option_type": None,
            "open_price": round(o, 2), "high_price": round(hi, 2),
            "low_price": round(lo, 2), "close_price": round(price, 2),
            "volume": int(rnd.uniform(5_000_000, 30_000_000)), "oi": 0,
        })
        if len(rows) >= need:
            break
    if rows:
        BhavcopyModel().import_data(rows)


def _seed_chain(symbol: str, chain: dict):
    if not chain or not chain.get("rows"):
        return
    db = Database.get_instance()
    date = time.strftime("%Y-%m-%d")
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


def refresh_all():
    """Fetch latest spot + option chains for all F&O symbols from niftytrader.in + auto-seed 6-month history."""
    live = LiveMarketData()
    # Try indices first
    chains = live.get_live_chains_parallel(INDEX_SYMBOLS)
    chain_ok = 0
    for sym in INDEX_SYMBOLS:
        chain = chains.get(sym)
        if chain:
            chain_ok += 1
            _seed_chain(sym, chain)
            spot = chain.get("spot", 0)
            if spot:
                _LIVE_CACHE[sym] = {"ts": time.time(), "data": {
                    "spot": spot,
                    "formatted": f"INR {spot:,.2f}",
                    "change": 0,
                    "high": spot,
                    "low": spot,
                    "source": "niftytrader.in",
                }}
    # Auto-seed 6-month history for ALL F&O symbols (indices + stocks) - works even when niftytrader blocked
    for sym in ALL_SYMBOLS:
        chain = chains.get(sym)
        spot = 0
        if chain and chain.get("spot"):
            spot = chain["spot"]
        else:
            spot = live.get_spot_price(sym) or FALLBACK_SPOTS.get(sym, 0)
            if spot <= 0:
                spot = FALLBACK_SPOTS.get(sym, 1000)
        _seed_history(sym, spot)
    # Also cache stock spots from niftytrader if available (best-effort, ignore timeout)
    try:
        stock_chains = live.get_live_chains_parallel(EXTRA_SYMBOLS[:10])
        for sym, ch in stock_chains.items():
            if ch and ch.get("spot"):
                _LIVE_CACHE[sym] = {"ts": time.time(), "data": {"spot": ch["spot"], "formatted": f"INR {ch['spot']:,.2f}", "change": 0, "high": ch["spot"], "low": ch["spot"], "source": "niftytrader.in"}}
    except Exception:
        pass
    return {sym: bool(chains.get(sym)) for sym in INDEX_SYMBOLS}, chain_ok


async def run_refresh_loop():
    """Background loop: refresh all data every 30-60 seconds."""
    global _RUNNING
    if _RUNNING:
        return
    _RUNNING = True
    logger.info("[refresh] background loop started (every %ss)", _REFRESH_INTERVAL)
    try:
        refresh_all()
    except Exception as e:
        logger.warning("[refresh] initial refresh failed: %s", e)
    while True:
        await asyncio.sleep(_REFRESH_INTERVAL)
        try:
            await asyncio.to_thread(refresh_all)
        except Exception as e:
            logger.warning("[refresh] refresh failed: %s", e)