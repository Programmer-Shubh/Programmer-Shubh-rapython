from fastapi import APIRouter
from core.models.bhavcopy_model import BhavcopyModel
from core.models.trade_model import TradeModel
from core.services.live_market_data import LiveMarketData
from utils.helpers import get_lot_size, format_currency

router = APIRouter()


@router.get("/spot")
def get_spots():
    live = LiveMarketData()
    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
    result = {}
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
        # Free websites fallback (NiftyTrader/Google etc) - auto-fetch latest close and cache in DB
        spot = _free_latest_spot(sym)
        if spot > 0:
            # Compute change vs prev close from DB history
            change_pct = _free_change_pct(sym, spot)
            result[sym] = {
                "spot": round(spot, 2),
                "formatted": f"INR {spot:,.2f}",
                "change": round(change_pct, 2),
                "high": 0,
                "low": 0,
                "source": "free",
            }
        else:
            db_spot = live.get_spot_price(sym)
            result[sym] = {
                "spot": round(db_spot, 2) if db_spot > 0 else None,
                "formatted": f"INR {db_spot:,.2f}" if db_spot > 0 else "No Data",
                "change": 0,
                "high": 0,
                "low": 0,
                "source": "db" if db_spot > 0 else "na",
            }
    return result


def _free_latest_spot(symbol: str) -> float:
    """Latest spot via free sources; caches into bhavcopy_data table so scanner/trades reuse it."""
    try:
        bhav = BhavcopyModel()
        row = bhav.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1",
            [symbol],
        )
        if row and row["close_price"] and float(row["close_price"]) > 0:
            return float(row["close_price"])
        # Fetch last 10 days from free fetcher and store
        import datetime as _dt
        end = _dt.date.today().strftime("%Y-%m-%d")
        start = (_dt.date.today() - _dt.timedelta(days=14)).strftime("%Y-%m-%d")
        from core.services.historical_fetcher import fetch_historical
        data = fetch_historical(symbol, start, end)
        if data:
            bhav.import_data(data)
            return float(data[-1]["close_price"])
    except Exception:
        pass
    return 0


def _free_change_pct(symbol: str, spot: float) -> float:
    try:
        bhav = BhavcopyModel()
        rows = bhav.db.fetch_all(
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
    bhav = BhavcopyModel()
    dates = bhav.get_dates(symbol)
    if not dates:
        return {"error": "No data imported"}
    latest = dates[0]
    expiries = bhav.get_expiries(symbol, latest)
    if not expiries:
        return {"error": "No expiries found"}
    chain = bhav.get_option_chain(symbol, latest, expiries[0])
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
