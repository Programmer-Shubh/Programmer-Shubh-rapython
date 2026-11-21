"""RSI+VWAP+ST+PCR combo + hardened SMV gate."""
import os
import sys
import random
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.services.indicator_engine import IndicatorEngine


def _bars(n=150, seed=7):
    # Oscillating regimes: rally -> pullback (RSI dips under 70, ST flips)
    # -> re-rally (fresh RSI70 cross + ST flip = combo entry zone).
    rnd = random.Random(seed)
    bars, px, d = [], 50000.0, datetime.date(2026, 1, 1)
    plan = ([0.010] * 25 + [-0.009] * 14 + [0.011] * 25 + [-0.012] * 10
            + [0.008] * 25 + [0.0] * 20 + [0.009] * 21)
    plan = (plan + [0.004] * n)[:n]
    k = 0
    while len(bars) < n:
        if d.weekday() < 5:
            drift = plan[k] + rnd.uniform(-0.007, 0.007)
            k += 1
            vol = 6_000_000 if len(bars) % 12 == 5 else rnd.randint(800_000, 1_400_000)
            o = px
            c = max(1000.0, px * (1 + drift))
            h = max(o, c) * 1.003
            l = min(o, c) * 0.997
            bars.append({"trade_date": d.strftime("%Y-%m-%d"), "open_price": o,
                         "high_price": h, "low_price": l, "close_price": c,
                         "volume": vol, "oi": 2_000_000})
            px = c
        d += datetime.timedelta(days=1)
    return bars


def test_combo_fires_on_momentum_with_pcr():
    eng = IndicatorEngine()
    bars = _bars()
    closes = [b["close_price"] for b in bars]
    pcr = {b["trade_date"]: 1.3 for b in bars}  # supportive
    res = eng.calculate_rsi_vwap_pcr_combo(bars, closes, {}, pcr)
    assert sum(res["buy"]) > 0, "momentum ignition must fire at least once"
    assert sum(res["exit_buy"]) > 0, "weakness exits must fire"
    # every buy bar: all five legs true
    for i, b in enumerate(res["buy"]):
        if not b:
            continue
        assert closes[i] > res["supertrend"][i]
        assert closes[i] > res["vwap"][i]
        assert res["rsi"][i] > 70
        assert res["pcr"][i] >= 1.0


def test_combo_pcr_blocks_call():
    eng = IndicatorEngine()
    bars = _bars()
    closes = [b["close_price"] for b in bars]
    pcr_bad = {b["trade_date"]: 0.5 for b in bars}  # heavy call writing
    pcr_ok = {b["trade_date"]: 1.3 for b in bars}
    bad = eng.calculate_rsi_vwap_pcr_combo(bars, closes, {}, pcr_bad)
    ok = eng.calculate_rsi_vwap_pcr_combo(bars, closes, {}, pcr_ok)
    assert sum(ok["buy"]) > 0
    assert sum(bad["buy"]) == 0, "PCR<1 must block all call entries"


def test_combo_neutral_pcr_without_map():
    eng = IndicatorEngine()
    bars = _bars()
    closes = [b["close_price"] for b in bars]
    res = eng.calculate_rsi_vwap_pcr_combo(bars, closes, {}, None)
    assert all(p == 1.0 for p in res["pcr"])
    # neutral 1.0 passes >= 1.0 gate (not punished for missing data)
    assert sum(res["buy"]) > 0


def test_smv_gate_blocks_overbought_chase():
    from core.services.scanner import OptionScanner
    sc = OptionScanner()
    bars = _bars(60, seed=11)
    # force vertical rally -> RSI extreme
    px = bars[-1]["close_price"]
    import datetime as _dt
    d = _dt.date(2026, 6, 1)
    for _ in range(10):
        px *= 1.04
        bars.append({"trade_date": d.strftime("%Y-%m-%d"), "open_price": px / 1.04,
                     "high_price": px * 1.01, "low_price": px / 1.04,
                     "close_price": px, "volume": 2_000_000, "oi": 0})
        d += _dt.timedelta(days=1)
    gd, gr = sc._smv_gate("TEST", bars)
    # parabolic + RSI>=80 must be blocked (or at least not a fresh-chase buy)
    if gd == "bullish":
        assert "overbought" not in str(gr).lower()  # blocked returns None instead
    assert gd is None, f"parabolic chase must be blocked, got {gd} {gr}"
