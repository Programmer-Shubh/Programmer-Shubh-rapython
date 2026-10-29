from django.urls import path
from . import views

urlpatterns = [
    # Dashboard
    path('dashboard/spot', views.api_dashboard_spot, name='api_dashboard_spot'),
    path('dashboard/top-movers', views.api_dashboard_top_movers, name='api_dashboard_top_movers'),
    path('dashboard/stats', views.api_dashboard_stats, name='api_dashboard_stats'),
    path('dashboard/portfolio', views.api_dashboard_portfolio, name='api_dashboard_portfolio'),
    path('dashboard/trade-history', views.api_dashboard_trade_history, name='api_dashboard_trade_history'),

    # Trades
    path('trades/open', views.api_trades_open, name='api_trades_open'),
    path('trades/history', views.api_trades_history, name='api_trades_history'),
    path('trades/close', views.api_trades_close, name='api_trades_close'),
    path('trades/<int:trade_id>', views.api_trades_delete, name='api_trades_delete'),
    path('trades/mode', views.api_trades_mode, name='api_trades_mode'),
    path('trades/update-sltp', views.api_trades_update_sltp, name='api_trades_update_sltp'),

    # Option Chain
    path('chain/symbols', views.api_chain_symbols, name='api_chain_symbols'),
    path('chain/dates/<str:symbol>', views.api_chain_dates, name='api_chain_dates'),
    path('chain/expiries/<str:symbol>/<str:date>', views.api_chain_expiries, name='api_chain_expiries'),
    path('chain/chain/<str:symbol>/<str:date>/<str:expiry>', views.api_chain_chain, name='api_chain_chain'),
    path('chain/live/<str:symbol>', views.api_chain_live, name='api_chain_live'),
    path('chain/place-trade', views.api_chain_place_trade, name='api_chain_place_trade'),

    # Backtest
    path('backtest/run', views.api_backtest_run, name='api_backtest_run'),

    # Strategies
    path('strategies/save', views.api_strategies_save, name='api_strategies_save'),
    path('strategies/list', views.api_strategies_list, name='api_strategies_list'),
    path('strategies/<int:strat_id>', views.api_strategies_detail, name='api_strategies_detail'),
    path('strategies/<int:strat_id>/delete', views.api_strategies_delete, name='api_strategies_delete'),

    # Bhavcopy
    path('bhavcopy/dates/<str:symbol>', views.api_bhavcopy_dates, name='api_bhavcopy_dates'),

    # Scanner
    path('scanner/scan-all', views.api_scanner_scan_all, name='api_scanner_scan_all'),
    path('scanner/opportunities', views.api_scanner_opportunities, name='api_scanner_opportunities'),

    # Brokers
    path('broker/list', views.api_broker_list, name='api_broker_list'),
    path('broker/save-config', views.api_broker_save_config, name='api_broker_save_config'),
    path('broker/connect', views.api_broker_connect, name='api_broker_connect'),
    path('broker/account', views.api_broker_account, name='api_broker_account'),
    path('broker/fyers-auth-url', views.api_broker_fyers_auth_url, name='api_broker_fyers_auth_url'),
    path('broker/fyers-auth', views.api_broker_fyers_auth, name='api_broker_fyers_auth'),
    path('broker/auto-connect', views.api_broker_auto_connect, name='api_broker_auto_connect'),
    path('broker/refresh-tokens', views.api_broker_refresh_tokens, name='api_broker_refresh_tokens'),
]
