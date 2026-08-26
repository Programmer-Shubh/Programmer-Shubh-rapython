import json
import random
from datetime import datetime, timedelta

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.shortcuts import render

from .models import (
    Strategy, StrategyLeg, BacktestRun, BacktestTrade,
    BhavcopyData, LiveTrade, BrokerConfig,
)
from .backtest_engine import run_backtest, AVAILABLE_SYMBOLS, get_lot_size
from .data_fetcher import (
    generate_date_range_data, fetch_spot_prices,
    SYMBOL_SPOTS, SYMBOL_LOT_SIZES,
)
from .transaction_costs import calculate_total_cost_with_slippage


def index_view(request):
    return render(request, 'tmp_root.html')


def _json_body(request):
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return {}


@require_GET
def api_dashboard_spot(request):
    return JsonResponse(fetch_spot_prices())


@require_GET
def api_dashboard_stats(request):
    open_count = LiveTrade.objects.filter(status='open').count()
    closed_count = LiveTrade.objects.filter(status='closed').count()
    open_trades = LiveTrade.objects.filter(status='open')
    total_pnl = sum(t.pnl for t in open_trades)
    closed_trades = LiveTrade.objects.filter(status='closed')
    wins = sum(1 for t in closed_trades if t.pnl > 0)
    total = closed_count
    win_rate = round(wins / total * 100, 1) if total > 0 else 0
    return JsonResponse({
        'open_count': open_count,
        'closed_count': closed_count,
        'total_pnl': round(total_pnl, 2),
        'win_rate': win_rate,
    })


@require_GET
def api_dashboard_portfolio(request):
    positions = LiveTrade.objects.filter(status='open').order_by('-created_at')
    return JsonResponse({'positions': [p.to_dict() for p in positions]})


@require_GET
def api_dashboard_trade_history(request):
    trades = LiveTrade.objects.filter(status='closed').order_by('-closed_at')[:50]
    result = []
    for t in trades:
        result.append({
            'id': t.id,
            'date': t.date,
            'symbol': t.symbol,
            'option_type': t.option_type,
            'strike': t.strike,
            'entry': t.entry_price,
            'exit': t.exit_price,
            'pnl': t.pnl,
            'pnl_formatted': f"{'₹' if t.pnl >= 0 else '-₹'}{abs(t.pnl):,.2f}",
        })
    return JsonResponse({'trades': result})


@csrf_exempt
@require_POST
def api_trades_close(request):
    body = _json_body(request)
    trade_id = body.get('trade_id')
    exit_price = body.get('exit_price', 0)
    try:
        trade = LiveTrade.objects.get(id=trade_id)
    except LiveTrade.DoesNotExist:
        return JsonResponse({'error': 'Trade not found'}, status=404)
    trade.exit_price = exit_price
    trade.current_price = exit_price
    qty = trade.quantity * trade.lot_size
    if trade.transaction_type == 'BUY':
        trade.pnl = (exit_price - trade.entry_price) * qty
    else:
        trade.pnl = (trade.entry_price - exit_price) * qty
    trade.status = 'closed'
    trade.closed_at = datetime.now()
    trade.save()
    return JsonResponse({'success': True})


@csrf_exempt
@require_POST
def api_trades_delete(request, trade_id):
    try:
        trade = LiveTrade.objects.get(id=trade_id)
        trade.delete()
        return JsonResponse({'success': True})
    except LiveTrade.DoesNotExist:
        return JsonResponse({'error': 'Trade not found'}, status=404)


@csrf_exempt
@require_POST
def api_trades_mode(request):
    body = _json_body(request)
    trade_id = body.get('trade_id')
    mode = body.get('trade_mode', 'paper')
    try:
        trade = LiveTrade.objects.get(id=trade_id)
        trade.trade_mode = mode
        trade.save()
        return JsonResponse({'success': True})
    except LiveTrade.DoesNotExist:
        return JsonResponse({'error': 'Trade not found'}, status=404)


@csrf_exempt
@require_POST
def api_trades_update_sltp(request):
    body = _json_body(request)
    trade_id = body.get('trade_id')
    sl = body.get('stop_loss', 0)
    tp = body.get('target', 0)
    try:
        trade = LiveTrade.objects.get(id=trade_id)
        if sl:
            trade.stop_loss = sl
        if tp:
            trade.take_profit = tp
        trade.save()
        return JsonResponse({'success': True})
    except LiveTrade.DoesNotExist:
        return JsonResponse({'error': 'Trade not found'}, status=404)


@require_GET
def api_trades_open(request):
    positions = LiveTrade.objects.filter(status='open').order_by('-created_at')
    return JsonResponse({'positions': [p.to_dict() for p in positions]})


