"""Margin guard: Fyers margin-shortfall handling (REAL MONEY safety).

User incident (11-Nov-2026): 12 Fyers orders REJECTED with
"RED:Margin Shortfall: INR 25,886.00 Available: INR 0.00 [FYERS_RISK_CUG]".
Required: detect it, show an actionable message, and stop the rejected-order
spam by auto-halting that strategy's LIVE after 3 consecutive rejects.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from routes.broker import (_is_margin_rejection, _friendly_margin_error,
                           _note_margin_rejection, _margin_halt_key,
                           _margin_fail_key)

MARGIN_MSG = ("RED:Margin Shortfall:INR 25,886.00 Available:INR 0.00 "
              "for C-FAI14261 [FYERS_RISK_CUG]")


def test_detects_margin_shortfall():
    assert _is_margin_rejection(MARGIN_MSG) is True
    assert _is_margin_rejection("insufficient funds") is True
    assert _is_margin_rejection("INSUFFICIENT MARGIN") is True
    # unrelated rejections must NOT trip the guard
    assert _is_margin_rejection("Invalid symbol") is False
    assert _is_margin_rejection("STOP_LOSS_ORDER_NOT_ALLOWED") is False
    assert _is_margin_rejection("") is False


def test_friendly_error_shows_required_and_available():
    msg = _friendly_margin_error(MARGIN_MSG)
    assert "margin" in msg.lower()
    assert "25,886" in msg
    assert "Fyers" in msg
    # actionable: funds/segment guidance
    assert "deposit" in msg.lower()


def test_margin_rejection_counts_and_halts_after_three(monkeypatch):
    store = {}
    monkeypatch.setattr("routes.broker._setting_get",
                        lambda k, d="": store.get(k, d))
    monkeypatch.setattr("routes.broker._setting_set",
                        lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr("routes.strategies._strat_live_key",
                        lambda sid: f"strat_live_enabled_{int(sid)}", raising=False)

    sid = 999
    # 1st reject: count 1, no halt yet
    r1 = _note_margin_rejection(sid, MARGIN_MSG)
    assert store[_margin_fail_key(sid)] == "1"
    assert store.get(_margin_halt_key(sid), "") == ""
    assert "auto-HALT" in r1

    _note_margin_rejection(sid, MARGIN_MSG)
    assert store[_margin_fail_key(sid)] == "2"
    assert store.get(_margin_halt_key(sid), "") == ""

    # 3rd consecutive reject -> LIVE auto-HALT + reason stored
    _note_margin_rejection(sid, MARGIN_MSG)
    assert store[_margin_fail_key(sid)] == "3"
    assert store[_margin_halt_key(sid)]
    assert "margin" in store[_margin_halt_key(sid)].lower()
    # live toggle turned OFF by the guard
    assert store.get("strat_live_enabled_999") == "0"


def test_funds_parser_reads_fyers_fund_limit_shape():
    """Fyers v3 /api/v3/funds ACTUAL shape: data.fund_limit = [{title,
    equityAmount, commodityAmount}] - 'Available Balance' 0 hona chahiye
    detect ho (yaad: available=0 par live orders reject hoti hain)."""
    from core.services.broker_fyers import FyersV3
    raw = {"success": True, "data": {"code": 200, "s": "ok", "fund_limit": [
        {"id": 1, "title": "Total Balance", "equityAmount": 0, "commodityAmount": 0},
        {"id": 2, "title": "Utilized Amount", "equityAmount": 0, "commodityAmount": 0},
        {"id": 10, "title": "Available Balance", "equityAmount": 0, "commodityAmount": 0},
    ]}}
    parsed = FyersV3._parse_funds(raw)
    assert parsed["available_margin"] == 0.0
    assert parsed["total_balance"] == 0.0
    # Non-zero balance parse hona chahiye
    raw2 = {"data": {"fund_limit": [
        {"id": 1, "title": "Total Balance", "equityAmount": 50000.0},
        {"id": 10, "title": "Available Balance", "equityAmount": 32000.0},
    ]}}
    p2 = FyersV3._parse_funds(raw2)
    assert p2["available_margin"] == 32000.0
    assert p2["total_balance"] == 50000.0


def test_halt_reason_survives_as_text():
    msg = _friendly_margin_error("Margin Shortfall:INR 1,000.00 Available:INR 0.00")
    assert "1,000" in msg
    assert "0.00" in msg
