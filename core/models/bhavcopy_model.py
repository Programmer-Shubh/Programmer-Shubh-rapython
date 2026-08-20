from core.models.database import Database


class BhavcopyModel:
    def __init__(self):
        self.db = Database.get_instance()

    def get_dates(self, symbol: str) -> list:
        rows = self.db.fetch_all(
            "SELECT DISTINCT trade_date FROM bhavcopy_data WHERE symbol=? ORDER BY trade_date DESC",
            [symbol],
        )
        return [r["trade_date"] for r in rows]

    def get_expiries(self, symbol: str, date: str) -> list:
        rows = self.db.fetch_all(
            "SELECT DISTINCT expiry_date FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND option_type IS NOT NULL ORDER BY expiry_date",
            [symbol, date],
        )
        return [r["expiry_date"] for r in rows]

    def get_date_range(self, symbol: str) -> dict:
        row = self.db.fetch_one(
            "SELECT MIN(trade_date) as min_date, MAX(trade_date) as max_date, COUNT(*) as cnt FROM bhavcopy_data WHERE symbol=?",
            [symbol],
        )
        return {
            "min_date": row["min_date"] or "",
            "max_date": row["max_date"] or "",
            "count": row["cnt"] or 0,
        }

    def get_symbols(self) -> list:
        rows = self.db.fetch_all(
            "SELECT DISTINCT symbol FROM bhavcopy_data ORDER BY symbol",
        )
        return [r["symbol"] for r in rows]

    def get_by_symbol(self, symbol: str, start_date: str, end_date: str, include_options: bool = True) -> list:
        if include_options:
            return self.db.fetch_all(
                """SELECT * FROM bhavcopy_data WHERE symbol=? AND trade_date BETWEEN ? AND ?
                   ORDER BY trade_date, option_type, strike_price""",
                [symbol, start_date, end_date],
            )
        return self.db.fetch_all(
            "SELECT * FROM bhavcopy_data WHERE symbol=? AND trade_date BETWEEN ? AND ? AND option_type IS NULL ORDER BY trade_date",
            [symbol, start_date, end_date],
        )

    def get_option_chain(self, symbol: str, date: str, expiry: str) -> list:
        return self.db.fetch_all(
            """SELECT * FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND expiry_date=?
               AND option_type IS NOT NULL ORDER BY strike_price, option_type""",
            [symbol, date, expiry],
        )

    def get_option_data(self, symbol: str, date: str, expiry: str, strike: float, option_type: str) -> dict:
        row = self.db.fetch_one(
            """SELECT * FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND strike_price=?
               AND option_type=? AND (expiry_date=? OR expiry_date IS NULL) LIMIT 1""",
            [symbol, date, strike, option_type, expiry],
        )
        return row

    def import_data(self, records: list) -> int:
        if not records:
            return 0
        self.db.executemany(
            """INSERT OR REPLACE INTO bhavcopy_data
               (symbol, trade_date, expiry_date, strike_price, option_type,
                open_price, high_price, low_price, close_price, volume, oi)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    r.get("symbol"), r.get("trade_date"), r.get("expiry_date"),
                    r.get("strike_price"), r.get("option_type"),
                    r.get("open_price", 0), r.get("high_price", 0),
                    r.get("low_price", 0), r.get("close_price", 0),
                    r.get("volume", 0), r.get("oi", 0),
                )
                for r in records
            ],
        )
        return len(records)
