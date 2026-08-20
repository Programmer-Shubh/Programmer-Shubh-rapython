import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.services.live_market_data import LiveMarketData, _LIVE_CACHE
from core.models.database import Database
from core.models.bhavcopy_model import BhavcopyModel

logger = logging.getLogger(__name__)

INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
EXTRA_SYMBOLS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "TCS", "INFY", "ITC", "SBIN"]
ALL_SYMBOLS = INDEX_SYMBOLS + EXTRA_SYMBOLS
_REFRESH_INTERVAL = 45  # seconds (30-60 range)
_RUNNING = False


def _seed_history(symbol: str, anchor: float):
    """Generate ~260 days of synthetic daily closes so the scanner has enough history."""
    import random
    from datetime import datetime, timedelta
    from core.services.indicator_engine import IndicatorEngine

    db = Database.get_instance()
    has = db.fetch_one(
        "SELECT COUNT(*) as c FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL",
        [symbol],
    )
    if has and has["c"] > 210:
        return
    dates = []
    d = datetime.now()
    while len(dates) < 260:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    rnd = random.Random(symbol)
    price = float(anchor or 20000)
    rows = []
    seen = set()
    for dt in dates:
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
    """Fetch latest spot + option chains for all index symbols from niftytrader.in."""
    live = LiveMarketData()
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
    # ensure scanner has history for all symbols (only fills once when empty)
    for sym in INDEX_SYMBOLS:
        chain = chains.get(sym)
        if chain and chain.get("spot"):
            _seed_history(sym, chain["spot"])
    for sym in EXTRA_SYMBOLS:
        _seed_history(sym, live.get_spot_price(sym) or 1000)
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