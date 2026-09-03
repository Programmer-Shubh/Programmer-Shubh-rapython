import math
import os, json, time as _time

_NSE_LOTS_CACHE = None
_NSE_LOTS_CACHE_TS = 0
_NSE_LOTS_TTL = 24*3600

def _load_nse_lots():
    global _NSE_LOTS_CACHE, _NSE_LOTS_CACHE_TS
    if _NSE_LOTS_CACHE and _time.time() - _NSE_LOTS_CACHE_TS < _NSE_LOTS_TTL:
        return _NSE_LOTS_CACHE
    # Try daily file cache
    for p in [os.path.join(os.path.dirname(__file__), "..", "data", "nse_lots.json"), os.path.join("data","nse_lots.json")]:
        try:
            if os.path.exists(p):
                with open(p,"r") as f: j=json.load(f)
                if j.get("ts",0) > _time.time() - _NSE_LOTS_TTL and j.get("lots"):
                    _NSE_LOTS_CACHE=j["lots"]; _NSE_LOTS_CACHE_TS=j["ts"]; return _NSE_LOTS_CACHE
        except Exception: pass
    # Fetch from NSE
    try:
        import urllib.request as _ur
        _ur_headers={"User-Agent":"Mozilla/5.0"}
        # NSE endpoint for F&O securities
        for url in ["https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O","https://archives.nseindia.com/content/fo/fo_mktlots.csv"]:
            try:
                req=_ur.Request(url, headers=_ur_headers)
                with _ur.urlopen(req, timeout=5) as r:
                    data=r.read().decode()
                    lots={}
                    if "symbol" in data.lower():
                        import csv, io
                        reader=csv.DictReader(io.StringIO(data))
                        for row in reader:
                            sym=(row.get("SYMBOL") or row.get("symbol") or "").strip().upper()
                            ls=row.get("LOT SIZE") or row.get("lotSize") or row.get("marketLot") or ""
                            try: lots[sym]=int(str(ls).strip())
                            except: pass
                    elif data.strip().startswith("{"):
                        j=json.loads(data)
                        for d in j.get("data",[]):
                            sym=str(d.get("symbol","")).upper(); ls=d.get("marketLot") or d.get("lotSize")
                            try: lots[sym]=int(ls)
                            except: pass
                    if lots:
                        _NSE_LOTS_CACHE=lots; _NSE_LOTS_CACHE_TS=_time.time()
                        # persist
                        try:
                            os.makedirs("data", exist_ok=True)
                            with open("data/nse_lots.json","w") as f: json.dump({"ts":_NSE_LOTS_CACHE_TS,"lots":lots},f)
                        except: pass
                        return lots
            except Exception: continue
    except Exception: pass
    return None

def get_lot_size(symbol: str) -> int:
    lots = {
        "NIFTY": 75, "BANKNIFTY": 15, "FINNIFTY": 60, "MIDCPNIFTY": 75,
        "SENSEX": 10, "BANKEX": 20,
        "RELIANCE": 250, "HDFCBANK": 550, "ICICIBANK": 700,
        "TCS": 175, "INFY": 400, "ITC": 1600, "SBIN": 700,
        "AXISBANK": 625, "KOTAKBANK": 400, "LT": 150, "HINDUNILVR": 300,
        "BHARTIARTL": 475, "M&M": 400, "MARUTI": 50, "BAJFINANCE": 125,
        "WIPRO": 1500, "ONGC": 1875, "SUNPHARMA": 400, "ULTRACEMCO": 50,
        "NTPC": 2250, "POWERGRID": 2700, "TATAMOTORS": 1125, "TATASTEEL": 550,
        "HCLTECH": 350, "JSWSTEEL": 675, "COALINDIA": 2700, "DRREDDY": 125,
        "CIPLA": 300, "ADANIENT": 309, "SBILIFE": 450, "BPCL": 1800,
        "GRASIM": 200, "TECHM": 600, "DIVISLAB": 75, "EICHERMOT": 300,
        "BRITANNIA": 140, "HINDALCO": 900, "VEDL": 1650, "INDUSINDBK": 900,
        "SHREECEM": 30, "NESTLEIND": 40, "BAJAJFINSV": 125, "HEROMOTOCO": 300,
        "APOLLOHOSP": 75, "UPL": 1100,
    }
    # Correct exchange lots override (Aug 2026)
    _override={"FINNIFTY":60,"ADANIENT":309,"NIFTY":75}
    if symbol.upper() in _override: return _override[symbol.upper()]
    try:
        live=_load_nse_lots()
        if live and symbol.upper() in live:
            return int(live[symbol.upper()])
    except Exception: pass
    return lots.get(symbol.upper(), 50)


