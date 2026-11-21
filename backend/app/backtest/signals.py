from __future__ import annotations

from datetime import datetime

import polars as pl

from ..config import settings


def _wilder_smooth(s: pl.Expr, period: int) -> pl.Expr:
    """Wilder's smoothed moving average (recursive, approximated via ewm alpha=1/period)."""
    return s.ewm_mean(alpha=1.0 / period, adjust=False)


def compute_indicators(bars: pl.DataFrame) -> pl.DataFrame:
    """
    Compute institutional indicators on underlying 1-minute bars.

    Input columns: timestamp, open, high, low, close, volume (+ underlying ltp column).
    Output adds: ema_20, ema_50, rsi_14, vwap (per session), supertrend, atr_14,
                 plus_di_14, minus_di_14, adx_14, regime (trending/ranging).
    """
    df = bars.sort("timestamp")
    close = pl.col("close")
    high = pl.col("high")
    low = pl.col("low")
    vol = pl.col("volume")

    # EMA
    df = df.with_columns(
        close.ewm_mean(span=20, adjust=False).alias("ema_20"),
        close.ewm_mean(span=50, adjust=False).alias("ema_50"),
    )

    # RSI 14 (Wilder)
    delta = close.diff()
    gain = pl.when(delta > 0).then(delta).otherwise(pl.lit(0.0))
    loss = pl.when(delta < 0).then(-delta).otherwise(pl.lit(0.0))
    avg_gain = _wilder_smooth(gain, 14)
    avg_loss = _wilder_smooth(loss, 14)
    rs = avg_gain / avg_loss.replace(0, None)
    df = df.with_columns((100.0 - (100.0 / (1.0 + rs))).alias("rsi_14"))

    # VWAP per session (reset daily)
    typical = (high + low + close) / 3.0
    df = df.with_columns(
        (typical * vol).alias("_tpv"),
    )
    date_col = pl.col("timestamp").dt.date()
    df = df.with_columns(
        (pl.col("_tpv").cum_sum() / pl.col("volume").cum_sum()).over(date_col).alias("vwap")
    )

    # True range & ATR 14
    prev_close = close.shift(1)
    tr = pl.max_horizontal(
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    )
    atr = _wilder_smooth(tr, 14)
    df = df.with_columns(atr.alias("atr_14"))

    # SuperTrend (10, 3) — classic iterative
    hl2 = (high + low) / 2.0
    upper_basic = (hl2 + 3.0 * atr).alias("_ub")
    lower_basic = (hl2 - 3.0 * atr).alias("_lb")
    df = df.with_columns(upper_basic, lower_basic, atr.alias("_atr"))

    upper = [None] * len(df)
    lower = [None] * len(df)
    st = [None] * len(df)
    direction = [1] * len(df)  # 1 = uptrend, -1 = downtrend

    h = df["high"].to_list()
    l = df["low"].to_list()
    c = df["close"].to_list()
    ub = df["_ub"].to_list()
    lb = df["_lb"].to_list()
    atrv = df["_atr"].to_list()

    for i in range(len(df)):
        if i == 0:
            upper[i] = ub[i]
            lower[i] = lb[i]
            st[i] = ub[i] if c[i] <= ub[i] else lb[i]
            direction[i] = -1 if c[i] <= ub[i] else 1
            continue
        # Upper band
        if ub[i] < upper[i - 1] or c[i - 1] > upper[i - 1]:
            upper[i] = ub[i]
        else:
            upper[i] = upper[i - 1]
        # Lower band
        if lb[i] > lower[i - 1] or c[i - 1] < lower[i - 1]:
            lower[i] = lb[i]
        else:
            lower[i] = lower[i - 1]
        if direction[i - 1] == 1:
            if c[i] <= lower[i]:
                direction[i] = -1
                st[i] = upper[i]
            else:
                direction[i] = 1
                st[i] = lower[i]
        else:
            if c[i] >= upper[i]:
                direction[i] = 1
                st[i] = lower[i]
            else:
                direction[i] = -1
                st[i] = upper[i]

    df = df.with_columns(
        pl.Series("supertrend", st),
        pl.Series("supertrend_dir", direction),
    )

    # ADX 14
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = pl.when((up_move > down_move) & (up_move > 0)).then(up_move).otherwise(pl.lit(0.0))
    minus_dm = pl.when((down_move > up_move) & (down_move > 0)).then(down_move).otherwise(pl.lit(0.0))
    smoothed_tr = atr  # Wilder smoothed TR
    smoothed_plus = _wilder_smooth(plus_dm, 14)
    smoothed_minus = _wilder_smooth(minus_dm, 14)
    plus_di = 100.0 * (smoothed_plus / smoothed_tr.replace(0, None))
    minus_di = 100.0 * (smoothed_minus / smoothed_tr.replace(0, None))
    di_sum = plus_di + minus_di
    dx = 100.0 * ((plus_di - minus_di).abs() / di_sum.replace(0, None))
    adx = _wilder_smooth(dx, 14)
    df = df.with_columns(plus_di.alias("plus_di_14"), minus_di.alias("minus_di_14"), adx.alias("adx_14"))

    df = df.with_columns(
        pl.when(pl.col("adx_14") > 25).then(pl.lit("trending")).otherwise(pl.lit("ranging")).alias("regime")
    )

    return df.drop(["_tpv", "_ub", "_lb", "_atr"])


