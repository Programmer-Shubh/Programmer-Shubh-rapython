import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.services.live_market_data import LiveMarketData, _LIVE_CACHE
from core.models.database import Database
from core.models.bhavcopy_model import BhavcopyModel

logger = logging.getLogger(__name__)

INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
_REFRESH_INTERVAL = 45  # seconds (30-60 range)
_RUNNING = False


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