@require_GET
def api_trades_history(request):
    trades = LiveTrade.objects.filter(status='closed').order_by('-closed_at')[:100]
    result = []
    for t in trades:
        qty = t.quantity * t.lot_size
        pnl = t.pnl
        result.append({
            'id': t.id,
            'date': t.date,
            'symbol': t.symbol,
            'option_type': t.option_type,
            'strike': t.strike,
            'entry': t.entry_price,
            'exit': t.exit_price,
            'pnl': pnl,
            'pnl_formatted': f"{'₹' if pnl >= 0 else '-₹'}{abs(pnl):,.2f}",
        })
    return JsonResponse({'trades': result})


@csrf_exempt
@require_POST
def api_chain_place_trade(request):
    body = _json_body(request)
    symbol = body.get('symbol', 'NIFTY')
    option_type = body.get('option_type', 'CE')
    strike = body.get('strike', 0)
    expiry = body.get('expiry', '')
    date = body.get('date', str(datetime.now().date()))
    txn_type = body.get('transaction_type', 'BUY')
    quantity = body.get('quantity', 1)
    lot_size = SYMBOL_LOT_SIZES.get(symbol, 50)
    stop_loss = body.get('stop_loss', 500)
    take_profit = body.get('take_profit', 1000)

    base = SYMBOL_SPOTS.get(symbol, 24000)
    entry_price = round(base * 0.02, 2)

    trade = LiveTrade.objects.create(
        symbol=symbol,
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        date=date,
        transaction_type=txn_type,
        quantity=quantity,
        lot_size=lot_size,
        entry_price=entry_price,
        current_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        trade_mode='paper',
        status='open',
    )
    return JsonResponse({
        'success': True,
        'trade_id': trade.id,
        'entry_price': trade.entry_price,
    })


@require_GET
def api_chain_symbols(request):
    return JsonResponse({'symbols': AVAILABLE_SYMBOLS})


@require_GET
def api_chain_dates(request, symbol):
    dates = BhavcopyData.objects.filter(
        symbol=symbol.upper()
    ).values_list('date', flat=True).distinct().order_by('-date')
    dates_list = list(dates)
    return JsonResponse({'count': len(dates_list), 'dates': dates_list})


@require_GET
def api_chain_expiries(request, symbol, date):
    expiries = BhavcopyData.objects.filter(
        symbol=symbol.upper(), date=date
    ).values_list('expiry', flat=True).distinct()
    return JsonResponse({'expiries': list(expiries)})


@require_GET
def api_chain_chain(request, symbol, date, expiry):
    rows = BhavcopyData.objects.filter(
        symbol=symbol.upper(), date=date, expiry=expiry
    ).order_by('strike')

    if not rows.exists():
        return JsonResponse({'error': 'No data for this date/expiry'}, status=404)

    underlying = rows.first().underlying_close if rows else 0
    atm = 0
    if underlying:
        step = 50
        atm = round(underlying / step) * step

    strikes = sorted(set(r.strike for r in rows))
    chain_rows = []
    for s in strikes:
        ce = rows.filter(strike=s, option_type='CE').first()
        pe = rows.filter(strike=s, option_type='PE').first()
        distance = s - underlying if underlying else 0
        chain_rows.append({
            'strike': s,
            'distance': distance,
            'ce_ltp': ce.close if ce else 0,
            'ce_vol': ce.volume if ce else 0,
            'ce_oi': ce.oi if ce else 0,
            'pe_ltp': pe.close if pe else 0,
            'pe_vol': pe.volume if pe else 0,
            'pe_oi': pe.oi if pe else 0,
        })

    return JsonResponse({
        'spot': underlying,
        'atm': atm,
        'rows': chain_rows,
    })


@require_GET
def api_chain_live(request, symbol):
    base = SYMBOL_SPOTS.get(symbol.upper(), 24000)
    random_offset = base * 0.001
    spot = round(base + random_offset, 2)
    step = STRIKE_STEP.get(symbol.upper(), 50)
    atm = round(spot / step) * step

    rows = []
    for offset in range(-10, 11):
        strike = atm + offset * step
        distance = strike - spot
        ce_premium = max(5, 50 - abs(distance) * 0.1 + random.uniform(-2, 2))
        pe_premium = max(5, 50 - abs(distance) * 0.1 + random.uniform(-2, 2))
        rows.append({
            'strike': strike,
            'distance': round(distance, 2),
            'ce_ltp': round(ce_premium, 2),
            'ce_vol': int(random.uniform(1000, 50000)),
            'ce_oi': int(random.uniform(5000, 200000)),
            'pe_ltp': round(pe_premium, 2),
            'pe_vol': int(random.uniform(1000, 50000)),
            'pe_oi': int(random.uniform(5000, 200000)),
        })

    return JsonResponse({
        'spot': spot,
        'atm': atm,
        'rows': rows,
    })