def generate_entry_signals(
    bars_with_indicators: pl.DataFrame,
    config: dict,
    signal_ts: datetime | None = None,
) -> list[dict]:
    """
    Evaluate entry signals at each bar timestamp.

    Config keys:
      indicators (list): subset of ["supertrend","rsi","adx","vwap","ema"] required
      option_side ("CE"|"PE"|"both"): which side to trade; "both" = follow trend
      strike_offset ("ATM"|"OTM-3"|"OTM-4"|"OTM-5"...)
      expiry_type ("weekly"|"monthly")
    Returns list of {timestamp, side ("BUY_CE"/"BUY_PE"), underlying_ltp, strike_offset, expiry_type}.
    """
    indicators = set(config.get("indicators", ["supertrend", "rsi", "adx", "vwap", "ema"]))
    side_pref = config.get("option_side", "both")

    signals: list[dict] = []
    rows = bars_with_indicators.to_dicts()
    for row in rows:
        ts = row["timestamp"]
        if signal_ts is not None and ts != signal_ts:
            continue
        close = row["close"] or 0.0

        bullish = True
        bearish = True
        if "supertrend" in indicators:
            bullish = bullish and row.get("supertrend_dir") == 1
            bearish = bearish and row.get("supertrend_dir") == -1
        if "rsi" in indicators:
            rsi = row.get("rsi_14")
            bullish = bullish and rsi is not None and rsi > 50
            bearish = bearish and rsi is not None and rsi < 50
        if "adx" in indicators:
            adx = row.get("adx_14")
            bullish = bullish and adx is not None and adx > 20
            bearish = bearish and adx is not None and adx > 20
        if "vwap" in indicators:
            bullish = bullish and close > (row.get("vwap") or 0)
            bearish = bearish and close < (row.get("vwap") or 0)
        if "ema" in indicators:
            bullish = bullish and close > (row.get("ema_20") or 0)
            bearish = bearish and close < (row.get("ema_50") or 0)

        if bullish and side_pref in ("CE", "both"):
            signals.append(
                {
                    "timestamp": ts,
                    "side": "BUY_CE",
                    "underlying_ltp": close,
                    "strike_offset": config.get("strike_offset", "ATM"),
                    "expiry_type": config.get("expiry_type", "weekly"),
                }
            )
        if bearish and side_pref in ("PE", "both"):
            signals.append(
                {
                    "timestamp": ts,
                    "side": "BUY_PE",
                    "underlying_ltp": close,
                    "strike_offset": config.get("strike_offset", "ATM"),
                    "expiry_type": config.get("expiry_type", "weekly"),
                }
            )
    return signals


def resolve_strike(underlying_ltp: float, strike_offset: str, step: int = 50) -> float:
    """Resolve ATM / OTM-n strike from underlying price."""
    try:
        n = int(strike_offset.split("-")[1]) if "-" in str(strike_offset) else 0
    except Exception:
        n = 0
    atm = round(underlying_ltp / step) * step
    if str(strike_offset).upper().startswith("OTM"):
        atm = atm + n * step
    return float(atm)
