"""Paper + Live dono ON - sync (user bug report 27-10-2026):

"dono on rahta hai to kewal broker me order lagata hai, paper trade me
order nahi lagata" - Trading Center saved strategy par.

Fix coverage:
1. place-live ka PAPER MIRROR ab KABHI silent fail nahi - note wapas aata
   hai (paper_mirror_note), aur DB me mirror row sach me insert hota hai
   (entry_price bilkul live `adj` ke barabar - same reference price).
2. Manual Paper button (both ON) signal-skip par bhi paper copy lagata hai
   (guards: no open position / max-day / 3PM cutoff fail-closed).
3. Paper button (force ya both-ON sync) KABHI real broker order nahi
   jata - _execute_legs(force_paper=True) live routing ignore karta hai.
Backtest engine inna kuch bhi nahi chheda gaya.
"""
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import routes.broker as B
import routes.strategies as S


def _mk_strategy(db, name, status="active", legs=None, rm=None):
    db.execute("DELETE FROM strategies WHERE name=?", [name])
    db.execute(
        "INSERT INTO strategies (user_id, name, symbol, legs, risk_management,"
        " status) VALUES (1,?,'NIFTY',?,?,?)",
        [name, json.dumps(legs or [{"option_type": "CE", "transaction": "buy",
                                    "lots": 1}]),
         json.dumps(rm or {"daily_stop_loss": 1500, "daily_take_profit": 3000,
                           "max_trades_per_day": 5}),
         status])
    return int(db.fetch_one("SELECT id FROM strategies WHERE name=?", [name])["id"])


class _StubTM:
    """TradeModel stand-in: DB-touch ki zaroorat nahi, "insert" record karo."""

    def __init__(self, raise_on=False):
        self.rows = []
        self.raise_on = raise_on

    def insert_trade(self, data):
        if self.raise_on:
            raise RuntimeError("boom: test insert crash")
        self.rows.append(dict(data))
        return 4000 + len(self.rows)


class _StubCosts(dict):
    pass


def test_mirror_inserts_paper_copy(monkeypatch):
    """Happy path: active strategy -> mirror row, empty note, live `adj` jaisa
    hi entry_price."""
    import datetime as _dt
    from core.models.database import Database
    db = Database.get_instance()
    name = f"t-mirror-{os.urandom(4).hex()}"
    sid = _mk_strategy(db, name)
    tm = _StubTM()
    tid, note = B._paper_mirror_insert(
        sid, tm, 65.8, {"total": 24.5}, _dt, 1, 75, "NIFTY", "CE", 23400.0,
        "2026-10-29", "SELL", 1500.0, 3000.0)
    assert tid > 0, (tid, note)
    assert note == "", note
    assert len(tm.rows) == 1
    row = tm.rows[0]
    assert row["trade_mode"] == "paper"
    assert float(row["entry_price"]) == 65.8
    assert int(row["strategy_id"]) == sid
    assert row["broker_order_id"] == ""
    db.execute("DELETE FROM strategies WHERE id=?", [sid])


def test_mirror_never_silent(monkeypatch):
    """Missing strategy / paused strategy / insert crash - sab notes lautta
    hai (pehle `except: pass` ye sab chhupa deta tha)."""
    import datetime as _dt
    from core.models.database import Database
    db = Database.get_instance()
    # (1) missing strategy id
    tid, note = B._paper_mirror_insert(
        99999999, _StubTM(), 65.8, {"total": 0}, _dt, 1, 75,
        "NIFTY", "CE", 23400.0, "2026-10-29", "SELL", 1500.0, 3000.0)
    assert tid == 0 and note, (tid, note)
    # (2) paused strategy = Paper Trade OFF
    name = f"t-mirror-paused-{os.urandom(4).hex()}"
    sid = _mk_strategy(db, name, status="paused")
    tid, note = B._paper_mirror_insert(
        sid, _StubTM(), 65.8, {"total": 0}, _dt, 1, 75,
        "NIFTY", "CE", 23400.0, "2026-10-29", "SELL", 1500.0, 3000.0)
    assert tid == 0 and "Pause" not in note, note
    assert "paused" in note, note
    db.execute("DELETE FROM strategies WHERE id=?", [sid])
    # (3) insert crash = loud note (koi silent swallow nahi)
    name2 = f"t-mirror-crash-{os.urandom(4).hex()}"
    sid2 = _mk_strategy(db, name2)
    tid, note = B._paper_mirror_insert(
        sid2, _StubTM(raise_on=True), 65.8, {"total": 0}, _dt, 1, 75,
        "NIFTY", "CE", 23400.0, "2026-10-29", "SELL", 1500.0, 3000.0)
    assert tid == 0 and "FAIL" in note and "boom" in note, (tid, note)
    db.execute("DELETE FROM strategies WHERE id=?", [sid2])


def test_paper_button_force_never_fires_broker(monkeypatch):
    """FORCE paper click -> _execute_legs ko force_paper=True jana CHAHIYE
    (real money paper button se kabhi nahi)."""
    from core.models.database import Database
    import core.services.auto_paper as ap
    db = Database.get_instance()
    name = f"t-force-paper-{os.urandom(4).hex()}"
    sid = _mk_strategy(db, name)
    db.execute("INSERT OR REPLACE INTO settings (setting_key, setting_value)"
               " VALUES (?, '1')", [S._strat_live_key(sid)])
    seen = {}

    def fake_exec(s, legs, db_, tm_, tday, force_paper=False):
        seen["force_paper"] = force_paper
        return [{"trade_id": 77, "entry": 100.0}], []

    monkeypatch.setattr(ap, "_execute_legs", fake_exec)
    try:
        out = S.strategy_paper_trade(sid, body={"force": True})
        assert out.get("placed"), out
        assert seen.get("force_paper") is True, seen
    finally:
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        db.execute("DELETE FROM strategies WHERE id=?", [sid])
        db.execute("DELETE FROM settings WHERE setting_key=?",
                   [S._strat_live_key(sid)])


