"""
Realistic Indian Options Trading Transaction Costs
Based on NSE fee structure (as of 2024-2025)
"""


def calculate_transaction_costs(option_price, quantity, lot_size, position='Buy'):
    """
    Calculate realistic transaction costs for Indian options trading.
    Returns dict with individual cost components.
    """
    total_qty = quantity * lot_size
    turnover = option_price * total_qty

    # Brokerage: Rs 20 per order or 0.03% (whichever is lower) for discount brokers
    # For options: flat Rs 20 per executed order (Zerodha/Upstox model)
    brokerage = min(20.0, turnover * 0.0003)
    brokerage = max(brokerage, 0.0)

    # STT (Securities Transaction Tax)
    # Options buying: 0.05% on premium
    # Options selling: 0.05% on premium (increased to 0.1% in Budget 2024 for intraday)
    if position == 'Sell':
        stt = turnover * 0.001  # 0.1% on sell side (intraday)
    else:
        stt = turnover * 0.0005  # 0.05% on buy side

    # Exchange Transaction Charges
    # NSE: Rs 3.5 per lakh for options
    exchange_charges = (turnover / 100000) * 3.5

    # SEBI Turnover Fees: Rs 10 per crore
    sebi_charges = (turnover / 10000000) * 10

    # GST: 18% on (Brokerage + Exchange Transaction Charges)
    gst = (brokerage + exchange_charges) * 0.18

    # Stamp Duty (varies by state, using Maharashtra rates)
    # Options buy: 0.003%, Options sell: 0.003%
    stamp_duty = turnover * 0.00003

    total_charges = brokerage + stt + gst + exchange_charges + sebi_charges + stamp_duty

    return {
        'brokerage': round(brokerage, 2),
        'stt': round(stt, 2),
        'gst': round(gst, 2),
        'exchange_charges': round(exchange_charges, 2),
        'sebi_charges': round(sebi_charges, 2),
        'stamp_duty': round(stamp_duty, 2),
        'total_charges': round(total_charges, 2),
    }


def calculate_slippage(price, lot_size, slippage_pct=0.005):
    """
    Calculate realistic slippage.
    Default 0.5% slippage on premium price.
    In real markets, LTP doesn't guarantee execution.
    """
    slippage_per_unit = price * slippage_pct
    return round(slippage_per_unit, 2)


def calculate_total_cost_with_slippage(price, quantity, lot_size, position='Buy'):
    """Calculate total cost including slippage and transaction costs."""
    slippage = calculate_slippage(price, lot_size)

    # Entry slippage (you pay more when buying, receive less when selling)
    if position == 'Buy':
        effective_entry = price + slippage
    else:
        effective_entry = price - slippage

    costs = calculate_transaction_costs(effective_entry, quantity, lot_size, position)

    return {
        'slippage_per_unit': slippage,
        'effective_price': round(effective_entry, 2),
        'costs': costs,
    }
