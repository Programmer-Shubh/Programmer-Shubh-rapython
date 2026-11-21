from __future__ import annotations

from datetime import time

from ..config import settings


def parse_hhmm(value: str) -> time:
    h, m = str(value).split(":")
    return time(int(h), int(m))


INTRADAY_EXIT = parse_hhmm(settings.INTRADAY_EXIT_TIME)


def check_exits(
    row: dict,
    position: dict,
    sl_points: float,
    tp_points: float,
    trailing_points: float,
) -> tuple[float | None, str | None]:
    """
    Check SL / TP / trailing SL / intraday exit against REAL option bar/tick data.

    Returns (exit_price, reason) or (None, None) if nothing triggered.
    High/low of the bar govern intra-bar exits; ltp for ticks.
    """
    side = position["side"]  # "BUY_CE" / "BUY_PE" (long option premium)
    entry = position["entry_price"]
    high = row.get("high") or row.get("ltp") or entry
    low = row.get("low") or row.get("ltp") or entry

    # Update peak for trailing (favorable excursion from entry)
    peak = position.get("peak", entry)
    if side in ("BUY_CE", "BUY_PE"):
        peak = max(peak, high)
    position["peak"] = peak

    # 1) Stop loss
    if side in ("BUY_CE", "BUY_PE") and low <= entry - sl_points:
        return entry - sl_points, "SL hit"
    # 2) Take profit
    if side in ("BUY_CE", "BUY_PE") and high >= entry + tp_points:
        return entry + tp_points, "TP hit"
    # 3) Trailing SL: activate after profit >= trailing_points, trail at peak - trailing
    if trailing_points > 0 and peak >= entry + trailing_points:
        trail_stop = peak - trailing_points
        if low <= trail_stop:
            return trail_stop, "Trailing SL hit"

    return None, None


def is_intraday_exit_time(ts) -> bool:
    try:
        t = ts.time() if hasattr(ts, "time") else __import__("datetime").datetime.fromisoformat(str(ts)).time()
    except Exception:
        return False
    return t >= INTRADAY_EXIT
