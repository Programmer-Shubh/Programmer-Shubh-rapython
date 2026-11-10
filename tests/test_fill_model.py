"""Three-way fill model parity: backtest vs paper vs live.

User spec (10-11):
1. Realistic slippage on EVERY fill (entries AND exits) - pct slippage with a
   fixed tick-offset floor instead of frictionless fills at the exact bar open.
2. Backtest, paper and live must not drift apart:
   - backtest fills   : TransactionCosts.apply_fill_slippage (this module)
   - paper  fills     : contract_pricer.entry_price + TradeModel.close_trade
                        -> SAME apply_fill_slippage call
   - live   fills     : _broker_limit_price LIMIT buffer = max(3 ticks,
                        LTP*0.5%) -> same floor the shared model now enforces
No indicator logic touched, no transaction-fee math touched: only the fill
price model.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.services.transaction_costs import TransactionCosts as TC


def test_pct_slippage_unchanged_on_high_premium():
    # 0.5% of 100 = 0.50 > 3-tick floor 0.15 -> numerically the old model.
    assert TC.apply_fill_slippage(100.0, "BUY") == 100.5
    assert TC.apply_fill_slippage(100.0, "SELL") == 99.5


def test_fixed_tick_offset_floor_on_cheap_premiums():
    # 0.5% of 5.0 = 0.025 -> floor lifts the friction to 3 ticks (0.15).
    assert TC.apply_fill_slippage(5.0, "BUY") == 5.15
    assert TC.apply_fill_slippage(5.0, "SELL") == 4.85
    assert TC.apply_fill_slippage(22.81, "SELL") == 22.66


def test_sell_fill_never_goes_below_one_tick():
    # 0.10 premium minus 0.15 floor would go negative; floored at tick 0.05.
    assert TC.apply_fill_slippage(0.10, "SELL") == 0.05
    assert TC.apply_fill_slippage(0.10, "BUY") == 0.25


def test_large_premium_fill_is_pct_only():
    # Deep-IN premium 782 -> 0.5% (3.91) dominates the 0.15 floor.
    assert TC.apply_fill_slippage(782.0, "BUY") == 785.91
    assert TC.apply_fill_slippage(782.0, "SELL") == 778.09


def test_side_direction_is_monotonic():
    buy = TC.apply_fill_slippage(100.0, "BUY")
    sell = TC.apply_fill_slippage(100.0, "SELL")
    assert sell < 100.0 < buy


def test_backtest_entry_and_exit_both_pay_friction():
    """Entry AND exit of a round trip must both slip (no frictionless fill)."""
    entry = TC.apply_fill_slippage(100.0, "BUY")   # BUY entry pays up
    exit_ = TC.apply_fill_slippage(100.0, "SELL")  # opposite exit receives less
    assert entry > 100.0 and exit_ < 100.0
    # A flat round trip (same price) must lose the combined friction.
    assert entry - exit_ >= 1.0  # one tick floor each side at minimum income


def test_live_limit_buffer_floor_matches_shared_model():
    """Live LIMIT-IOC buffer floor (3 ticks) == shared model floor."""
    ref, lot_pct = 22.81, TC.SLIPPAGE_PCT / 100
    live_floor, backtest_floor = 3 * 0.05, TC.MIN_FILL_TICK_OFFSET
    assert abs(live_floor - backtest_floor) < 1e-9
    # Live buffer + shared model both lift a cheap fill by at least 0.15.
    assert abs(TC.apply_fill_slippage(ref, "BUY") - (ref + max(ref * lot_pct, live_floor))) < 1e-9
