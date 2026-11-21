from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

from core.models.database import Database

try:
    import polars as pl

    HAS_POLARS = True
except ImportError:
    pl = None  # type: ignore
    HAS_POLARS = False

import pandas as pd

_ROOT2 = math.sqrt(2.0)


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _ROOT2))


def _normal_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


class PolarsEngine:
    def __init__(self) -> None:
        self._db = Database.get_instance()
        self._db_path: str = self._db._path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _execute(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _rows_to_polars(rows: List[Dict[str, Any]]) -> "pl.DataFrame":
        if not rows:
            return pl.DataFrame()
        return pl.DataFrame(rows)

    @staticmethod
    def _rows_to_pandas(rows: List[Dict[str, Any]]) -> pd.DataFrame:
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    def load_bhavcopy_polars(
        self, symbol: str, start_date: str, end_date: str
    ) -> Union["pl.DataFrame", pd.DataFrame]:
        rows = self._execute(
            """SELECT symbol, trade_date AS date, expiry_date AS expiry,
                      strike_price AS strike, option_type, open_price AS open,
                      high_price AS high, low_price AS low, close_price AS close,
                      volume, oi AS open_interest
               FROM bhavcopy_data
               WHERE symbol=? AND trade_date BETWEEN ? AND ?
               ORDER BY trade_date, option_type, strike_price""",
            (symbol, start_date, end_date),
        )
        if HAS_POLARS:
            df = self._rows_to_polars(rows)
            if df.height == 0:
                return df
            df = df.with_columns(
                pl.col("date").cast(pl.Utf8),
                pl.col("expiry").cast(pl.Utf8),
                pl.col("strike").cast(pl.Float64),
                pl.col("option_type").cast(pl.Utf8),
                pl.col("open").cast(pl.Float64),
                pl.col("high").cast(pl.Float64),
                pl.col("low").cast(pl.Float64),
                pl.col("close").cast(pl.Float64),
                pl.col("volume").cast(pl.Int64),
                pl.col("open_interest").cast(pl.Int64),
            )
            return df
        return self._rows_to_pandas(rows)

    def load_option_chain_polars(
        self, symbol: str, date: str, expiry: str
    ) -> Union["pl.DataFrame", pd.DataFrame]:
        rows = self._execute(
            """SELECT strike_price AS strike, option_type, open_price AS open,
                      high_price AS high, low_price AS low, close_price AS close,
                      volume, oi AS open_interest
               FROM bhavcopy_data
               WHERE symbol=? AND trade_date=? AND expiry_date=?
                 AND option_type IS NOT NULL
               ORDER BY strike_price, option_type""",
            (symbol, date, expiry),
        )
        if HAS_POLARS:
            return self._rows_to_polars(rows)
        return self._rows_to_pandas(rows)

    def calculate_greeks_polars(
        self,
        strike: float,
        spot: float,
        r: float,
        sigma: float,
        t: float,
        option_type: str,
    ) -> Dict[str, float]:
        if spot <= 0 or strike <= 0 or t <= 0 or sigma <= 0:
            delta = 0.5 if option_type == "CE" else -0.5
            return {
                "delta": delta,
                "gamma": 0.0,
                "theta": 0.0,
                "vega": 0.0,
                "iv": sigma,
            }

        sqrt_t = math.sqrt(t)
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (
            sigma * sqrt_t
        )
        d2 = d1 - sigma * sqrt_t
        nd1 = _normal_cdf(d1)
        pdf_d1 = _normal_pdf(d1)
        nd2 = _normal_cdf(d2)

        if option_type == "CE":
            delta = nd1
            theta_annual = (
                -spot * pdf_d1 * sigma / (2 * sqrt_t)
                - r * strike * math.exp(-r * t) * nd2
            )
        else:
            delta = nd1 - 1.0
            theta_annual = (
                -spot * pdf_d1 * sigma / (2 * sqrt_t)
                + r * strike * math.exp(-r * t) * _normal_cdf(-d2)
            )

        gamma = pdf_d1 / (spot * sigma * sqrt_t)
        vega = spot * pdf_d1 * sqrt_t / 100.0
        theta_per_day = theta_annual / 365.0

        return {
            "delta": round(delta, 6),
            "gamma": round(gamma, 6),
            "theta": round(theta_per_day, 6),
            "vega": round(vega, 6),
            "iv": sigma,
        }

    def calculate_greeks_vectorized(
        self,
        strikes: List[float],
        spot: float,
        r: float,
        sigmas: List[float],
        t: float,
        option_type: str,
    ) -> Union["pl.DataFrame", pd.DataFrame]:
        results = []
        for strike, sigma in zip(strikes, sigmas):
            greeks = self.calculate_greeks_polars(strike, spot, r, sigma, t, option_type)
            greeks["strike"] = strike
            results.append(greeks)

        if HAS_POLARS:
            return self._rows_to_polars(results)
        return self._rows_to_pandas(results)

    def build_option_chain_polars(
        self,
        data: Union["pl.DataFrame", pd.DataFrame],
        spot: float,
        expiry: str,
    ) -> Union["pl.DataFrame", pd.DataFrame]:
        if HAS_POLARS and isinstance(data, pl.DataFrame):
            if data.height == 0:
                return data

            chain = data.filter(pl.col("expiry") == expiry)
            if chain.height == 0:
                chain = data

            strikes = chain.get_column("strike").unique().sort().to_list()

            ce_data = chain.filter(pl.col("option_type") == "CE")
            pe_data = chain.filter(pl.col("option_type") == "PE")

            ce_map = {
                row["strike"]: row
                for row in ce_data.iter_rows(named=True)
            }
            pe_map = {
                row["strike"]: row
                for row in pe_data.iter_rows(named=True)
            }

            results = []
            for strike in strikes:
                ce = ce_map.get(strike, {})
                pe = pe_map.get(strike, {})

                ce_close = ce.get("close", 0) or 0
                pe_close = pe.get("close", 0) or 0
                intrinsic_ce = max(0, spot - strike)
                intrinsic_pe = max(0, strike - spot)
                itm_ce = 1 if spot > strike else 0
                itm_pe = 1 if strike > spot else 0
                itm_atm = "ITM" if spot > strike else ("OTM" if spot < strike else "ATM")

                results.append(
                    {
                        "strike": strike,
                        "ce_ltp": ce_close,
                        "ce_volume": ce.get("volume", 0) or 0,
                        "ce_oi": ce.get("open_interest", 0) or 0,
                        "ce_change": ce_close - (ce.get("open", 0) or ce_close),
                        "ce_intrinsic": intrinsic_ce,
                        "ce_itm": itm_ce,
                        "pe_ltp": pe_close,
                        "pe_volume": pe.get("volume", 0) or 0,
                        "pe_oi": pe.get("open_interest", 0) or 0,
                        "pe_change": pe_close - (pe.get("open", 0) or pe_close),
                        "pe_intrinsic": intrinsic_pe,
                        "pe_itm": itm_pe,
                        "moneyness": itm_atm,
                        "spot": spot,
                        "expiry": expiry,
                    }
                )

            return self._rows_to_polars(results)

        if isinstance(data, pd.DataFrame) and not data.empty:
            chain = data[data["expiry"] == expiry]
            if chain.empty:
                chain = data

            results = []
            for strike in sorted(chain["strike"].unique()):
                ce_row = chain[(chain["strike"] == strike) & (chain["option_type"] == "CE")]
                pe_row = chain[(chain["strike"] == strike) & (chain["option_type"] == "PE")]

                ce_close = float(ce_row["close"].iloc[0]) if not ce_row.empty else 0
                pe_close = float(pe_row["close"].iloc[0]) if not pe_row.empty else 0
                ce_vol = int(ce_row["volume"].iloc[0]) if not ce_row.empty else 0
                pe_vol = int(pe_row["volume"].iloc[0]) if not pe_row.empty else 0
                ce_oi = int(ce_row["open_interest"].iloc[0]) if not ce_row.empty else 0
                pe_oi = int(pe_row["open_interest"].iloc[0]) if not pe_row.empty else 0
                ce_open = float(ce_row["open"].iloc[0]) if not ce_row.empty else ce_close
                pe_open = float(pe_row["open"].iloc[0]) if not pe_row.empty else pe_close

                results.append(
                    {
                        "strike": strike,
                        "ce_ltp": ce_close,
                        "ce_volume": ce_vol,
                        "ce_oi": ce_oi,
                        "ce_change": ce_close - ce_open,
                        "ce_intrinsic": max(0, spot - strike),
                        "ce_itm": 1 if spot > strike else 0,
                        "pe_ltp": pe_close,
                        "pe_volume": pe_vol,
                        "pe_oi": pe_oi,
                        "pe_change": pe_close - pe_open,
                        "pe_intrinsic": max(0, strike - spot),
                        "pe_itm": 1 if strike > spot else 0,
                        "moneyness": "ITM" if spot > strike else ("OTM" if spot < strike else "ATM"),
                        "spot": spot,
                        "expiry": expiry,
                    }
                )

            return pd.DataFrame(results)

        return self._rows_to_polars([])

    def detect_support_resistance_polars(
        self, df: Union["pl.DataFrame", pd.DataFrame]
    ) -> Union["pl.DataFrame", pd.DataFrame]:
        if HAS_POLARS and isinstance(df, pl.DataFrame):
            if df.height == 0:
                return df

            close = df.get_column("close").to_list()
            high = df.get_column("high").to_list()
            low = df.get_column("low").to_list()

            levels = self._find_sr_levels(close, high, low)

            result = [
                {
                    "level": lvl,
                    "type": "support" if lvl < close[-1] else "resistance",
                    "strength": strength,
                    "touches": touches,
                }
                for lvl, strength, touches in levels
            ]

            return self._rows_to_polars(result)

        if isinstance(df, pd.DataFrame) and not df.empty:
            close = df["close"].tolist()
            high = df["high"].tolist()
            low = df["low"].tolist()

            levels = self._find_sr_levels(close, high, low)

            return pd.DataFrame(
                [
                    {
                        "level": lvl,
                        "type": "support" if lvl < close[-1] else "resistance",
                        "strength": strength,
                        "touches": touches,
                    }
                    for lvl, strength, touches in levels
                ]
            )

        return self._rows_to_polars([])

    @staticmethod
    def _find_sr_levels(
        close: List[float], high: List[float], low: List[float]
    ) -> List[Tuple[float, float, int]]:
        if not close:
            return []

        all_levels: Dict[float, int] = {}
        tolerance_pct = 0.5

        for i in range(1, len(high) - 1):
            if high[i] > high[i - 1] and high[i] > high[i + 1]:
                key = round(high[i], 2)
                all_levels[key] = all_levels.get(key, 0) + 1
            if low[i] < low[i - 1] and low[i] < low[i + 1]:
                key = round(low[i], 2)
                all_levels[key] = all_levels.get(key, 0) + 1

        for price in close:
            key = round(price, 2)
            all_levels[key] = all_levels.get(key, 0) + 1

        if not all_levels:
            current = close[-1]
            return [(current, 1.0, 0)]

        merged: List[Tuple[float, int]] = []
        sorted_levels = sorted(all_levels.items())
        for price, count in sorted_levels:
            if merged and abs(price - merged[-1][0]) / max(merged[-1][0], 0.01) * 100 < tolerance_pct:
                merged[-1] = (merged[-1][0], merged[-1][1] + count)
            else:
                merged.append((price, count))

        max_count = max(c for _, c in merged) if merged else 1
        current = close[-1]

        levels = []
        for price, count in merged:
            strength = count / max_count
            if strength >= 0.2:
                levels.append((price, round(strength, 3), count))

        levels.sort(key=lambda x: -x[1])
        return levels[:10]

    def calculate_pivot_points_polars(
        self, high: float, low: float, close: float
    ) -> Dict[str, float]:
        pivot = (high + low + close) / 3.0

        s1 = 2 * pivot - high
        s2 = pivot - (high - low)
        s3 = low - 2 * (high - pivot)

        r1 = 2 * pivot - low
        r2 = pivot + (high - low)
        r3 = high + 2 * (pivot - low)

        return {
            "pivot": round(pivot, 2),
            "s1": round(s1, 2),
            "s2": round(s2, 2),
            "s3": round(s3, 2),
            "r1": round(r1, 2),
            "r2": round(r2, 2),
            "r3": round(r3, 2),
        }

    def calculate_camarilla_pivots(
        self, high: float, low: float, close: float
    ) -> Dict[str, float]:
        diff = high - low
        return {
            "h3": round(close + diff * 1.1 / 2, 2),
            "h4": round(close + diff * 1.1 / 4, 2),
            "h5": round(close + diff * 1.1 / 1, 2),
            "l3": round(close - diff * 1.1 / 2, 2),
            "l4": round(close - diff * 1.1 / 4, 2),
            "l5": round(close - diff * 1.1 / 1, 2),
        }

    def calculate_fibonacci_pivots(
        self, high: float, low: float, close: float
    ) -> Dict[str, float]:
        pivot = (high + low + close) / 3.0
        diff = high - low
        return {
            "pivot": round(pivot, 2),
            "r1": round(pivot + 0.382 * diff, 2),
            "r2": round(pivot + 0.618 * diff, 2),
            "r3": round(pivot + 1.0 * diff, 2),
            "s1": round(pivot - 0.382 * diff, 2),
            "s2": round(pivot - 0.618 * diff, 2),
            "s3": round(pivot - 1.0 * diff, 2),
        }

    def process_tick_data_polars(
        self, ticks: List[Dict[str, Any]]
    ) -> Union["pl.DataFrame", pd.DataFrame]:
        if not ticks:
            empty = {
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "close": 0.0,
                "volume": 0,
                "vwap": 0.0,
                "buy_volume": 0,
                "sell_volume": 0,
                "tick_count": 0,
                "first_tick_price": 0.0,
                "last_tick_price": 0.0,
            }
            if HAS_POLARS:
                return pl.DataFrame([empty])
            return pd.DataFrame([empty])

        prices = [t.get("price", 0) for t in ticks]
        volumes = [t.get("volume", 0) for t in ticks]

        open_price = prices[0]
        high_price = max(prices)
        low_price = min(prices)
        close_price = prices[-1]
        total_volume = sum(volumes)

        buy_volume = 0
        sell_volume = 0
        for tick in ticks:
            tick_vol = tick.get("volume", 0)
            direction = tick.get("direction", "buy")
            if direction == "buy":
                buy_volume += tick_vol
            else:
                sell_volume += tick_vol

        cum_pv = sum(p * v for p, v in zip(prices, volumes))
        vwap = cum_pv / total_volume if total_volume > 0 else close_price

        result = {
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": total_volume,
            "vwap": round(vwap, 2),
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "tick_count": len(ticks),
            "first_tick_price": prices[0],
            "last_tick_price": prices[-1],
        }

        if HAS_POLARS:
            return pl.DataFrame([result])
        return pd.DataFrame([result])

    def aggregate_ticks_to_ohlc(
        self,
        ticks: List[Dict[str, Any]],
        interval_seconds: int = 60,
    ) -> Union["pl.DataFrame", pd.DataFrame]:
        if not ticks:
            return self.process_tick_data_polars([])

        time_key = ticks[0].get("timestamp", "time")
        if isinstance(time_key, str):
            try:
                time_key = datetime.fromisoformat(time_key)
            except (ValueError, TypeError):
                time_key = datetime.now()

        base_time = time_key
        buckets: Dict[int, List[Dict[str, Any]]] = {}

        for tick in ticks:
            ts = tick.get("timestamp", datetime.now())
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except (ValueError, TypeError):
                    ts = datetime.now()
            diff = (ts - base_time).total_seconds()
            bucket = int(diff // interval_seconds)
            buckets.setdefault(bucket, []).append(tick)

        results = []
        for bucket_idx in sorted(buckets.keys()):
            bucket_ticks = buckets[bucket_idx]
            processed = self.process_tick_data_polars(bucket_ticks)
            if HAS_POLARS and isinstance(processed, pl.DataFrame) and processed.height > 0:
                results.append(processed.to_dicts()[0])
            elif isinstance(processed, pd.DataFrame) and not processed.empty:
                results.append(processed.to_dict(orient="records")[0])

        if not results:
            return self.process_tick_data_polars([])

        if HAS_POLARS:
            return pl.DataFrame(results)
        return pd.DataFrame(results)

    def calculate_volume_profile_polars(
        self,
        ticks: List[Dict[str, Any]],
        num_bins: int = 20,
    ) -> Union["pl.DataFrame", pd.DataFrame]:
        if not ticks:
            return self._rows_to_polars([]) if HAS_POLARS else pd.DataFrame()

        prices = [t.get("price", 0) for t in ticks]
        volumes = [t.get("volume", 0) for t in ticks]
        min_p, max_p = min(prices), max(prices)

        if min_p == max_p:
            return (
                self._rows_to_polars(
                    [{"price_level": min_p, "volume": sum(volumes), "poc": True}]
                )
                if HAS_POLARS
                else pd.DataFrame(
                    [{"price_level": min_p, "volume": sum(volumes), "poc": True}]
                )
            )

        bin_size = (max_p - min_p) / num_bins
        bins: Dict[int, int] = {}
        for price, vol in zip(prices, volumes):
            idx = int((price - min_p) / bin_size)
            idx = min(idx, num_bins - 1)
            bins[idx] = bins.get(idx, 0) + vol

        results = []
        max_vol = max(bins.values()) if bins else 1
        for idx in range(num_bins):
            price_level = min_p + (idx + 0.5) * bin_size
            vol = bins.get(idx, 0)
            results.append(
                {
                    "price_level": round(price_level, 2),
                    "volume": vol,
                    "pct_of_total": round(vol / max_vol * 100, 2) if max_vol > 0 else 0,
                    "poc": False,
                }
            )

        if results:
            poc_idx = max(range(len(results)), key=lambda i: results[i]["volume"])
            results[poc_idx]["poc"] = True

        if HAS_POLARS:
            return self._rows_to_polars(results)
        return pd.DataFrame(results)

    def calculate_wahoo_iv(
        self,
        market_price: float,
        strike: float,
        spot: float,
        r: float,
        t: float,
        option_type: str,
        max_iter: int = 50,
        tol: float = 1e-6,
    ) -> float:
        if t <= 0 or market_price <= 0 or spot <= 0 or strike <= 0:
            return 0.0

        intrinsic = max(0, (spot - strike) if option_type == "CE" else (strike - spot))
        if market_price <= intrinsic + 0.01:
            return 0.0

        iv = 0.30
        for _ in range(max_iter):
            greeks = self.calculate_greeks_polars(strike, spot, r, iv, t, option_type)
            sqrt_t = math.sqrt(t)
            d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (
                iv * sqrt_t
            )
            d2 = d1 - iv * sqrt_t
            if option_type == "CE":
                model_price = spot * _normal_cdf(d1) - strike * math.exp(
                    -r * t
                ) * _normal_cdf(d2)
            else:
                model_price = strike * math.exp(-r * t) * _normal_cdf(
                    -d2
                ) - spot * _normal_cdf(-d1)

            diff = model_price - market_price
            vega = greeks["vega"] * 100

            if abs(vega) < 1e-12:
                break

            iv -= diff / vega
            iv = max(0.01, min(iv, 5.0))

            if abs(diff) < tol:
                break

        return round(iv, 6)

    def build_full_chain_with_greeks(
        self,
        symbol: str,
        date: str,
        expiry: str,
        spot: float,
        r: float = 0.06,
        t: float = 7 / 365.0,
    ) -> Union["pl.DataFrame", pd.DataFrame]:
        chain = self.load_option_chain_polars(symbol, date, expiry)

        if HAS_POLARS and isinstance(chain, pl.DataFrame):
            if chain.height == 0:
                return chain
            records = chain.to_dicts()
        elif isinstance(chain, pd.DataFrame) and not chain.empty:
            records = chain.to_dict(orient="records")
        else:
            return self._rows_to_polars([]) if HAS_POLARS else pd.DataFrame()

        results = []
        for row in records:
            strike = row["strike"]
            option_type = row["option_type"]
            close = row.get("close", 0) or 0

            iv = self.calculate_wahoo_iv(
                close, strike, spot, r, t, option_type
            )
            greeks = self.calculate_greeks_polars(
                strike, spot, r, iv, t, option_type
            )

            results.append(
                {
                    **row,
                    "iv": greeks["iv"],
                    "delta": greeks["delta"],
                    "gamma": greeks["gamma"],
                    "theta": greeks["theta"],
                    "vega": greeks["vega"],
                    "intrinsic": max(0, (spot - strike) if option_type == "CE" else (strike - spot)),
                    "time_value": max(0, close - max(0, (spot - strike) if option_type == "CE" else (strike - spot))),
                    "spot": spot,
                    "expiry": expiry,
                }
            )

        if HAS_POLARS:
            return self._rows_to_polars(results)
        return pd.DataFrame(results)

    def get_chain_summary(
        self,
        symbol: str,
        date: str,
        expiry: str,
        spot: float,
        r: float = 0.06,
        t: float = 7 / 365.0,
    ) -> Dict[str, Any]:
        chain = self.build_full_chain_with_greeks(
            symbol, date, expiry, spot, r, t
        )

        if HAS_POLARS and isinstance(chain, pl.DataFrame):
            if chain.height == 0:
                return {}
            records = chain.to_dicts()
        elif isinstance(chain, pd.DataFrame) and not chain.empty:
            records = chain.to_dict(orient="records")
        else:
            return {}

        ce_records = [r for r in records if r.get("option_type") == "CE"]
        pe_records = [r for r in records if r.get("option_type") == "PE"]

        total_ce_oi = sum(r.get("open_interest", 0) for r in ce_records)
        total_pe_oi = sum(r.get("open_interest", 0) for r in pe_records)
        total_ce_vol = sum(r.get("volume", 0) for r in ce_records)
        total_pe_vol = sum(r.get("volume", 0) for r in pe_records)

        pcr_oi = total_pe_oi / max(total_ce_oi, 1)
        pcr_vol = total_pe_vol / max(total_ce_vol, 1)

        max_pain_strike = 0.0
        min_pain = float("inf")
        all_strikes = sorted(set(r["strike"] for r in records))

        for test_strike in all_strikes:
            pain = 0.0
            for r in records:
                oi = r.get("open_interest", 0)
                if r["option_type"] == "CE" and test_strike < r["strike"]:
                    pain += oi * (r["strike"] - test_strike)
                elif r["option_type"] == "PE" and test_strike > r["strike"]:
                    pain += oi * (test_strike - r["strike"])
            if pain < min_pain:
                min_pain = pain
                max_pain_strike = test_strike

        atm_strike = min(all_strikes, key=lambda s: abs(s - spot)) if all_strikes else spot

        return {
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "total_ce_volume": total_ce_vol,
            "total_pe_volume": total_pe_vol,
            "pcr_oi": round(pcr_oi, 3),
            "pcr_volume": round(pcr_vol, 3),
            "max_pain": max_pain_strike,
            "atm_strike": atm_strike,
            "spot": spot,
            "expiry": expiry,
            "chain_data": records,
        }
