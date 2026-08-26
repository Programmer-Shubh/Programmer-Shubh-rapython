from fastapi import APIRouter
from core.models.database import Database
from core.models.trade_model import TradeModel
from core.services.live_market_data import LiveMarketData
from utils.helpers import get_lot_size, format_currency

router = APIRouter()



_NSE_INDEX_MAP = {
    "NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FINANCIAL SERVICES", "MIDCPNIFTY": "NIFTY MIDCAP SELECT",
}

_NSE_SPOT_CACHE = {}
_NSE_SPOT_TTL = 30


def _fetch_nse_spot_all():
    """Fetch real spot prices for all indices from NSE India /api/allIndices."""
    import time
    now = time.time()
    if _NSE_SPOT_CACHE and now - _NSE_SPOT_CACHE.get("_ts", 0) < _NSE_SPOT_TTL:
        return _NSE_SPOT_CACHE
    try:
        import requests
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        s.get("https://www.nseindia.com/api/allIndices", timeout=10)
        r = s.get("https://www.nseindia.com/api/allIndices", timeout=10, headers={"Accept": "application/json"})
        if r.status_code == 200:
            data = r.json().get("data", [])
            for item in data:
                idx_name = item.get("index", "")
                for sym, nse_name in _NSE_INDEX_MAP.items():
                    if idx_name == nse_name:
                        _NSE_SPOT_CACHE[sym] = {
                            "spot": float(item.get("last", 0)),
                            "change": float(item.get("percentChange", 0)),
                            "high": float(item.get("high", 0)),
                            "low": float(item.get("low", 0)),
                        }
            _NSE_SPOT_CACHE["_ts"] = now
    except Exception:
        pass
    return _NSE_SPOT_CACHE


def _fetch_google_spot(symbol: str) -> float:
    """Scrape Google Finance for live spot price."""
    try:
        import requests, re
        r = requests.get(
            f"https://www.google.com/finance/quote/{symbol.upper()}:NSE",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
        )
        if r.status_code == 200:
            m = re.search(r'data-last-price="([^"]+)"', r.text)
            if m:
                return float(m.group(1).replace(",", ""))
    except Exception:
        pass
    return 0


def _fetch_nselib_spot(symbol: str) -> float:
    """Fetch spot via nselib (NSE bhavcopy, reliable for all stocks)."""
    try:
        from nselib.capital_market import price_volume_data
        import datetime
        sd = (datetime.datetime.now() - datetime.timedelta(days=5)).strftime("%d-%m-%Y")
        ed = datetime.datetime.now().strftime("%d-%m-%Y")
        df = price_volume_data(symbol, from_date=sd, to_date=ed)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            cl = float(last.get("ClosePrice", last.get("LastPrice", 0)) or 0)
            if cl > 0:
                return float(cl)
    except Exception:
        pass
    return 0


@router.get("/spot")
def get_spots():
    live = LiveMarketData()
    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
    result = {}
    # 1) Try live NiftyTrader cache
    live_data_map = live.get_live_spots_cached(symbols)
    for sym in symbols:
        live_data = live_data_map.get(sym)
        if live_data:
            result[sym] = {
                "spot": live_data["spot"],
                "formatted": f"INR {live_data['spot']:,.2f}",
                "change": live_data.get("change", 0),
                "high": live_data.get("high", 0),
                "low": live_data.get("low", 0),
                "source": "live",
            }
            continue
        # 2) Try NSE India /api/allIndices (real, always works)
        nse_data = _fetch_nse_spot_all().get(sym)
        if nse_data and nse_data["spot"] > 0:
            result[sym] = {
                "spot": nse_data["spot"],
                "formatted": f"INR {nse_data['spot']:,.2f}",
                "change": round(nse_data["change"], 2),
                "high": nse_data["high"],
                "low": nse_data["low"],
                "source": "nse",
            }
            continue
        # 3) Try nselib / Google
        spot, source = _free_latest_spot(sym)
        if spot > 0:
            change_pct = _free_change_pct(sym, spot)
            result[sym] = {
                "spot": round(spot, 2),
                "formatted": f"INR {spot:,.2f}",
                "change": round(change_pct, 2),
                "high": 0, "low": 0,
                "source": source,
            }
        else:
            result[sym] = {
                "spot": None, "formatted": "No Data",
                "change": 0, "high": 0, "low": 0, "source": "na",
            }
    return result


