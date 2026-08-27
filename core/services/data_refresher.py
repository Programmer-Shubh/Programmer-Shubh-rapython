import asyncio
import logging
import time
from core.services.live_market_data import LiveMarketData, _LIVE_CACHE
from core.models.database import Database
from core.models.bhavcopy_model import BhavcopyModel

logger = logging.getLogger(__name__)

INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
EXTRA_SYMBOLS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "TCS", "INFY", "ITC", "SBIN",
                 "AXISBANK", "KOTAKBANK", "LT", "HINDUNILVR", "BHARTIARTL", "M&M", "MARUTI",
                 "BAJFINANCE", "WIPRO", "ONGC", "SUNPHARMA", "ULTRACEMCO", "NTPC", "POWERGRID",
                 "TATAMOTORS", "TATASTEEL", "HCLTECH", "JSWSTEEL", "COALINDIA", "DRREDDY", "CIPLA",
                 "ADANIENT", "SBILIFE", "BPCL", "GRASIM", "TECHM", "DIVISLAB", "EICHERMOT", "BRITANNIA",
                 "HINDALCO", "VEDL", "INDUSINDBK", "SHREECEM", "NESTLEIND", "BAJAJFINSV", "HEROMOTOCO",
                 "APOLLOHOSP", "UPL"]
ALL_SYMBOLS = INDEX_SYMBOLS + EXTRA_SYMBOLS
_REFRESH_INTERVAL = 45
_RUNNING = False


def _seed_history(symbol: str):
    """Fetch real historical data via 3 fast alternatives: NSE nselib -> StocksRin -> Black-Scholes synthetic."""
    from datetime import datetime, timedelta
    db = Database.get_instance()
    has = db.fetch_one(
        "SELECT COUNT(*) as c FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL",
        [symbol],
    )
    existing = has["c"] if has else 0
    if existing >= 100:
        return
    bhav = BhavcopyModel()
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=190)).strftime("%Y-%m-%d")
    try:
        from core.services.historical_fetcher import fetch_historical
        data = fetch_historical(symbol, start, end)
        if data and len(data) >= 5:
            bhav.import_data(data)
    except Exception:
        pass


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


def refresh_all(light: bool = False):
    """Startup light=True -> return instantly (no network) so health/503 never hangs.
    Full refresh does background seeding (DB-only spot, no live hang)."""
    if light:
        return {}, 0
    live = LiveMarketData()
    chains = live.get_live_chains_parallel(INDEX_SYMBOLS)
    chain_ok = 0
    for sym in INDEX_SYMBOLS:
        chain = chains.get(sym)
        if chain:
            chain_ok += 1
            try:
                _seed_chain(sym, chain)
            except Exception:
                pass
            spot = chain.get("spot", 0)
            if spot:
                src = chain.get("source", "nse")
                _LIVE_CACHE[sym] = {"ts": time.time(), "data": {
                    "spot": spot, "formatted": f"INR {spot:,.2f}",
                    "change": 0, "high": spot, "low": spot, "source": src,
                }}
    if light:
        return {sym: bool(chains.get(sym)) for sym in INDEX_SYMBOLS}, chain_ok
    # Full refresh (background, after startup) - Yahoo live batch (subprocess 12s) + history seed
    try:
        # Use batch Yahoo live (reliable, 400ms) for 8 stocks per cycle
        batch_syms = EXTRA_SYMBOLS[:8]
        try:
            batch = live.get_live_spots_parallel(batch_syms, max_workers=8)
            for sym, spot_data in batch.items():
                if spot_data and spot_data.get("spot"):
                    src = spot_data.get("source", "yahoo")
                    _LIVE_CACHE[sym] = {"ts": time.time(), "data": {
                        "spot": spot_data["spot"], "formatted": f"INR {spot_data['spot']:,.2f}",
                        "change": spot_data.get("change", 0), "high": spot_data.get("high", spot_data["spot"]), "low": spot_data.get("low", spot_data["spot"]), "source": src,
                    }}
        except Exception:
            pass
        # indices also via Yahoo
        try:
            ibatch = live.get_live_spots_parallel(INDEX_SYMBOLS, max_workers=4)
            for sym, spot_data in ibatch.items():
                if spot_data and spot_data.get("spot"):
                    _LIVE_CACHE[sym] = {"ts": time.time(), "data": spot_data}
        except Exception:
            pass
        # Seed historical spot cache so backtest/option-chain use DB (instant, no 'Network error')
        for sym in ALL_SYMBOLS[:4]:
            try:
                _seed_history(sym)
            except Exception:
                continue
    except Exception:
        pass
    return {sym: bool(_LIVE_CACHE.get(sym)) for sym in INDEX_SYMBOLS}, len([s for s in INDEX_SYMBOLS if s in _LIVE_CACHE])


async def run_refresh_loop():
    global _RUNNING
    if _RUNNING:
        return
    _RUNNING = True
    logger.info("[refresh] background loop started (every %ss)", _REFRESH_INTERVAL)
    # Light initial refresh in background thread - non-blocking so health check returns 200 instantly
    try:
        await asyncio.to_thread(refresh_all, True)
    except Exception as e:
        logger.warning("[refresh] initial light refresh failed: %s", e)
    while True:
        await asyncio.sleep(_REFRESH_INTERVAL)
        try:
            await asyncio.to_thread(refresh_all, False)
        except Exception as e:
            logger.warning("[refresh] refresh failed: %s", e)
