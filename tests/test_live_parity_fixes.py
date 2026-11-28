"""Parity guards: live/paper worker must follow the SAME setup as backtest.

- Strategy-level entry window (IST) enforced in paper worker.
- Multi-leg engine trades fire ALL legs with the engine's per-leg strikes,
  instead of being dropped (spread position string) or collapsing to one ATM.
Run: python -m pytest tests/test_live_parity_fixes.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.auto_paper import _entry_window_ok_for, _legs_for_backtest_trade


def test_paper_entry_window_matches_backtest_window():
    notes = []
    adv = {"entry_window": {"start": "12:30", "end": "15:00",
                            "label": "Afternoon Session"}, "entry_time": "09:35"}
    ok, _ = _entry_window_ok_for(adv, notes, _now="2026-11-27T10:00:00+05:30")
    assert ok is False and any("pehle" in n for n in notes)
    ok, _ = _entry_window_ok_for(adv, [], _now="2026-11-27T12:29:00+05:30")
    assert ok is False
    ok, _ = _entry_window_ok_for(adv, [], _now="2026-11-27T12:30:00+05:30")
    assert ok is True
    ok, _ = _entry_window_ok_for(adv, [], _now="2026-11-27T14:45:00+05:30")
    assert ok is True
    notes2 = []
    ok, _ = _entry_window_ok_for(adv, notes2, _now="2026-11-27T15:01:00+05:30")
    assert ok is False and any("baad" in n for n in notes2)
    # Legacy strategies (no entry_window) keep the old till-15:00 rule.
    ok, _ = _entry_window_ok_for({"entry_time": "09:35"}, [],
                                 _now="2026-11-27T12:42:00+05:30")
    assert ok is True


def test_spread_trade_maps_all_legs_with_engine_strikes():
    legs = [
        {"option_type": "PE", "transaction": "sell", "strike_selection": "otm", "otm_distance": 2},
        {"option_type": "PE", "transaction": "buy", "strike_selection": "otm", "otm_distance": 5},
    ]
    trade = {"is_spread": True, "option_type": "Spread",
             "position": "Sell PE 25000 + Buy PE 24800",
             "legs_detail": [
                 {"option_type": "PE", "transaction": "sell", "strike": 25000, "premium": 120.0},
                 {"option_type": "PE", "transaction": "buy", "strike": 24800, "premium": 40.0},
             ]}
    mapped, is_spread = _legs_for_backtest_trade(trade, legs)
    assert is_spread is True
    assert len(mapped) == 2
    assert [l["strike_override"] for l in mapped] == [25000.0, 24800.0]
    assert [l["_signal_price"] for l in mapped] == [120.0, 40.0]
    # Single leg: old matching behaviour preserved.
    single, sp = _legs_for_backtest_trade(
        {"option_type": "CE", "position": "SELL"}, legs + [
            {"option_type": "CE", "transaction": "sell"}])
    assert sp is False and len(single) == 1 and single[0]["option_type"] == "CE"


def test_intraday_signal_fires_every_spread_leg(monkeypatch):
    import datetime as _dt
    import json
    from core.services import auto_paper as ap
    from core.services.backtest_engine import BacktestEngine

    today = "2026-11-27"
    bars = []
    for k in range(32):
        h, m = 9 + (k + 1) // 12, (k + 1) % 12 * 5
        px = 25000.0 + k
        bars.append({"trade_date": today, "bar_time": f"{h:02d}:{m:02d}",
                     "open_price": px, "high_price": px + 2,
                     "low_price": px - 2, "close_price": px + 1,
                     "volume": 1000})
    monkeypatch.setattr("core.services.scanner.OptionScanner._fetch_intraday_bars",
                        lambda self, sym, tf: bars)
    monkeypatch.setattr(BacktestEngine, "run",
                        lambda self, *a, **k: {
                            "trade_list": [{
                                "entry_date": today, "entry_time": "12:35",
                                "strike": 25000, "entry_price": 6000.0,
                                "option_type": "Spread", "is_spread": True,
                                "position": "Sell PE 25000 + Buy PE 24800",
                                "legs_detail": [
                                    {"option_type": "PE", "transaction": "sell",
                                     "strike": 25000, "premium": 120.0},
                                    {"option_type": "PE", "transaction": "buy",
                                     "strike": 24800, "premium": 40.0},
                                ]}], "metrics": {}})
    ap._INTRA_CACHE.clear()
    s = {"id": 999001, "symbol": "NIFTY",
         "legs": json.dumps([
             {"option_type": "PE", "transaction": "sell",
              "strike_selection": "otm", "otm_distance": 2},
             {"option_type": "PE", "transaction": "buy",
              "strike_selection": "otm", "otm_distance": 5}]),
         "advanced_options": json.dumps({"timeframe": "5m", "trade_mode": "intraday"}),
         "risk_management": "{}", "indicators": "[]"}
    fired, why, strike, price = ap._intraday_signal(999001, s, today)
    assert fired and len(fired) == 2, (fired, why)
    assert [l["strike_override"] for l in fired] == [25000.0, 24800.0]
    assert strike is None  # no single shared strike for a spread
    assert price == 0.0


def test_spread_siblings_exit_together(monkeypatch):
    """One leg's SL/TP must close every leg of the same spread signal."""
    import importlib.util
    import json
    root = os.path.join(os.path.dirname(__file__), "..")
    path = os.path.join(root, "scripts", "auto_trade.py")
    spec = importlib.util.spec_from_file_location("ratrade_at_parity", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    trades = [
        {"id": 1, "symbol": "NIFTY", "option_type": "PE", "strike_price": 25000,
         "expiry_date": "", "transaction_type": "SELL", "entry_price": 100.0,
         "stop_loss": 10.0, "target": 10.0, "quantity": 1, "lot_size": 1,
         "sl_unit": "pt", "tp_unit": "pt", "strategy_id": 0,
         "auto_action": json.dumps({"spread_group": "g1"})},
        {"id": 2, "symbol": "NIFTY", "option_type": "PE", "strike_price": 24800,
         "expiry_date": "", "transaction_type": "BUY", "entry_price": 50.0,
         "stop_loss": 10.0, "target": 10.0, "quantity": 1, "lot_size": 1,
         "sl_unit": "pt", "tp_unit": "pt", "strategy_id": 0,
         "auto_action": json.dumps({"spread_group": "g1"})},
    ]
    px = {25000: 120.0, 24800: 45.0}   # only leg-1 breaches its SL
    closed_calls = []

    class _TM:
        def get_open_trades(self):
            return [dict(t) for t in trades]

        def get_option_premium(self, sym, opt, strike, expiry):
            return px.get(float(strike), 0.0)

        def close_trade(self, tid, exit_px, date, reason):
            closed_calls.append((int(tid), reason))

    monkeypatch.setattr(mod, "TradeModel", _TM)
    monkeypatch.setattr(mod, "log_exit_skip", lambda *a, **k: None, raising=False)
    out = mod.check_stoploss_target(confirm_secs=0)
    assert any(c[1] == "stoploss" for c in closed_calls), out
    assert {c[0] for c in closed_calls} == {1, 2}, closed_calls
    assert all(c[1] == "stoploss" for c in closed_calls)
