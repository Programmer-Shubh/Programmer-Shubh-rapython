"""Compatibility entry point for `uvicorn app.main:app` run from the repo root."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", os.path.join(ROOT, "data", "ratrade.db"))

from main import app  # noqa: E402,F401
