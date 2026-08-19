import math


def get_lot_size(symbol: str) -> int:
    lots = {
        "NIFTY": 50, "BANKNIFTY": 15, "FINNIFTY": 40, "MIDCPNIFTY": 75,
        "SENSEX": 20, "BANKEX": 30,
        "RELIANCE": 250, "HDFCBANK": 550, "ICICIBANK": 700,
        "TCS": 175, "INFY": 400, "ITC": 1600, "SBIN": 700,
    }
    return lots.get(symbol.upper(), 50)


def get_strike_step(symbol: str) -> float:
    steps = {
        "NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 50,
        "SENSEX": 100, "BANKEX": 100,
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
