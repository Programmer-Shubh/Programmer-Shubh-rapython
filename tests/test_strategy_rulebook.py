# -*- coding: utf-8 -*-
"""Master Strategy Rulebook tests (user rulebook 16-11-2026).

Rules:
  1) All-day active (no daily cutoff / no daily limit) - engine me
     max_trades_per_day=-1, daily_loss_limit=0 (already shipped).
  2) Strict trend gate: ADX(14) > 22 + VWAP direction (CE upar, PE neeche).
  3) Tight per-index SL/TP (points): NIFTY 15/20, FINNIFTY 18/22,
     BANKNIFTY 25/35.
  4) Tick size 0.05 on fills/levels.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.strategy_rulebook import (
    ADX_MIN, INDEX_SLTP_POINTS, index_sltp_points, round_tick, strict_trend_ok,
)
from utils.helpers import resolve_strategy_sltp


def test_per_index_sltp_points():
    assert index_sltp_points("NIFTY") == (15.0, 20.0)
    assert index_sltp_points("finnifty") == (18.0, 22.0)
    assert index_sltp_points("BANKNIFTY") == (25.0, 35.0)
    assert index_sltp_points("RELIANCE") is None


def test_resolver_uses_index_defaults_when_no_sltp_set():
    sl, sl_u, tp, tp_u = resolve_strategy_sltp({}, {}, symbol="NIFTY")
    assert (sl, sl_u) == (15.0, "pt")
    assert (tp, tp_u) == (20.0, "pt")
    sl, sl_u, tp, tp_u = resolve_strategy_sltp({}, {}, symbol="BANKNIFTY")
    assert (sl, sl_u) == (25.0, "pt")
    assert (tp, tp_u) == (35.0, "pt")


def test_resolver_keeps_user_explicit_sltp():
    rm = {"daily_stop_loss": 40, "daily_take_profit": 60,
          "sl_unit": "pt", "tp_unit": "pt"}
    sl, sl_u, tp, tp_u = resolve_strategy_sltp(rm, {}, symbol="NIFTY")
    assert (sl, sl_u) == (40.0, "pt")
    assert (tp, tp_u) == (60.0, "pt")
    # percent bhi priority me
    rm = {"daily_stop_loss_pct": 30, "daily_take_profit_pct": 50}
    sl, sl_u, tp, tp_u = resolve_strategy_sltp(rm, {}, symbol="NIFTY")
    assert (sl, sl_u) == (30.0, "pct")
    assert (tp, tp_u) == (50.0, "pct")


def test_non_index_keeps_rs_default():
    sl, sl_u, tp, tp_u = resolve_strategy_sltp({}, {}, symbol="RELIANCE")
    assert sl_u == "rs" and tp_u == "rs"


def test_strict_trend_gate_adx_threshold():
    # ADX strictly above 22 only
    assert strict_trend_ok(22.5, 101.0, 100.0, "CE", "BUY") is True
    assert strict_trend_ok(22.0, 101.0, 100.0, "CE", "BUY") is False
    assert strict_trend_ok(15.0, 101.0, 100.0, "CE", "BUY") is False
    assert strict_trend_ok(None, 101.0, 100.0, "CE", "BUY") is False


def test_strict_trend_gate_vwap_direction():
    # CE long: close VWAP se upar hona chahiye
    assert strict_trend_ok(30.0, 101.0, 100.0, "CE", "BUY") is True
    assert strict_trend_ok(30.0, 99.0, 100.0, "CE", "BUY") is False
    # PE long: close VWAP se neeche hona chahiye
    assert strict_trend_ok(30.0, 99.0, 100.0, "PE", "BUY") is True
    assert strict_trend_ok(30.0, 101.0, 100.0, "PE", "BUY") is False
    # CE short (sell): neeche
    assert strict_trend_ok(30.0, 99.0, 100.0, "CE", "SELL") is True
    assert strict_trend_ok(30.0, 101.0, 100.0, "CE", "SELL") is False


def test_tick_rounding():
    assert round_tick(100.123) == 100.10
    assert round_tick(100.126) == 100.15
    assert round_tick(0) == 0
    assert ADX_MIN == 22.0
    assert INDEX_SLTP_POINTS["FINNIFTY"] == (18.0, 22.0)


# ---- Rule 5: Buy/Sell default profiles -------------------------------------
from core.services.strategy_rulebook import (  # noqa: E402
    BUY_PROFILE, SELL_PROFILE, apply_profile_defaults, default_profile,
    legs_side, profile_sltp_points,
)
from utils.helpers import trailing_lock_level  # noqa: E402


def test_profile_ranges_match_rulebook():
    assert SELL_PROFILE["entry_time"] == "10:15"
    assert SELL_PROFILE["sl_points"] == (12.0, 15.0)
    assert SELL_PROFILE["tp_points"] == (15.0, 20.0)
    assert SELL_PROFILE["max_trades_per_day"] == 5
    assert SELL_PROFILE["reentry_on_sl"] == "No Re-entry"
    assert (SELL_PROFILE["lock_profit_at_pts"], SELL_PROFILE["lock_profit_pts"]) == (10.0, 5.0)

    assert BUY_PROFILE["sl_points"] == (10.0, 12.0)
    assert BUY_PROFILE["tp_points"] == (25.0, 30.0)
    assert BUY_PROFILE["max_trades_per_day"] == 2
    assert BUY_PROFILE["strike_selection"] == "atm"
    assert (BUY_PROFILE["lock_profit_at_pts"], BUY_PROFILE["lock_profit_pts"]) == (12.0, 6.0)
    assert BUY_PROFILE["trail_step_pts"] == 5.0


def test_legs_side_detection():
    assert legs_side([{"transaction": "sell"}]) == "sell"
    assert legs_side([{"transaction": "buy"}, {"transaction": "sell"}]) == "sell"
    assert legs_side([{"transaction": "buy"}]) == "buy"
    assert legs_side([]) == "buy"
    assert default_profile("SELL")["max_trades_per_day"] == 5


def test_profile_sltp_index_override_wins_for_both_sides():
    assert profile_sltp_points("buy", "NIFTY") == (15.0, 20.0)
    assert profile_sltp_points("sell", "BANKNIFTY") == (25.0, 35.0)
    # Non-index: profile ranges (SL lower edge, TP midpoint)
    assert profile_sltp_points("sell", "RELIANCE") == (12.0, 17.5)
    assert profile_sltp_points("buy", "RELIANCE") == (10.0, 27.5)


def test_apply_defaults_sell_profile():
    risk, adv = apply_profile_defaults(
        {}, {"trade_mode": "intraday"},
        [{"option_type": "PE", "transaction": "sell"}], "NIFTY")
    assert (risk["daily_stop_loss"], risk["sl_unit"]) == (15.0, "pt")
    assert (risk["daily_take_profit"], risk["tp_unit"]) == (20.0, "pt")
    assert risk["max_trades_per_day"] == 5
    assert adv["entry_time"] == "10:15"
    assert adv["max_holding_bars"] == 0          # no intraday time exit
    assert adv["trailing"] == "5"
    assert adv["lock_profit_at"] == 10.0 and adv["lock_profit"] == 5.0
    assert adv["lock_profit_unit"] == "pt"
    assert adv["reentry_on_sl"] == "No Re-entry"
    assert adv["strict_trend"] is True


def test_apply_defaults_buy_profile_and_explicit_values_win():
    legs = [{"option_type": "CE", "transaction": "buy"}]
    risk, adv = apply_profile_defaults({}, {}, legs, "FINNIFTY")
    assert (risk["daily_stop_loss"], risk["daily_take_profit"]) == (18.0, 22.0)
    assert risk["max_trades_per_day"] == 2
    # explicit user values are never overridden
    risk2, adv2 = apply_profile_defaults(
        {"daily_stop_loss": 40, "sl_unit": "pt", "max_trades_per_day": -1},
        {"entry_time": "11:30", "trailing": "8", "lock_profit_at": 20},
        legs, "FINNIFTY")
    assert risk2["daily_stop_loss"] == 40
    assert risk2["max_trades_per_day"] == -1
    assert adv2["entry_time"] == "11:30"
    assert adv2["trailing"] == "8"
    assert adv2["lock_profit_at"] == 20


def test_trailing_lock_points_mode():
    # BUY: 12 pts profit -> lock 6 pts above entry
    assert trailing_lock_level(100.0, 112.0, True, 12, 6, unit="pt") == 106.0
    # not armed below trigger
    assert trailing_lock_level(100.0, 111.0, True, 12, 6, unit="pt") == 0.0
    # SELL: 10 pts profit -> lock 5 pts below entry
    assert trailing_lock_level(200.0, 190.0, False, 10, 5, unit="pt") == 195.0
    # legacy percent mode still works
    assert round(trailing_lock_level(100.0, 110.0, True, 10, 5), 2) == 105.0

