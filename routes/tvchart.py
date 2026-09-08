"""TradingView-chart data feed: spot candles + SuperTrend/EMA overlays +
trade markers (entries/exits/open positions, paper + live modes)."""
from fastapi import APIRouter
from core.models.database import Database
from core.services.indicator_engine import IndicatorEngine

router = APIRouter()


def _candles(symbol: str, start: str, end: str) -> list:
    db = Database.get_instance()
    rows = db.fetch_all(
        """SELECT trade_date, open_price, high_price, low_price, close_price, volume
           FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL
           AND trade_date BETWEEN ? AND ? ORDER BY trade_date""",
        [symbol, start, end],
    )
    if not rows or len(rows) < 5:
        try:
            from routes.strategy_builder import _generate_synthetic_fallback
            rows = _generate_synthetic_fallback(symbol, start, end)
        except Exception:
            rows = []
    out = []
    for r in rows:
        try:
            out.append({
                "time": str(r.get("trade_date", ""))[:10],
                "open": float(r.get("open_price", 0) or 0),
                "high": float(r.get("high_price", 0) or 0),
                "low": float(r.get("low_price", 0) or 0),
                "close": float(r.get("close_price", 0) or 0),
            })
        except Exception:
            continue
    return [c for c in out if c["time"] and c["close"] > 0]


@router.get("/data")
def chart_data(symbol: str = "NIFTY", start: str = "2026-08-01", end: str = "2026-09-08",
               mode: str = "all", st_period: int = 10, st_mult: float = 3.0,
               ema_fast: int = 9, ema_slow: int = 21):
    try:
        symbol = (symbol or "NIFTY").upper()
        candles = _candles(symbol, start, end)
        if not candles:
            return {"error": f"No candle data for {symbol}."}
        closes = [c["close"] for c in candles]
        ind = IndicatorEngine()
        hist = [{"open_price": c["open"], "high_price": c["high"],
                 "low_price": c["low"], "close_price": c["close"]} for c in candles]
        try:
            st = ind.calculate_supertrend(hist, int(st_period), float(st_mult)) or []
        except Exception:
            st = []
        try:
            ef = ind.calculate_ema(closes, int(ema_fast)) or []
        except Exception:
            ef = []
        try:
            es = ind.calculate_ema(closes, int(ema_slow)) or []
        except Exception:
            es = []
        series = {
            "supertrend": [{"time": c["time"], "value": round(st[i], 2)} for i, c in enumerate(candles)
                           if i < len(st) and st[i]],
            "ema_fast": [{"time": c["time"], "value": round(ef[i], 2)} for i, c in enumerate(candles)
                         if i < len(ef) and ef[i]],
            "ema_slow": [{"time": c["time"], "value": round(es[i], 2)} for i, c in enumerate(candles)
                         if i < len(es) and es[i]],
        }
        # ---- trade markers (paper + live, entries/exits/open) ----
        db = Database.get_instance()
        q = """SELECT id, symbol, option_type, strike_price, transaction_type, quantity,
                      lot_size, entry_price, exit_price, entry_date, exit_date, pnl,
                      trade_mode, trade_type, status, exit_status FROM paper_trades
               WHERE symbol=? AND entry_date BETWEEN ? AND ?"""
        params: list = [symbol, start, end]
        if (mode or "all").lower() in ("paper", "live"):
            q += " AND COALESCE(trade_mode,'paper')=?"
            params.append(mode.lower())
        q += " ORDER BY entry_date"
        trades = db.fetch_all(q, params)
        dates = {c["time"] for c in candles}
        first, last = candles[0]["time"], candles[-1]["time"]

        def snap(d: str) -> str:
            d = str(d or "")[:10]
            if d in dates:
                return d
            if not d:
                return last
            # clamp out-of-range dates to nearest candle
            return first if d < first else last

        markers = []
        for t in trades:
            try:
                side = str(t.get("transaction_type", "BUY")).upper()
                is_buy = side == "BUY"
                opt = t.get("option_type", "")
                strike = t.get("strike_price", "")
                ep = float(t.get("entry_price") or 0)
                xp = t.get("exit_price")
                pnl = t.get("pnl")
                tmode = str(t.get("trade_mode") or "paper").upper()
                tag = f"[{tmode}]"
                markers.append({
                    "time": snap(t.get("entry_date")),
                    "position": "belowBar" if is_buy else "aboveBar",
                    "color": "#198754" if is_buy else "#dc3545",
                    "shape": "arrowUp" if is_buy else "arrowDown",
                    "text": f"{tag} {side} {opt} {strike} @ {ep:.2f}",
                })
                if str(t.get("status")) == "closed" and t.get("exit_date"):
                    ptxt = ""
                    try:
                        ptxt = f" ({float(pnl):+.0f})" if pnl is not None else ""
                    except Exception:
                        pass
                    markers.append({
                        "time": snap(t.get("exit_date")),
                        "position": "aboveBar" if is_buy else "belowBar",
                        "color": "#0d6efd",
                        "shape": "circle",
                        "text": f"{tag} EXIT @ {float(xp or 0):.2f}{ptxt}",
                    })
                elif str(t.get("status")) == "open":
                    markers.append({
                        "time": last,
                        "position": "belowBar" if is_buy else "aboveBar",
                        "color": "#ff9800",
                        "shape": "circle",
                        "text": f"{tag} OPEN {opt} {strike}",
                    })
            except Exception:
                continue
        markers.sort(key=lambda m: m["time"])
        return {"success": True, "symbol": symbol, "candles": candles,
                "series": series, "markers": markers,
                "params": {"st_period": st_period, "st_mult": st_mult,
                           "ema_fast": ema_fast, "ema_slow": ema_slow, "mode": mode}}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": f"Chart data error: {str(e)}"}
