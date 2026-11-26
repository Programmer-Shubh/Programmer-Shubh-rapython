"""Single-source-of-truth checks for Saved BT pill / cards / Trade History.

Regression guards for the exact questions:
  1. Saved BT pill aur strategy card ke metrics match kar rahe hain ya nahi?
  2. Trade History me TICK DATA / 1M OPTION CHAIN badge sahi aa raha hai ya nahi?
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

import json

from routes.strategies import _bt_summary, bt_pill_text, data_source_badge


def _row(mode="1m_chain", trades=17, pnl=4321.5, win=70.588235):
    result = {
        "run_id": "bt-test-1",
        "data_source": "real_ticks" if mode == "tick" else mode,
        "metrics": {"total_trades": trades, "net_pnl": pnl, "win_rate": win,
                    "profit_factor": 2.3456},
    }
    return {"id": 9, "name": "User strategy", "symbol": "NIFTY",
            "last_backtest": json.dumps(result),
            "last_backtest_at": "2026-11-25 15:20:00",
            "last_backtest_hash": "x"}


def test_summary_matches_saved_record_exactly():
    row = _row()
    s = _bt_summary(row)
    m = json.loads(row["last_backtest"])["metrics"]
    assert s["trades"] == m["total_trades"]
    assert s["net_pnl"] == round(m["net_pnl"], 2)
    assert s["win_rate"] == round(m["win_rate"], 2)
    assert s["profit_factor"] == 2.35          # pill now carries PF too
    assert s["run_id"] == "bt-test-1"
    assert s["stored_at"] == "2026-11-25 15:20:00"
    assert s["data_source_badge"] == "1M OPTION CHAIN"
    assert bt_pill_text(s) == ("17 trades • ₹4,321.50 • Win 70.6% • PF 2.35 • "
                               "2026-11-25 15:20:00")


def test_badge_labels_for_both_real_modes():
    assert data_source_badge({"data_source": "real_ticks"}) == "TICK DATA"
    assert data_source_badge({"data_source": "real_1m_ticks"}) == "1M OPTION CHAIN"
    assert data_source_badge({"data_source": "1m_chain"}) == "1M OPTION CHAIN"
    assert data_source_badge({"data_source": "real_eod_bhavcopy"}) == "EOD BHAVCOPY"
    assert data_source_badge({"data_source": "black_scholes"}) == "MODEL"
    assert _bt_summary(_row("tick"))["data_source_badge"] == "TICK DATA"


def test_ui_uses_shared_helper_and_badge_column():
    html = open(os.path.join(os.path.dirname(__file__), "..", "static", "index.html"),
                encoding="utf-8").read()
    assert "function savedBtHtml(s,compact)" in html
    assert "savedBtHtml(s,true)" in html           # Paper ON/OFF table
    assert "<th>Data Source</th>" in html          # Trade History
    assert "data_source_badge" in html
    assert "bt_trades" in html
    assert "r-is-oos" not in html          # IS/OOS toggle removed (user rule)
    assert "Fetch Real Data (NSE)" in html


def test_only_high_frequency_tick_data_mode_is_visible():
    """User rule: 1-Minute Option Chain dropdown option hata diya gaya hai."""
    html = open(os.path.join(os.path.dirname(__file__), "..", "static", "index.html"),
                encoding="utf-8").read()
    assert "1-Minute Option Chain" not in html
    for select_id in ("r-data-mode", "lh-mode"):
        marker = f'<select id="{select_id}"'
        chunk = html.split(marker, 1)[1].split("</select>", 1)[0]
        assert chunk.count("<option") == 1, chunk
        assert 'value="tick"' in chunk and "High-Frequency" in chunk
