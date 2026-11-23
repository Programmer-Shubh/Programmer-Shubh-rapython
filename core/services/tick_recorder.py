"""Tick recorder: captures real option LTP per minute while broker is connected,
stores into the `option_ticks` table so historical backtests can use real intraday
option data instead of daily-anchored interpolation.

Usage:
    from core.services.tick_recorder import start_tick_recorder, stop_tick_recorder
    start_tick_recorder(interval_sec=60)   # default 1-min ticks
    # ... run your app ...
    stop_tick_recorder()
"""

import asyncio
import time as _time
from core.models.database import Database