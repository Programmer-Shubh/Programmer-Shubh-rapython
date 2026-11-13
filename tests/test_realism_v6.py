"""v6 REALISM tests - backtest ko paper/live ke qareeb laane wale fixes.

Covers:
  1. SHARED SL/TP resolver (backtest == paper == live, ek hi rule).
  2. Sirf COMPLETED candles: chal rahi (forming) candle drop hoti hai -
     engine aur paper worker dono.
  3. Strict intrabar option band: delta-based (vega-free) band BS-extreme
     band se CHHOTI hoti hai (unreal target hits nahi).
  4. Latency cost entry par lagta hai (aur global slippage mutate nahi hota).
  5. Strike selection LEG se aata hai (paper jaisa), advanced default nahi.
"""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.backtest_engine import BacktestEngine, ENGINE_VERSION
from utils.helpers import (resolve_strategy_sltp, drop_forming_bars, black_scholes,
                           bs_delta, model_iv)


def _bars(n=40, seed=7, start=datetime.date(2026, 6, 1), tf=None):
    import random
    rnd = random.Random(seed)
    bars = []
    px = 25000.0
    d = start
    while len(bars) < n:
        if d.weekday() < 5:
            drift = rnd.uniform(-0.006, 0.008)
            o = px
            c = max(100.0, px * (1 + drift))
            h = max(o, c) * 1.003
            low = min(o, c) * 0.997
            b = {"symbol": "NIFTY", "trade_date": d.strftime("%Y-%m-%d"),
                 "open_price": round(o, 2), "high_price": round(h, 2),
                 "low_price": round(low, 2), "close_price": round(c, 2),
                 "volume": 500000, "oi": 100000}
            if tf:
                b["bar_time"] = f"{9 + (len(bars) % 6):02d}:{(len(bars) * 5) % 60:02d}"
            bars.append(b)
            px = c
        d += datetime.timedelta(days=1)
    return bars


def _cfg(adv_extra=None, risk_extra=None):
    legs = [{"option_type": "CE", "transaction": "buy", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0}]
    adv = {"trade_mode": "intraday", "timeframe": "1d"}
    adv.update(adv_extra or {})
    risk = {"max_trades_per_day": 5, "daily_stop_loss": 1500,
            "daily_take_profit": 3000}
    risk.update(risk_extra or {})
    inds = [{"id": "supertrend", "params": {"period": 10, "multiplier": 3}}]
    return inds, legs, adv, risk


# ---------------------------------------------------------------- 1. resolver
def test_shared_sltp_resolver_is_single_source():
    """Backtest ka _resolve_sltp aur paper ka resolver EXACT same numbers."""
    eng = BacktestEngine(is_live=False)
    cases = [
        ({"daily_stop_loss_pct": 30, "daily_take_profit_pct": 10}, {}),
        ({"daily_stop_loss": 6000, "daily_take_profit": 0}, {}),
        ({}, {"stop_loss": "30%"}),
        ({"daily_stop_loss_pct": 0, "daily_take_profit_pct": 0}, {"take_profit": "5%"}),
        ({}, {}),
    ]
    for rm, lg in cases:
        sl_a, tp_a, slu_a, tpu_a = eng._resolve_sltp(lg, rm)
        sl_b, slu_b, tp_b, tpu_b = resolve_strategy_sltp(rm, lg)
        assert abs(sl_a - sl_b) < 1e-6 and slu_a == slu_b, (rm, lg, sl_a, sl_b)
        assert abs(tp_a - tp_b) < 1e-6 and tpu_a == tpu_b, (rm, lg, tp_a, tp_b)


def test_zero_means_unset_and_falls_back_to_default():
    """'0' = 'set nahi' (paper wala rule) -> default Rs1500/3000 lagta hai."""
    sl, slu, tp, tpu = resolve_strategy_sltp(
        {"daily_stop_loss": 0, "daily_take_profit": 0}, {})
    assert (sl, slu) == (1500.0, "rs")
    assert (tp, tpu) == (3000.0, "rs")
    # pct key jeetta hai
    sl2, slu2, _, _ = resolve_strategy_sltp(
        {"daily_stop_loss_pct": 30, "daily_stop_loss": 6000}, {})
    assert (sl2, slu2) == (30.0, "pct")


# ------------------------------------------------------------ 2. forming bars
def test_drop_forming_bars_keeps_only_completed():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    today = now.strftime("%Y-%m-%d")
    # bar jo abhi chal rahi hai (abhi se 2 min baad khatam hogi)
    bt_now = now.strftime("%H:%M")
    bars = [
        {"trade_date": "2026-06-01", "bar_time": "09:30"},
        {"trade_date": today, "bar_time": bt_now},
    ]
    out = drop_forming_bars(bars, "5m")
    assert len(out) == 1, out
    # pichle din ka last bar complete hai -> nahi hatna chahiye
    bars2 = [{"trade_date": "2026-06-01", "bar_time": "15:25"}]
    assert len(drop_forming_bars(bars2, "5m")) == 1
    # daily bars (bar_time nahi) kabhi nahi hatte
    assert len(drop_forming_bars([{"trade_date": today}], "1d")) == 1


def test_engine_drops_forming_bar_and_reports_it():
    """Engine aakhri (chal rahi) candle hata kar hi trade kare + report de."""
    hist = _bars(n=30, seed=21)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    today = now.strftime("%Y-%m-%d")
    hist.append({"symbol": "NIFTY", "trade_date": today,
                 "bar_time": now.strftime("%H:%M"),
                 "open_price": 25000.0, "high_price": 25100.0,
                 "low_price": 24900.0, "close_price": 25050.0,
                 "volume": 1000, "oi": 100})
    inds, legs, adv, risk = _cfg()
    adv["timeframe"] = "5m"
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    res = eng.run(list(hist), "NIFTY", hist[0]["trade_date"], hist[-1]["trade_date"],
                  inds, [], [], legs, dict(adv), dict(risk), is_live=False)
    realism = (res.get("metrics") or {}).get("realism") or {}
    assert realism.get("forming_bars_dropped") == 1, realism
    assert realism.get("closed_candles_only") is True, realism


