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
    """6-month EOD archive: NSE archives -> nselib -> jugaad, always 180 days."""
    from datetime import datetime, timedelta
    db = Database.get_instance()
    has = db.fetch_one(
        "SELECT COUNT(*) as c FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL",
        [symbol],
    )
    existing = has["c"] if has else 0
    if existing >= 120:
        return
    bhav = BhavcopyModel()
    # Always 6 months archive, clamp to last trading day
    from core.services.historical_fetcher import _last_trading_day
    end = _last_trading_day().strftime("%Y-%m-%d")
    start = (_last_trading_day() - timedelta(days=190)).strftime("%Y-%m-%d")
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
    # Full refresh (background, after startup) - Stooq/Google free live batch + history seed
    try:
        # Use batch free live (NSE/Stooq/Google, 400ms) for 8 stocks per cycle
        batch_syms = EXTRA_SYMBOLS[:8]
        try:
            batch = live.get_live_spots_parallel(batch_syms, max_workers=8)
            for sym, spot_data in batch.items():
                if spot_data and spot_data.get("spot"):
                    src = spot_data.get("source", "stooq")
                    _LIVE_CACHE[sym] = {"ts": time.time(), "data": {
                        "spot": spot_data["spot"], "formatted": f"INR {spot_data['spot']:,.2f}",
                        "change": spot_data.get("change", 0), "high": spot_data.get("high", spot_data["spot"]), "low": spot_data.get("low", spot_data["spot"]), "source": src,
                    }}
        except Exception:
            pass
        # indices also via free live (NSE/Stooq/Google)
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
    # Nightly auto-import at 18:30 IST (13:00 UTC): 1Y bhavcopy for deep backtest
    async def _nightly_import_loop():
        import datetime as _dt
        while True:
            try:
                now_ist = _dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)
                # Run at 18:30 IST daily
                target = now_ist.replace(hour=18, minute=30, second=0, microsecond=0)
                if now_ist >= target:
                    target += _dt.timedelta(days=1)
                wait_sec = (target - now_ist).total_seconds()
                await asyncio.sleep(wait_sec)
                await asyncio.to_thread(_nightly_bhavcopy_import)
            except Exception as e:
                logger.warning("[nightly] loop error: %s", e)
                await asyncio.sleep(3600)

    def _nightly_bhavcopy_import():
        try:
            from core.services.historical_fetcher import fetch_historical
            from core.models.bhavcopy_model import BhavcopyModel
            bhav = BhavcopyModel()
            # Last 1 year for all F&O - chunked to avoid NSE block
            end = __import__("datetime").date.today().strftime("%Y-%m-%d")
            start = (__import__("datetime").date.today() - __import__("datetime").timedelta(days=365)).strftime("%Y-%m-%d")
            for sym in ALL_SYMBOLS:
                try:
                    # Skip if already has 200+ rows in last year
                    has = bhav.get_dates(sym)
                    if len([d for d in has if d >= start]) >= 200:
                        continue
                    data = fetch_historical(sym, start, end, allow_synthetic=False)
                    if data and len(data) >= 5:
                        bhav.import_data(data)
                        logger.info(f"[nightly] {sym} imported {len(data)}")
                    import time as _t; _t.sleep(0.8)
                except Exception as ex:
                    logger.warning(f"[nightly] {sym} fail: {ex}")
        except Exception as e:
            logger.warning(f"[nightly] import error: {e}")

    asyncio.get_running_loop().create_task(_nightly_import_loop())
    while True:
        await asyncio.sleep(_REFRESH_INTERVAL)
        try:
            await asyncio.to_thread(refresh_all, False)
        except Exception as e:
            logger.warning("[refresh] refresh failed: %s", e)
