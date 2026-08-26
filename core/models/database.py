import sqlite3
import os
from contextlib import contextmanager


class Database:
    _instance = None
    _db_path = None

    @classmethod
    def set_path(cls, path):
        cls._db_path = path

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        if Database._db_path is None:
            # Allow override via env (Render persistent disk) else local data folder
            env_path = os.environ.get("DB_PATH")
            if env_path:
                Database._db_path = env_path
            else:
                Database._db_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "ratrade.db")
        os.makedirs(os.path.dirname(Database._db_path), exist_ok=True)
        self._path = Database._db_path

    def _conn(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS bhavcopy_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    expiry_date TEXT,
                    strike_price REAL,
                    option_type TEXT,
                    open_price REAL DEFAULT 0,
                    high_price REAL DEFAULT 0,
                    low_price REAL DEFAULT 0,
                    close_price REAL DEFAULT 0,
                    volume INTEGER DEFAULT 0,
                    oi INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_bhav_symbol_date ON bhavcopy_data(symbol, trade_date);
                CREATE INDEX IF NOT EXISTS idx_bhav_strike ON bhavcopy_data(symbol, strike_price, option_type);

                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER DEFAULT 1,
                    strategy_id INTEGER,
                    symbol TEXT NOT NULL,
                    option_type TEXT NOT NULL,
                    strike_price REAL NOT NULL,
                    expiry_date TEXT,
                    transaction_type TEXT NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    lot_size INTEGER DEFAULT 50,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    entry_date TEXT NOT NULL,
                    exit_date TEXT,
                    stop_loss REAL DEFAULT 0,
                    target REAL DEFAULT 0,
                    auto_action TEXT DEFAULT 'OFF',
                    total_cost REAL DEFAULT 0,
                    exit_cost REAL DEFAULT 0,
                    pnl REAL DEFAULT 0,
                    pnl_percent REAL DEFAULT 0,
                    status TEXT DEFAULT 'open',
                    trade_mode TEXT DEFAULT 'paper',
                    exit_status TEXT DEFAULT 'manual',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS strategies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER DEFAULT 1,
                    name TEXT NOT NULL,
                    symbol TEXT DEFAULT 'NIFTY',
                    start_date TEXT,
                    end_date TEXT,
                    timeframe TEXT DEFAULT 'daily',
                    description TEXT,
                    indicators TEXT DEFAULT '[]',
                    entry_conditions TEXT DEFAULT '[]',
                    exit_conditions TEXT DEFAULT '[]',
                    legs TEXT DEFAULT '[]',
                    advanced_options TEXT DEFAULT '{}',
                    risk_management TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'active',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS auto_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER DEFAULT 1,
                    name TEXT,
                    strategy_id INTEGER,
                    mode TEXT DEFAULT 'paper',
                    status TEXT DEFAULT 'stopped',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                );
            """)
        # Migrate: add missing columns to existing DB
        try:
            self._migrate()
        except Exception:
            pass

    def _migrate(self):
        # Add status column if missing (migrate existing DB)
        try:
            cols = {r[1] for r in self.fetch_all("PRAGMA table_info(strategies)")}
            if "status" not in cols:
                self.execute("ALTER TABLE strategies ADD COLUMN status TEXT DEFAULT 'active'")
        except Exception:
            pass

    def fetch_one(self, query, params=None):
        with self._conn() as conn:
            row = conn.execute(query, params or []).fetchone()
            return dict(row) if row else None

    def fetch_all(self, query, params=None):
        with self._conn() as conn:
            rows = conn.execute(query, params or []).fetchall()
            return [dict(r) for r in rows]

    def execute(self, query, params=None):
        with self._conn() as conn:
            cur = conn.execute(query, params or [])
            conn.commit()
            return cur.lastrowid

    def executemany(self, query, params_list):
        with self._conn() as conn:
            conn.executemany(query, params_list)
            conn.commit()
