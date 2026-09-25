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


def scan_pairs(symbols=None, pairs=None) -> Dict:
    from core.services.scanner import OptionScanner
    sc = OptionScanner()
    if pairs is None:
        pairs = DEFAULT_PAIRS
    if symbols:
        # filter pairs to requested symbols
        pairs = [p for p in pairs if p[0] in symbols or p[1] in symbols]
    results = []
    for a, b in pairs:
        try:
            ha = sc._get_historical(a)
            hb = sc._get_historical(b)
            r = analyze_pair(ha, hb, a, b)
            results.append(r)
        except Exception as e:
            results.append({"pair": f"{a}/{b}", "error": str(e)[:100]})
    results.sort(key=lambda x: abs(x.get("zscore", 0)), reverse=True)
    return {"pairs": results, "count": len(results)}
