"""SL/TP parity: ONE helper, identical levels for backtest + paper monitor."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from utils.helpers import sltp_premium_level as lvl


def test_buy_rupees():
    # entry 50, SL 1500, 1x50 -> 30 pts: SL 20 / TP 80
    assert lvl(50, 1500, 1, 50, "BUY", True) == 20.0
    assert lvl(50, 1500, 1, 50, "BUY", False) == 80.0


def test_sell_rupees():
    # entry 56.23, SL 1500, 1x50 -> 30 pts
    assert lvl(56.23, 1500, 1, 50, "SELL", True) == pytest.approx(86.23)
    assert lvl(56.23, 1500, 1, 50, "SELL", False) == pytest.approx(26.23)


def test_percent_legacy():
    assert lvl(100, 10, 1, 50, "BUY", True) == 90.0
    assert lvl(100, 10, 1, 50, "BUY", False) == pytest.approx(110.0)
    assert lvl(100, 10, 1, 50, "SELL", True) == pytest.approx(110.0)
    assert lvl(100, 10, 1, 50, "SELL", False) == 90.0


def test_disabled_and_floor():
    assert lvl(50, 0, 1, 50, "BUY", True) == 0.0
    assert lvl(50, None, 1, 50, "BUY", True) == 0.0
    # deep TP for sell floored at tick, never negative
    assert lvl(5, 100000, 1, 50, "SELL", False) == 0.05


def test_matches_backtest_and_monitor_math():
    # Old backtest inline: pts=amt/units; buy SL=entry-pts ...
    # Old monitor inline: identical. Helper must reproduce exactly.
    cases = [(50, 1500, 1, 50, "BUY"), (50, 1500, 1, 50, "SELL"),
             (22.84, 1500, 2, 15, "BUY"), (120, 3000, 1, 75, "SELL"),
             (100, 10, 1, 50, "BUY"), (100, 10, 1, 50, "SELL")]
    for entry, amt, q, lot, txn in cases:
        for is_sl in (True, False):
            got = lvl(entry, amt, q, lot, txn, is_sl)
            units = max(q * lot, 1)
            if amt > 20:
                pts = amt / units
                if txn == "BUY":
                    exp = (entry - pts) if is_sl else (entry + pts)
                else:
                    exp = (entry + pts) if is_sl else (entry - pts)
                exp = max(0.05, exp)
            else:
                if txn == "BUY":
                    exp = entry * (1 - amt / 100) if is_sl else entry * (1 + amt / 100)
                else:
                    exp = entry * (1 + amt / 100) if is_sl else entry * (1 - amt / 100)
            assert abs(got - exp) < 1e-9, (entry, amt, q, lot, txn, is_sl, got, exp)


def test_lowercase_txn_like_engine():
    # BacktestEngine passes 'buy'/'sell' lowercase
    assert lvl(50, 1500, 75, 1, "buy", True) == 30.0
    assert lvl(50, 1500, 75, 1, "sell", True) == 70.0
