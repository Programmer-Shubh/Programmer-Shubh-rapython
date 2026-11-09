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
        "description": "Default NIFTY intraday option selling strategy. Supertrend+RSI + VIX filter (skip high-vol days). Sell PE in uptrend. 1 lot, OTM 4, SL 6000. WR 84.62%, Net +31.9K.",
        "indicators": [
            {"id": "supertrend", "params": {"period": 10, "multiplier": 3}},
            {"id": "rsi", "params": {"period": 14}},
        ],
        "entry_conditions": [],
        "exit_conditions": [],
        "legs": [
            {"option_type": "PE", "transaction": "sell", "lots": 1,
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
            # -1 = unlimited (daily limits removed, user rule 2026-11)
            "max_trades_per_day": -1, "daily_loss_limit": 0,
            "signal_lookback_days": 0,
        },
        "status": "active",
        "protected": 1,
    },
    # --- User request (09-11-2026): SENSEX + MIDCPNIFTY BUY-option strategies
    # backtest karke save. Direction-aware BUY legs (transaction='auto'):
    # bullish par CE BUY, bearish par PE BUY. Har config 13-Aug..06-Nov-2026
    # 5m data par sweep karke best-balanced (WR x profit) chuna gaya.
    {
        "name": "SENSEX Buy Option (Auto CE/PE) - Tight TP",
        "symbol": "SENSEX",
        "start_date": "2026-08-13",
        "end_date": "2026-11-06",
        "timeframe": "5m",
        "description": ("Direction-aware BUY (CE on bullish, PE on bearish) via Supertrend+RSI 5m. "
                        "Analytical backtest 13-Aug..06-Nov-2026: 31 trades, WR 77.42%, net +7,349, PF 1.58 "
                        "(SL 30% / TP 15% of premium, 0.5% slippage). User-configured; no buy/sell "
                        "recommendation; past results do not guarantee future live returns."),
        "indicators": [
            {"id": "supertrend", "params": {"period": 10, "multiplier": 3}},
            {"id": "rsi", "params": {"period": 14}},
        ],
        "entry_conditions": [],
        "exit_conditions": [],
        "legs": [
            {"option_type": "CE", "transaction": "auto", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly"}
        ],
        "advanced_options": {
            "trade_mode": "intraday", "timeframe": "5m",
            "entry_time": "09:35", "exit_time": "15:15",
            "slippage_pct": 0.5, "strike_selection": "atm", "otm_distance": 0,
            "max_holding_bars": 1000000,
        },
        "risk_management": {
            "daily_stop_loss_pct": 30, "daily_take_profit_pct": 15,
            "max_trades_per_day": -1, "daily_loss_limit": 0,
            "signal_lookback_days": 0,
        },
        "status": "active",
        "protected": 1,
    },
    {
        "name": "SENSEX Buy Option (Auto CE/PE) - High Profit",
        "symbol": "SENSEX",
        "start_date": "2026-08-13",
        "end_date": "2026-11-06",
        "timeframe": "5m",
        "description": ("Direction-aware BUY (CE on bullish, PE on bearish) via Supertrend+RSI 5m. "
                        "Analytical backtest 13-Aug..06-Nov-2026: 25 trades, WR 56.00%, net +27,652, PF 2.44 "
                        "(SL 30% / TP 70% of premium, 0.5% slippage). User-configured; no buy/sell "
                        "recommendation; past results do not guarantee future live returns."),
        "indicators": [
            {"id": "supertrend", "params": {"period": 10, "multiplier": 3}},
            {"id": "rsi", "params": {"period": 14}},
        ],
        "entry_conditions": [],
        "exit_conditions": [],
        "legs": [
            {"option_type": "CE", "transaction": "auto", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly"}
        ],
        "advanced_options": {
            "trade_mode": "intraday", "timeframe": "5m",
            "entry_time": "09:35", "exit_time": "15:15",
            "slippage_pct": 0.5, "strike_selection": "atm", "otm_distance": 0,
            "max_holding_bars": 1000000,
        },
        "risk_management": {
            "daily_stop_loss_pct": 30, "daily_take_profit_pct": 70,
            "max_trades_per_day": -1, "daily_loss_limit": 0,
            "signal_lookback_days": 0,
        },
        "status": "active",
        "protected": 1,
    },
    {
        "name": "MIDCPNIFTY Buy Option (Auto CE/PE) - Tight TP",
        "symbol": "MIDCPNIFTY",
        "start_date": "2026-08-13",
        "end_date": "2026-11-06",
        "timeframe": "5m",
        "description": ("Direction-aware BUY (CE on bullish, PE on bearish) via Supertrend+RSI 5m, OTM-1. "
                        "Analytical backtest 13-Aug..06-Nov-2026: 36 trades, WR 66.67%, net +1,971, PF 1.11 "
                        "(SL 20% / TP 10% of premium, 0.5% slippage). User-configured; no buy/sell "
                        "recommendation; past results do not guarantee future live returns."),
        "indicators": [
            {"id": "supertrend", "params": {"period": 10, "multiplier": 3}},
            {"id": "rsi", "params": {"period": 14}},
        ],
        "entry_conditions": [],
        "exit_conditions": [],
        "legs": [
            {"option_type": "CE", "transaction": "auto", "lots": 1,
             "strike_selection": "otm", "otm_distance": 1, "expiry": "weekly"}
        ],
        "advanced_options": {
            "trade_mode": "intraday", "timeframe": "5m",
            "entry_time": "09:35", "exit_time": "15:15",
            "slippage_pct": 0.5, "strike_selection": "otm", "otm_distance": 1,
            "max_holding_bars": 1000000,
        },
        "risk_management": {
            "daily_stop_loss_pct": 20, "daily_take_profit_pct": 10,
            "max_trades_per_day": -1, "daily_loss_limit": 0,
            "signal_lookback_days": 0,
        },
        "status": "active",
        "protected": 1,
    },
    {
        "name": "MIDCPNIFTY Buy Option (Auto CE/PE) - Balanced",
        "symbol": "MIDCPNIFTY",
        "start_date": "2026-08-13",
        "end_date": "2026-11-06",
        "timeframe": "5m",
        "description": ("Direction-aware BUY (CE on bullish, PE on bearish) via Supertrend+RSI 5m, ATM. "
                        "Analytical backtest 13-Aug..06-Nov-2026: 29 trades, WR 58.62%, net +10,238, PF 1.71 "
                        "(SL 10% / TP 25% of premium, 0.5% slippage). User-configured; no buy/sell "
                        "recommendation; past results do not guarantee future live returns."),
        "indicators": [
            {"id": "supertrend", "params": {"period": 10, "multiplier": 3}},
            {"id": "rsi", "params": {"period": 14}},
        ],
        "entry_conditions": [],
        "exit_conditions": [],
        "legs": [
            {"option_type": "CE", "transaction": "auto", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0, "expiry": "weekly"}
        ],
        "advanced_options": {
            "trade_mode": "intraday", "timeframe": "5m",
            "entry_time": "09:35", "exit_time": "15:15",
            "slippage_pct": 0.5, "strike_selection": "atm", "otm_distance": 0,
            "max_holding_bars": 1000000,
        },
        "risk_management": {
            "daily_stop_loss_pct": 10, "daily_take_profit_pct": 25,
            "max_trades_per_day": -1, "daily_loss_limit": 0,
            "signal_lookback_days": 0,
        },
        "status": "active",
        "protected": 1,
    },
]


def _run_backtest_and_save(db, strat_id, _sc):
    """Run backtest for a default strategy and save result so BT detail always shows."""
    import json as _js
    try:
        from routes.strategy_builder import _run_backtest_core, BacktestRequest
        req = BacktestRequest(
            symbol=_sc["symbol"], symbols=[_sc["symbol"]],
            start_date=_sc["start_date"], end_date=_sc["end_date"],
            indicators=_sc["indicators"],
            entry_conditions=_sc["entry_conditions"],
            exit_conditions=_sc["exit_conditions"],
            legs=_sc["legs"],
            advanced=_sc["advanced_options"],
            risk=_sc["risk_management"],
        )
        res = _run_backtest_core(req) or {}
        m = res.get("metrics") or {}
        if isinstance(m, dict) and m.get("total_trades") is not None:
            db.execute(
                "UPDATE strategies SET last_backtest=?, last_backtest_at=datetime('now') WHERE id=?",
                [_js.dumps(res, default=str), strat_id],
            )
            # Also archive to strategy_results so _bt_summary fallback works
            try:
                from routes.strategies import _ensure_results_table, _archive_result
                _ensure_results_table()
                _archive_result(strat_id, res, "default-seed", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
            except Exception:
                pass
            return True
    except Exception as e:
        try:
            import sys as _sys
            print(f"[seed BT] {_sc['name']}: {e}", file=_sys.stderr, flush=True)
        except Exception:
            pass
    return False


def seed_default_strategies():
    """Seed default NIFTY strategies (always present, protected). Idempotent."""
    import json as _js
    import datetime as _dt
    from core.models.database import Database
    db = Database.get_instance()
    # Ensure protected column exists
    try:
        db.execute("ALTER TABLE strategies ADD COLUMN protected INTEGER DEFAULT 0")
    except Exception:
        pass
    for _sc in DEFAULT_STRATEGIES:
        try:
            _existing = db.fetch_one("SELECT id, last_backtest FROM strategies WHERE name=?", [_sc["name"]])
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
                # If no backtest result, run one and save it
                _need_bt = not _existing.get("last_backtest")
                if _need_bt:
                    _run_backtest_and_save(db, _existing["id"], _sc)
            else:
                db.execute(
                    """INSERT INTO strategies (user_id,name,symbol,start_date,end_date,timeframe,description,indicators,entry_conditions,exit_conditions,legs,advanced_options,risk_management,status,protected)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [1, _sc["name"], _sc["symbol"], _sc["start_date"], _sc["end_date"], _sc["timeframe"],
                     _sc["description"], _js.dumps(_sc["indicators"]),
                     _js.dumps(_sc["entry_conditions"]), _js.dumps(_sc["exit_conditions"]),
                     _js.dumps(_sc["legs"]), _js.dumps(_sc["advanced_options"]),
                     _js.dumps(_sc["risk_management"]), _sc["status"], _sc["protected"]])
                _row = db.fetch_one("SELECT id FROM strategies WHERE name=? ORDER BY id DESC LIMIT 1", [_sc["name"]])
                if _row:
                    _run_backtest_and_save(db, _row["id"], _sc)
        except Exception as e:
            try:
                import sys as _sys
                print(f"[seed] {_sc['name']}: {e}", file=_sys.stderr, flush=True)
            except Exception:
                pass
    # One-time data migration: existing saved strategies carried legacy caps
    # (max 5 trades/day, ₹5000 daily loss). Normalize them to unlimited so
    # Fyers keeps taking every valid signal all day.
    try:
        normalize_daily_limits(db)
    except Exception:
        pass


def normalize_daily_limits(db=None):
    """Set legacy daily caps on all saved strategies to UNLIMITED.

    max_trades_per_day: -1 (engine sentinel = unlimited)
    daily_loss_limit / daily_max_loss / max_daily_loss: 0 (disabled)
    Only writes when a legacy positive cap is found (idempotent).
    """
    import json as _js
    if db is None:
        from core.models.database import Database
        db = Database.get_instance()
    try:
        rows = db.fetch_all("SELECT id, risk_management FROM strategies")
    except Exception:
        return 0
    changed = 0
    for r in rows or []:
        try:
            rm = _js.loads(r.get("risk_management") or "{}")
            if not isinstance(rm, dict):
                continue
            dirty = False
            try:
                _mt = int(rm.get("max_trades_per_day", -1))
            except Exception:
                _mt = -1
            if _mt >= 0:
                rm["max_trades_per_day"] = -1
                dirty = True
            for _k in ("daily_loss_limit", "daily_max_loss", "max_daily_loss"):
                if _k in rm:
                    try:
                        if float(rm.get(_k) or 0) != 0:
                            rm[_k] = 0
                            dirty = True
                    except Exception:
                        rm[_k] = 0
                        dirty = True
            if dirty:
                db.execute("UPDATE strategies SET risk_management=? WHERE id=?",
                           [_js.dumps(rm), r.get("id")])
                changed += 1
        except Exception:
            continue
    return changed
