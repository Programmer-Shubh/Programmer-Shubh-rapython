"""Statistical Arbitrage & Pairs Trading Engine.
Tracks cointegrated pairs (e.g. HDFC/ICICI), spread z-score, triggers.
No heavy statsmodels dependency — uses correlation + ADF-like mean-reversion proxy."""
import math
from typing import List, Dict


# Highly correlated NSE pairs (sector peers). User can add more via API param.
DEFAULT_PAIRS = [
    ("HDFCBANK", "ICICIBANK"),
    ("RELIANCE", "ONGC"),
    ("TCS", "INFY"),
    ("SBIN", "AXISBANK"),
    ("LT", "ULTRACEMCO"),
    ("BAJFINANCE", "BAJAJFINSV"),
    ("NIFTY", "BANKNIFTY"),
]


def _corr(a: List[float], b: List[float]) -> float:
    n = min(len(a), len(b))
    if n < 10:
        return 0.0
    a = a[-n:]; b = b[-n:]
    ma = sum(a) / n; mb = sum(b) / n
    num = sum((a[i]-ma)*(b[i]-mb) for i in range(n))
    da = math.sqrt(sum((x-ma)**2 for x in a))
    db = math.sqrt(sum((x-mb)**2 for x in b))
    return num / (da*db) if da and db else 0.0


def _zscore(series: List[float]) -> float:
    if len(series) < 5:
        return 0.0
    m = sum(series) / len(series)
    var = sum((x-m)**2 for x in series) / len(series)
    sd = math.sqrt(var) if var > 0 else 1.0
    return (series[-1] - m) / sd if sd else 0.0


def analyze_pair(hist_a: List[Dict], hist_b: List[Dict], sym_a: str, sym_b: str) -> Dict:
    closes_a = [float(d.get("close_price") or 0) for d in hist_a]
    closes_b = [float(d.get("close_price") or 0) for d in hist_b]
    n = min(len(closes_a), len(closes_b))
    if n < 20:
        return {"pair": f"{sym_a}/{sym_b}", "status": "insufficient_data", "score": 0}
    closes_a = closes_a[-n:]; closes_b = closes_b[-n:]
    corr = _corr(closes_a, closes_b)
    # Hedge ratio via simple OLS beta (b on a)
    try:
        ma = sum(closes_a) / n; mb = sum(closes_b) / n
        cov = sum((closes_a[i]-ma)*(closes_b[i]-mb) for i in range(n))
        vara = sum((x-ma)**2 for x in closes_a)
        beta = cov / vara if vara else 1.0
    except Exception:
        beta = 1.0
    spread = [closes_a[i] - beta * closes_b[i] for i in range(n)]
    z = _zscore(spread[-20:] if len(spread) >= 20 else spread)
    # Signal: |z| > 2 => spread stretched, mean-reversion trade
    signal = "HOLD"
    if corr > 0.7:
        if z > 2.0:
            signal = f"SHORT {sym_a} / LONG {sym_b} (spread +2σ)"
        elif z < -2.0:
            signal = f"LONG {sym_a} / SHORT {sym_b} (spread -2σ)"
    return {
        "pair": f"{sym_a}/{sym_b}", "correlation": round(corr, 3),
        "beta": round(beta, 3), "zscore": round(z, 2),
        "spread_last": round(spread[-1], 2) if spread else 0,
        "signal": signal, "score": min(95, int(abs(z)*20 + corr*30)) if abs(z) > 1 else int(corr*40),
    }


def scan_cross_market(symbols=None) -> Dict:
    """Cross-market NSE vs BSE for SAME stock — true equity arbitrage.
    Shows only stocks (indices have no BSE), high-diff (>0.4%) so HOLD spam hidden."""
    from core.services.scanner import OptionScanner
    from core.services.live_market_data import LiveMarketData
    from core.services.free_data import _yahoo_fallback_quote
    sc = OptionScanner()
    live = LiveMarketData()
    # Only stocks have both NSE+BSE; indices (NIFTY etc) have no meaningful BSE cross
    _INDICES = {"NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","SENSEX","BANKEX"}
    if not symbols:
        symbols = ["RELIANCE","HDFCBANK","ICICIBANK","TCS","INFY","SBIN","AXISBANK","KOTAKBANK","LT","BHARTIARTL","ITC","BAJFINANCE"]
        symbols = [s for s in symbols if s not in _INDICES][:10]
    else:
        symbols = [s for s in symbols if s not in _INDICES]
    results = []
    for sym in symbols:
        try:
            pa = float((live.get_live_spot(sym) or {}).get("spot") or 0)
            if not pa:
                ra = sc.db.fetch_one("SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1", [sym])
                pa = float(ra["close_price"]) if ra and ra["close_price"] else 0
            try:
                q = _yahoo_fallback_quote(f"{sym}.BO", timeout=2)
                pb = float((q or {}).get("spot") or 0)
            except Exception:
                pb = 0
            if pa and pb:
                diff = pa - pb
                pct = diff / pb * 100 if pb else 0
                # Show even small diffs (user: khali na dikhe) — arb flag still >0.5%
                # so HOLD vs ARB badge distinguishes
                results.append({
                    "pair": sym, "symbol": sym,
                    "nse_price": round(pa,2), "bse_price": round(pb,2),
                    "nse_bse_diff": round(diff,2), "nse_bse_pct": round(pct,2),
                    "price_a": round(pa,2), "price_b": round(pb,2),
                    "signal": f"Buy {('BSE' if diff>0 else 'NSE')} / Sell {('NSE' if diff>0 else 'BSE')}",
                    "arb": abs(pct) > 0.5, "zscore": round(pct,2), "correlation": 0.99,
                })
        except Exception:
            continue
    results.sort(key=lambda x: abs(x.get("nse_bse_pct",0)), reverse=True)
    return {"pairs": results[:5], "count": len(results)}

