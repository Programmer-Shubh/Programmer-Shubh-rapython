"""Compatibility entry point for the existing Render start command.

The service starts `uvicorn app.main:app` from the backend/ directory.
The restored RaTrade application lives at the repository root (main.py),
so expose the very same FastAPI app through this module.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", os.path.join(ROOT, "data", "ratrade.db"))

from main import app  # noqa: E402,F401
