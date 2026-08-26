from django.db import models
import json


class Strategy(models.Model):
    name = models.CharField(max_length=200)
    symbol = models.CharField(max_length=50, default='NIFTY')
    strategy_type = models.CharField(max_length=50, default='Intraday')
    entry_time = models.CharField(max_length=10, default='09:35')
    exit_time = models.CharField(max_length=10, default='15:14')
    start_date = models.CharField(max_length=20, default='')
    end_date = models.CharField(max_length=20, default='')
    momentum = models.IntegerField(default=0)
    lots = models.IntegerField(default=1)
    stop_loss = models.FloatField(default=500)
    take_profit = models.FloatField(default=1000)
    reentry_on_sl = models.CharField(max_length=50, default='RE ASAP')
    reentry_on_tp = models.CharField(max_length=50, default='RE ASAP')
    trailing = models.CharField(max_length=50, default='Lock')
    lock_profit_at = models.FloatField(default=0)
    lock_profit = models.FloatField(default=1)
    max_trades_per_day = models.IntegerField(default=5)
    status = models.CharField(max_length=20, default='draft')
    description = models.TextField(default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'symbol': self.symbol,
            'strategy_type': self.strategy_type,
            'entry_time': self.entry_time,
            'exit_time': self.exit_time,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'momentum': self.momentum,
            'lots': self.lots,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'reentry_on_sl': self.reentry_on_sl,
            'reentry_on_tp': self.reentry_on_tp,
            'trailing': self.trailing,
            'lock_profit_at': self.lock_profit_at,
            'lock_profit': self.lock_profit,
            'max_trades_per_day': self.max_trades_per_day,
            'status': self.status,
            'description': self.description,
            'legs': [leg.to_dict() for leg in self.legs.all()],
            'indicators': [],
        }


class StrategyLeg(models.Model):
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name='legs')
    option_type = models.CharField(max_length=5, default='CE')
    position = models.CharField(max_length=10, default='Buy')
    strike_type = models.CharField(max_length=20, default='ATM')
    lots = models.IntegerField(default=1)
    expiry = models.CharField(max_length=20, default='Monthly')
    stop_loss = models.FloatField(default=0)
    take_profit = models.FloatField(default=0)
    delta = models.FloatField(default=0.3)

    def to_dict(self):
        return {
            'id': self.id,
            'option_type': self.option_type,
            'position': self.position,
            'strike_type': self.strike_type,
            'lots': self.lots,
            'expiry': self.expiry,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'delta': self.delta,
        }


class BacktestRun(models.Model):
    strategy = models.ForeignKey(Strategy, on_delete=models.SET_NULL, null=True, blank=True)
    symbol = models.CharField(max_length=50, default='NIFTY')
    strategy_type = models.CharField(max_length=50, default='Intraday')
    entry_time = models.CharField(max_length=10, default='09:35')
    exit_time = models.CharField(max_length=10, default='15:14')
    start_date = models.CharField(max_length=20)
    end_date = models.CharField(max_length=20)
    lots = models.IntegerField(default=1)
    stop_loss = models.FloatField(default=500)
    take_profit = models.FloatField(default=1000)
    legs_config = models.JSONField(default=list)
    advanced_config = models.JSONField(default=dict)
    result_metrics = models.JSONField(default=dict)
    equity_curve = models.JSONField(default=list)
    monthly_pnl = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def to_dict(self):
        return {
            'id': self.id,
            'symbol': self.symbol,
            'strategy_type': self.strategy_type,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'metrics': self.result_metrics,
        }


