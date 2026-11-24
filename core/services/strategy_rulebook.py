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

5) POSITION-TYPE DEFAULT PROFILES (Buy vs Sell) - rule 16-11-2026:
   Option SELL (Sell PE / Sell CE):
       entry 10:15, SL 12-15 pts, target 15-20 pts, trailing ON
       (10 pts profit -> 5 pts lock, 5 pts step), re-entry on SL = No,
       max 5 trades/day.
   Option BUY (Buy CE / Buy PE):
       strike ATM / ITM-1 (deep OTM nahi), SL 10-12 pts (ya premium ka
       15-20%), target 25-30 pts, ADX > 22 + SuperTrend/VWAP/Market-Regime
       filters ON, trailing ON (12 pts -> 6 pts lock, 5 pts step),
       re-entry on SL = No, max 2 trades/day.
   Index overrides (rule 3) hamesha profile se upar: NIFTY 15/20,
   FINNIFTY 18/22, BANKNIFTY 25/35.

   Ye defaults hain - UI se har field editable hai. `apply_profile_defaults`
   sirf KHAALI fields bharta hai (explicit user value kabhi override nahi).
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


# ============================================================================
# RULE 5: position-type default profiles (Buy vs Sell)
# ============================================================================
# Ye SIRF defaults hain. UI/API explicit value bheje to wahi chalta hai.
SELL_PROFILE = {
    "side": "sell",
    "entry_time": "10:15",          # 09:45 -> 10:15 (morning fake signals avoid)
    "exit_time": "15:15",           # mandatory intraday square-off
    "sl_points": (12.0, 15.0),
    "tp_points": (15.0, 20.0),
    "strike_selection": "otm",
    "otm_distance": 1,
    "trailing_enabled": True,
    "lock_profit_at_pts": 10.0,     # 10 pts profit
    "lock_profit_pts": 5.0,         # -> 5 pts lock
    "trail_step_pts": 5.0,          # trail har 5 pts
    "reentry_on_sl": "No Re-entry",
    "reentry_on_tp": "RE ASAP",
    "max_trades_per_day": 5,
    "strict_trend": True,
}

BUY_PROFILE = {
    "side": "buy",
    "entry_time": "09:35",
    "exit_time": "15:15",
    "sl_points": (10.0, 12.0),
    "tp_points": (25.0, 30.0),
    "sl_pct_of_premium": (15.0, 20.0),
    "strike_selection": "atm",      # ATM ya ITM-1 (deep OTM nahi)
    "otm_distance": 0,
    "allow_itm1": True,
    "trailing_enabled": True,
    "lock_profit_at_pts": 12.0,     # 12 pts profit
    "lock_profit_pts": 6.0,         # -> 6 pts lock
    "trail_step_pts": 5.0,          # trail har 5 pts
    "reentry_on_sl": "No Re-entry",
    "reentry_on_tp": "RE ASAP",
    "max_trades_per_day": 2,
    "strict_trend": True,           # ADX > 22 + VWAP
}

_PROFILES = {"buy": BUY_PROFILE, "sell": SELL_PROFILE}


def normalize_side(side) -> str:
    """'SELL'/'s'/'short' -> 'sell', baaki sab 'buy'."""
    s = str(side or "").strip().lower()
    if s in ("sell", "s", "short", "se", "st"):
        return "sell"
    return "buy"


def legs_side(legs) -> str:
    """Legs se side nikalo: koi bhi sell leg ho to 'sell' warna 'buy'."""
    try:
        for lg in (legs or []):
            if not isinstance(lg, dict):
                continue
            if str(lg.get("transaction") or "").strip().lower() == "sell":
                return "sell"
    except Exception:
        pass
    return "buy"


def default_profile(side) -> dict:
    """Copy of the Buy/Sell default profile."""
    return dict(_PROFILES.get(normalize_side(side), BUY_PROFILE))


def profile_sltp_points(side, symbol=None):
    """(sl_pts, tp_pts) - index override pehle, warna profile default.

    Index override always wins (rule 3: NIFTY 15/20, FINNIFTY 18/22,
    BANKNIFTY 25/35) - chahe side buy ho ya sell."""
    idx = index_sltp_points(symbol) if symbol else None
    if idx:
        return (float(idx[0]), float(idx[1]))
    p = _PROFILES.get(normalize_side(side), BUY_PROFILE)
    sl_lo = float(p["sl_points"][0])
    tp = p["tp_points"]
    # TP range ka mid (conservative R:R - lower edge nahi, upper edge bhi nahi)
    tp_pts = round((float(tp[0]) + float(tp[1])) / 2.0, 2)
    return (sl_lo, tp_pts)


