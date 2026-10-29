from django.contrib import admin
from .models import (
    Strategy, StrategyLeg, BacktestRun, BacktestTrade,
    BhavcopyData, LiveTrade, BrokerConfig
)


@admin.register(Strategy)
class StrategyAdmin(admin.ModelAdmin):
    list_display = ('name', 'symbol', 'strategy_type', 'status', 'created_at')
    list_filter = ('status', 'strategy_type', 'symbol')
    search_fields = ('name',)


@admin.register(StrategyLeg)
class StrategyLegAdmin(admin.ModelAdmin):
    list_display = ('strategy', 'option_type', 'position', 'strike_type', 'lots')
    list_filter = ('option_type', 'position', 'strike_type')


@admin.register(BacktestRun)
class BacktestRunAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'strategy_type', 'start_date', 'end_date', 'created_at')
    list_filter = ('symbol', 'strategy_type')


@admin.register(BacktestTrade)
class BacktestTradeAdmin(admin.ModelAdmin):
    list_display = ('backtest_run', 'trade_index', 'entry_date', 'exit_date',
                    'option_type', 'position', 'net_pnl', 'exit_reason')
    list_filter = ('exit_reason', 'option_type', 'position')


@admin.register(BhavcopyData)
class BhavcopyDataAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'date', 'expiry', 'strike', 'option_type', 'close', 'volume')
    list_filter = ('symbol', 'option_type')
    search_fields = ('symbol', 'date')
    list_per_page = 100


@admin.register(LiveTrade)
class LiveTradeAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'option_type', 'strike', 'transaction_type',
                    'entry_price', 'current_price', 'pnl', 'status')
    list_filter = ('status', 'trade_mode')


@admin.register(BrokerConfig)
class BrokerConfigAdmin(admin.ModelAdmin):
    list_display = ('broker', 'connected', 'updated_at')
