"""Trailing-lock rule (shared) + paper TP-at-level exit honesty.

User rules (11-Nov-2026):
1. "jab trade 10-15% profit me aaye, SL ko breakeven (cost price) par trail
    karo" + "profitable trades ko lock karne ke liye Trailing Stop-Loss".
2. Paper history fix: TP hit par exit LEVEL par hona chahiye (backtest
    parity) - current quote par exit se fake +40% target profit banta tha.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from utils.helpers import trailing_lock_level as T


# ---------------------------------------------------------------- trailing
def test_breakeven_lock_at_trigger():
    # BUY: +12% best pe SL cost price (100) par chala jata hai
    assert T(100.0, 112.0, True, 10.0, 0.0) == 100.0
    # SELL mirror: premium 88 (best) = +12% -> SL = entry
    assert T(100.0, 88.0, False, 10.0, 0.0) == 100.0


def test_profit_lock_amount():
    # lock_profit = 1% -> SL entry+1 (BUY), entry-1 (SELL)
    assert T(100.0, 112.0, True, 10.0, 1.0) == 101.0
    assert T(100.0, 88.0, False, 10.0, 1.0) == 99.0


def test_lock_not_armed_below_trigger():
    assert T(100.0, 105.0, True, 10.0, 0.0) == 0.0
    assert T(100.0, 109.9, True, 10.0, 0.0) == 0.0
    # invalid inputs
    assert T(0, 112, True, 10, 0) == 0.0
    assert T(100, 0, True, 10, 0) == 0.0
    assert T(100, 112, True, 0, 0) == 0.0


def test_trigger_boundary_is_inclusive():
    assert T(100.0, 110.0, True, 10.0, 0.0) == 100.0


# ------------------------------------------------------- paper TP-at-level
def test_paper_tp_exit_uses_level_not_far_quote():
    """Paper monitor: TP hit par exit LEVEL par (backtest parity)."""
    import inspect
    import scripts.auto_trade as at
    src = inspect.getsource(at.check_stoploss_target)
    assert "_exit_px = tp_level" in src, "TP must exit at the level"
    assert "_exit_px = current" in src, "SL exit keeps the observed quote"
    # backtest parity marker: reason 'trailing_lock' for a trailing exit
    assert "trailing_lock" in src


def test_paper_monitor_has_soft_spike_guard():
    import inspect
    import scripts.auto_trade as at
    src = inspect.getsource(at.check_stoploss_target)
    assert "_ratio > 1.6 or _ratio < 0.6" in src


def test_engine_reads_lock_profit_fields():
    """Engine pehle advanced.lock_profit_at / lock_profit IGNORE karta tha."""
    import inspect
    from core.services.backtest_engine import BacktestEngine
    src = inspect.getsource(BacktestEngine._check_sl_tp)
    assert "lock_profit_at" in src
    assert "lock_profit" in src
    assert "trailing_lock_level" in src
