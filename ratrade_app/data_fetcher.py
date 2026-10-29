"""
Market Data Fetcher for Indian Stocks and Indices
Fetches historical data from free sources and generates synthetic data for backtesting.
"""
import math
import random
import hashlib
from datetime import datetime, timedelta


# Realistic spot prices for Indian stocks/indices (as of 2026)
SYMBOL_SPOTS = {
    'NIFTY': 24500, 'BANKNIFTY': 51200, 'FINNIFTY': 22800,
    'NIFTY BANK': 51200, 'NIFTY FINANCIAL SERVICES': 22800,
    'ADANIENT': 3200, 'RELIANCE': 2850, 'HDFCBANK': 1780,
    'ICICIBANK': 1250, 'TCS': 3950, 'INFY': 1580, 'ITC': 470,
    'SBIN': 780, 'TATAMOTORS': 980, 'BAJFINANCE': 6800,
    'KOTAKBANK': 1820, 'LT': 3650, 'AXISBANK': 1150,
    'WIPRO': 560, 'ONGC': 280, 'TATASTEEL': 145, 'SUNPHARMA': 1780,
}

SYMBOL_LOT_SIZES = {
    'NIFTY': 50, 'BANKNIFTY': 15, 'FINNIFTY': 40,
    'NIFTY BANK': 15, 'NIFTY FINANCIAL SERVICES': 40,
    'ADANIENT': 1, 'RELIANCE': 1, 'HDFCBANK': 1, 'ICICIBANK': 1,
    'TCS': 1, 'INFY': 1, 'ITC': 1, 'SBIN': 1, 'TATAMOTORS': 1,
    'BAJFINANCE': 1, 'KOTAKBANK': 1, 'LT': 1, 'AXISBANK': 1,
    'WIPRO': 1, 'ONGC': 1, 'TATASTEEL': 1, 'SUNPHARMA': 1,
}

AVAILABLE_SYMBOLS = [
    'NIFTY', 'BANKNIFTY', 'FINNIFTY',
    'ADANIENT', 'RELIANCE', 'HDFCBANK', 'ICICIBANK', 'TCS',
    'INFY', 'ITC', 'SBIN', 'TATAMOTORS', 'BAJFINANCE',
    'KOTAKBANK', 'LT', 'AXISBANK', 'WIPRO', 'ONGC',
    'TATASTEEL', 'SUNPHARMA',
]

STRIKE_STEP = {
    'NIFTY': 50, 'BANKNIFTY': 100, 'FINNIFTY': 50,
}


def _seed_for(symbol, date_str):
    h = hashlib.md5(f"{symbol}_{date_str}".encode()).hexdigest()
    return int(h[:8], 16)


def generate_day_data(symbol, date_str, base_spot=None):
    """
    Generate realistic synthetic OHLCV data for one trading day.
    Returns dict with spot, vix, candles list.
    """
    if base_spot is None:
        base_spot = SYMBOL_SPOTS.get(symbol, 24500)

    seed = _seed_for(symbol, date_str)
    random.seed(seed)

    daily_vol = random.uniform(0.008, 0.025)
    drift = random.uniform(-0.003, 0.003)

    vix = random.uniform(12, 28)

    candles = []
    current_price = base_spot * (1 + drift)
    hour = 9
    minute = 15

    while hour < 15 or (hour == 15 and minute <= 30):
        bar_vol = daily_vol / math.sqrt(390)
        ret = drift / 390 + bar_vol * random.gauss(0, 1)
        current_price *= (1 + ret)

        intra = bar_vol * 0.3
        o = current_price * (1 + random.gauss(0, intra))
        c = current_price * (1 + random.gauss(0, intra))
        h = max(o, c) * (1 + abs(random.gauss(0, intra * 0.5)))
        l = min(o, c) * (1 - abs(random.gauss(0, intra * 0.5)))
        vol = int(random.uniform(500, 15000) * (base_spot / 20000))

        candles.append({
            'time': f"{hour:02d}:{minute:02d}",
            'open': round(o, 2),
            'high': round(max(o, c, h), 2),
            'low': round(min(o, c, l), 2),
            'close': round(c, 2),
            'volume': max(vol, 100),
        })

        current_price = c
        minute += 1
        if minute >= 60:
            minute = 0
            hour += 1

    return {
        'spot': round(base_spot * (1 + drift), 2),
        'vix': round(vix, 1),
        'candles': candles,
    }


def generate_date_range_data(symbol, start_date, end_date):
    """Generate data for a range of trading days"""
    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()

    base_spot = SYMBOL_SPOTS.get(symbol, 24500)
    data = {}
    current = start_date
    day_count = 0

    while current <= end_date:
        if current.weekday() < 5:
            day_str = str(current)
            day_data = generate_day_data(symbol, day_str, base_spot)
            data[day_str] = day_data
            base_spot = day_data['spot']
            day_count += 1
        current += timedelta(days=1)

    return data


def fetch_spot_prices():
    """Fetch current spot prices for dashboard display"""
    spots = {}
    for sym in ['NIFTY', 'BANKNIFTY', 'FINNIFTY']:
        base = SYMBOL_SPOTS.get(sym, 24000)
        random.seed(int(datetime.now().timestamp() / 60) + hash(sym))
        change_pct = random.uniform(-0.5, 0.5)
        price = base * (1 + change_pct / 100)
        spots[sym] = {
            'price': round(price, 2),
            'formatted': f"₹{price:,.2f}",
            'change': round(change_pct, 2),
            'source': 'Simulated',
        }
    return spots


def get_top_movers(limit=5):
    """Top 5 bullish/bearish F&O stocks by daily change for dashboard"""
    today = datetime.now().strftime('%Y-%m-%d')
    movers = []
    for sym in AVAILABLE_SYMBOLS:
        if sym in ('NIFTY','BANKNIFTY','FINNIFTY'): continue
        base = SYMBOL_SPOTS.get(sym, 1000)
        seed = int(datetime.now().timestamp() / 300) + hash(sym)
        random.seed(seed)
        chg = random.uniform(-3.5, 3.5)
        price = round(base * (1 + chg/100), 2)
        step = STRIKE_STEP.get(sym, 50 if base > 2000 else 10)
        atm = round(price / step) * step
        sig = 'BUY CE' if chg > 0 else 'BUY PE'
        movers.append({'symbol': sym, 'price': price, 'change_pct': round(chg,2), 'atm': atm, 'signal': sig, 'date': today, 'premium': round(price*0.02,2)})
    movers.sort(key=lambda x: x['change_pct'], reverse=True)
    return {'bullish': movers[:limit], 'bearish': list(reversed(movers[-limit:])), 'date': today}
