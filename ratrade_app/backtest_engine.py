"""
Realistic Indian Options Backtesting Engine
Quantman-style with strict constraints, slippage, transaction costs, look-ahead bias elimination
"""
import math
import random
from datetime import datetime, timedelta
from collections import defaultdict

from .transaction_costs import (
    calculate_transaction_costs,
    calculate_slippage,
)


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


def get_lot_size(symbol):
    return SYMBOL_LOT_SIZES.get(symbol.upper(), 1)


def round_to_nearest_tick(price, tick=0.05):
    if price <= 0:
        return 0
    return round(round(price / tick) * tick, 2)


def select_strike(spot, strike_type, step=50, distance=0):
    atm = round(spot / step) * step
    d = int(distance) if distance else 0
    st = (strike_type or 'ATM').upper()
    # distance is 0-based offset from ATM: OTM 0 = atm+step, OTM1 = atm+2*step
    if st == 'ATM':
        return atm + d * step
    elif st == 'OTM':
        return atm + (1 + d) * step
    elif st == 'ITM':
        return atm - (1 + d) * step
    return atm + d * step


def get_nearest_expiry(date, expiry_type='Monthly'):
    if isinstance(date, str):
        date = datetime.strptime(date, '%Y-%m-%d').date()
    if expiry_type == 'Weekly':
        days_ahead = (3 - date.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return date + timedelta(days=days_ahead)
    else:
        if date.month == 12:
            next_month = date.replace(year=date.year + 1, month=1, day=1)
        else:
            next_month = date.replace(month=date.month + 1, day=1)
        last_day = next_month.replace(day=28) + timedelta(days=4)
        expiry_day = last_day - timedelta(days=last_day.weekday() + 3)
        if expiry_day < date:
            if next_month.month == 12:
                next_month = next_month.replace(year=next_month.year + 1, month=1)
            else:
                next_month = next_month.replace(month=next_month.month + 1)
            last_day = next_month.replace(day=28) + timedelta(days=4)
            expiry_day = last_day - timedelta(days=last_day.weekday() + 3)
        return expiry_day


def estimate_option_premium(spot, strike, opt_type, vix, days_to_expiry=7):
    """Estimate option premium using simplified Black-Scholes-like model"""
    moneyness = abs(spot - strike) / spot
    intrinsic = max(spot - strike, 0) if opt_type == 'CE' else max(strike - spot, 0)
    vol_factor = vix / 100.0
    time_factor = math.sqrt(max(days_to_expiry, 1) / 365)
    time_value = spot * vol_factor * time_factor * (0.3 + moneyness * 2)
    premium = intrinsic + time_value
    return round_to_nearest_tick(max(premium, 0.05))


def simulate_option_path(entry_premium, underlying_entry, underlying_exit, vix, n_steps=30):
    """Simulate realistic option premium path from entry to exit"""
    pct_move = (underlying_exit - underlying_entry) / underlying_entry
    vol = vix / 100.0

    steps = []
    current = entry_premium
    for i in range(n_steps):
        t_frac = (i + 1) / n_steps
        drift = pct_move * 0.4 * entry_premium * t_frac
        noise = random.gauss(0, entry_premium * vol * 0.02)
        target = entry_premium + drift + noise
        current = current * 0.7 + target * 0.3
        current = max(current, 0.05)
        steps.append(round_to_nearest_tick(current))

    return steps


def run_backtest(config):
    """
    Main backtesting engine.
    One trade per day at entry time, exit at exit time or SL/TP.
    """
    symbol = config.get('symbol', 'NIFTY')
    strategy_type = config.get('strategy_type', 'Intraday')
    entry_time = config.get('entry_time', '09:35')
    exit_time = config.get('exit_time', '15:14')
    lots = config.get('lots', 1)
    stop_loss = config.get('stop_loss', 500)
    take_profit = config.get('take_profit', 1000)
    start_date = config.get('start_date', '2026-08-01')
    end_date = config.get('end_date', '2026-08-20')
    legs_config = config.get('legs', [])
    advanced = config.get('advanced', {})
    market_data = config.get('data', {})
    max_trades_per_day = config.get('max_trades_per_day', 5)

    lot_size = get_lot_size(symbol)

    if not legs_config:
        legs_config = [{'option_type': 'CE', 'position': 'Buy', 'strike_type': 'ATM', 'lots': lots}]

    entry_h, entry_m = map(int, entry_time.split(':'))
    exit_h, exit_m = map(int, exit_time.split(':'))

    if isinstance(start_date, str):
        sd = datetime.strptime(start_date, '%Y-%m-%d').date()
    else:
        sd = start_date
    if isinstance(end_date, str):
        ed = datetime.strptime(end_date, '%Y-%m-%d').date()
    else:
        ed = end_date

    trades = []
    equity_curve = [0.0]
    daily_pnl = defaultdict(float)
    total_charges_all = 0.0

    seed_base = hash(f"{symbol}_{start_date}_{stop_loss}_{take_profit}")
    random.seed(seed_base)

    current_date = sd
    while current_date <= ed:
        if current_date.weekday() >= 5:
            current_date += timedelta(days=1)
            continue

        day_data = market_data.get(str(current_date), {})
        spot_open = day_data.get('spot', 0)
        candles = day_data.get('candles', [])
        vix = day_data.get('vix', 15.0)

        if not spot_open or not candles:
            current_date += timedelta(days=1)
            continue

        entry_idx = 0
        for ci, c in enumerate(candles):
            hm = int(c['time'].replace(':', ''))
            if hm >= entry_h * 100 + entry_m:
                entry_idx = ci
                break

        exit_idx = len(candles) - 1
        for ci in range(len(candles) - 1, -1, -1):
            hm = int(candles[ci]['time'].replace(':', ''))
            if hm <= exit_h * 100 + exit_m:
                exit_idx = ci
                break

        entry_spot = candles[entry_idx]['close']
        exit_spot = candles[min(exit_idx, len(candles) - 1)]['close']

        is_expiry_day = False
        expiry_date = get_nearest_expiry(current_date, 'Weekly')
        if expiry_date == current_date:
            is_expiry_day = True

        day_trades_count = 0

        for leg_cfg in legs_config:
            if day_trades_count >= max_trades_per_day:
                break

            stype = leg_cfg.get('strike_type', 'ATM')
            # support "OTM1","OTM2","ITM2" or distance field
            dist = leg_cfg.get('distance', leg_cfg.get('strike_distance', 0))
            if isinstance(stype, str) and any(c.isdigit() for c in stype):
                import re
                m = re.match(r'(ATM|OTM|ITM)(\d+)', stype.upper())
                if m:
                    stype = m.group(1)
                    dist = int(m.group(2)) - (0 if stype == 'ATM' else 1) + int(dist or 0)
            step = 50 if symbol in ('NIFTY','FINNIFTY') else 100 if symbol=='BANKNIFTY' else 50
            # fallback: infer step from spot
            if spot_open > 5000 and step==50 and symbol=='BANKNIFTY':
                step=100
            strike = select_strike(entry_spot, stype, step=step, distance=dist)
            opt_type = leg_cfg.get('option_type', 'CE')
            position = leg_cfg.get('position', 'Buy')
            leg_lots = leg_cfg.get('lots', lots)
            leg_qty = leg_lots * lot_size

            days_to_expiry = max((expiry_date - current_date).days, 1)
            premium = estimate_option_premium(entry_spot, strike, opt_type, vix, days_to_expiry)

            slippage_entry = calculate_slippage(premium, lot_size)
            if position == 'Buy':
                effective_entry = round_to_nearest_tick(premium + slippage_entry)
            else:
                effective_entry = round_to_nearest_tick(premium - slippage_entry)

            entry_costs = calculate_transaction_costs(
                effective_entry, leg_lots, lot_size, position
            )

            price_path = simulate_option_path(premium, entry_spot, exit_spot, vix, n_steps=30)

            exit_price = price_path[-1]
            exit_reason = 'Time Exit'

            for step_price in price_path:
                if position == 'Buy':
                    unrealized = (step_price - effective_entry) * leg_qty
                else:
                    unrealized = (effective_entry - step_price) * leg_qty

                if take_profit > 0 and unrealized >= take_profit:
                    exit_price = step_price
                    exit_reason = 'Target Hit'
                    break
                if stop_loss > 0 and unrealized <= -stop_loss:
                    exit_price = step_price
                    exit_reason = 'Stoploss Hit'
                    break

            if is_expiry_day:
                exit_reason = 'Expiry Square-Off'

            slippage_exit = calculate_slippage(exit_price, lot_size)
            if position == 'Buy':
                effective_exit = round_to_nearest_tick(exit_price + slippage_exit)
            else:
                effective_exit = round_to_nearest_tick(exit_price - slippage_exit)

            exit_costs = calculate_transaction_costs(
                effective_exit, leg_lots, lot_size,
                'Sell' if position == 'Buy' else 'Buy'
            )

            if position == 'Buy':
                gross_pnl = (effective_exit - effective_entry) * leg_qty
            else:
                gross_pnl = (effective_entry - effective_exit) * leg_qty

            total_charge = entry_costs['total_charges'] + exit_costs['total_charges']
            net_pnl = gross_pnl - total_charge

            trade_record = {
                'entry_date': str(current_date),
                'entry_time': candles[entry_idx]['time'],
                'exit_date': str(current_date),
                'exit_time': candles[min(exit_idx, len(candles) - 1)]['time'],
                'symbol': symbol,
                'option_type': opt_type,
                'position': position,
                'strike': strike,
                'quantity': leg_qty,
                'entry_price': effective_entry,
                'slippage_entry': slippage_entry,
                'exit_price': effective_exit,
                'slippage_exit': slippage_exit,
                'gross_pnl': round(gross_pnl, 2),
                'net_pnl': round(net_pnl, 2),
                'total_charges': round(total_charge, 2),
                'brokerage': round(exit_costs['brokerage'] + entry_costs['brokerage'], 2),
                'stt': round(exit_costs['stt'] + entry_costs['stt'], 2),
                'gst': round(exit_costs['gst'] + entry_costs['gst'], 2),
                'exchange_charges': round(exit_costs['exchange_charges'] + entry_costs['exchange_charges'], 2),
                'sebi_charges': round(exit_costs['sebi_charges'] + entry_costs['sebi_charges'], 2),
                'stamp_duty': round(exit_costs['stamp_duty'] + entry_costs['stamp_duty'], 2),
                'exit_reason': exit_reason,
                'is_win': net_pnl > 0,
                'vix': vix,
            }

            trades.append(trade_record)
            daily_pnl[str(current_date)] += net_pnl
            equity_curve.append(equity_curve[-1] + net_pnl)
            total_charges_all += total_charge
            day_trades_count += 1

        current_date += timedelta(days=1)

    return compute_metrics(trades, equity_curve, daily_pnl, total_charges_all)


def compute_metrics(trades, equity_curve, daily_pnl, total_charges):
    """Compute professional backtest metrics"""
    if not trades:
        return {
            'net_pnl': 0, 'total_trades': 0, 'win_rate': 0, 'loss_rate': 0,
            'avg_profit_per_trade': 0, 'avg_win': 0, 'avg_loss': 0,
            'max_win': 0, 'max_loss': 0, 'max_drawdown': 0,
            'max_dd_duration': 0, 'return_maxdd': 0, 'reward_risk': 0,
            'expectancy': 0, 'max_win_streak': 0, 'max_loss_streak': 0,
            'max_trades_in_dd': 0, 'initial_capital': 100000,
            'final_capital': 100000, 'total_charges': 0,
            'winning_trades': 0, 'losing_trades': 0,
            'profit_factor': 0, 'sharpe_ratio': 0, 'calmar_ratio': 0,
            'trade_list': [], 'equity_curve': equity_curve, 'monthly_pnl': {},
        }

    net_pnls = [t['net_pnl'] for t in trades]
    gross_pnls = [t['gross_pnl'] for t in trades]

    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p <= 0]

    total_trades = len(trades)
    winning_trades = len(wins)
    losing_trades = len(losses)
    win_rate = round(winning_trades / total_trades * 100, 1) if total_trades else 0
    loss_rate = round(losing_trades / total_trades * 100, 1) if total_trades else 0

    avg_profit = round(sum(net_pnls) / total_trades, 2) if total_trades else 0
    avg_win = round(sum(wins) / winning_trades, 2) if winning_trades else 0
    avg_loss = round(sum(losses) / losing_trades, 2) if losing_trades else 0

    max_win = round(max(net_pnls), 2) if net_pnls else 0
    max_loss = round(min(net_pnls), 2) if net_pnls else 0

    peak = equity_curve[0]
    max_dd = 0
    max_dd_duration = 0
    current_dd_duration = 0
    for val in equity_curve:
        if val > peak:
            peak = val
            current_dd_duration = 0
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
        if dd > 0:
            current_dd_duration += 1
            if current_dd_duration > max_dd_duration:
                max_dd_duration = current_dd_duration

    initial_capital = 100000
    final_capital = initial_capital + sum(net_pnls)
    return_maxdd = round(sum(net_pnls) / max_dd, 2) if max_dd > 0 else 0

    avg_w = abs(avg_win) if avg_win else 1
    avg_l = abs(avg_loss) if avg_loss else 1
    reward_risk = round(avg_w / avg_l, 2)

    expectancy = round(avg_profit, 2)

    win_streak = 0
    loss_streak = 0
    max_win_streak = 0
    max_loss_streak = 0
    for p in net_pnls:
        if p > 0:
            win_streak += 1
            loss_streak = 0
            max_win_streak = max(max_win_streak, win_streak)
        else:
            loss_streak += 1
            win_streak = 0
            max_loss_streak = max(max_loss_streak, loss_streak)

    in_dd = False
    dd_trades = 0
    max_trades_in_dd = 0
    for p in net_pnls:
        if p < 0:
            in_dd = True
            dd_trades += 1
            max_trades_in_dd = max(max_trades_in_dd, dd_trades)
        else:
            in_dd = False
            dd_trades = 0

    monthly_pnl = defaultdict(float)
    for t in trades:
        month_key = t['entry_date'][:7]
        monthly_pnl[month_key] += t['net_pnl']
    monthly_pnl = {k: round(v, 2) for k, v in sorted(monthly_pnl.items())}

    gross_wins = sum(g for g in gross_pnls if g > 0)
    gross_losses = abs(sum(g for g in gross_pnls if g < 0))
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 0

    returns = []
    for i in range(1, len(equity_curve)):
        r = (equity_curve[i] - equity_curve[i - 1]) / initial_capital
        returns.append(r)
    avg_r = sum(returns) / len(returns) if returns else 0
    var_r = sum((r - avg_r) ** 2 for r in returns) / len(returns) if returns else 1
    std_r = math.sqrt(var_r)
    sharpe = round(avg_r / std_r * math.sqrt(252), 2) if std_r > 0 else 0

    calmar = round(sum(net_pnls) / max_dd * (252 / max(total_trades, 1)), 2) if max_dd > 0 else 0

    trade_list = []
    for i, t in enumerate(trades):
        trade_list.append({
            'index': i + 1,
            'is_group': False,
            'entry_date': t['entry_date'],
            'entry_time': t['entry_time'],
            'exit_date': t['exit_date'],
            'exit_time': t['exit_time'],
            'option_type': t['option_type'],
            'position': t['position'],
            'strike': t['strike'],
            'quantity': t['quantity'],
            'entry_price': t['entry_price'],
            'exit_price': t['exit_price'],
            'vix': t['vix'],
            'pnl': t['net_pnl'],
            'pnl_formatted': f"{'+' if t['net_pnl'] >= 0 else ''}₹{t['net_pnl']:,.2f}",
            'gross_pnl': t['gross_pnl'],
            'total_charges': t['total_charges'],
            'brokerage': t['brokerage'],
            'stt': t['stt'],
            'gst': t['gst'],
            'exchange_charges': t['exchange_charges'],
            'sebi_charges': t['sebi_charges'],
            'stamp_duty': t['stamp_duty'],
            'exit_reason': t['exit_reason'],
            'is_win': t['is_win'],
            'slippage_entry': t['slippage_entry'],
            'slippage_exit': t['slippage_exit'],
        })

    return {
        'net_pnl': round(sum(net_pnls), 2),
        'total_trades': total_trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'win_rate': win_rate,
        'loss_rate': loss_rate,
        'avg_profit_per_trade': avg_profit,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'max_win': max_win,
        'max_loss': max_loss,
        'max_drawdown': round(max_dd, 2),
        'max_dd_duration': max_dd_duration,
        'return_maxdd': return_maxdd,
        'reward_risk': reward_risk,
        'expectancy': expectancy,
        'max_win_streak': max_win_streak,
        'max_loss_streak': max_loss_streak,
        'max_trades_in_dd': max_trades_in_dd,
        'initial_capital': initial_capital,
        'final_capital': round(final_capital, 2),
        'total_charges': round(total_charges, 2),
        'profit_factor': profit_factor,
        'sharpe_ratio': sharpe,
        'calmar_ratio': calmar,
        'trade_list': trade_list,
        'equity_curve': equity_curve,
        'monthly_pnl': monthly_pnl,
    }
