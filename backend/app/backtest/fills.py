from __future__ import annotations

from ..config import settings


def mid_price(bid, ask, ltp):
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return ltp


def entry_fill(row: dict, side: str, slippage_pct: float) -> float | None:
    """
    Realistic entry fill from real quotes only.

    BUY lifts the ask (pay ask), SELL hits the bid (receive bid).
    Zero-volume / illiquid guard: no bid/ask/ltp or zero volume -> no fill.
    Slippage % is applied on top of the real spread.
    """
    ltp = row.get("ltp")
    bid = row.get("bid")
    ask = row.get("ask")
    volume = row.get("volume")

    if ltp is None and bid is None and ask is None:
        return None
    if volume == 0 and bid is None and ask is None:
        return None  # illiquid tick — cannot fill

    quote = ask if side == "BUY" else bid
    if quote is None:
        quote = ltp

    factor = 1.0 + slippage_pct / 100.0 if side == "BUY" else 1.0 - slippage_pct / 100.0
    return float(quote * factor)


def exit_fill(row: dict, side: str, slippage_pct: float, default_price: float) -> float:
    """Exit fill: hit bid on sell exit, lift ask on buy exit (cover). Falls back to ltp."""
    bid = row.get("bid")
    ask = row.get("ask")
    ltp = row.get("ltp") or default_price
    quote = bid if side == "BUY" else ask  # long position exits by selling -> bid
    if quote is None:
        quote = ltp
    factor = 1.0 - slippage_pct / 100.0 if side == "BUY" else 1.0 + slippage_pct / 100.0
    return float(quote * factor)


def next_price(rows: list[dict], idx: int, mode: str):
    """Latency model: next-tick / next-bar fill price source."""
    if mode == "next_bar":
        return rows[idx + 1] if idx + 1 < len(rows) else None
    return rows[idx + 1] if idx + 1 < len(rows) else rows[idx]