# --------------------------------------------------------------- 3. strict band
def test_intrabar_band_is_stricter_than_bs_extreme():
    """Delta-based (vega-free) band BS-extreme band se CHHOTA hona chahiye -
    warna unreal target hits aate hain (80% BT vs 30% paper)."""
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    eng.bt_symbol = "NIFTY"
    eng.bt_expiry = "2026-11-24"          # DTE 7 for the bar below
    eng._advanced = {"timeframe": "5m"}
    eng.implied_volatility = None
    spot = 25000.0
    strike = 25000.0
    cur = {"symbol": "NIFTY", "trade_date": "2026-11-17", "bar_time": "10:00",
           "open_price": spot, "high_price": spot * 1.004,
           "low_price": spot * 0.996, "close_price": spot * 1.001,
           "volume": 1000}
    entry = {"date": "2026-11-17", "strike": str(strike), "price": 160.0,
             "quantity": 75, "type": "buy", "legs": []}
    captured = {}
    orig = eng._check_sl_tp_levels

    def spy(txn_type, sl_level, tp_level, opt_high, opt_low):
        captured["high"] = opt_high
        captured["low"] = opt_low
        return orig(txn_type, sl_level, tp_level, opt_high, opt_low)

    eng._check_sl_tp_levels = spy
    eng._check_sl_tp(cur, entry, "CE", "buy", 1500, 3000, 75)
    assert captured.get("high"), captured
    iv = model_iv("NIFTY")
    t = 7 / 365.0
    bs_high = black_scholes(cur["high_price"], strike, t, iv, "CE")
    bs_low = black_scholes(cur["low_price"], strike, t, iv, "CE")
    # Delta band (tangent-line, vega-free) kabhi trader ko favour nahi karta:
    #   - TP side: option high CHHOTA (BS-extreme se) -> unreal TP hits nahi
    #   - SL side: option low BS-low se bhi kam/baarabar -> risk jaldi pakda
    assert captured["high"] <= bs_high + 0.5, (captured["high"], bs_high)
    assert captured["low"] <= bs_low + 0.5, (captured["low"], bs_low)
    assert captured["high"] < bs_high, (captured["high"], bs_high)


# ------------------------------------------------------- 4. latency + slippage
def test_latency_cost_is_charged_on_entry():
    hist = _bars(n=40, seed=3)
    inds, legs, adv, risk = _cfg()
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    r0 = eng.run(list(hist), "NIFTY", hist[0]["trade_date"], hist[-1]["trade_date"],
                 inds, [], [], legs, dict(adv, latency_slippage_pct=0.0),
                 dict(risk), is_live=False)
    eng2 = BacktestEngine(is_live=False)
    eng2._skip_db = True
    r1 = eng2.run(list(hist), "NIFTY", hist[0]["trade_date"], hist[-1]["trade_date"],
                  inds, [], [], legs, dict(adv, latency_slippage_pct=0.25),
                  dict(risk), is_live=False)
    t0 = (r0.get("metrics") or {}).get("trade_list") or []
    t1 = (r1.get("metrics") or {}).get("trade_list") or []
    assert t0 and t1, (len(t0), len(t1))
    # same bar par entry -> latency wala entry THODA upar (BUY side cost)
    same = [(a, b) for a, b in zip(t0, t1)
            if a.get("entry_date") == b.get("entry_date")
            and abs(float(a.get("entry_price") or 0) - float(b.get("entry_price") or 0)) > 0.01]
    assert same, "latency cost entry price par dikhna chahiye"
    a, b = same[0]
    assert float(b["entry_price"]) > float(a["entry_price"]), (a, b)


def test_backtest_does_not_mutate_global_slippage():
    """Backtest ka slippage override GLOBAL mutate nahi karta - warna
    paper/live ke fills beech me badal jate hain (race)."""
    from core.services.transaction_costs import TransactionCosts
    before = TransactionCosts.SLIPPAGE_PCT
    hist = _bars(n=30, seed=5)
    inds, legs, adv, risk = _cfg()
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    eng.run(list(hist), "NIFTY", hist[0]["trade_date"], hist[-1]["trade_date"],
            inds, [], [], legs, dict(adv, slippage_pct=0.05), dict(risk), is_live=False)
    assert TransactionCosts.SLIPPAGE_PCT == before, (before, TransactionCosts.SLIPPAGE_PCT)


# ------------------------------------------------------------ 5. strike parity
def test_strike_selection_comes_from_leg():
    """Leg 'atm' + advanced 'otm/2' -> engine ATM strike use kare (paper jaisa)."""
    hist = _bars(n=40, seed=13)
    inds, legs, adv, risk = _cfg(adv_extra={"strike_selection": "otm", "otm_distance": 2})
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    res = eng.run(list(hist), "NIFTY", hist[0]["trade_date"], hist[-1]["trade_date"],
                  inds, [], [], legs, dict(adv), dict(risk), is_live=False)
    tl = (res.get("metrics") or {}).get("trade_list") or []
    assert tl, "koi trade nahi"
    for t in tl:
        strike = float(t.get("strike_price") or t.get("strike") or 0)
        spot = float(t.get("entry_spot") or t.get("spot") or 0)
        if not spot:
            continue
        assert abs(strike - round(spot / 50) * 50) < 1.0, (strike, spot, t)


def test_engine_version_is_v7():
    assert ENGINE_VERSION == "v7", ENGINE_VERSION
