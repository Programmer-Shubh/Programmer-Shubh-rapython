import numpy as np
import pandas as pd
from typing import Dict, List, Optional


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute ~80 TA features from a single symbol's OHLCV DataFrame.

    Returns a copy with all feature columns appended.
    """
    if df.empty or len(df) < 50:
        return df

    df = df.copy().sort_values("timestamp")
    cl = df["close"].values.astype(float)
    hi = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    op = df["open"].values.astype(float)
    vo = df["volume"].values.astype(float)
    n = len(cl)

    s = lambda a, p: pd.Series(a).rolling(p)
    e = lambda a, p: pd.Series(a).ewm(span=p, adjust=False)

    # Price features (20)
    for p in [1, 5, 10, 20, 50]:
        r = np.divide(cl, pd.Series(cl).shift(p).values, out=np.ones(n), where=pd.Series(cl).shift(p).values > 0)
        df[f"ret_{p}"] = (r - 1) * 100
    df["log_ret_1"] = np.log(np.maximum(cl / pd.Series(cl).shift(1).values, 1e-10))
    df["hl_pct"] = (hi - lo) / cl * 100
    df["co_pct"] = (cl - op) / op * 100

    # Volatility (6)
    df["atr"] = _atr(hi, lo, cl, 14)
    df["atr_pct"] = df["atr"] / cl * 100
    bb_mid = s(cl, 20).mean()
    bb_std = s(cl, 20).std()
    df["bb_upper"] = (bb_mid + 2 * bb_std).values
    df["bb_lower"] = (bb_mid - 2 * bb_std).values
    df["bb_width"] = ((df["bb_upper"] - df["bb_lower"]) / bb_mid * 100).values
    bb_denom = np.where(bb_std.values * 2 == 0, np.nan, bb_std.values * 2)
    df["bb_position"] = (cl - bb_mid.values) / bb_denom

    # Volume (12)
    for tf_w in [5, 10, 20, 50]:
        avg_v = s(vo, tf_w).mean()
        df[f"rvol_{tf_w}"] = (vo / avg_v).values
    df["obv"] = _obv(cl, vo, n)
    df["obv_slope"] = pd.Series(df["obv"]).diff(5).values / cl
    df["mfi"] = _mfi(hi, lo, cl, vo, 14)
    df["vwap"] = _vwap(df)

    # Momentum (8)
    df["rsi"] = _rsi(cl, 14)
    df["macd"], df["macd_signal"], df["macd_hist"] = _macd(cl)
    stoch_denom = (s(hi, 14).max() - s(lo, 14).min()).replace(0, np.nan)
    df["stoch_k"] = ((cl - s(lo, 14).min()) / stoch_denom * 100).values
    df["stoch_d"] = s(df["stoch_k"].values, 3).mean().values

    # Trend (20)
    for p in [10, 20, 50, 200]:
        sma = s(cl, p).mean()
        df[f"sma_{p}"] = sma.values
        df[f"sma_{p}_dist"] = ((cl - sma.values) / sma.values * 100)
    for p in [9, 20, 50]:
        ema = e(cl, p).mean()
        df[f"ema_{p}"] = ema.values
        df[f"ema_{p}_dist"] = ((cl - ema.values) / ema.values * 100)
    df["adx"], df["pdi"], df["ndi"] = _adx(hi, lo, cl, 14)

    # Ichimoku (6) — using corrected Chikou direction
    tenkan = ((s(hi, 9).max() + s(lo, 9).min()) / 2)
    kijun = ((s(hi, 26).max() + s(lo, 26).min()) / 2)
    df["ichi_senkou_a"] = ((tenkan + kijun) / 2).shift(26).values
    df["ichi_senkou_b"] = ((s(hi, 52).max() + s(lo, 52).min()) / 2).shift(26).values
    df["ichi_chikou"] = pd.Series(cl).shift(26).values
    df["senkou_a_dist"] = ((cl - df["ichi_senkou_a"]) / df["ichi_senkou_a"] * 100).values
    df["chikou_confirm"] = np.where(cl > df["ichi_chikou"].values, 1, np.where(cl < df["ichi_chikou"].values, -1, 0))

    # SMC (6)
    df["fvg_bullish"], df["fvg_bearish"] = _fvg(hi, lo, n)
    df["bos_bullish"], df["bos_bearish"] = _bos(hi, lo, cl, n)

    # VWAP (3)
    df["vwap_dist"] = ((cl - df["vwap"].values) / df["vwap"].values * 100)
    df["vwap_band_pos"] = np.where(cl > df["vwap"].values * 1.02, 2,
                                    np.where(cl < df["vwap"].values * 0.98, -2, 0))

    return df


def add_cross_sectional_ranks(features_list: List[pd.DataFrame], symbols: List[str]) -> pd.DataFrame:
    """Compute 50 cross-sectional percentile rank features across symbols.

    For each numeric feature column, compute the rank (0-1) across all symbols
    at each timestamp. Returns a DataFrame indexed by symbol with rank columns.
    """
    if not features_list:
        return pd.DataFrame()

    combined = pd.concat(features_list, keys=symbols, names=["symbol", "idx"])
    combined = combined.drop(columns=["symbol"], errors="ignore")
    combined = combined.reset_index("symbol")

    rank_cols = [c for c in combined.columns
                 if c not in ("symbol", "timestamp", "open", "high", "low", "close", "volume")
                 and combined[c].dtype in (np.float64, np.float32, np.int64, np.int32)]

    rank_dfs = []
    for ts, group in combined.groupby("timestamp", sort=False):
        for col in rank_cols:
            group[f"rank_{col}"] = group[col].rank(pct=True)
        rank_dfs.append(group)

    if not rank_dfs:
        return pd.DataFrame()
    result = pd.concat(rank_dfs).set_index("symbol", append=True)
    rank_features = [c for c in result.columns if c.startswith("rank_")]
    return result[rank_features]


def _atr(hi, lo, cl, period):
    tr = np.maximum(hi - lo, np.maximum(
        np.abs(hi - pd.Series(cl).shift(1).values),
        np.abs(lo - pd.Series(cl).shift(1).values)
    ))
    return pd.Series(tr).rolling(period).mean().values


def _obv(cl, vo, n):
    obv = np.zeros(n)
    for i in range(1, n):
        if cl[i] > cl[i - 1]:
            obv[i] = obv[i - 1] + vo[i]
        elif cl[i] < cl[i - 1]:
            obv[i] = obv[i - 1] - vo[i]
        else:
            obv[i] = obv[i - 1]
    return obv


def _mfi(hi, lo, cl, vo, period):
    tp = (hi + lo + cl) / 3
    mf = tp * vo
    flow = pd.Series(mf).diff()
    pos = flow.clip(lower=0).rolling(period).sum()
    neg = (-flow.clip(upper=0)).rolling(period).sum()
    ratio = pos / neg.replace(0, np.nan)
    mfi = 100 - (100 / (1 + ratio))
    return mfi.fillna(50).values


def _rsi(cl, period):
    delta = pd.Series(cl).diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50).values


def _macd(cl):
    ema12 = pd.Series(cl).ewm(span=12, adjust=False).mean()
    ema26 = pd.Series(cl).ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return macd.values, signal.values, hist.values


def _adx(hi, lo, cl, period):
    tr = np.maximum(hi - lo, np.maximum(
        np.abs(hi - pd.Series(cl).shift(1).values),
        np.abs(lo - pd.Series(cl).shift(1).values)
    ))
    up = hi - pd.Series(hi).shift(1).values
    down = pd.Series(lo).shift(1).values - lo
    plus_dm = np.where((up > down) & (up > 0), up, 0)
    minus_dm = np.where((down > up) & (down > 0), down, 0)
    atr_s = pd.Series(tr).rolling(period).mean().replace(0, np.nan).ffill().values
    pdi = 100 * pd.Series(plus_dm).rolling(period).mean() / atr_s
    ndi = 100 * pd.Series(minus_dm).rolling(period).mean() / atr_s
    dx = 100 * np.abs(pdi - ndi) / (pdi + ndi).replace(0, np.nan)
    adx = dx.rolling(period).mean()
    return adx.values, pdi.values, ndi.values


def _vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df = df.copy()
    if "timestamp" in df.columns:
        df["_date"] = pd.to_datetime(df["timestamp"]).dt.date
    else:
        df["_date"] = "all"
    vwap = df.groupby("_date").apply(
        lambda g: (tp.loc[g.index] * g["volume"]).cumsum() / g["volume"].cumsum(),
        include_groups=False
    )
    if isinstance(vwap, pd.DataFrame):
        vwap = vwap.iloc[:, 0]
    return vwap.values if vwap is not None else tp.values


def _fvg(hi, lo, n):
    bullish = np.zeros(n, dtype=int)
    bearish = np.zeros(n, dtype=int)
    for i in range(n - 2):
        if lo[i + 2] > hi[i]:
            bullish[i + 2] = 1
        if hi[i + 2] < lo[i]:
            bearish[i + 2] = 1
    return bullish, bearish


def _bos(hi, lo, cl, n, lookback=10):
    bullish = np.zeros(n, dtype=int)
    bearish = np.zeros(n, dtype=int)
    for i in range(lookback * 2, n):
        left_high = max(hi[i - lookback * 2:i - lookback])
        right_low = min(lo[i - lookback:i])
        if cl[i] > left_high and lo[i] > right_low:
            bullish[i] = 1
        left_low = min(lo[i - lookback * 2:i - lookback])
        right_high = max(hi[i - lookback:i])
        if cl[i] < left_low and hi[i] < right_high:
            bearish[i] = 1
    return bullish, bearish
