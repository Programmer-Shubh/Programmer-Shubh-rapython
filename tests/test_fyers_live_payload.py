"""Fyers live-order correctness: v3 symbology + STRICT payload casting.

User spec:
1. Monthly options: NSE:{Sym}{YY}{MMM}{Strike}{CE/PE} (e.g. NSE:NIFTY26OCT22450PE)
   Weekly options:  NSE:{Sym}{YY}{M}{dd}{Strike}{CE/PE} (M = 1-9/O/N/D)
2. side: 1=BUY / -1=SELL (strict int); type: 1=LIMIT, 2=MARKET (strict int;
   pehle INVERTED tha -> MARKET LIMIT ban ke limitPrice=0 par atak jata tha)
3. qty strict int
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.symbol_resolver import _fyers_symbol
from core.services.broker_fyers import FyersV3


def test_fyers_monthly_symbol_format():
    # 27-Oct-2026 = NIFTY ki LAST Tuesday (monthly contract)
    assert (_fyers_symbol("NIFTY", "2026-10-27", 22450, "PE")
            == "NSE:NIFTY26OCT22450PE")


def test_fyers_weekly_symbol_format():
    # 13-Oct-2026 = non-last Tuesday (weekly contract), M=O(Oct)
    assert (_fyers_symbol("NIFTY", "2026-10-13", 22450, "CE")
            == "NSE:NIFTY26O1322450CE")


def test_fyers_stock_option_monthly():
    assert (_fyers_symbol("BOSCHLTD", "2026-10-27", 33500.0, "CE")
            == "NSE:BOSCHLTD26OCT33500CE")


def _mock_fy(monkeypatch):
    """FyersV3 with _request captured (no network)."""
    fy = FyersV3(app_id="TEST-100", secret_key="s", redirect_uri="r",
                 access_token="tok", refresh_token="rt")
    captured = {}

    async def _fake(method, path, data=None, retries=3):
        captured["method"] = method
        captured["path"] = path
        captured["data"] = data
        return {"success": True, "data": {"id": "O123"}}

    monkeypatch.setattr(fy, "_request", _fake)
    return fy, captured


def test_payload_type_side_qty_strict(monkeypatch):
    fy, cap = _mock_fy(monkeypatch)
    import asyncio
    res = asyncio.run(fy.place_order("NSE:NIFTY26OCT22450PE", "BUY", 75, "MARKET"))
    assert res["success"] and res["data"]["order_id"] == "O123"
    p = cap["data"]
    assert p["type"] == 2          # MARKET = 2 (pehle 1 = LIMIT tha)
    assert p["side"] == 1          # BUY
    assert p["qty"] == 75 and isinstance(p["qty"], int)
    assert p["limitPrice"] == 0


def test_payload_limit_and_sell(monkeypatch):
    fy, cap = _mock_fy(monkeypatch)
    import asyncio
    asyncio.run(fy.place_order("NSE:NIFTY26O1322450CE", "SELL", 75, "LIMIT", price=189.5))
    p = cap["data"]
    assert p["type"] == 1          # LIMIT = 1
    assert p["side"] == -1         # SELL
    assert p["limitPrice"] == 189.5


def test_payload_rejects_bad_qty_and_limit_zero_price(monkeypatch):
    fy, cap = _mock_fy(monkeypatch)
    import asyncio
    r1 = asyncio.run(fy.place_order("NSE:X", "BUY", 0, "MARKET"))
    assert not r1["success"] and "qty" in str(r1.get("error", "")).lower()
    r2 = asyncio.run(fy.place_order("NSE:X", "BUY", 75, "LIMIT"))
    assert not r2["success"] and "price" in str(r2.get("error", "")).lower()
    assert "data" not in cap       # kuch bhi broker ko nahi bheja gaya
