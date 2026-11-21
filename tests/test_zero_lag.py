"""Zero-Lag trend (Pine-exact port) + resample tests."""
import os
import sys
import random
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.services.indicator_engine import IndicatorEngine


def _bars(n=200, seed=9, start=datetime.date(2026, 1, 1)):
    rnd = random.Random(seed)
    bars = []
    px = 25000.0
    d = start
    plan = ([0.008] * 30 + [-0.009] * 20 + [0.010] * 30 + [-0.004] * 20) * 3
    k = 0
    while len(bars) < n:
        if d.weekday() < 5:
            drift = plan[k % len(plan)] + rnd.uniform(-0.005, 0.005)
            k += 1
            o = px
            c = max(100.0, px * (1 + drift))
            h = max(o, c) * 1.004
            low = min(o, c) * 0.996
            bars.append({"trade_date": d.strftime("%Y-%m-%d"),
                         "open_price": o, "high_price": h,
                         "low_price": low, "close_price": c,
                         "volume": 500000, "oi": 0})
            px = c
        d += datetime.timedelta(days=1)
    return bars


def test_trend_flips_and_entries():
    eng = IndicatorEngine()
    bars = _bars()
    closes = [b["close_price"] for b in bars]
    r = eng.calculate_zero_lag_trend(bars, closes, 70, 1.2)
    assert len(r["trend"]) == len(bars)
    assert any(t == 1 for t in r["trend"]), "must go bullish sometime"
    assert any(t == -1 for t in r["trend"]), "must go bearish sometime"
    assert sum(r["flip_up"]) >= 1 and sum(r["flip_down"]) >= 1
    # entries only while trend confirmed both bars
    for i, b in enumerate(r["buy"]):
        if b:
            assert r["trend"][i] == 1
    for i, s in enumerate(r["sell"]):
        if s:
            assert r["trend"][i] == -1
    # bands follow trend side (Pine parity)
    for i in range(len(bars)):
        if r["trend"][i] == 1:
            assert r["lower"][i] is not None and r["upper"][i] is None
        elif r["trend"][i] == -1:
            assert r["upper"][i] is not None and r["lower"][i] is None


def test_no_repaint_prefix_stability():
    """Gold standard: signal at bar i identical on full vs truncated series."""
    eng = IndicatorEngine()
    bars = _bars()
    closes = [b["close_price"] for b in bars]
    full = eng.calculate_zero_lag_trend(bars, closes, 70, 1.2)
    for cut in (80, 120, 160):
        part = eng.calculate_zero_lag_trend(bars[:cut], closes[:cut], 70, 1.2)
        for i in range(cut):
            assert part["trend"][i] == full["trend"][i], (cut, i)
            assert part["buy"][i] == full["buy"][i], (cut, i)
            assert part["sell"][i] == full["sell"][i], (cut, i)


def test_short_series_safe():
    eng = IndicatorEngine()
    bars = _bars(20)
    closes = [b["close_price"] for b in bars]
    r = eng.calculate_zero_lag_trend(bars, closes, 70, 1.2)
    assert all(v is None for v in r["zlema"])
    assert sum(r["buy"]) == 0 and sum(r["sell"]) == 0


def test_resample_15m_to_1h():
    eng = IndicatorEngine()
    bars = []
    d = "2026-10-12"
    px = 25000.0
    for k in range(26):
        hh, mm = divmod(9 * 60 + 15 + k * 15, 60)
        px *= 1.0005
        bars.append({"trade_date": d, "bar_time": f"{hh:02d}:{mm:02d}",
                     "open_price": px, "high_price": px * 1.001,
                     "low_price": px * 0.999, "close_price": px,
                     "volume": 1000, "oi": 0})
    h1 = eng.resample_ohlc(bars, 60)
    assert len(h1) == 7, len(h1)  # 9:15->15:30 covers 7 hourly buckets
    assert h1[0]["bar_time"] == "09:00"
    assert abs(h1[-1]["close_price"] - bars[-1]["close_price"]) < 1e-9
    tot = sum(b["volume"] for b in bars)
    assert abs(sum(b["volume"] for b in h1) - tot) < 1e-6
    d1 = eng.resample_ohlc(bars, 1440)
    assert len(d1) == 1 and d1[0]["trade_date"] == d
