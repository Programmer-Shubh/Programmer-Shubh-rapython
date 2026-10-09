"""PG self-heal: transient timeout must not kill Postgres for days."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.models.database import Database


def test_pg_self_heal_retry_window():
    db = Database.get_instance()
    # stash real state (local dev has no PG configured)
    stash = (Database._use_postgres, Database._pg_url, Database._pg_failed,
             Database._pg_failed_at, db._use_postgres, db._pg_url)
    try:
        db._use_postgres = True
        db._pg_url = "postgresql://u:p@localhost/db"
        Database._use_postgres = True
        Database._pg_url = "postgresql://u:p@localhost/db"
        # failed LONG ago -> retry allowed, flags cleared (no connection made)
        Database._pg_failed = True
        Database._pg_failed_at = time.time() - 700
        db._pg_failed = False
        assert db._is_postgres() is True
        assert Database._pg_failed is False
        # failed JUST now -> still sqlite
        Database._pg_failed = True
        Database._pg_failed_at = time.time()
        assert db._is_postgres() is False
    finally:
        (Database._use_postgres, Database._pg_url, Database._pg_failed,
         Database._pg_failed_at, db._use_postgres, db._pg_url) = stash
        db._pg_failed = False
