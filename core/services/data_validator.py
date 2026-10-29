"""Data source priority + validation (user-spec, strictly enforced).

Priority (primary truth first):
  1. NSE/BSE official - nsefin bhavcopy + nsepython + local DB archive
  2. Yahoo Finance (ONLY last-60-day window) + Stooq (keyless secondary)
  3. Alpha Vantage / Twelve Data - API key (env) hone par fallback
  4. openchart - reference ONLY (bars ke liye istemal nahi)

Validation statuses:
  VALID          - primary source se bars, ya dono sources agree
  MISMATCH       - price/date/volume mismatch (overlap me >20% dates 1.5% se zyada alag)
  SECONDARY_ONLY - sirf Yahoo/secondary uplabdh (auto-validated NAHI)
  INSUFFICIENT   - primary missing aur koi secondary bhi nahi
  SYNTHETIC      - model-generated (market data nahi) - sirf last-resort fallback

Rules:
  - Missing trading days ko interpolate/fill NAHI karte (koi merge-over nahi).
  - Yahoo 60-day se purana data ignore.
"""
import datetime as _dt
import os as _os

YAHOO_MAX_AGE_DAYS = 60
CLOSE_TOL = 0.015
MISMATCH_FRAC = 0.20

SOURCE_RANK = {
    "nse": 1, "bse": 1, "nsefin": 1, "db": 1, "nse_official_db": 1,
    "nsepython": 1,
    "yahoo": 2, "stooq": 2,
    "alphavantage": 3, "twelvedata": 3,
    "openchart": 4, "synthetic": 9,
}


def _today():
    try:
        return (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).date()
    except Exception:
        return _dt.date.today()


def yahoo_60d_filter(bars):
    """Yahoo bars older than 60 days -> ignore (spec rule)."""
    try:
        cutoff = _today() - _dt.timedelta(days=YAHOO_MAX_AGE_DAYS)
        out = [b for b in (bars or [])
               if str(b.get("trade_date", ""))[:10] >= cutoff.strftime("%Y-%m-%d")]
        return out
    except Exception:
        return bars or []


def _fetch_alphavantage_daily(symbol, start_date, end_date, timeout=10):
    try:
        import requests
        key = (_os.environ.get("ALPHAVANTAGE_API_KEY") or "").strip()
        if not key:
            return []
        r = requests.get("https://www.alphavantage.co/query", params={
            "function": "TIME_SERIES_DAILY", "symbol": symbol,
            "outputsize": "full", "apikey": key}, timeout=timeout)
        if r.status_code != 200:
            return []
        series = (r.json().get("Time Series (Daily)") or {})
        out = []
        for d in sorted(series):
            td = str(d)[:10]
            if td < start_date[:10] or td > end_date[:10]:
                continue
            v = series[d] or {}
            try:
                cl = float(v.get("4. close", 0) or 0)
            except Exception:
                continue
            if cl <= 0:
                continue
            out.append({"symbol": symbol.upper(), "trade_date": td,
                        "open_price": float(v.get("1. open", cl) or cl),
                        "high_price": float(v.get("2. high", cl) or cl),
                        "low_price": float(v.get("3. low", cl) or cl),
                        "close_price": round(cl, 2),
                        "volume": int(float(v.get("5. volume", 0) or 0)), "oi": 0})
        return out if len(out) >= 5 else []
    except Exception:
        return []


def _fetch_twelvedata_daily(symbol, start_date, end_date, timeout=10):
    try:
        import requests
        key = (_os.environ.get("TWELVEDATA_API_KEY") or "").strip()
        if not key:
            return []
        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": f"{symbol}/NSE", "interval": "1day",
            "start_date": start_date[:10], "end_date": end_date[:10],
            "apikey": key}, timeout=timeout)
        if r.status_code != 200:
            return []
        vals = (r.json().get("values") or [])
        out = []
        for v in vals:
            td = str(v.get("datetime", ""))[:10]
            if td < start_date[:10] or td > end_date[:10]:
                continue
            try:
                cl = float(v.get("close", 0) or 0)
            except Exception:
                continue
            if cl <= 0:
                continue
            out.append({"symbol": symbol.upper(), "trade_date": td,
                        "open_price": float(v.get("open", cl) or cl),
                        "high_price": float(v.get("high", cl) or cl),
                        "low_price": float(v.get("low", cl) or cl),
                        "close_price": round(cl, 2),
                        "volume": int(float(v.get("volume", 0) or 0)), "oi": 0})
        out.sort(key=lambda r: r["trade_date"])
        return out if len(out) >= 5 else []
    except Exception:
        return []


def compare_sources(primary, secondary, tol=CLOSE_TOL):
    """Overlapping dates par close compare. Returns (agree: bool, detail: str)."""
    try:
        pm = {str(b.get("trade_date", ""))[:10]: float(b.get("close_price", 0) or 0)
              for b in (primary or [])}
        n, bad = 0, 0
        for b in (secondary or []):
            d = str(b.get("trade_date", ""))[:10]
            if d not in pm or pm[d] <= 0:
                continue
            try:
                c = float(b.get("close_price", 0) or 0)
            except Exception:
                continue
            if c <= 0:
                continue
            n += 1
            if abs(c - pm[d]) / pm[d] > tol:
                bad += 1
        if n < 5:
            return False, f"overlap sirf {n} dates (min 5)"
        if bad / max(n, 1) > MISMATCH_FRAC:
            return False, f"{bad}/{n} dates mismatch (>1.5%)"
        return True, f"{n - bad}/{n} dates agree"
    except Exception as e:
        return False, f"compare fail: {e}"[:100]


def validate_symbol(primary_bars, primary_source, secondary_bars=None,
                     secondary_source=""):
    """Spec verdict for one symbol. Returns quality dict."""
    try:
        if primary_bars and len(primary_bars) >= 5:
            rank = SOURCE_RANK.get(str(primary_source or "").lower(), 5)
            if rank <= 1:
                return {"status": "VALID", "source": primary_source,
                        "bars": len(primary_bars),
                        "note": "NSE/BSE official primary truth"}
            if secondary_bars and len(secondary_bars) >= 5:
                ok, detail = compare_sources(primary_bars, secondary_bars)
                if ok:
                    return {"status": "VALID", "source": primary_source,
                            "bars": len(primary_bars),
                            "note": f"dono sources agree ({detail})"}
                return {"status": "MISMATCH", "source": primary_source,
                        "bars": len(primary_bars), "note": detail}
            if str(primary_source or "").lower() == "yahoo":
                return {"status": "SECONDARY_ONLY", "source": "yahoo",
                        "bars": len(primary_bars),
                        "note": "sirf Yahoo - auto-validated nahi"}
            return {"status": "SECONDARY_ONLY", "source": primary_source,
                    "bars": len(primary_bars),
                    "note": "secondary source only - auto-validated nahi"}
        if secondary_bars and len(secondary_bars) >= 5:
            return {"status": "SECONDARY_ONLY", "source": secondary_source,
                    "bars": len(secondary_bars),
                    "note": "primary missing - secondary only, auto-validated nahi"}
        return {"status": "INSUFFICIENT", "source": primary_source or "none",
                "bars": 0, "note": "primary missing, koi secondary bhi nahi"}
    except Exception as e:
        return {"status": "INSUFFICIENT", "source": "none", "bars": 0,
                "note": f"validate fail: {e}"[:120]}
