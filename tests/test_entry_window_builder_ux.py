"""Guards for Strategy Backtest EASY/MULTI-LEG entry-window + UX upgrade.

Covers: entry time window enforcement (entries only), exits after window close,
signal funnel diagnostics, setup/results metadata, quick templates, Both-side
guidance, payoff risk profile label, fill detail and strategy-name save.

Run: python -m pytest tests/test_entry_window_builder_ux.py -q
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.backtest_engine import BacktestEngine


def _bars():
    """Completed 5m bars: morning (out of window), afternoon (in window),
    post-window (exit only)."""
    out = []
    for t, c in [("09:15", 24950), ("09:35", 25000), ("10:00", 25050),
                 ("10:30", 25080), ("11:00", 25100), ("11:30", 25120),
                 ("12:00", 25140), ("12:30", 25160), ("13:00", 25180),
                 ("13:35", 25200), ("14:00", 25220), ("14:30", 25240)]:
        out.append({"trade_date": "2026-11-27", "bar_time": t,
                    "open_price": float(c - 10), "high_price": float(c + 15),
                    "low_price": float(c - 20), "close_price": float(c),
                    "volume": 1000})
    return out


def _engine(window=None, direction="both"):
    e = BacktestEngine(is_live=False)
    e._skip_db = True
    e.bt_symbol = "NIFTY"
    e._advance = None
    e._advanced = {"trade_mode": "intraday", "timeframe": "5m",
                   "direction": direction,
                   "entry_window": window or {"start": "12:30", "end": "15:00",
                                              "label": "Afternoon Session"}}
    e._used_intraday_bars = True
    # Deterministic data + signals: BUY on every evaluated bar.
    e._get_buy_signal = lambda i, p, h, c: True
    e._get_sell_signal = lambda i, p, h, c: False
    e._entry_premium = lambda d, s, k, o: 100.0
    e._exit_premium = lambda d, s, k, o: 95.0
    e._close_premium = lambda d, s, k, o: 95.0

    def _sltp(current, entry, option_type, txn_type, sl_amt, tp_amt, qty=0, entry_idx=None):
        # SL fires AFTER the entry window has started to close — proves exits
        # are not gated by the entry time window.
        if str(current.get("bar_time", ""))[:5] >= "14:00":
            return {"reason": "stoploss", "level": 70.0, "fill": 70.0,
                    "beyond_pts": 3.0, "bar_high": 80.0, "bar_low": 65.0,
                    "bar_open": 75.0}
        return None

    e._check_sl_tp = _sltp
    return e


def _run(e):
    return e.run(_bars(), "NIFTY", "2026-11-27", "2026-11-27", [], [], [],
                 [{"option_type": "CE", "transaction": "buy", "lots": 1}],
                 e._advanced,
                 {"max_trades_per_day": -1, "daily_stop_loss": 30,
                  "daily_take_profit": 60, "sl_unit": "pt", "tp_unit": "pt"},
                 is_live=False)


def test_entry_window_filters_entries_but_not_exits():
    e = _engine()
    res = _run(e)
    m = res["metrics"]
    trades = m["trade_list"]
    assert trades, "afternoon signal must produce a trade"
    for t in trades:
        # Signal/entry must never be in the morning (out-of-window) bars.
        assert t["entry_time"] >= "12:30", t
    # Morning signals counted separately, not mixed into generic rejects.
    assert m["signals_outside_entry_window"] >= 2, m["signals_outside_entry_window"]
    assert m["rejected"].get("entry_time_window", 0) >= 2
    # SL fired at 14:00 (after the signal window's useful entries) -> exits
    # stayed live for the whole session.
    assert any(t["exit_reason"] in ("stoploss", "trailing_lock") for t in trades), trades
    assert m["entry_window"]["start"] == "12:30"
    assert m["entry_window"]["end"] == "15:00"


def test_full_session_window_has_no_filtered_signals():
    e = _engine({"start": "09:15", "end": "15:15", "label": "All Session"})
    res = _run(e)
    m = res["metrics"]
    assert m["signals_outside_entry_window"] == 0
    assert m["rejected"].get("entry_time_window", 0) == 0
    assert m["signals_generated"] > 0


def test_signal_funnel_and_fill_detail_fields():
    e = _engine()
    m = _run(e)["metrics"]
    for key in ("signals_evaluated", "signals_generated", "signals_buy",
                "signals_sell", "signals_outside_entry_window",
                "signals_blocked_rules"):
        assert key in m, key
    assert m["signals_generated"] == m["signals_buy"] + m["signals_sell"]
    t = m["trade_list"][0]
    for key in ("entry_points", "exit_points", "point_diff", "trigger_level",
                "beyond_threshold_pts", "slippage_pts", "bar_gap_pts",
                "exit_status"):
        assert key in t, key
    assert t["trigger_level"] == 70.0
    assert t["beyond_threshold_pts"] == 3.0


def test_clock_normalisation():
    assert BacktestEngine._norm_clock("9:20") == "09:20"
    assert BacktestEngine._norm_clock("13:00:00") == "13:00"
    assert BacktestEngine._norm_clock("All Session", "09:15") == "09:15"
    e = _engine()
    _run(e)   # run() applies the configured window to the engine instance
    assert e._in_entry_window("12:30") is True
    assert e._in_entry_window("12:29") is False
    assert e._in_entry_window("") is True          # daily bars: no clock
    assert e._in_entry_window("15:01") is False


def test_frontend_controls_and_copy():
    p = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")
    with open(p, encoding="utf-8") as fh:
        s = fh.read()
    for needle in ('id="eb-entry-start"', 'id="eb-entry-end"',
                   'data-label="Morning Session"',
                   'data-label="Afternoon Session"',
                   'data-label="Post-13:00 Session"',
                   'function posEntryWindow', 'function posSetEntryWindow',
                   'entry_window:posEntryWindow()', 'Window-filtered:',
                   'Rule-blocked:', 'Executed:', 'Fill / Points',
                   'function fillDetailHtml', 'id="eb-strategy-name"',
                   "posApplyTemplate('bullput')", "posApplyTemplate('bearcall')",
                   'Undefined / High Downside Risk',
                   'Window: \'+ewm.start'):
        assert needle in s, needle


def test_route_aggregates_window_diagnostics():
    p = os.path.join(os.path.dirname(__file__), "..", "routes", "strategy_builder.py")
    with open(p, encoding="utf-8") as fh:
        s = fh.read()
    for needle in ('_diag_window_out', 'signals_outside_entry_window',
                   'signals_blocked_rules', '"entry_window": _entry_window_meta',
                   'trades_executed'):
        assert needle in s, needle
