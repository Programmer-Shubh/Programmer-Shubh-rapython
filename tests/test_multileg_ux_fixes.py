"""End-to-end guards for the 5 Strategy Backtest EASY • MULTI-LEG fixes.

1. Symbol/spot/step/lot/expiry single source (MIDCPNIFTY wrong-instrument bug)
2. Intraday candle-based SL/TP (no more EOD-only exits)
3. Trade Direction + per-leg Trigger On routing
4. Max DD from equity curve + data-coverage warning
5. Defined-risk multi-leg net P&L / net max loss

Run: python -m pytest tests/test_multileg_ux_fixes.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.backtest_engine import BacktestEngine
from utils.helpers import spot_sane, nearest_active_expiry
from core.services.symbol_resolver import FYERS_UNDERLYING


def _eng():
    e = BacktestEngine(is_live=False)
    e._skip_db = True
    e.bt_symbol = "FINNIFTY"
    e.bt_expiry = "2026-12-24"
    e._expiry_hint = "monthly"
    e._advanced = {}
    e._used_intraday_bars = False
    e._trade_mode = "intraday"
    return e


def test_midc_fyers_symbol_and_spot_sanity():
    """MIDCPNIFTY must never route to an EQ symbol (~63,040 quote).

    The real Fyers index symbol is NSE:MIDCPNIFTY-INDEX (live close ~14,360).
    NSE:NIFTY_MID_SELECT-INDEX / NSE:NIFTYMIDSELECT-INDEX are BOTH rejected by
    Fyers with code -300 (invalid symbol) and broke WS subscription.
    """
    assert FYERS_UNDERLYING["MIDCPNIFTY"] == "NSE:MIDCPNIFTY-INDEX"
    assert spot_sane("MIDCPNIFTY", 63040) is False
    assert spot_sane("MIDCPNIFTY", 14200) is True
    assert spot_sane("NIFTY", 24500) is True
    assert spot_sane("NIFTY", 63040) is False
    # Every MIDCPNIFTY live/history route must use the verified symbol.
    for path in ("core/services/fast_exit.py", "routes/strategy_builder.py",
                 "core/services/fyers_history_sync.py"):
        import os as _os
        p = os.path.join(os.path.dirname(__file__), "..", path)
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        assert "NSE:MIDCPNIFTY-INDEX" in src, path
    with open(os.path.join(os.path.dirname(__file__), "..", "core", "services",
                           "fast_exit.py"), encoding="utf-8") as fh:
        assert 'setdefault("NSE:MIDCPNIFTY-INDEX"' in fh.read()


def test_strict_expiry_guard_no_far_strike():
    e = _eng()
    guard = e._contract_guard("2026-11-28")
    assert guard is not None
    assert guard[0] == "2026-12-24"      # explicit selected expiry wins
    assert guard[1] == 1                 # max 1 strike step (no deep ITM)


def test_intraday_candle_band_evaluates_sltp():
    """No tick bar: underlying candle high/low -> option premium band via
    shared delta. SL hit must be detectable intraday, not only at EOD."""
    e = _eng()
    e._cur_bar_time = "12:05"
    e._sl_unit = "pt"
    e._tp_unit = "pt"
    entry = {"spot": 25000.0, "price": 200.0, "strike": 25000,
             "quantity": 75, "legs": [{"option_type": "CE"}]}
    cur = {"trade_date": "2026-11-28", "open_price": 25000,
           "high_price": 25100, "low_price": 24900,
           "close_price": 25080, "bar_time": "12:05"}
    bar = e._modelled_option_bar(cur, entry, "CE", "sell")
    assert bar and bar["high"] > bar["low"]
    assert bar["close"] > entry["price"]     # spot up -> CE premium up
    # SELL entry: SL above (200+30) and TP below (200-60). Candle band reaches
    # both now, so an intraday exit is possible.
    hit = e._check_sl_tp_levels("sell", 230.0, 140.0, bar["high"], bar["low"],
                                bar_open=bar["open"])
    assert hit is not None


def test_direction_and_trigger_on_routing():
    e = _eng()
    bull_leg = {"option_type": "CE", "transaction": "buy"}
    bear_leg = {"option_type": "CE", "transaction": "sell"}
    # Default: leg's natural bias (BUY CE = bullish).
    assert e._leg_triggered(bull_leg, True, False) is True
    assert e._leg_triggered(bull_leg, False, True) is False
    assert e._leg_triggered(bear_leg, False, True) is True
    assert e._leg_triggered(bear_leg, True, False) is False
    # Explicit trigger overrides natural bias (user chose two-way).
    assert e._leg_triggered({**bull_leg, "trigger_on": "both"}, False, True) is True
    assert e._leg_triggered({**bear_leg, "trigger_on": "buy"}, True, False) is True
    assert e._leg_bias({"option_type": "PE", "transaction": "sell"}) == "bull"


def test_max_dd_pct_alias_and_drawdown_math():
    e = _eng()
    eq = [1000000.0, 1000500.0, 1000200.0, 1000100.0]
    assert abs(e._max_drawdown(eq) - 0.04) < 0.001
    m = e._build_result("FINNIFTY", "2026-11-01", "2026-11-28", [], [])
    assert "max_drawdown_pct" in m["metrics"]
    assert "equity_curve" in m["metrics"]


def test_defined_risk_spread_bounds():
    e = _eng()
    entry = {"legs": [
        {"option_type": "PE", "type": "sell", "strike": 25000,
         "premium": 120.0, "quantity": 75, "signed_value": 9000.0},
        {"option_type": "PE", "type": "buy", "strike": 24800,
         "premium": 40.0, "quantity": 75, "signed_value": -3000.0},
    ]}
    r = e._spread_risk(entry)
    assert r["net_credit"] == 6000.0
    assert r["defined_risk"] is True
    assert r["net_max_loss"] is not None
    assert r["net_max_loss"] > 0
    assert len(r["legs_detail"]) == 2


def test_structure_bias_vertical_spreads():
    """A spread's direction depends on which strike is bought, not on each
    leg's isolated BUY/SELL label."""
    e = _eng()
    bull_call = [{"option_type": "CE", "transaction": "buy", "otm_distance": 0},
                 {"option_type": "CE", "transaction": "sell", "otm_distance": 2}]
    bear_call = [{"option_type": "CE", "transaction": "sell", "otm_distance": 2},
                 {"option_type": "CE", "transaction": "buy", "otm_distance": 5}]
    bull_put = [{"option_type": "PE", "transaction": "sell", "otm_distance": 2},
                {"option_type": "PE", "transaction": "buy", "otm_distance": 5}]
    assert e._structure_bias(bull_call) == "bull"
    assert e._structure_bias(bear_call) == "bear"
    assert e._structure_bias(bull_put) == "bull"
    assert e._structure_bias(bull_call + bear_call) == "neutral"  # 4 legs
    e._advanced = {"structure_bias": "bear"}
    assert e._structure_bias(bull_call) == "bear"  # explicit UI template wins


