"""TradingView-chart data feed: spot candles + SuperTrend/EMA overlays +
trade markers (entries/exits/open positions, paper + live modes)."""
from fastapi import APIRouter
from core.models.database import Database
from core.services.indicator_engine import IndicatorEngine

router = APIRouter()


def _st_flip_times(st_series: list) -> dict:
    """Pure helper: SuperTrend bullish-flag series se flip points.
    Returns {"flip_up": [times], "flip_down": [times]} — buy/sell teer ke liye."""
    up, down = [], []
    try:
        prev = None
        for p in st_series or []:
            try:
                cur = bool(p.get("bullish"))
                if prev is not None and cur != prev:
                    (up if cur else down).append(p.get("time"))
                prev = cur
            except Exception:
                continue
    except Exception:
        pass
    return {"flip_up": up, "flip_down": down}


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
               ema_fast: int = 9, ema_slow: int = 21,
               zl_length: int = 70, zl_mult: float = 1.2,
               start_date: str = "", end_date: str = ""):
    try:
        # Frontend sends start_date/end_date, old callers send start/end — accept both
        if start_date:
            start = start_date
        if end_date:
            end = end_date
        symbol = (symbol or "NIFTY").upper()
        try:
            zl_length = max(5, int(zl_length or 70))
        except Exception:
            zl_length = 70
        try:
            zl_mult = float(zl_mult or 1.2)
            if zl_mult <= 0:
                zl_mult = 1.2
        except Exception:
            zl_mult = 1.2
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
        # Build SuperTrend color per candle: green when price above ST (bullish), red below
        st_series = []
        for i, c in enumerate(candles):
            if i < len(st) and st[i]:
                sv = round(st[i], 2)
                bullish = c["close"] > sv
                st_series.append({"time": c["time"], "value": sv, "bullish": bullish})
        # Split into green/red segments for TradingView (single series can't color per point)
        st_bull = [{"time": p["time"], "value": p["value"]} for p in st_series if p["bullish"]]
        st_bear = [{"time": p["time"], "value": p["value"]} for p in st_series if not p["bullish"]]
        _flips = _st_flip_times(st_series)
        series = {
            "supertrend": [{"time": c["time"], "value": round(st[i], 2)} for i, c in enumerate(candles)
                           if i < len(st) and st[i]],
            "supertrend_bull": st_bull,
            "supertrend_bear": st_bear,
            "st_flip_up": _flips["flip_up"],
            "st_flip_down": _flips["flip_down"],
            "ema_fast": [{"time": c["time"], "value": round(ef[i], 2)} for i, c in enumerate(candles)
                         if i < len(ef) and ef[i]],
            "ema_slow": [{"time": c["time"], "value": round(es[i], 2)} for i, c in enumerate(candles)
                         if i < len(es) and es[i]],
        }
        # ---- Zero-Lag Trend (AlgoAlpha port) on chart timeframe ----
        zerolag = {"zlema_bull": [], "zlema_bear": [], "upper": [], "lower": [],
                   "buy": [], "sell": [], "flip_up": [], "flip_down": [],
                   "trend": None, "length": zl_length, "mult": zl_mult}
        try:
            zl = ind.calculate_zero_lag_trend(hist, closes, zl_length, zl_mult) or {}
            _z = zl.get("zlema", []) or []
            _tr = zl.get("trend", []) or []
            for i, c in enumerate(candles):
                try:
                    if i >= len(_z) or _z[i] is None:
                        continue
                    _t = _tr[i] if i < len(_tr) else 0
                    _pt = {"time": c["time"], "value": round(float(_z[i]), 2)}
                    if _t == 1:
                        zerolag["zlema_bull"].append(_pt)
                    elif _t == -1:
                        zerolag["zlema_bear"].append(_pt)
                    _up = (zl.get("upper", []) or [])
                    _lo = (zl.get("lower", []) or [])
                    if i < len(_up) and _up[i] is not None:
                        zerolag["upper"].append({"time": c["time"], "value": round(float(_up[i]), 2)})
                    if i < len(_lo) and _lo[i] is not None:
                        zerolag["lower"].append({"time": c["time"], "value": round(float(_lo[i]), 2)})
                    _buys = zl.get("buy", []) or []
                    _sells = zl.get("sell", []) or []
                    if i < len(_buys) and _buys[i]:
                        zerolag["buy"].append(c["time"])
                    if i < len(_sells) and _sells[i]:
                        zerolag["sell"].append(c["time"])
                    _fu = zl.get("flip_up", []) or []
                    _fd = zl.get("flip_down", []) or []
                    if i < len(_fu) and _fu[i]:
                        zerolag["flip_up"].append(c["time"])
                    if i < len(_fd) and _fd[i]:
                        zerolag["flip_down"].append(c["time"])
                except Exception:
                    continue
            try:
                zerolag["trend"] = "Bullish" if _tr and _tr[-1] == 1 else ("Bearish" if _tr and _tr[-1] == -1 else "Flat")
            except Exception:
                pass
        except Exception:
            pass
        series["zerolag"] = zerolag
        # ---- MTF trend table (5m/15m/1H/4H/1D, real bars only) ----
        mtf = []
        try:
            from core.services.free_data import fetch_yahoo_intraday as _fy
            _tf_defs = [("5m", "5m", 3), ("15m", "15m", 8)]
            _intra = {}
            for _tf_name, _iv, _days in _tf_defs:
                try:
                    _bars = _fy(symbol, _iv, days=_days) or []
                    # forming bar hatao (closed only, no repaint)
                    if _bars and len(_bars) > 1:
                        _bars = _bars[:-1]
                    _intra[_tf_name] = _bars
                except Exception:
                    _intra[_tf_name] = []
            _tf_targets = [("5m", 5), ("15m", 15), ("1H", 60), ("4H", 240), ("1D", 1440)]
            for _label, _mins in _tf_targets:
                try:
                    if _mins <= 15:
                        _src = list(_intra.get(_label, []) or [])
                    else:
                        _src = ind.resample_ohlc(list(_intra.get("15m", []) or []), _mins)
                    _note_src = "live intraday"
                    if (not _src or len(_src) < zl_length + 2) and _mins >= 1440:
                        # Fallback: Yahoo blocked/empty ho to chart ke daily candles
                        # se 1D trend — Pine Timeframe-5 parity, kabhi blank nahi
                        if len(hist) >= zl_length + 2:
                            _src = list(hist)
                            _note_src = "daily chart"
                    if not _src or len(_src) < zl_length + 2:
                        mtf.append({"tf": _label, "signal": "n/a",
                                    "note": f"sirf {len(_src or [])} bars (70+ chahiye)"})
                        continue
                    _cc = [float(b.get("close_price", 0) or 0) for b in _src]
                    _rr = ind.calculate_zero_lag_trend(_src, _cc, zl_length, zl_mult) or {}
                    _tt = _rr.get("trend", []) or []
                    _sig = "Bullish" if _tt and _tt[-1] == 1 else ("Bearish" if _tt and _tt[-1] == -1 else "Flat")
                    mtf.append({"tf": _label, "signal": _sig,
                                "note": f"{len(_src)} closed bars ({_note_src})"})
                except Exception as _me:
                    mtf.append({"tf": _label, "signal": "n/a", "note": str(_me)[:60]})
        except Exception:
            pass
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
                    "size": 3,
                    "text": f"{tag} {'\u25b2 BUY' if is_buy else '\u25bc SELL'} {opt} {strike} @ {ep:.2f}",
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
                        "size": 1,
                        "text": f"{tag} EXIT @ {float(xp or 0):.2f}{ptxt}",
                    })
                elif str(t.get("status")) == "open":
                    markers.append({
                        "time": last,
                        "position": "belowBar" if is_buy else "aboveBar",
                        "color": "#ff9800",
                        "shape": "circle",
                        "size": 1,
                        "text": f"{tag} OPEN {opt} {strike}",
                    })
            except Exception:
                continue
        markers.sort(key=lambda m: m["time"])
        return {"success": True, "symbol": symbol, "candles": candles,
                "series": series, "markers": markers, "mtf": mtf,
                "params": {"st_period": st_period, "st_mult": st_mult,
                           "ema_fast": ema_fast, "ema_slow": ema_slow, "mode": mode,
                           "zl_length": zl_length, "zl_mult": zl_mult}}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": f"Chart data error: {str(e)}"}
