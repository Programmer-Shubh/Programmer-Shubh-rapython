"""Suite-wide test config.

The auto-paper entry-time gate (backtest parity: legacy entry only inside
the strategy's advanced.entry_time window; default 09:35-09:50 IST) is
wall-clock dependent - most existing tests place entries at whatever time
pytest runs. Auto-pass the gate suite-wide; the dedicated test
(test_auto_paper_entry_window) verifies the REAL gate with explicit _now.
"""
import pytest


@pytest.fixture(autouse=True)
def _entry_window_pass(request):
    try:
        import core.services.auto_paper as ap
        ap._ENTRY_WINDOW_PASS = True
        yield
        ap._ENTRY_WINDOW_PASS = False
    except Exception:
        yield
