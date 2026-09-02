import numpy as np
import pandas as pd
from typing import Optional, Tuple


def triple_barrier_labels(
    df: pd.DataFrame,
    pt_sl: float = 0.05,
    max_hold: int = 20,
    min_ret: float = 0.005,
) -> pd.DataFrame:
    """Triple-barrier labeling for financial time series.

    For each bar, label:
      - 1  (win / bullish)  if price hits pt_sl % profit target first
      - -1 (loss / bearish) if price hits -pt_sl % stop loss first
      - 0  (neutral)        if max_hold bars expire before either barrier

    Also returns the return at exit and the number of bars held.
    """
    if df.empty or len(df) < max_hold + 10:
        df["label"] = 0
        df["exit_ret"] = 0.0
        df["bars_held"] = 0
        return df

    close = df["close"].values
    n = len(close)
    labels = np.zeros(n, dtype=int)
    exit_rets = np.zeros(n, dtype=float)
    bars_held = np.zeros(n, dtype=int)

    for i in range(n - 1):
        entry = close[i]
        upper = entry * (1 + pt_sl)
        lower = entry * (1 - pt_sl)

        for j in range(i + 1, min(i + max_hold + 1, n)):
            if close[j] >= upper:
                labels[i] = 1
                exit_rets[i] = (close[j] - entry) / entry
                bars_held[i] = j - i
                break
            if close[j] <= lower:
                labels[i] = -1
                exit_rets[i] = (close[j] - entry) / entry
                bars_held[i] = j - i
                break
        else:
            # Max hold reached — label based on final return
            final_ret = (close[min(i + max_hold, n - 1)] - entry) / entry
            if final_ret > min_ret:
                labels[i] = 1
            elif final_ret < -min_ret:
                labels[i] = -1
            else:
                labels[i] = 0
            exit_rets[i] = final_ret
            bars_held[i] = min(max_hold, n - 1 - i)

    df["label"] = labels
    df["exit_ret"] = exit_rets
    df["bars_held"] = bars_held
    return df


def purged_train_test_split(
    df: pd.DataFrame,
    test_size: float = 0.2,
    embargo: int = 10,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Purged walk-forward split to prevent data leakage.

    Test set: last test_size fraction of data.
    Train set: everything before test, minus embargo bars.
    Embargo bars are removed from train set to prevent leakage.
    """
    if df.empty:
        return df, df

    n = len(df)
    split_idx = int(n * (1 - test_size))

    train = df.iloc[:split_idx - embargo].copy()
    test = df.iloc[split_idx:].copy()

    return train, test