@csrf_exempt
@require_POST
def api_backtest_run(request):
    body = _json_body(request)
    symbol = body.get('symbol', 'NIFTY')
    start_date = body.get('start_date', '2026-08-01')
    end_date = body.get('end_date', '2026-08-20')

    data = generate_date_range_data(symbol, start_date, end_date)

    config = {
        'symbol': symbol,
        'strategy_type': body.get('strategy_type', 'Intraday'),
        'entry_time': body.get('entry_time', '09:35'),
        'exit_time': body.get('exit_time', '15:14'),
        'lots': body.get('lots', 1),
        'stop_loss': body.get('stop_loss', 500),
        'take_profit': body.get('take_profit', 1000),
        'momentum': body.get('momentum', 0),
        'start_date': start_date,
        'end_date': end_date,
        'legs': body.get('legs', []),
        'advanced': body.get('advanced', {}),
        'max_trades_per_day': body.get('max_trades_per_day', 5),
        'data': data,
    }

    try:
        result = run_backtest(config)

        run_record = BacktestRun.objects.create(
            symbol=symbol,
            strategy_type=config['strategy_type'],
            entry_time=config['entry_time'],
            exit_time=config['exit_time'],
            start_date=start_date,
            end_date=end_date,
            lots=config['lots'],
            stop_loss=config['stop_loss'],
            take_profit=config['take_profit'],
            legs_config=config['legs'],
            advanced_config=config['advanced'],
            result_metrics={
                k: v for k, v in result.items()
                if k not in ('trade_list', 'equity_curve', 'monthly_pnl')
            },
            equity_curve=result.get('equity_curve', []),
            monthly_pnl=result.get('monthly_pnl', {}),
        )

        for i, t in enumerate(result.get('trade_list', [])):
            BacktestTrade.objects.create(
                backtest_run=run_record,
                trade_index=t.get('index', i + 1),
                entry_date=t.get('entry_date', ''),
                entry_time=t.get('entry_time', ''),
                exit_date=t.get('exit_date', ''),
                exit_time=t.get('exit_time', ''),
                symbol=symbol,
                option_type=t.get('option_type', ''),
                position=t.get('position', ''),
                strike=t.get('strike', 0),
                quantity=t.get('quantity', 0),
                entry_price=t.get('entry_price', 0),
                exit_price=t.get('exit_price', 0),
                slippage_entry=t.get('slippage_entry', 0),
                slippage_exit=t.get('slippage_exit', 0),
                brokerage=t.get('brokerage', 0),
                stt=t.get('stt', 0),
                gst=t.get('gst', 0),
                exchange_charges=t.get('exchange_charges', 0),
                sebi_charges=t.get('sebi_charges', 0),
                stamp_duty=t.get('stamp_duty', 0),
                total_charges=t.get('total_charges', 0),
                gross_pnl=t.get('gross_pnl', 0),
                net_pnl=t.get('pnl', 0),
                exit_reason=t.get('exit_reason', ''),
                is_win=t.get('is_win', False),
                vix=t.get('vix', 15.0),
            )

        result['source'] = 'ratrade.quantitative.engine'
        return JsonResponse(result)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_POST
