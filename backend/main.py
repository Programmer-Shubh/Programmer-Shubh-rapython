"""Compatibility shim for a possibly dashboard-configured start command of
`cd backend && uvicorn main:app ...`.

The real RaTrade application lives in the repository-root main.py. This module
loads that file explicitly by path (so it cannot accidentally import itself)
and re-exports the FastAPI `app`.
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location("ratrade_root_main", os.path.join(ROOT, "main.py"))
_mod = importlib.util.module_from_spec(_spec)
sys.modules["ratrade_root_main"] = _mod
_spec.loader.exec_module(_mod)

app = _mod.app
