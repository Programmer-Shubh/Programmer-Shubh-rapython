from fastapi import APIRouter, UploadFile, File
import pandas as pd
import io
from core.models.bhavcopy_model import BhavcopyModel

router = APIRouter()


@router.get("/dates/{symbol}")
def get_dates(symbol: str):
    bhav = BhavcopyModel()
    dates = bhav.get_dates(symbol)
    return {"dates": dates, "count": len(dates)}


@router.post("/import-csv")
def import_csv(file: UploadFile = File(...), symbol: str = "BANKNIFTY"):
    content = file.file.read().decode("utf-8")
    df = pd.read_csv(io.StringIO(content))
    records = []
    for _, row in df.iterrows():
        records.append({
            "symbol": symbol,
            "trade_date": str(row.get("trade_date", "")),
            "expiry_date": str(row.get("expiry_date", "")),
            "strike_price": float(row.get("strike_price", 0)),
            "option_type": row.get("option_type", ""),
            "open_price": float(row.get("open_price", 0)),
            "high_price": float(row.get("high_price", 0)),
            "low_price": float(row.get("low_price", 0)),
            "close_price": float(row.get("close_price", 0)),
            "volume": int(row.get("volume", 0)),
            "oi": int(row.get("oi", 0)),
        })
    bhav = BhavcopyModel()
    count = bhav.import_data(records)
    return {"imported": count, "symbol": symbol}


@router.post("/auto-seed")
def auto_seed():
    """Auto-seed 6 months (260 days) for all F&O indices + stocks - no manual CSV needed."""
    from core.services.data_refresher import refresh_all
    try:
        refresh_all()
    except Exception as e:
        return {"seeded": 0, "error": str(e)}
    from core.services.data_refresher import ALL_SYMBOLS
    bhav = BhavcopyModel()
    counts = {}
    for sym in ALL_SYMBOLS:
        dates = bhav.get_dates(sym)
        counts[sym] = len(dates)
    return {"seeded": len(ALL_SYMBOLS), "counts": counts}


@router.get("/data/{symbol}/{date}")
def get_data(symbol: str, date: str):
    bhav = BhavcopyModel()
    chain = bhav.get_option_chain(symbol, date, bhav.get_expiries(symbol, date)[0] if bhav.get_expiries(symbol, date) else "")
    return {"count": len(chain), "data": chain[:50]}
