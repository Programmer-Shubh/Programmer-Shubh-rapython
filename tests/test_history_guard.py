"""Issue fixes: (1) closed-trade history robustness + delete audit,
(2) stale (purane-trend, score-70) setups opportunities se bahar.
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

UID = 99997


def test_stale_setups_hidden_from_4part(monkeypatch):
    """Score-70 stale setups kisi part me na dikhen; fresh-80 + combo rahen."""
    from core.services.scanner import OptionScanner
    sc = OptionScanner()
    stale = {"symbol": "STALE", "signal_type": "BUY CE", "direction": "bullish",
             "score": 70, "price": 25000.0, "reasons": ["ST bullish"],
             "ai": True, "combo": False,
             "option_suggestion": {"strike": 25000}}
    fresh = {"symbol": "FRESH", "signal_type": "BUY PE", "direction": "bearish",
             "score": 80, "price": 25000.0,
             "reasons": ["ST bearish (fresh cross breakdown, 0 bar pehle)"],
             "ai": True, "combo": False,
             "option_suggestion": {"strike": 25000}}
    combo = {"symbol": "COMBO", "signal_type": "BUY CE", "direction": "bullish",
             "score": 100, "price": 25000.0, "reasons": ["combo"],
             "ai": True, "combo": True,
             "option_suggestion": {"strike": 25000}}
    monkeypatch.setattr(sc, "_smv_signals", lambda symbols=None: [dict(stale), dict(fresh), dict(combo)])
    monkeypatch.setattr(sc, "_combo_rvp_signals", lambda symbols=None, pcr=None: [])
    monkeypatch.setattr(sc, "_warm_history", lambda symbols=None: None)
    monkeypatch.setattr(sc, "_suggest_option_floor",
                        lambda sym, px, opt, txn, *a: {"strike": 25000, "spot": px})
    out = sc.get_4_part_opportunities(min_score=70)
    assert isinstance(out, dict) and "ce_buy" in out, out
    seen = [x.get("symbol") for part in ("ce_buy", "pe_buy", "ce_sell", "pe_sell")
            for x in (out.get(part) or [])]
    assert "STALE" not in seen, seen
    assert "FRESH" in seen and "COMBO" in seen, seen


def test_closed_history_order_nullsafe():
    """exit_date khaali ho to bhi row dikhe; nayi sabse upar."""
    from core.models.trade_model import TradeModel
    tm = TradeModel()
    tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])
    try:
        for _ed, _xd in (("2026-10-12", "2026-10-12"), ("2026-10-13", ""), ("2026-10-14", "2026-10-14")):
            tm.db.execute(
                "INSERT INTO paper_trades (user_id, symbol, option_type, strike_price,"
                " transaction_type, quantity, lot_size, entry_price, exit_price,"
                " entry_date, exit_date, pnl, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [UID, "NIFTY", "CE", 25000, "BUY", 1, 65, 100.0, 110.0,
                 _ed, _xd, 100.0, "closed"])
        rows = tm.get_closed_trades(user_id=UID)
        assert len(rows) == 3, len(rows)
        assert rows[0]["exit_date"] == "2026-10-14", rows[0]["exit_date"]
        assert rows[-1]["exit_date"] == "", rows[-1]
    finally:
        tm.db.execute("DELETE FROM paper_trades WHERE user_id=?", [UID])


def test_delete_is_logged():
    """Har delete ka snapshot JSONL me (gayab-trade ka hisaab)."""
    import datetime as _dt
    from utils.helpers import log_trade_delete
    _tid = 987654321
    log_trade_delete({"id": _tid, "symbol": "TESTX", "status": "closed",
                      "exit_status": "target", "pnl": 10.0}, "unit-test")
    _day = (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).strftime("%Y%m%d")
    _p = os.path.join(os.path.dirname(__file__), "..", "data", "logs",
                       f"trade_deletes_{_day}.jsonl")
    found = False
    try:
        with open(os.path.abspath(_p), "r", encoding="utf-8") as _f:
            for _ln in _f.read().strip().split("\n"):
                try:
                    _r = json.loads(_ln)
                    if (_r.get("trade") or {}).get("id") == _tid:
                        found = True
                        assert _r.get("where") == "unit-test"
                        break
                except Exception:
                    pass
    except Exception as e:
        assert False, f"log file missing: {e}"
    assert found, "delete snapshot log me nahi mila"