def get_strike_step(symbol: str) -> float:
    steps = {
        "NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 50,
        "SENSEX": 100, "BANKEX": 100,
        # Stocks: use appropriate strike steps based on price range
        "BAJFINANCE": 200, "BAJAJFINSV": 200, "MARUTI": 200, "SHREECEM": 100,
        "NESTLEIND": 100, "LT": 50, "HDFCBANK": 20, "ICICIBANK": 20,
        "RELIANCE": 20, "TCS": 50, "INFY": 20, "ITC": 10, "SBIN": 10,
        "AXISBANK": 10, "KOTAKBANK": 10, "HINDUNILVR": 10, "BHARTIARTL": 20,
        "M&M": 20, "BAJFINANCE": 200, "WIPRO": 10, "ONGC": 10, "SUNPHARMA": 20,
        "ULTRACEMCO": 100, "NTPC": 10, "POWERGRID": 10, "TATAMOTORS": 10,
        "TATASTEEL": 10, "HCLTECH": 20, "JSWSTEEL": 10, "COALINDIA": 10,
        "DRREDDY": 20, "CIPLA": 20, "ADANIENT": 20, "SBILIFE": 10, "BPCL": 10,
        "GRASIM": 20, "TECHM": 20, "DIVISLAB": 20, "EICHERMOT": 20, "BRITANNIA": 20,
        "HINDALCO": 10, "VEDL": 10, "INDUSINDBK": 10, "HEROMOTOCO": 20,
        "APOLLOHOSP": 20, "UPL": 10,
    }
    return steps.get(symbol.upper(), 50)


def format_currency(amount: float) -> str:
    if abs(amount) >= 10000000:
        return f"₹{amount/10000000:.2f}Cr"
    elif abs(amount) >= 100000:
        return f"₹{amount/100000:.2f}L"
    return f"₹{amount:,.2f}"


def normal_cdf(x: float) -> float:
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x = abs(x) / math.sqrt(2)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - ((((a5 * t + a4) * t + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
    return 0.5 * (1.0 + sign * y)


def black_scholes(spot: float, strike: float, time: float, iv: float, option_type: str) -> float:
    if time <= 0:
        time = 0.003
    if iv <= 0:
        iv = 0.14
    if spot <= 0 or strike <= 0:
        return 0
    r = 0.065
    sigma_sqrt_t = iv * math.sqrt(time)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * time) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    if option_type == "CE":
        price = spot * normal_cdf(d1) - strike * math.exp(-r * time) * normal_cdf(d2)
    else:
        price = strike * math.exp(-r * time) * normal_cdf(-d2) - spot * normal_cdf(-d1)
    return max(1.0, round(price, 2))


# ---- Unified option model (single source of truth for ALL symbols) ----
# Entry (order placement), Current (open positions), Scanner suggestion and
# Backtest MUST use the same IV + floor, else every trade instantly shows a
# fake profit/loss. NSE option chain is blocked from cloud IPs (Render), so on
# cloud the model priced off LIVE Yahoo spot is the closest NSE match.
# IVs are NSE-typical: indices ~13-16%, stocks ~30% (high-vol ~35%).
OPTION_MODEL_IV = 0.25
OPTION_MODEL_MIN = 1.5

_SYMBOL_IV = {
    "NIFTY": 0.13, "BANKNIFTY": 0.15, "FINNIFTY": 0.14, "MIDCPNIFTY": 0.16,
    "SENSEX": 0.13, "BANKEX": 0.14,
    "ADANIENT": 0.35, "VEDL": 0.35, "INDUSINDBK": 0.35,
}


def model_iv(symbol: str | None) -> float:
    """Per-symbol model IV: NSE-typical values so ATM premium lands near NSE LTP."""
    if not symbol:
        return OPTION_MODEL_IV
    s = str(symbol).upper().strip()
    if s in _SYMBOL_IV:
        return _SYMBOL_IV[s]
    # Indices default 0.14, stocks default 0.30
    if s in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX",
             "INDIAVIX"):
        return 0.14
    return 0.30


def model_premium(spot: float, strike: float, expiry_days: float, option_type: str, symbol: str | None = None, iv: float | None = None) -> float:
    """Shared model premium: BS(per-symbol NSE-typical IV) with floor 1.5.
    Used by order entry, open-position LTP, scanner suggestion and backtest
    alike. Real DB premiums are used as-is (never inflated) - only model
    values get the floor. Pass iv= to pin a trade's entry IV (old positions
    never reprice when the map is tuned)."""
    try:
        t = float(expiry_days) / 365.0 if expiry_days and float(expiry_days) > 0 else 7 / 365.0
    except Exception:
        t = 7 / 365.0
    use_iv = float(iv) if iv and float(iv) > 0 else model_iv(symbol)
    try:
        bs = black_scholes(float(spot), float(strike), t, use_iv, option_type)
    except Exception:
        bs = 0
    if not bs or bs <= 0:
        return 0
    return max(round(float(bs), 2), OPTION_MODEL_MIN)
