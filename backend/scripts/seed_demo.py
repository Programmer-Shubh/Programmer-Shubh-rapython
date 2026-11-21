"""Seed RaTrade with demo option data + one completed backtest run (offline, no broker).

DEMO DATA ONLY — replace with real broker feed uploads or the AutoRecorder in production.
No Black-Scholes synthetic pricing is used; these are stored option ticks/chain snapshots.
"""
from __future__ import annotations

import random
import sys
from datetime import date, datetime, time, timedelta, timezone

sys.path.insert(0, "C:/Users/hp/ratrade/backend")

import polars as pl

from app.db import SessionLocal, init_db
from app.models import BacktestRun, Strategy, Trade, UploadRecord
from app.lakehouse.schema import arrow_table, SCHEMA_COLUMNS, write_parquet_atomic
from app.lakehouse.ingestion import _refresh_catalog
from app.lakehouse.partitioning import partition_dir
from app.backtest.engine import BacktestEngine
from app.config import settings

random.seed(42)
UNDERLYING = "NIFTY"
SPOT = 23750.0
STRIKES = list(range(23600, 23901, 50))
EXPIRIES = [date(2026, 11, 19), date(2026, 11, 26)]  # weeklies (Thursdays)
TRADING_DAYS = [date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20)]
TZ = "Asia/Kolkata"


def ist(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))


def gen_underlying(trade_date: date):
    rows = []
    t = datetime.combine(trade_date, time(9, 15))
    end = datetime.combine(trade_date, time(15, 30))
    px = SPOT
    while t <= end:
        px += random.uniform(-3, 3)
        rows.append(
            {
                "timestamp": ist(t),
                "underlying": UNDERLYING,
                "data_mode": "tick",
                "expiry": EXPIRIES[0],
                "option_type": "UNDERLYING",
                "strike": 0.0,
                "ltp": round(px, 2),
                "bid": round(px - 1, 2),
                "ask": round(px + 1, 2),
                "oi": 0,
                "volume": random.randint(50, 500),
                "trade_date": trade_date,
                "source": "demo",
            }
        )
        t += timedelta(seconds=5)
    return rows


def gen_option_chain(trade_date: date, expiry: date):
    rows = []
    days_to_expiry = max((expiry - trade_date).days, 0)
    for strike in STRIKES:
        for opt in ("CE", "PE"):
            intrinsic = max(SPOT - strike, 0) if opt == "CE" else max(strike - SPOT, 0)
            base = intrinsic + max(40 - days_to_expiry * 8, 4) + random.uniform(-2, 2)
            t = datetime.combine(trade_date, time(9, 15))
            end = datetime.combine(trade_date, time(15, 30))
            px = max(base, 1.0)
            while t <= end:
                px = max(px + random.uniform(-0.6, 0.6), 0.5)
                illiquid = random.random() < 0.05  # ~5% zero-volume ticks (guard test)
                bid = round(max(px - 0.7, 0.2), 2) if not illiquid else None
                ask = round(px + 0.7, 2) if not illiquid else None
                rows.append(
                    {
                        "timestamp": ist(t),
                        "underlying": UNDERLYING,
                        "data_mode": "tick",
                        "expiry": expiry,
                        "option_type": opt,
                        "strike": float(strike),
                        "ltp": round(px, 2),
                        "bid": bid,
                        "ask": ask,
                        "oi": random.randint(100, 5000),
                        "volume": 0 if illiquid else random.randint(1, 200),
                        "trade_date": trade_date,
                        "source": "demo",
                    }
                )
                t += timedelta(seconds=30)
    return rows


