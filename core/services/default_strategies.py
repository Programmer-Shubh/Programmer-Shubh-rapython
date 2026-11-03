# -*- coding: utf-8 -*-
"""Default NIFTY strategies - always present, protected (not deletable).
Seeded on app startup. User can click to place paper/live orders."""

DEFAULT_STRATEGIES = [
    {
        "name": "NIFTY Intraday Sell PE + VIX Filter",
        "symbol": "NIFTY",
        "start_date": "2026-08-01",
        "end_date": "2026-10-30",
        "timeframe": "5m",
        "description": "Default NIFTY intraday option selling strategy. Supertrend+RSI + VIX filter (skip high-vol days). Sell PE in uptrend. 6 lots, OTM 4, SL 6000. WR 84.62%, Net +31.9K.",
        "indicators": [
            {"id": "supertrend", "params": {"period": 10, "multiplier": 3}},
            {"id": "rsi", "params": {"period": 14}},
        ],
        "entry_conditions": [],
        "exit_conditions": [],
        "legs": [
            {"option_type": "PE", "transaction": "sell", "lots": 6,
             "strike_selection": "otm", "otm_distance": 4, "expiry": "weekly"}
        ],
        "advanced_options": {
            "trade_mode": "intraday", "timeframe": "5m",
            "entry_time": "09:35", "exit_time": "15:15",
            "slippage_pct": 0.5, "strike_selection": "otm", "otm_distance": 4,
            "max_holding_bars": 1000000,
        },
        "risk_management": {
            "daily_stop_loss": 6000, "daily_take_profit": 0,
            "max_trades_per_day": 8, "signal_lookback_days": 0,
        },
        "status": "active",
        "protected": 1,
    },
    {
        "name": "NIFTY PE Sell + Market Regime (Auto)",
        "symbol": "NIFTY",
        "start_date": "2026-08-01",
        "end_date": "2026-10-30",
        "timeframe": "5m",
        "description": "Default NIFTY PE sell with Market Regime auto bullish/bearish filter. Supertrend+RSI+MarketRegime. 6 lots, OTM 4, SL 6000. WR 57.14%, Net +14.7K.",
        "indicators": [
            {"id": "supertrend", "params": {"period": 10, "multiplier": 3}},
            {"id": "rsi", "params": {"period": 14}},
            {"id": "market_regime", "params": {"vwap_period": 20, "ema_fast": 20, "ema_slow": 50, "adx_period": 14, "adx_threshold": 20}},
        ],
        "entry_conditions": [],
        "exit_conditions": [],
        "legs": [
            {"option_type": "PE", "transaction": "sell", "lots": 6,
             "strike_selection": "otm", "otm_distance": 4, "expiry": "weekly"}
        ],
        "advanced_options": {
            "trade_mode": "intraday", "timeframe": "5m",
            "entry_time": "09:35", "exit_time": "15:15",
            "slippage_pct": 0.5, "strike_selection": "otm", "otm_distance": 4,
            "max_holding_bars": 1000000,
        },
        "risk_management": {
            "daily_stop_loss": 6000, "daily_take_profit": 0,
            "max_trades_per_day": 8, "signal_lookback_days": 0,
        },
        "status": "active",
        "protected": 1,
    },
]


def seed_default_strategies():
    """Seed default NIFTY strategies (always present, protected). Idempotent."""
    import json as _js
    from core.models.database import Database
    db = Database.get_instance()
    # Ensure protected column exists
    try:
        db.execute("ALTER TABLE strategies ADD COLUMN protected INTEGER DEFAULT 0")
    except Exception:
        pass
    for _sc in DEFAULT_STRATEGIES:
        try:
            _existing = db.fetch_one("SELECT id FROM strategies WHERE name=?", [_sc["name"]])
            if _existing:
                # Update protected flag and settings (always re-save)
                db.execute(
                    """UPDATE strategies SET symbol=?, start_date=?, end_date=?, timeframe=?,
                       description=?, indicators=?, entry_conditions=?, exit_conditions=?,
                       legs=?, advanced_options=?, risk_management=?, status=?, protected=1
                       WHERE id=?""",
                    [_sc["symbol"], _sc["start_date"], _sc["end_date"], _sc["timeframe"],
                     _sc["description"], _js.dumps(_sc["indicators"]),
                     _js.dumps(_sc["entry_conditions"]), _js.dumps(_sc["exit_conditions"]),
                     _js.dumps(_sc["legs"]), _js.dumps(_sc["advanced_options"]),
                     _js.dumps(_sc["risk_management"]), _sc["status"], _existing["id"]])
            else:
                db.execute(
                    """INSERT INTO strategies (user_id,name,symbol,start_date,end_date,timeframe,description,indicators,entry_conditions,exit_conditions,legs,advanced_options,risk_management,status,protected)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [1, _sc["name"], _sc["symbol"], _sc["start_date"], _sc["end_date"], _sc["timeframe"],
                     _sc["description"], _js.dumps(_sc["indicators"]),
                     _js.dumps(_sc["entry_conditions"]), _js.dumps(_sc["exit_conditions"]),
                     _js.dumps(_sc["legs"]), _js.dumps(_sc["advanced_options"]),
                     _js.dumps(_sc["risk_management"]), _sc["status"], _sc["protected"]])
        except Exception as e:
            try:
                import sys as _sys
                print(f"[seed] {_sc['name']}: {e}", file=_sys.stderr, flush=True)
            except Exception:
                pass