def api_strategies_save(request):
    body = _json_body(request)
    strat_id = body.get('id')

    if strat_id:
        try:
            strat = Strategy.objects.get(id=strat_id)
            strat.name = body.get('name', strat.name)
            strat.symbol = body.get('symbol', strat.symbol)
            strat.strategy_type = body.get('strategy_type', strat.strategy_type)
            strat.entry_time = body.get('entry_time', strat.entry_time)
            strat.exit_time = body.get('exit_time', strat.exit_time)
            strat.start_date = body.get('start_date', strat.start_date)
            strat.end_date = body.get('end_date', strat.end_date)
            strat.momentum = body.get('momentum', strat.momentum)
            strat.lots = body.get('lots', strat.lots)
            strat.stop_loss = body.get('stop_loss', strat.stop_loss)
            strat.take_profit = body.get('take_profit', strat.take_profit)
            strat.reentry_on_sl = body.get('reentry_on_sl', strat.reentry_on_sl)
            strat.reentry_on_tp = body.get('reentry_on_tp', strat.reentry_on_tp)
            strat.trailing = body.get('trailing', strat.trailing)
            strat.lock_profit_at = body.get('lock_profit_at', strat.lock_profit_at)
            strat.lock_profit = body.get('lock_profit', strat.lock_profit)
            strat.status = body.get('status', strat.status)
            strat.save()
            return JsonResponse({'id': strat.id})
        except Strategy.DoesNotExist:
            return JsonResponse({'error': 'Strategy not found'}, status=404)

    strat = Strategy.objects.create(
        name=body.get('name', body.get('symbol', 'Untitled') + '_' + body.get('strategy_type', 'Intraday')),
        symbol=body.get('symbol', 'NIFTY'),
        strategy_type=body.get('strategy_type', 'Intraday'),
        entry_time=body.get('entry_time', '09:35'),
        exit_time=body.get('exit_time', '15:14'),
        start_date=body.get('start_date', ''),
        end_date=body.get('end_date', ''),
        momentum=body.get('momentum', 0),
        lots=body.get('lots', 1),
        stop_loss=body.get('stop_loss', 500),
        take_profit=body.get('take_profit', 1000),
        reentry_on_sl=body.get('reentry_on_sl', 'RE ASAP'),
        reentry_on_tp=body.get('reentry_on_tp', 'RE ASAP'),
        trailing=body.get('trailing', 'Lock'),
        lock_profit_at=body.get('lock_profit_at', 0),
        lock_profit=body.get('lock_profit', 1),
        status=body.get('status', 'draft'),
    )

    for leg in body.get('legs', []):
        StrategyLeg.objects.create(
            strategy=strat,
            option_type=leg.get('option_type', 'CE'),
            position=leg.get('position', 'Buy'),
            strike_type=leg.get('strike_type', 'ATM'),
            lots=leg.get('lots', 1),
            expiry=leg.get('expiry', 'Monthly'),
            stop_loss=leg.get('stop_loss', 0),
            take_profit=leg.get('take_profit', 0),
        )

    return JsonResponse({'id': strat.id})


@require_GET
def api_strategies_list(request):
    strats = Strategy.objects.all().order_by('-created_at')
    return JsonResponse({'strategies': [s.to_dict() for s in strats]})


@require_GET
def api_strategies_detail(request, strat_id):
    try:
        strat = Strategy.objects.get(id=strat_id)
        return JsonResponse(strat.to_dict())
    except Strategy.DoesNotExist:
        return JsonResponse({'error': 'Strategy not found'}, status=404)


@csrf_exempt
def api_strategies_delete(request, strat_id):
    try:
        strat = Strategy.objects.get(id=strat_id)
        strat.delete()
        return JsonResponse({'success': True})
    except Strategy.DoesNotExist:
        return JsonResponse({'error': 'Strategy not found'}, status=404)


@require_GET
def api_bhavcopy_dates(request, symbol):
    dates = BhavcopyData.objects.filter(
        symbol=symbol.upper()
    ).values_list('date', flat=True).distinct().order_by('-date')
    dates_list = list(dates)
    return JsonResponse({
        'count': BhavcopyData.objects.filter(symbol=symbol.upper()).count(),
        'dates': dates_list,
    })


@require_GET
def api_scanner_scan_all(request):
    from datetime import datetime as dt
    today = dt.now().strftime('%Y-%m-%d')
    indices = ['NIFTY', 'BANKNIFTY', 'FINNIFTY']

    bullish = []
    bearish = []

    for sym in indices:
        base = SYMBOL_SPOTS.get(sym, 24000)
        random.seed(int(dt.now().timestamp() / 300) + hash(sym))
        change = random.uniform(-0.3, 0.3)
        price = base * (1 + change / 100)
        score = random.randint(45, 85)

        reasons_st = []
        if change > 0:
            reasons_st.append('SuperTrend Buy')
            reasons_st.append('MACD Bullish Cross')
            bullish.append({
                'symbol': sym, 'price': round(price, 2), 'date': today,
                'score': score, 'reasons': reasons_st,
                'option_suggestion': {'strike': round(price / 50) * 50, 'expiry': '', 'premium': round(price * 0.015, 2)},
                'indicators': {'RSI': round(random.uniform(40, 70), 1), 'MACD': round(random.uniform(-20, 20), 1)},
            })
        else:
            reasons_st.append('SuperTrend Sell')
            reasons_st.append('MACD Bearish Cross')
            bearish.append({
                'symbol': sym, 'price': round(price, 2), 'date': today,
                'score': score, 'reasons': reasons_st,
                'option_suggestion': {'strike': round(price / 50) * 50, 'expiry': '', 'premium': round(price * 0.015, 2)},
                'indicators': {'RSI': round(random.uniform(30, 60), 1), 'MACD': round(random.uniform(-20, 20), 1)},
            })

    return JsonResponse({
        'st_macd': {'total_scanned': len(indices), 'bullish': bullish, 'bearish': bearish},
        'vwap': {'long': bullish[:1], 'short': bearish[:1]},
    })