def test_both_on_paper_signal_skip_still_places(monkeypatch):
    """Dono ON + signal gate skip -> paper copy TURANT lagti hai (sync)."""
    from core.models.database import Database
    import core.services.auto_paper as ap
    import core.services.sl_monitor as slm
    db = Database.get_instance()
    name = f"t-bothon-{os.urandom(4).hex()}"
    sid = _mk_strategy(db, name)
    db.execute("INSERT OR REPLACE INTO settings (setting_key, setting_value)"
               " VALUES (?, '1')", [S._strat_live_key(sid)])
    monkeypatch.setattr(slm, "entry_open_ist", lambda: True)

    def fake_gated(gate_sid, signal_check):
        # signal gate ne entry deny ki - sirf skip note
        return {"placed": [], "notes": [f"N: aaj koi signal nahi - skipped"]}

    seen = {}

    def fake_exec(s, legs, db_, tm_, tday, force_paper=False):
        seen["force_paper"] = force_paper
        seen["legs"] = legs
        return [{"trade_id": 88, "entry": 101.0}], []

    monkeypatch.setattr(ap, "place_for_strategy", fake_gated)
    monkeypatch.setattr(ap, "_execute_legs", fake_exec)
    try:
        out = S.strategy_paper_trade(sid, body={})
        assert out.get("placed"), out
        assert out.get("forced_by"), out
        assert seen.get("force_paper") is True
        assert seen["legs"][0]["option_type"] == "CE"
    finally:
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        db.execute("DELETE FROM strategies WHERE id=?", [sid])
        db.execute("DELETE FROM settings WHERE setting_key=?",
                   [S._strat_live_key(sid)])


def test_both_on_guards_never_double_trade(monkeypatch):
    """Open position / max-day par both-ON sync force NAHI karna chahiye
    (double trade protection fail-closed)."""
    from core.models.database import Database
    import core.services.auto_paper as ap
    import core.services.sl_monitor as slm
    import datetime as _dt
    db = Database.get_instance()
    name = f"t-bothon-guard-{os.urandom(4).hex()}"
    sid = _mk_strategy(db, name)
    db.execute("INSERT OR REPLACE INTO settings (setting_key, setting_value)"
               " VALUES (?, '1')", [S._strat_live_key(sid)])
    today = (_dt.datetime.utcnow() +
             _dt.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
    monkeypatch.setattr(slm, "entry_open_ist", lambda: True)

    def fake_gated(gate_sid, signal_check):
        return {"placed": [], "notes": [f"N: aaj koi signal nahi - skipped"]}

    monkeypatch.setattr(ap, "place_for_strategy", fake_gated)
    try:
        # (1) open position -> force nahi
        db.execute(
            "INSERT INTO paper_trades (symbol, option_type, strike_price,"
            " transaction_type, quantity, lot_size, entry_price, entry_date,"
            " status, strategy_id) VALUES ('NIFTY','CE',25000,'BUY',1,75,"
            " 100.0,?,'open',?)", [today, sid])
        out = S.strategy_paper_trade(sid, body={})
        assert out.get("placed") == [], out
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        # (2) max-day reach -> force nahi
        for _ in range(5):
            db.execute(
                "INSERT INTO paper_trades (symbol, option_type, strike_price,"
                " transaction_type, quantity, lot_size, entry_price, entry_date,"
                " exit_price, pnl, status, strategy_id) VALUES "
                "('NIFTY','CE',25000,'BUY',1,75,100.0,?,90.0,-750.0,'closed',?)",
                [today, sid])
        out2 = S.strategy_paper_trade(sid, body={})
        assert out2.get("placed") == [], out2
    finally:
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        db.execute("DELETE FROM strategies WHERE id=?", [sid])
        db.execute("DELETE FROM settings WHERE setting_key=?",
                   [S._strat_live_key(sid)])


def test_paper_only_keeps_signal_gate(monkeypatch):
    """Paper SINGLE mode: signal skip par KOI blind force nahi (purani parity
    philosophy untouched)."""
    from core.models.database import Database
    import core.services.auto_paper as ap
    import core.services.sl_monitor as slm
    db = Database.get_instance()
    name = f"t-paponly-{os.urandom(4).hex()}"
    sid = _mk_strategy(db, name)
    monkeypatch.setattr(slm, "entry_open_ist", lambda: True)

    def fake_gated(gate_sid, signal_check):
        return {"placed": [], "notes": [f"N: aaj koi signal nahi - skipped"]}

    monkeypatch.setattr(ap, "place_for_strategy", fake_gated)
    called = {"n": 0}

    def fake_exec(*a, **k):
        called["n"] += 1
        return [], []

    monkeypatch.setattr(ap, "_execute_legs", fake_exec)
    try:
        out = S.strategy_paper_trade(sid, body={})
        assert out.get("placed") == [] and not out.get("forced_by"), out
        assert called["n"] == 0, called
    finally:
        db.execute("DELETE FROM paper_trades WHERE strategy_id=?", [sid])
        db.execute("DELETE FROM strategies WHERE id=?", [sid])