def test_spread_uses_one_structure_trigger():
    """Opposing legs must never fire on the same signal; all legs share one
    entry timestamp."""
    e = _eng()
    e._advanced = {}
    bull_put = [{"option_type": "PE", "transaction": "sell", "otm_distance": 2},
                {"option_type": "PE", "transaction": "buy", "otm_distance": 5}]
    assert e._spread_triggered(bull_put, True, False) is True
    assert e._spread_triggered(bull_put, False, True) is False
    # Explicit common trigger overrides the structure bias.
    both = [{**l, "trigger_on": "both"} for l in bull_put]
    assert e._spread_triggered(both, False, True) is True
    # Conflicting triggers collapse to structure bias (not half-leg firing).
    conflict = [{"option_type": "PE", "transaction": "sell", "otm_distance": 2,
                 "trigger_on": "buy"},
                {"option_type": "PE", "transaction": "buy", "otm_distance": 5,
                 "trigger_on": "sell"}]
    assert e._spread_triggered(conflict, True, False) is True
    assert e._spread_triggered(conflict, False, True) is False
    assert getattr(e, "_rej", {}).get("conflicting_leg_triggers", 0) == 2


def test_modelled_premium_respects_pe_direction():
    """CE premium rises with spot, PE premium falls (signed Black-Scholes
    delta). abs() here used to invert every PE intraday SL/TP evaluation."""
    e = _eng()
    e._cur_bar_time = "12:05"
    e._sl_unit = e._tp_unit = "pt"
    entry = {"spot": 25000.0, "price": 200.0, "strike": 25000,
             "quantity": 75, "legs": [{"option_type": "PE"}]}
    up = {"trade_date": "2026-11-28", "open_price": 25000, "high_price": 25100,
          "low_price": 24900, "close_price": 25080, "bar_time": "12:05"}
    down = {**up, "high_price": 25050, "low_price": 24880, "close_price": 24900}
    pe_up = e._modelled_option_bar(up, entry, "PE", "sell")
    pe_down = e._modelled_option_bar(down, entry, "PE", "sell")
    assert pe_up["close"] < entry["price"] < pe_down["close"]
    ce_up = e._modelled_option_bar(up, entry, "CE", "buy")
    assert ce_up["close"] > entry["price"]


def test_spread_net_points_per_spread_not_per_leg_unit():
    """SL 30 / TP 60 are premium points PER SPREAD, not per sum of all leg
    quantities (which halved a 2-leg credit and quartered a 4-leg condor)."""
    e = _eng()
    e.bt_symbol = "NIFTY"
    e._pricing_iv = lambda d, k, o, s: 0.2
    e._days_to_expiry = lambda d, *a, **k: 7
    e._entry_premium = lambda d, s, k, o: (
        120.0 if o == "PE" and k == 25000 else 40.0)
    legs = [{"option_type": "PE", "transaction": "sell", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0},
            {"option_type": "PE", "transaction": "buy", "lots": 1,
             "strike_selection": "otm", "otm_distance": 2}]
    tr = e._enter_spread("2026-11-28", 25000.0, "NIFTY", legs, "atm", None, 0)
    assert tr is not None and tr["is_spread"]
    # net credit ~80 pts per spread after fill slippage, NOT ~40 pts
    # (the old bug divided by the sum of both legs' quantities).
    assert 70 < tr["net_pts"] < 90, tr["net_pts"]
    assert tr["credit"] is True


def test_frontend_controls_present():
    p = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")
    s = open(p, encoding="utf-8").read()
    for needle in ('id="eb-direction"', "trigger_on", "posRenderDirectionMap",
                   "max_drawdown_pct", "dr.coverage_warning", "pos-payoff-note",
                   "/api/symbol/context/"):
        assert needle in s, needle


def test_route_wires_direction_and_trigger_on():
    p = os.path.join(os.path.dirname(__file__), "..", "routes", "strategy_builder.py")
    s = open(p, encoding="utf-8").read()
    for needle in ('direction: str = "both"', 'entry["trigger_on"]',
                   'advanced_in["direction"]', '"max_drawdown_pct"',
                   "DATA COVERAGE WARNING"):
        assert needle in s, needle