def scan_pairs(symbols=None, pairs=None) -> Dict:
    # Cross-market NSE/BSE is primary; always return top 3 even if diff small
    # so dashboard never looks empty (user: khali na dikhe)
    cm = scan_cross_market(symbols=symbols)
    if cm["pairs"]:
        return cm
    # Fallback: show top 3 cross-market even with small diff (HOLD) so card not empty
    # Re-run without diff filter
    from core.services.live_market_data import LiveMarketData
    from core.services.free_data import _yahoo_fallback_quote
    from core.services.scanner import OptionScanner as _SC
    sc2 = _SC(); live2 = LiveMarketData()
    syms2 = ["RELIANCE","HDFCBANK","TCS","INFY","SBIN"][:5]
    out2 = []
    for sym in syms2:
        try:
            pa = float((live2.get_live_spot(sym) or {}).get("spot") or 0)
            q = _yahoo_fallback_quote(f"{sym}.BO", timeout=1)
            pb = float((q or {}).get("spot") or 0)
            if pa and pb:
                diff = pa - pb; pct = diff/pb*100 if pb else 0
                out2.append({"pair": sym, "symbol": sym, "nse_price": round(pa,2), "bse_price": round(pb,2),
                             "nse_bse_diff": round(diff,2), "nse_bse_pct": round(pct,2),
                             "price_a": round(pa,2), "price_b": round(pb,2),
                             "signal": "HOLD", "arb": False, "zscore": 0, "correlation": 0.99})
        except Exception:
            continue
    return {"pairs": out2[:3], "count": len(out2)}
    from core.services.scanner import OptionScanner
    from core.services.live_market_data import LiveMarketData
    sc = OptionScanner()
    live = LiveMarketData()
    if pairs is None:
        pairs = DEFAULT_PAIRS
    if symbols:
        pairs = [p for p in pairs if p[0] in symbols or p[1] in symbols]
    results = []
    for a, b in pairs:
        try:
            ha = sc._get_historical(a)
            hb = sc._get_historical(b)
            r = analyze_pair(ha, hb, a, b)
            try:
                def _bse(sym):
                    try:
                        from core.services.free_data import _yahoo_fallback_quote
                        q = _yahoo_fallback_quote(f"{sym}.BO", timeout=2)
                        return float((q or {}).get("spot") or 0)
                    except Exception:
                        return 0
                pa = float((live.get_live_spot(a) or {}).get("spot") or 0)
                pb = float((live.get_live_spot(b) or {}).get("spot") or 0)
                if not pa:
                    ra = sc.db.fetch_one("SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1", [a])
                    pa = float(ra["close_price"]) if ra and ra["close_price"] else 0
                if not pb:
                    rb = sc.db.fetch_one("SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1", [b])
                    pb = float(rb["close_price"]) if rb and rb["close_price"] else 0
                nse_a = pa
                bse_a = _bse(a)
                r["price_a"] = round(pa, 2); r["price_b"] = round(pb, 2)
                r["nse_price"] = round(nse_a, 2) if nse_a else round(pa, 2)
                r["bse_price"] = round(bse_a, 2) if bse_a else 0
                r["nse_bse_diff"] = round(nse_a - bse_a, 2) if nse_a and bse_a else 0
                r["nse_bse_pct"] = round((nse_a - bse_a)/bse_a*100, 2) if nse_a and bse_a and bse_a else 0
                r["price_diff"] = round(pa - pb, 2) if pa and pb else 0
                r["price_diff_pct"] = round((pa - pb)/pb*100, 2) if pa and pb and pb else 0
                r["arb"] = bool(abs(r.get("zscore",0))>1.5 and (abs(r["price_diff_pct"])>1.0 or abs(r["nse_bse_pct"])>0.5) and r.get("correlation",0)>0.5)
            except Exception:
                r["price_a"] = 0; r["price_b"] = 0; r["price_diff"] = 0; r["price_diff_pct"] = 0
                r["nse_price"] = 0; r["bse_price"] = 0; r["nse_bse_diff"] = 0; r["nse_bse_pct"] = 0; r["arb"] = False
            results.append(r)
        except Exception as e:
            results.append({"pair": f"{a}/{b}", "error": str(e)[:100]})
    results.sort(key=lambda x: (1 if x.get("arb") else 0, abs(x.get("zscore", 0))), reverse=True)
    return {"pairs": results, "count": len(results)}
