"""Fyers headless AUTO-LOGIN helpers (offline-safe tests)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.fyers_auto_login import (
    _b64, _totp_now, missing_creds, auto_login,
)


def test_b64_roundtrip():
    assert _b64("AB1234") == __import__("base64").b64encode(b"AB1234").decode()


def test_totp_generation():
    # RFC 4231 style secret (pyotp accepts arbitrary base32)
    code = _totp_now("JBSWY3DPEHPK3PXP")
    assert len(code) == 6 and code.isdigit()


def test_missing_creds_report():
    miss = missing_creds({"app_id": "X-100", "secret": "s"})
    assert "fy_id" in miss and "totp_secret" in miss and "pin" in miss
    assert "app_id" not in miss and "secret" not in miss


def test_auto_login_fails_fast_without_creds():
    import asyncio
    r = asyncio.run(auto_login({"app_id": "X-100"}))
    assert not r["success"]
    assert "fy_id" in str(r.get("error")) and "totp_secret" in str(r.get("error"))