@require_GET
def api_scanner_opportunities(request):
    from datetime import datetime as dt
    today = dt.now().strftime('%Y-%m-%d')
    indices = ['NIFTY', 'BANKNIFTY', 'FINNIFTY']
    opportunities = []
    for sym in indices:
        base = SYMBOL_SPOTS.get(sym, 24000)
        random.seed(int(dt.now().timestamp() / 600) + hash(sym))
        change = random.uniform(-0.5, 0.5)
        price = base * (1 + change / 100)
        score = random.randint(50, 90)
        sig_type = 'CE Buy' if change > 0 else 'PE Buy'
        step = STRIKE_STEP.get(sym, 50)
        strike = round(price / step) * step
        reasons = ['Price above VWAP' if change > 0 else 'Price below VWAP',
                    'RSI Momentum' if change > 0 else 'RSI Weakness']
        opportunities.append({
            'symbol': sym, 'price': round(price, 2), 'date': today,
            'score': score, 'type': sig_type, 'signal_type': sig_type,
            'reasons': reasons,
            'option_suggestion': {
                'strike': strike,
                'expiry': '',
                'premium': round(price * 0.015, 2),
            },
        })
    return JsonResponse({'opportunities': opportunities})


@csrf_exempt
@require_POST
def api_broker_save_config(request):
    body = _json_body(request)
    broker = body.get('broker', '')
    config = body.get('config', {})
    obj, created = BrokerConfig.objects.update_or_create(
        broker=broker,
        defaults={'config': config, 'connected': False}
    )
    return JsonResponse({'success': True})


@require_GET
def api_broker_list(request):
    broker_defs = [
        {'key': 'shoonya', 'name': 'Shoonya (Finvasia)', 'icon': 'bi-cpu', 'color': 'text-primary',
         'desc': 'Zero brokerage options trading', 'fields': ['uid', 'pwd', 'vc', 'apikey', 'secret_code', 'actid']},
        {'key': 'dhan', 'name': 'Dhan', 'icon': 'bi-lightning', 'color': 'text-warning',
         'desc': 'Fast execution, option selling', 'fields': ['client_id', 'access_token', 'refresh_token']},
        {'key': 'fyers', 'name': 'Fyers', 'icon': 'bi-graph-up', 'color': 'text-success',
         'desc': 'Advanced charting + trading', 'fields': ['app_id', 'secret', 'access_token', 'refresh_token']},
        {'key': 'angel', 'name': 'Angel One', 'icon': 'bi-shield', 'color': 'text-danger',
         'desc': 'Full service broker', 'fields': ['client_code', 'password', 'api_key', 'totp_secret']},
    ]
    for b in broker_defs:
        try:
            cfg = BrokerConfig.objects.get(broker=b['key'])
            b['config'] = cfg.config
            b['configured'] = bool(cfg.config)
            b['connected'] = cfg.connected
        except BrokerConfig.DoesNotExist:
            b['config'] = {}
            b['configured'] = False
            b['connected'] = False
    return JsonResponse({'brokers': broker_defs})


@csrf_exempt
@require_POST
def api_broker_connect(request):
    body = _json_body(request)
    broker = body.get('broker', '')
    try:
        cfg = BrokerConfig.objects.get(broker=broker)
        cfg.connected = True
        cfg.save()
        return JsonResponse({'success': True})
    except BrokerConfig.DoesNotExist:
        return JsonResponse({'error': 'Broker not configured'}, status=404)


@csrf_exempt
@require_POST
def api_broker_account(request):
    body = _json_body(request)
    broker = body.get('broker', '')
    return JsonResponse({
        'success': True,
        'funds': {'available_margin': 150000, 'used_margin': 0},
        'positions': [],
        'holdings': [],
        'orders': [],
    })


@require_GET
def api_broker_fyers_auth_url(request):
    return JsonResponse({'success': False, 'error': 'Configure Fyers credentials first'})


@csrf_exempt
@require_POST
def api_broker_fyers_auth(request):
    return JsonResponse({'success': False, 'error': 'OAuth not available in demo mode'})


@csrf_exempt
def api_broker_auto_connect(request):
    return JsonResponse({'success': True, 'results': {}})


@csrf_exempt
def api_broker_refresh_tokens(request):
    return JsonResponse({'success': True, 'results': {}})
