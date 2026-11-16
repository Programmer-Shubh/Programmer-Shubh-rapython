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