class BacktestTrade(models.Model):
    backtest_run = models.ForeignKey(BacktestRun, on_delete=models.CASCADE, related_name='trades')
    trade_index = models.IntegerField(default=0)
    entry_date = models.CharField(max_length=20, default='')
    entry_time = models.CharField(max_length=20, default='')
    exit_date = models.CharField(max_length=20, default='')
    exit_time = models.CharField(max_length=20, default='')
    symbol = models.CharField(max_length=50, default='')
    option_type = models.CharField(max_length=5, default='CE')
    position = models.CharField(max_length=10, default='Buy')
    strike = models.FloatField(default=0)
    quantity = models.IntegerField(default=0)
    entry_price = models.FloatField(default=0)
    exit_price = models.FloatField(default=0)
    slippage_entry = models.FloatField(default=0)
    slippage_exit = models.FloatField(default=0)
    brokerage = models.FloatField(default=0)
    stt = models.FloatField(default=0)
    gst = models.FloatField(default=0)
    exchange_charges = models.FloatField(default=0)
    sebi_charges = models.FloatField(default=0)
    stamp_duty = models.FloatField(default=0)
    total_charges = models.FloatField(default=0)
    gross_pnl = models.FloatField(default=0)
    net_pnl = models.FloatField(default=0)
    exit_reason = models.CharField(max_length=50, default='')
    is_win = models.BooleanField(default=False)
    vix = models.FloatField(default=15.0)

    def to_dict(self):
        return {
            'index': self.trade_index,
            'entry_date': self.entry_date,
            'entry_time': self.entry_time,
            'exit_date': self.exit_date,
            'exit_time': self.exit_time,
            'symbol': self.symbol,
            'option_type': self.option_type,
            'position': self.position,
            'strike': self.strike,
            'quantity': self.quantity,
            'entry_price': round(self.entry_price, 2),
            'exit_price': round(self.exit_price, 2),
            'slippage_entry': round(self.slippage_entry, 2),
            'slippage_exit': round(self.slippage_exit, 2),
            'brokerage': round(self.brokerage, 2),
            'stt': round(self.stt, 2),
            'gst': round(self.gst, 2),
            'exchange_charges': round(self.exchange_charges, 2),
            'sebi_charges': round(self.sebi_charges, 2),
            'stamp_duty': round(self.stamp_duty, 2),
            'total_charges': round(self.total_charges, 2),
            'gross_pnl': round(self.gross_pnl, 2),
            'net_pnl': round(self.net_pnl, 2),
            'exit_reason': self.exit_reason,
            'is_win': self.is_win,
            'vix': self.vix,
        }


class BhavcopyData(models.Model):
    symbol = models.CharField(max_length=50, db_index=True)
    date = models.CharField(max_length=20, db_index=True)
    expiry = models.CharField(max_length=20)
    strike = models.FloatField()
    option_type = models.CharField(max_length=5)
    open = models.FloatField(default=0)
    high = models.FloatField(default=0)
    low = models.FloatField(default=0)
    close = models.FloatField(default=0)
    volume = models.IntegerField(default=0)
    oi = models.IntegerField(default=0)
    underlying_close = models.FloatField(default=0)
    lot_size = models.IntegerField(default=50)

    class Meta:
        indexes = [
            models.Index(fields=['symbol', 'date']),
            models.Index(fields=['symbol', 'date', 'expiry']),
        ]


class LiveTrade(models.Model):
    symbol = models.CharField(max_length=50, default='NIFTY')
    option_type = models.CharField(max_length=5, default='CE')
    strike = models.FloatField(default=0)
    expiry = models.CharField(max_length=20, default='')
    date = models.CharField(max_length=20, default='')
    transaction_type = models.CharField(max_length=10, default='BUY')
    quantity = models.IntegerField(default=1)
    lot_size = models.IntegerField(default=50)
    entry_price = models.FloatField(default=0)
    current_price = models.FloatField(default=0)
    exit_price = models.FloatField(default=0)
    stop_loss = models.FloatField(default=500)
    take_profit = models.FloatField(default=1000)
    pnl = models.FloatField(default=0)
    trade_mode = models.CharField(max_length=10, default='paper')
    status = models.CharField(max_length=20, default='open')
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    def to_dict(self):
        qty = self.quantity * self.lot_size
        entry_val = self.entry_price * qty
        current_val = self.current_price * qty
        pnl = current_val - entry_val if self.transaction_type == 'BUY' else entry_val - current_val
        return {
            'id': self.id,
            'symbol': self.symbol,
            'option_type': self.option_type,
            'strike': self.strike,
            'expiry': self.expiry,
            'date': self.date,
            'transaction_type': self.transaction_type,
            'quantity': self.quantity,
            'lot_size': self.lot_size,
            'entry_price': self.entry_price,
            'current_price': self.current_price,
            'exit_price': self.exit_price,
            'sl': self.stop_loss,
            'tp': self.take_profit,
            'pnl': round(pnl, 2),
            'pnl_formatted': f"{'₹' if pnl >= 0 else '-₹'}{abs(pnl):,.2f}",
            'trade_mode': self.trade_mode,
            'status': self.status,
        }


class BrokerConfig(models.Model):
    broker = models.CharField(max_length=50, unique=True)
    config = models.JSONField(default=dict)
    connected = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