def _blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip() in ("", "0", "0.0"):
        return True
    try:
        if isinstance(v, (int, float)) and float(v) <= 0:
            return True
    except Exception:
        pass
    return False


def apply_profile_defaults(risk: dict, advanced: dict, legs=None, symbol=None):
    """Buy/Sell default profile ko risk/advanced me bhoro.

    SIRF khaali (None / '' / 0 / missing) fields bharte hain - user ka explicit
    value kabhi override nahi hota, isliye UI par sab kuch customizable rehta
    hai. Returns (risk, advanced) - naye dicts (input mutate nahi hote).
    """
    risk = dict(risk or {})
    advanced = dict(advanced or {})
    # EQ (cash equity) legs par option profile lagu nahi hota - wahan SL/TP
    # rupees me hota hai aur premium concept nahi hai.
    _is_option = False
    try:
        _is_option = any(
            str((lg or {}).get("option_type", "")).upper() in ("CE", "PE")
            for lg in (legs or []) if isinstance(lg, dict)
        )
    except Exception:
        _is_option = False
    if not _is_option:
        return risk, advanced
    side = legs_side(legs)
    prof = _PROFILES.get(side, BUY_PROFILE)
    sl_pts, tp_pts = profile_sltp_points(side, symbol)

    # ---- per-trade SL/TP (points) ----
    # Explicit % mode / legacy rupee mode chhua nahi jaata.
    _sl_pct = risk.get("daily_stop_loss_pct")
    _tp_pct = risk.get("daily_take_profit_pct")
    _sl_unit = str(risk.get("sl_unit") or "").lower()
    _tp_unit = str(risk.get("tp_unit") or "").lower()
    if _blank(_sl_pct) and _sl_unit in ("", "pt") and _blank(risk.get("daily_stop_loss")):
        risk["daily_stop_loss"] = float(sl_pts)
        risk["sl_unit"] = "pt"
    if _blank(_tp_pct) and _tp_unit in ("", "pt") and _blank(risk.get("daily_take_profit")):
        risk["daily_take_profit"] = float(tp_pts)
        risk["tp_unit"] = "pt"

    # ---- max trades/day (per strategy profile cap) ----
    # Missing -> profile default. Explicit -1 (unlimited) / 0 / >0 respect.
    if risk.get("max_trades_per_day") in (None, ""):
        risk["max_trades_per_day"] = int(prof["max_trades_per_day"])

    # ---- advanced execution defaults ----
    if _blank(advanced.get("entry_time")) or str(advanced.get("entry_time")) == "09:35":
        # 09:35 legacy default -> profile default (sell 10:15). Explicit
        # user-entered non-default time untouched.
        if side == "sell":
            advanced["entry_time"] = prof["entry_time"]
    if _blank(advanced.get("exit_time")):
        advanced["exit_time"] = prof["exit_time"]

    # Intraday: koi intermediate candle-completion time exit nahi - sirf
    # 15:15 square-off. `allow_intraday_time_exit` se explicit opt-in.
    _mode = str(advanced.get("trade_mode") or advanced.get("trade_type") or "").lower()
    if _mode == "intraday" and not advanced.get("allow_intraday_time_exit"):
        advanced["max_holding_bars"] = 0

    if advanced.get("strict_trend") in (None, ""):
        advanced["strict_trend"] = bool(prof["strict_trend"])

    if advanced.get("trailing") in (None, ""):
        advanced["trailing"] = str(int(prof["trail_step_pts"]))
    if _blank(advanced.get("lock_profit_at")):
        advanced["lock_profit_at"] = float(prof["lock_profit_at_pts"])
    if _blank(advanced.get("lock_profit")):
        advanced["lock_profit"] = float(prof["lock_profit_pts"])
    if not advanced.get("lock_profit_unit"):
        advanced["lock_profit_unit"] = "pt"
    if _blank(advanced.get("trailing_sl")):
        advanced["trailing_sl"] = bool(prof["trailing_enabled"])

    if not advanced.get("reentry_on_sl"):
        advanced["reentry_on_sl"] = prof["reentry_on_sl"]
    if not advanced.get("reentry_on_tp"):
        advanced["reentry_on_tp"] = prof["reentry_on_tp"]

    return risk, advanced


