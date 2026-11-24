# -*- coding: utf-8 -*-
"""Master Strategy Settings & Execution Rulebook (user rulebook 16-11-2026).

SINGLE source of truth - backtest engine, paper worker aur live order TEENO
yahi rules use karte hain (warna paper-vs-backtest ka delta banta hai, jo
user ka "Paper-BT Δ" column measure karta hai).

RULES
-----
1) ALL-DAY ACTIVE EXECUTION (no daily cutoff / no daily limit):
   Koi daily stop-loss circuit breaker ya trade-count limit nahi - subah se
   shaam tak har valid signal par trade. Engine me: max_trades_per_day = -1
   (unlimited) aur daily_loss_limit = 0 (disabled). Ye pehle se hata diya
   gaya tha; yahan sirf ek jagah rule documented hai.

2) STRICT MARKET REGIME & TREND FILTER (ADX > 22 + VWAP):
   - ADX(14) strictly > 22 hona chahiye (sideways/choppy market me entry
     auto-skip).
   - Price aur VWAP line ke "favorable" side me hona chahiye:
     CE ke liye close > VWAP, PE ke liye close < VWAP.
   Gate `advanced.strict_trend = True` se ON hota hai (backtest + paper +
   live - same engine, same gate).

3) TIGHT PER-TRADE SL/TP (points, index-wise):
     NIFTY     : SL 15 pts | TP 20 pts
     FINNIFTY  : SL 18 pts | TP 22 pts
     BANKNIFTY : SL 25 pts | TP 35 pts
   Strategy me apna SL/TP set hai to wahi (user choice) - ye sirf default
   hai. `resolve_strategy_sltp(..., symbol=...)` se wire hota hai.

4) TICK SIZE 0.05 + SLIPPAGE GUARD:
   - Saare fill/level prices 0.05 tick par round (exchange ke tick).
   - Entry slippage: pehle wala slippage model (0.5% default) + limit offset
     guard - backtest, paper aur live same.
"""
from __future__ import annotations

# ---- Rule 2: strict trend gate ----
ADX_MIN = 22.0
VWAP_PERIOD = 20

# ---- Rule 3: tight per-index SL/TP (points) ----
INDEX_SLTP_POINTS = {
    "NIFTY": (15.0, 20.0),
    "FINNIFTY": (18.0, 22.0),
    "BANKNIFTY": (25.0, 35.0),
}

# ---- Rule 4: tick size ----
TICK_SIZE = 0.05


def index_sltp_points(symbol: str):
    """(sl_pts, tp_pts) for the index - None for unknown symbols."""
    try:
        return INDEX_SLTP_POINTS.get(str(symbol or "").strip().upper())
    except Exception:
        return None


def round_tick(price, tick: float = TICK_SIZE) -> float:
    """Exchange tick rounding (0.05 default) - har fill/level par."""
    try:
        p = float(price or 0)
        if p <= 0:
            return p
        t = float(tick or TICK_SIZE) or TICK_SIZE
        return round(round(p / t) * t, 2)
    except Exception:
        try:
            return float(price or 0)
        except Exception:
            return 0.0


def strict_trend_ok(adx_value, close, vwap, option_type: str = "CE",
                    side: str = "BUY", adx_min: float = ADX_MIN) -> bool:
    """Rule 2 gate - ADX strictly above threshold + VWAP direction.

    adx_value : ADX(14) value (None = data nahi -> gate FAIL - sideways
                me trade nahi, data nahi me bhi trade nahi)
    close     : bar ka close (underlying)
    vwap      : VWAP line value
    option_type: 'CE' ya 'PE' (favourable side decide karta hai)
    side      : 'BUY' ya 'SELL' (option leg ka side)
    """
    try:
        if adx_value is None or close is None or vwap is None:
            return False
        if float(adx_value) <= float(adx_min):
            return False
        c = float(close)
        v = float(vwap)
        opt = str(option_type or "CE").upper()
        is_buy = str(side or "BUY").upper() != "SELL"
        if opt == "CE":
            # CE long: price VWAP se upar. CE short (sell): neeche.
            return c > v if is_buy else c < v
        # PE long: price VWAP se neeche. PE short: upar.
        return c < v if is_buy else c > v
    except Exception:
        return False


def index_default_sltp(symbol: str):
    """resolve_strategy_sltp ke liye (sl, tp, unit) - points me.

    Returns None jab symbol index nahi hai (ya list me nahi mila)."""
    v = index_sltp_points(symbol)
    if not v:
        return None
    return (float(v[0]), float(v[1]), "pt")


def session_vwap(bars, reset_on_new_date: bool = True) -> list:
    """Session-anchored VWAP ('institutional VWAP' line).

    Har nayi session (naya trade_date) par cumulative VWAP reset hota hai -
    hi.ee. classic VWAP. Intraday ke liye yehi use hota hai (daily VWAP
    nahi), warna 5m bars par kai hafte ka cumulative VWAP meaningless ho
    jata hai.

    Bars me 'volume' missing ho to typical-price ka simple average use hota
    hai. Bars bina date ke (daily series) -> single continuous VWAP.
    """
    out = []
    try:
        cum_pv = 0.0
        cum_vol = 0.0
        last_date = None
        for b in (bars or []):
            try:
                d = str(b.get("trade_date") or "")[:10]
            except Exception:
                d = ""
            if reset_on_new_date and d and last_date is not None and d != last_date:
                cum_pv = 0.0
                cum_vol = 0.0
            if d:
                last_date = d
            try:
                typical = (float(b.get("high_price") or 0)
                           + float(b.get("low_price") or 0)
                           + float(b.get("close_price") or 0)) / 3.0
            except Exception:
                typical = 0.0
            try:
                vol = float(b.get("volume") or 0) or 1.0
            except Exception:
                vol = 1.0
            cum_pv += typical * vol
            cum_vol += vol
            out.append(cum_pv / cum_vol if cum_vol > 0 else None)
        return out
    except Exception:
        return []


def sl_fill_cap(level: float, is_buy: bool, cap_pct: float = 2.0,
                sl_points: float = None) -> float:
    """SL fill ka realistic cap - level se max itna hi worse fill allowed.

    Backtest, paper aur live TEENO yahi use karte hain (parity): ek stop
    order market par ~level ke paas fill hota hai, bar ke extreme par nahi.
    (v7 engine bar-extreme fill use karta tha -> BT har stop par ~2x zyada
    loss book karta tha vs paper -> bada Paper-BT delta.)

    Cap = MIN(percentage of level, 10% of the SL distance in points).
    Percentage-only galat tha: ₹900 premium par 2% = 18 points, jabki SL
    sirf 25 points ka tha -> fill 42+ points peeche chala jata tha.
    10% allowance paper ke quote-fill ke bilkul paas rehta hai (parity).
    """
    try:
        lvl = float(level or 0)
        if lvl <= 0:
            return lvl
        cap = float(cap_pct or 0) / 100.0
        cap_abs = lvl * cap
        try:
            if sl_points is not None and float(sl_points) > 0:
                cap_abs = min(cap_abs, float(sl_points) * 0.10)
        except Exception:
            pass
        if cap_abs < 0:
            cap_abs = 0.0
        if is_buy:
            return max(lvl - cap_abs, 0.05)
        return lvl + cap_abs
    except Exception:
        return level