def main():
    init_db()
    db = SessionLocal()
    try:
        # Idempotent: clear prior demo seed so re-runs don't hit unique constraints.
        db.query(UploadRecord).filter_by(file_hash="demo-seed-001").delete(synchronize_session=False)
        db.query(Strategy).filter_by(name="SuperTrend NIFTY Option Strategy").delete(synchronize_session=False)
        db.commit()

        all_rows = []
        for td in TRADING_DAYS:
            all_rows.extend(gen_underlying(td))
            for ex in EXPIRIES:
                all_rows.extend(gen_option_chain(td, ex))

        df = pl.DataFrame(all_rows)
        # Persist partition-by-partition into the lakehouse (atomic ZSTD Parquet)
        total = 0
        for keys, group in df.group_by(
            ["underlying", "data_mode", "expiry", "trade_date"], maintain_order=True
        ):
            underlying, data_mode, expiry, trade_date = keys
            from app.lakehouse.schema import normalize_option_frame

            norm = normalize_option_frame(group)
            for _k, g in norm.group_by(
                ["underlying", "data_mode", "expiry", "trade_date"], maintain_order=True
            ):
                pdir = partition_dir(
                    g["underlying"][0], g["data_mode"][0], g["expiry"][0], g["trade_date"][0]
                )
                pdir.mkdir(parents=True, exist_ok=True)
                out = pdir / f"part-seed-{total}.parquet"
                write_parquet_atomic(arrow_table(g.select(SCHEMA_COLUMNS + ["trade_date", "ingested_at"])), out)
                total += len(g)

        _refresh_catalog(db)
        db.add(UploadRecord(filename="demo_option_ticks.parquet", file_hash="demo-seed-001", file_size=0, rows_ingested=total, status="ingested"))

        # Demo strategy
        strategy = Strategy(
            name="SuperTrend NIFTY Option Strategy",
            user_notes="Demo strategy seeded for offline backtesting. User-editable notes.",
            config_json={
                "indicators": ["supertrend", "rsi", "adx", "vwap", "ema"],
                "strike_offset": "ATM",
                "expiry_type": "weekly",
                "option_side": "both",
                "sl_points": 15.0,
                "tp_points": 35.0,
                "trailing_sl_points": 7.0,
                "slippage_pct": 0.05,
                "latency": "next_tick",
                "lot_size": 50,
            },
        )
        db.add(strategy)
        db.commit()
        db.refresh(strategy)

        # Run backtest offline (IS Nov 18–19, OOS Nov 20 — walk-forward)
        engine = BacktestEngine(db, UNDERLYING)
        result = engine.run(
            strategy.config_json,
            "tick",
            date(2026, 11, 18),
            date(2026, 11, 20),
            lot_size=50,
            walk_forward=True,
        )

        run = BacktestRun(
            strategy_id=strategy.id,
            status="completed",
            data_mode="tick",
            data_source="TICK DATA",
            use_walk_forward=True,
            is_start=date(2026, 11, 18),
            is_end=date(2026, 11, 19),
            oos_start=date(2026, 11, 20),
            oos_end=date(2026, 11, 20),
            metrics_json=result["metrics"],
            summary_json={
                "strategy": strategy.name,
                "data_mode": "tick",
                "data_source": "TICK DATA",
                "date_range": "2026-11-18 to 2026-11-20 (walk-forward: IS 18-19 / OOS 20)",
                "signals_evaluated": result.get("signals_evaluated", 0),
                "trades_executed": len(result["trades"]),
                "net_pnl": result["metrics"].get("net_pnl", 0.0),
                "win_rate": result["metrics"].get("win_rate", 0.0),
                "profit_factor": result["metrics"].get("profit_factor", 0.0),
                "expectancy": result["metrics"].get("expectancy", 0.0),
                "sharpe": result["metrics"].get("sharpe", 0.0),
                "calmar": result["metrics"].get("calmar", 0.0),
                "max_drawdown": result["metrics"].get("max_drawdown", 0.0),
                "run_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        db.add_all(
            Trade(
                run_id=run.id,
                entry_date=t["entry_date"],
                entry_time=t["entry_time"],
                exit_date=t["exit_date"],
                exit_time=t["exit_time"],
                symbol=t["symbol"],
                trade_type=t["trade_type"],
                strike=t["strike"],
                expiry=t["expiry"],
                entry_price=t["entry_price"],
                exit_price=t["exit_price"],
                status=t["status"],
                pnl=t["pnl"],
                mae=t["mae"],
                mfe=t["mfe"],
                exit_reason=t.get("exit_reason"),
                is_oos=t.get("is_oos", False),
            )
            for t in result["trades"]
        )
        db.commit()
        print(f"SEED_OK: {len(all_rows)} ticks cached, {len(result['trades'])} trades, run_id={run.id}")
        print("METRICS:", result["metrics"])
        if "walk_forward" in result:
            print("WALK_FORWARD:", result["walk_forward"])
    finally:
        db.close()


if __name__ == "__main__":
    main()
