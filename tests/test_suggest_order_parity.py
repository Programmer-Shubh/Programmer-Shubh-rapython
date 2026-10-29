"""Suggestion-vs-order premium parity: same expiry + same DTE both sides."""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.contract_pricer import resolve_weekly_expiry, dte_to_expiry


def test_weekly_expiry_valid():
    exp = resolve_weekly_expiry("NIFTY")
    assert len(exp) == 10 and exp[4] == "-" and exp[7] == "-", exp
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    assert exp >= today, (exp, today)


def test_dte_sane():
    assert dte_to_expiry("", 7) == 7
    # date arithmetic is truncating (same as order-side legacy formula)
    today = datetime.datetime.now()
    t3 = (today + datetime.timedelta(days=3)).strftime("%Y-%m-%d")
    got = dte_to_expiry(t3, 7)
    assert got in (2, 3), got
    t0 = today.strftime("%Y-%m-%d")
    assert dte_to_expiry(t0, 7) == 1


def test_suggestion_floor_matches_order_math():
    """Suggested strike (floor-passed) must also pass order-side model math
    at the SAME real DTE - else the floor error from the bug report."""
    from core.services.scanner import OptionScanner
    from utils.helpers import model_premium, get_strike_step
    sc = OptionScanner()
    for sym, spot in (("NIFTY", 25000.0), ("BANKNIFTY", 57000.0),
                      ("RELIANCE", 2850.0)):
        for opt in ("CE", "PE"):
            sug = sc._suggest_option_floor(sym, spot, opt, "BUY", 20.0)
            if not sug.get("strike"):
                continue  # honest skip (no mirror promise)
            # Parity inputs: suggestion ke APNE spot/expiry/DTE par order
            # math dobara chalao - floor-pass ka matlab order-pass hona chahiye
            s2 = float(sug.get("spot") or spot)
            exp = sug.get("expiry") or resolve_weekly_expiry(sym)
            dte = dte_to_expiry(exp, 7)
            px = model_premium(s2, float(sug["strike"]), dte, opt, symbol=sym)
            assert px >= 20.0 - 0.01, (sym, opt, sug, dte, px)


def test_stale_db_ignored_in_suggestion():
    """3+ din purana DB close suggestion ko inflate nahi karega."""
    from core.services.scanner import OptionScanner
    from core.models.database import Database
    sc = OptionScanner()
    db = Database.get_instance()
    old = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
    try:
        db.execute(
            "INSERT INTO bhavcopy_data (symbol, trade_date, expiry_date, strike_price,"
            " option_type, open_price, close_price, volume) VALUES "
            "('TESTSTALE', ?, '2030-01-01', 100.0, 'CE', 999.0, 999.0, 10)",
            [old],
        )
        step_ok = True
    except Exception:
        step_ok = False
    try:
        sug = sc._suggest_option("TESTSTALE", 100.0, "CE")
        # stale 999 must NOT leak through (model value instead)
        assert float(sug.get("premium") or 0) < 999.0, sug
    finally:
        try:
            db.execute("DELETE FROM bhavcopy_data WHERE symbol='TESTSTALE'")
        except Exception:
            pass
        assert step_ok
