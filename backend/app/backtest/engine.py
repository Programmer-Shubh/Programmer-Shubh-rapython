from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from typing import Any

import polars as pl

from ..config import settings
from ..lakehouse.query import get_contract_series, get_underlying_series
from .signals import compute_indicators, generate_entry_signals, resolve_strike
from .fills import entry_fill, exit_fill, next_price
from .risk import check_exits, is_intraday_exit_time
from .analytics import compute_trade_metrics, compute_portfolio_metrics, oos_metrics

STRIKE_STEP = 50  # NIFTY-style default; configurable per strategy later
IST = ZoneInfo("Asia/Kolkata")


class BacktestEngine:
    """
    100% offline backtest engine. Every leg runs strictly on real option tick or
    1-minute option chain data from the Parquet lakehouse — zero Black-Scholes,
    zero synthetic premiums.
    """

    def __init__(self, db, underlying: str = "NIFTY"):
        self.db = db
        self.underlying = underlying
        self._expiry_cache: dict[tuple[str, str], list[date]] = {}

    # ------------------------------------------------------------------ load
    def _load_underlying_bars(self, start_ts, end_ts) -> tuple:
        df = get_underlying_series(self.underlying, "tick", start_ts, end_ts)
        if df.is_empty():
            df = get_underlying_series(self.underlying, "1m_chain", start_ts, end_ts)
        if df.is_empty():
            return None, None
        # Resample ticks into 1-minute bars for indicators
        bars = (
            df.group_by_dynamic("timestamp", every="1m")
            .agg(
                pl.col("ltp").first().alias("open"),
                pl.col("ltp").max().alias("high"),
                pl.col("ltp").min().alias("low"),
                pl.col("ltp").last().alias("close"),
                pl.col("volume").sum().alias("volume"),
            )
            .sort("timestamp")
        )
        return df, compute_indicators(bars)

    # ------------------------------------------------------- contract lookup
    def _available_expiries(self) -> list[date]:
        """Cached list of expiries with cached CE/PE option data for the underlying."""
        key = (self.underlying, "tick")
        if key in self._expiry_cache:
            return self._expiry_cache[key]
        from ..config import settings
        import duckdb

        con = duckdb.connect()
        try:
            rows = con.execute(
                f"""
                SELECT DISTINCT CAST(expiry AS DATE) AS e
                FROM read_parquet('{settings.LAKEHOUSE_DIR}/**/*.parquet', hive_partitioning=1)
                WHERE underlying = ? AND data_mode IN ('tick','1m_chain')
                  AND option_type IN ('CE','PE')
                ORDER BY e
                """,
                [self.underlying],
            ).fetchall()
        finally:
            con.close()
        expiries = sorted({r[0] for r in rows if r[0] is not None})
        self._expiry_cache[key] = expiries
        return expiries

    def _resolve_expiry(self, signal_date: date, expiry_type: str) -> date:
        """Weekly = nearest Thursday expiry with cached chain data; monthly = last Thursday of month."""
        expiries = self._available_expiries()
        future = [e for e in expiries if e >= signal_date]
        if not future:
            d = signal_date
            while d.weekday() != 3:
                d += timedelta(days=1)
            return d
        if expiry_type == "monthly":
            month_end = (signal_date.replace(day=28) + timedelta(days=7)).replace(day=1) - timedelta(days=1)
            last_thu = month_end
            while last_thu.weekday() != 3:
                last_thu -= timedelta(days=1)
            monthly_in_range = [e for e in expiries if e.month == signal_date.month]
            return monthly_in_range[-1] if monthly_in_range else future[0]
        return future[0]

    # ---------------------------------------------------------------- simulate
    def _simulate_trade(self, signal, data_mode, sl, tp, trailing, slippage, latency, lot_size):
        """Simulate one trade strictly on the resolved option contract's real series."""
        ts = signal["timestamp"]
        side = signal["side"]  # BUY_CE / BUY_PE
        option_type = "CE" if side == "BUY_CE" else "PE"
        strike = resolve_strike(signal["underlying_ltp"], signal["strike_offset"], STRIKE_STEP)
        expiry = self._resolve_expiry(ts.date() if hasattr(ts, "date") else ts.date(), signal["expiry_type"])

        start_ts = ts
        end_ts = datetime.combine(ts.date() if hasattr(ts, "date") else ts.date(), time(15, 30), tzinfo=IST)

        series = get_contract_series(
            self.underlying, data_mode, expiry, option_type, strike, start_ts, end_ts
        )
        if series.is_empty():
            return None  # no real data for this contract — trade not taken (no synthetic pricing)

        rows = series.to_dicts()
        position = None
        exit_price = None
        exit_reason = None
        exit_row = None

        for i, row in enumerate(rows):
            # latency: entry fill on next tick/bar after signal
            fill_row = row if i == 0 else next_price(rows, i - 1, latency) if i > 0 else row
            if i == 0:
                fill_row = row

            if position is None:
                price = entry_fill(fill_row, "BUY", slippage)
                if price is None:
                    continue  # illiquid tick — wait for next valid quote
                position = {
                    "side": side,
                    "entry_price": price,
                    "entry_time": fill_row["timestamp"],
                    "peak": price,
                }
                continue

            # Mark-to-market excursions
            cur_high = fill_row.get("high") or fill_row.get("ltp") or position["entry_price"]
            cur_low = fill_row.get("low") or fill_row.get("ltp") or position["entry_price"]
            mfe = cur_high - position["entry_price"]
            mae = position["entry_price"] - cur_low
            position["mfe"] = max(position.get("mfe", 0.0), mfe)
            position["mae"] = max(position.get("mae", 0.0), mae)

            # Risk exits on REAL option data
            exit_px, reason = check_exits(fill_row, position, sl, tp, trailing)
            if exit_px is not None:
                exit_price = exit_px
                exit_reason = reason
                exit_row = fill_row
                break

            if is_intraday_exit_time(fill_row["timestamp"]):
                exit_price = exit_fill(fill_row, side, slippage, position["entry_price"])
                exit_reason = "Intraday Exit 15:15"
                exit_row = fill_row
                break

        if position is None:
            return None
        if exit_price is None:
            # Force close at last real quote of the day
            last = rows[-1]
            exit_price = exit_fill(last, side, slippage, position["entry_price"])
            exit_reason = "Open (EOD close)"
            exit_row = last

        pnl_points = round(exit_price - position["entry_price"], 2)
        return {
            "entry_timestamp": position["entry_time"],
            "exit_timestamp": exit_row["timestamp"],
            "entry_price": round(position["entry_price"], 2),
            "exit_price": round(exit_price, 2),
            "pnl_points": pnl_points,
            "pnl": round(pnl_points * lot_size, 2),
            "strike": strike,
            "expiry": expiry,
            "option_type": option_type,
            "mae": round(position.get("mae", 0.0), 2),
            "mfe": round(position.get("mfe", 0.0), 2),
            "status": exit_reason or "Open",
            "exit_reason": exit_reason,
            "data_source": "TICK DATA" if data_mode == "tick" else "1M OPTION CHAIN",
        }

    # -------------------------------------------------------------------- run
    def run(self, strategy_config: dict, data_mode: str, start_date, end_date, lot_size=None, walk_forward=False):
        lot_size = lot_size or settings.DEFAULT_LOT_SIZE
        cfg = strategy_config
        sl = float(cfg.get("sl_points", 20.0))
        tp = float(cfg.get("tp_points", 40.0))
        trailing = float(cfg.get("trailing_sl_points", 0.0))
        slippage = float(cfg.get("slippage_pct", settings.DEFAULT_SLIPPAGE_PCT))
        latency = cfg.get("latency", settings.DEFAULT_LATENCY_MODE)

        start_ts = datetime.combine(start_date, time(9, 15), tzinfo=IST)
        end_ts = datetime.combine(end_date, time(15, 30), tzinfo=IST)

        tick_df, bars = self._load_underlying_bars(start_ts, end_ts)
        if bars is None or bars.is_empty():
            return {
                "trades": [],
                "metrics": {"trades": 0, "net_pnl": 0.0, "message": "No cached underlying data for range"},
                "portfolio": {"sharpe": 0.0, "calmar": 0.0, "max_drawdown": 0.0, "max_drawdown_pct": 0.0},
                "data_source": "TICK DATA" if data_mode == "tick" else "1M OPTION CHAIN",
            }

        signals = generate_entry_signals(bars, cfg)
        trades = []
        for sig in signals:
            res = self._simulate_trade(sig, data_mode, sl, tp, trailing, slippage, latency, lot_size)
            if res:
                entry_ts = res["entry_timestamp"]
                exit_ts = res["exit_timestamp"]
                trades.append(
                    {
                        "entry_date": entry_ts.date() if hasattr(entry_ts, "date") else entry_ts.date(),
                        "entry_time": entry_ts.strftime("%H:%M:%S") if hasattr(entry_ts, "strftime") else str(entry_ts)[11:19],
                        "exit_date": exit_ts.date() if hasattr(exit_ts, "date") else exit_ts.date(),
                        "exit_time": exit_ts.strftime("%H:%M:%S") if hasattr(exit_ts, "strftime") else str(exit_ts)[11:19],
                        "symbol": self.underlying,
                        "trade_type": res["option_type"],
                        "strike": res["strike"],
                        "expiry": res["expiry"],
                        "entry_price": res["entry_price"],
                        "exit_price": res["exit_price"],
                        "status": res["status"],
                        "pnl": res["pnl"],
                        "mae": res["mae"],
                        "mfe": res["mfe"],
                        "exit_reason": res["exit_reason"],
                        "is_oos": False,
                        "data_source": res["data_source"],
                    }
                )

        metrics = compute_trade_metrics(trades, lot_size)
        portfolio = compute_portfolio_metrics(trades, lot_size)

        result = {
            "trades": trades,
            "metrics": {**metrics, **portfolio},
            "portfolio": portfolio,
            "data_source": "TICK DATA" if data_mode == "tick" else "1M OPTION CHAIN",
            "signals_evaluated": len(signals),
        }

        if walk_forward:
            mid = start_date + (end_date - start_date) / 2
            is_res = self.run(cfg, data_mode, start_date, mid, lot_size, walk_forward=False)
            oos_res = self.run(cfg, data_mode, mid + timedelta(days=1), end_date, lot_size, walk_forward=False)
            for t in oos_res["trades"]:
                t["is_oos"] = True
            result["walk_forward"] = oos_metrics(is_res["trades"], oos_res["trades"], lot_size)
            result["is_metrics"] = is_res["metrics"]
            result["oos_metrics"] = oos_res["metrics"]
            result["trades"] = is_res["trades"] + oos_res["trades"]
            for t in result["trades"]:
                pass
            result["metrics"] = compute_trade_metrics(result["trades"], lot_size)
            result["portfolio"] = compute_portfolio_metrics(result["trades"], lot_size)

        return result
