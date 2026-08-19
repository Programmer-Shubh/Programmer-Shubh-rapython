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
    for sym in symbols:
        spot = live.get_spot_price(sym)
        live_data = live.fetch_live_from_nse(sym)
        if live_data:
            result[sym] = {
                "spot": live_data["spot"],
                "formatted": f"INR {live_data['spot']:,.2f}",
                "change": live_data.get("change", 0),
                "high": live_data.get("high", 0),
                "low": live_data.get("low", 0),
                "source": "live",
            }
        else:
            result[sym] = {
                "spot": round(spot, 2) if spot > 0 else None,
                "formatted": f"INR {spot:,.2f}" if spot > 0 else "No Data",
                "change": 0,
                "high": 0,
                "low": 0,
                "source": "bhavcopy",
            }
    return result


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
                "current_price": t["current_price"],
                "pnl": t["unrealized_pnl"],
                "sl": t["trade"]["stop_loss"],
                "tp": t["trade"]["target"],
                "status": t["trade"]["status"],
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
                "date": t["entry_date"],
                "symbol": t["symbol"],
                "option_type": t["option_type"],
                "strike": t["strike_price"],
                "entry": t["entry_price"],
                "exit": t.get("exit_price", 0),
                "pnl": t["pnl"],
                "pnl_formatted": format_currency(t["pnl"]),
            }
            for t in closed[:30]
        ],
    }


@router.get("/stats")
def get_stats():
    trade_model = TradeModel()
    return trade_model.get_stats()