def _free_latest_spot(symbol: str):
    """NiftyTrader -> NSE nselib -> Google -> DB. No Yahoo."""
    # 1) NiftyTrader live (primary, has option chain)
    try:
        live = LiveMarketData()
        data = live.get_live_spot(symbol)
        if data and data.get("spot") and float(data["spot"]) > 0:
            return float(data["spot"]), "niftytrader"
    except Exception:
        pass
    # 2) NSE via nselib
    spot = _fetch_nselib_spot(symbol)
    if spot > 0:
        return spot, "nselib"
    # 3) Google Finance
    spot = _fetch_google_spot(symbol)
    if spot > 0:
        return spot, "google"
    # 4) DB fallback (last resort)
    db = Database.get_instance()
    try:
        row = db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1",
            [symbol],
        )
        if row and row["close_price"] and float(row["close_price"]) > 0:
            return float(row["close_price"]), "db"
    except Exception:
        pass
    return 0, "na"


def _free_change_pct(symbol: str, spot: float) -> float:
    try:
        db = Database.get_instance()
        rows = db.fetch_all(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 2",
            [symbol],
        )
        if len(rows) >= 2 and rows[1]["close_price"]:
            prev = float(rows[1]["close_price"])
            if prev > 0:
                return (spot - prev) / prev * 100
    except Exception:
        pass
    return 0


@router.get("/option-chain/{symbol}")
def get_option_chain(symbol: str):
    db = Database.get_instance()
    rows = db.fetch_all(
        "SELECT DISTINCT trade_date FROM bhavcopy_data WHERE symbol=? ORDER BY trade_date DESC",
        [symbol],
    )
    dates = [r["trade_date"] for r in rows]
    if not dates:
        return {"error": "No data imported"}
    latest = dates[0]
    exp_rows = db.fetch_all(
        "SELECT DISTINCT expiry_date FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND option_type IS NOT NULL ORDER BY expiry_date",
        [symbol, latest],
    )
    expiries = [r["expiry_date"] for r in exp_rows]
    if not expiries:
        return {"error": "No expiries found"}
    chain = db.fetch_all(
        "SELECT * FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND expiry_date=?",
        [symbol, latest, expiries[0]],
    )
    if not chain:
        return {"error": "No chain data"}
    ce = [{"strike": r["strike_price"], "ltp": r["close_price"], "oi": r.get("oi", 0), "vol": r.get("volume", 0)} for r in chain if r["option_type"] == "CE"]
    pe = [{"strike": r["strike_price"], "ltp": r["close_price"], "oi": r.get("oi", 0), "vol": r.get("volume", 0)} for r in chain if r["option_type"] == "PE"]
    return {"symbol": symbol, "date": latest, "expiry": expiries[0], "ce": ce, "pe": pe}


@router.get("/portfolio")
def get_portfolio():
    trade_model = TradeModel()
    positions = trade_model.get_open_positions_with_pnl()
    total_pnl = sum(p["unrealized_pnl"] for p in positions)
    return {
        "open_count": len(positions),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_formatted": format_currency(total_pnl),
        "positions": [
            {
                "id": t["trade"]["id"],
                "symbol": t["trade"]["symbol"],
                "option_type": t["trade"]["option_type"],
                "strike": t["trade"]["strike_price"],
                "transaction_type": t["trade"]["transaction_type"],
                "entry_price": t["trade"]["entry_price"],
                "current_price": t["current_price"] if t.get("current_price") is not None else t["trade"]["entry_price"],
                "pnl": t.get("unrealized_pnl", 0),
                "pnl_pct": t.get("unrealized_pct", 0),
                "sl": t["trade"]["stop_loss"],
                "tp": t["trade"]["target"],
                "status": t["trade"]["status"],
                "trade_mode": t["trade"].get("trade_mode", "paper"),
                "qty": t["trade"].get("quantity", 1),
                "lot_size": t["trade"].get("lot_size", 50),
                "entry_date": t["trade"].get("entry_date", ""),
                "expiry_date": t["trade"].get("expiry_date", ""),
            }
            for t in positions
        ],
    }


@router.get("/trade-history")
def get_trade_history():
    trade_model = TradeModel()
    closed = trade_model.get_closed_trades()
    total_pnl = sum(t["pnl"] for t in closed)
    return {
        "count": len(closed),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_formatted": format_currency(total_pnl),
        "trades": [
            {
                "id": t["id"],
                "entry_date": t["entry_date"],
                "exit_date": t.get("exit_date", ""),
                "symbol": t["symbol"],
                "option_type": t["option_type"],
                "strike": t["strike_price"],
                "transaction_type": t["transaction_type"],
                "entry": t["entry_price"],
                "exit": t.get("exit_price", 0),
                "pnl": t["pnl"],
                "pnl_formatted": format_currency(t["pnl"]),
                "status": t.get("exit_status", "closed"),
                "qty": t.get("quantity", 1),
            }
            for t in closed[:50]
        ],
    }


@router.get("/stats")
def get_stats():
    trade_model = TradeModel()
    return trade_model.get_stats()
