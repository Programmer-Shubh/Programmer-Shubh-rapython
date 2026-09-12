import os
import time

try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except Exception:
    HAS_PSYCOPG2 = False


class Database:
    _instance = None
    _db_path = None
    _use_postgres = False
    _pg_url = None

    @classmethod
    def set_path(cls, path):
        cls._db_path = path

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        # Check for external DB first (Render Postgres, Supabase, Turso)
        pg_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or os.environ.get("SUPABASE_DB_URL")
        if pg_url and pg_url.startswith(("postgres://", "postgresql://")) and HAS_PSYCOPG2:
            self._use_postgres = True
            self._pg_url = pg_url
            # Normalize postgres:// to postgresql:// for psycopg2
            if pg_url.startswith("postgres://"):
                pg_url = pg_url.replace("postgres://", "postgresql://", 1)
                self._pg_url = pg_url
            Database._use_postgres = True
            Database._pg_url = pg_url
            self._path = None
            return
        # Fallback to SQLite (local dev or Render disk)
        if Database._db_path is None:
            env_path = os.environ.get("DB_PATH")
            if env_path:
                Database._db_path = env_path
            else:
                Database._db_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "ratrade.db")
        # Inherit postgres flag from class if set
        if Database._use_postgres:
            self._use_postgres = True
            self._pg_url = Database._pg_url
            self._path = None
        else:
            os.makedirs(os.path.dirname(Database._db_path), exist_ok=True)
            self._path = Database._db_path

    def _is_postgres(self):
        return bool(self._use_postgres and self._pg_url)

    def _conn(self):
        if self._is_postgres():
            try:
                from urllib.parse import urlparse, urlunparse, parse_qsl
                parsed = urlparse(self._pg_url)
                # Strip query string to avoid "extra = in sslmode" with encoded passwords
                # Use parse_qsl for robust handling of special chars in password
                clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", parsed.fragment))
                qs_dict = dict(parse_qsl(parsed.query, keep_blank_values=True))
                sslmode = qs_dict.get("sslmode")
                if not sslmode:
                    sslmode = "require" if "supabase" in self._pg_url or "render" in self._pg_url else "prefer"
                conn = psycopg2.connect(clean_url, sslmode=sslmode)
                conn.autocommit = False
                return conn
            except Exception as e:
                # Fallback to SQLite so app stays up even if Postgres is unreachable
                print(f"Postgres connect failed, falling back to SQLite: {e}")
                import sqlite3
                if not self._path:
                    self._path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "ratrade.db")
                    os.makedirs(os.path.dirname(self._path), exist_ok=True)
                conn = sqlite3.connect(self._path)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                return conn
        else:
            import sqlite3
            conn = sqlite3.connect(self._path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn

    def _adapt_query(self, query):
        # Convert ? placeholders to %s for postgres
        if self._is_postgres():
            return query.replace("?", "%s")
        return query

    def init_schema(self):
        if self._is_postgres():
            # Postgres schema (SERIAL, NOW(), no AUTOINCREMENT)
            sql = """
                CREATE TABLE IF NOT EXISTS bhavcopy_data (
                    id SERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    expiry_date TEXT,
                    strike_price DOUBLE PRECISION,
                    option_type TEXT,
                    open_price DOUBLE PRECISION DEFAULT 0,
                    high_price DOUBLE PRECISION DEFAULT 0,
                    low_price DOUBLE PRECISION DEFAULT 0,
                    close_price DOUBLE PRECISION DEFAULT 0,
                    volume BIGINT DEFAULT 0,
                    oi BIGINT DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_bhav_symbol_date ON bhavcopy_data(symbol, trade_date);
                CREATE INDEX IF NOT EXISTS idx_bhav_strike ON bhavcopy_data(symbol, strike_price, option_type);

                CREATE TABLE IF NOT EXISTS paper_trades (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER DEFAULT 1,
                    strategy_id INTEGER,
                    symbol TEXT NOT NULL,
                    option_type TEXT NOT NULL,
                    strike_price DOUBLE PRECISION NOT NULL,
                    expiry_date TEXT,
                    transaction_type TEXT NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    lot_size INTEGER DEFAULT 50,
                    entry_price DOUBLE PRECISION NOT NULL,
                    exit_price DOUBLE PRECISION,
                    entry_date TEXT NOT NULL,
                    exit_date TEXT,
                    stop_loss DOUBLE PRECISION DEFAULT 0,
                    target DOUBLE PRECISION DEFAULT 0,
                    auto_action TEXT DEFAULT 'OFF',
                    total_cost DOUBLE PRECISION DEFAULT 0,
                    exit_cost DOUBLE PRECISION DEFAULT 0,
                    pnl DOUBLE PRECISION DEFAULT 0,
                    pnl_percent DOUBLE PRECISION DEFAULT 0,
                    status TEXT DEFAULT 'open',
                    trade_mode TEXT DEFAULT 'paper',
                    trade_type TEXT DEFAULT 'intraday',
                    broker_order_id TEXT DEFAULT '',
                    exit_status TEXT DEFAULT 'manual',
                    entry_iv DOUBLE PRECISION DEFAULT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS strategies (
                    id SERIAL PRIMARY KEY,
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
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS auto_trades (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER DEFAULT 1,
                    name TEXT,
                    strategy_id INTEGER,
                    mode TEXT DEFAULT 'paper',
                    trade_type TEXT DEFAULT 'intraday',
                    status TEXT DEFAULT 'stopped',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """
            with self._conn() as conn:
                cur = conn.cursor()
                try:
                    cur.execute(sql)
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
                conn.commit()
            # For Postgres (external DB), NEVER wipe bhavcopy_data - it's persistent
            try:
                self._migrate()
            except Exception:
                pass
            return
        # SQLite path (original)
        import sqlite3
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
                    trade_type TEXT DEFAULT 'intraday',
                    broker_order_id TEXT DEFAULT '',
                    exit_status TEXT DEFAULT 'manual',
                    entry_iv REAL DEFAULT NULL,
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
                    trade_type TEXT DEFAULT 'intraday',
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
        # Market data is API-only — clear stale bhavcopy (db ko hatao) - ONLY for SQLite ephemeral
        # For Postgres, skip wipe to preserve persistent data
        if not self._is_postgres():
            try:
                with self._conn() as c2:
                    c2.execute("DELETE FROM bhavcopy_data")
                    c2.commit()
            except Exception:
                pass
        try:
            self._migrate()
        except Exception:
            pass

    def _migrate(self):
        if self._is_postgres():
            # Postgres: check information_schema
            try:
                rows = self.fetch_all("SELECT column_name FROM information_schema.columns WHERE table_name='strategies'")
                cols = {r["column_name"] for r in rows}
                if "status" not in cols:
                    self.execute("ALTER TABLE strategies ADD COLUMN status TEXT DEFAULT 'active'")
            except Exception:
                pass
            try:
                rows = self.fetch_all("SELECT column_name FROM information_schema.columns WHERE table_name='paper_trades'")
                cols = {r["column_name"] for r in rows}
                if "entry_iv" not in cols:
                    self.execute("ALTER TABLE paper_trades ADD COLUMN entry_iv DOUBLE PRECISION DEFAULT NULL")
                if "trade_type" not in cols:
                    self.execute("ALTER TABLE paper_trades ADD COLUMN trade_type TEXT DEFAULT 'intraday'")
                if "broker_order_id" not in cols:
                    self.execute("ALTER TABLE paper_trades ADD COLUMN broker_order_id TEXT DEFAULT ''")
            except Exception:
                pass
            try:
                rows = self.fetch_all("SELECT column_name FROM information_schema.columns WHERE table_name='auto_trades'")
                cols = {r["column_name"] for r in rows}
                if "trade_type" not in cols:
                    self.execute("ALTER TABLE auto_trades ADD COLUMN trade_type TEXT DEFAULT 'intraday'")
            except Exception:
                pass
            try:
                self.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bhav_unique ON bhavcopy_data(symbol, trade_date, COALESCE(expiry_date,''), COALESCE(strike_price,0), COALESCE(option_type,''))")
            except Exception:
                pass
            return
        # SQLite migrations (original)
        try:
            cols = {r[1] for r in self.fetch_all("PRAGMA table_info(strategies)")}
            if "status" not in cols:
                self.execute("ALTER TABLE strategies ADD COLUMN status TEXT DEFAULT 'active'")
        except Exception:
            pass
        try:
            cols = {r[1] for r in self.fetch_all("PRAGMA table_info(paper_trades)")}
            if "entry_iv" not in cols:
                self.execute("ALTER TABLE paper_trades ADD COLUMN entry_iv REAL DEFAULT NULL")
            if "trade_type" not in cols:
                self.execute("ALTER TABLE paper_trades ADD COLUMN trade_type TEXT DEFAULT 'intraday'")
        except Exception:
            pass
        try:
            cols = {r[1] for r in self.fetch_all("PRAGMA table_info(paper_trades)")}
            if "broker_order_id" not in cols:
                self.execute("ALTER TABLE paper_trades ADD COLUMN broker_order_id TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            cols = {r[1] for r in self.fetch_all("PRAGMA table_info(auto_trades)")}
            if "trade_type" not in cols:
                self.execute("ALTER TABLE auto_trades ADD COLUMN trade_type TEXT DEFAULT 'intraday'")
        except Exception:
            pass
        try:
            self.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_bhav_unique
                ON bhavcopy_data(symbol, trade_date, COALESCE(expiry_date,''),
                COALESCE(strike_price,0), COALESCE(option_type,''))""")
        except Exception:
            pass

    def fetch_one(self, query, params=None):
        if self._is_postgres():
            import psycopg2.extras
            q = self._adapt_query(query)
            with self._conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(q, params or [])
                    row = cur.fetchone()
                    return dict(row) if row else None
        else:
            with self._conn() as conn:
                row = conn.execute(query, params or []).fetchone()
                return dict(row) if row else None

    def fetch_all(self, query, params=None):
        if self._is_postgres():
            import psycopg2.extras
            q = self._adapt_query(query)
            with self._conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(q, params or [])
                    rows = cur.fetchall()
                    return [dict(r) for r in rows]
        else:
            with self._conn() as conn:
                rows = conn.execute(query, params or []).fetchall()
                return [dict(r) for r in rows]

    def execute(self, query, params=None):
        if self._is_postgres():
            q = self._adapt_query(query)
            # Handle RETURNING id for inserts
            is_insert = q.strip().upper().startswith("INSERT")
            if is_insert and "RETURNING" not in q.upper():
                q = q.rstrip(";") + " RETURNING id"
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(q, params or [])
                    # Try to get lastrowid
                    try:
                        if is_insert:
                            row = cur.fetchone()
                            last_id = row[0] if row else None
                        else:
                            last_id = cur.rowcount
                    except Exception:
                        last_id = None
                    conn.commit()
                    # For non-inserts, return rowcount; for inserts, return id
                    return last_id if is_insert else cur.rowcount
        else:
            with self._conn() as conn:
                cur = conn.execute(query, params or [])
                conn.commit()
                return cur.lastrowid

    def executemany(self, query, params_list):
        if self._is_postgres():
            q = self._adapt_query(query)
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.executemany(q, params_list)
                conn.commit()
        else:
            with self._conn() as conn:
                conn.executemany(query, params_list)
                conn.commit()
