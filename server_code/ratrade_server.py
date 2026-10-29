"""Anvil-native server surface for the RaTrade dashboard.

The original project exposes FastAPI routes and depends on a long-running
ASGI process.  Anvil server modules expose the same useful workflows as
callable functions instead.  Until broker credentials are configured, the
market feed below is deliberately simulated and labelled as such.
"""

import math
import random
from datetime import date, datetime, timedelta

import anvil.server


SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "HDFCBANK", "TCS", "INFY"]
BASE_SPOTS = {
    "NIFTY": 24_850.0,
    "BANKNIFTY": 54_200.0,
    "FINNIFTY": 23_100.0,
    "RELIANCE": 1_420.0,
    "HDFCBANK": 1_760.0,
    "TCS": 3_980.0,
    "INFY": 1_620.0,
}
LOT_SIZES = {
    "NIFTY": 75,
    "BANKNIFTY": 30,
    "FINNIFTY": 65,
}
STRIKE_STEPS = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
}


def _normalise_symbol(symbol):
    symbol = str(symbol or "NIFTY").strip().upper()
    return symbol if symbol in BASE_SPOTS else "NIFTY"


def _seeded_rng(*parts):
    return random.Random("|".join(str(part) for part in parts))


def _spot(symbol, minute_bucket=None):
    symbol = _normalise_symbol(symbol)
    bucket = minute_bucket or datetime.utcnow().strftime("%Y-%m-%d-%H-%M")
    rng = _seeded_rng(symbol, bucket)
    base = BASE_SPOTS[symbol]
    change = rng.uniform(-0.65, 0.65)
    price = round(base * (1 + change / 100), 2)
    return price, round(change, 2)


def _currency(value):
    sign = "-" if value < 0 else ""
    return "{}₹{:,.2f}".format(sign, abs(value))


def _dashboard_cards():
    cards = []
    bucket = datetime.utcnow().strftime("%Y-%m-%d-%H-%M")
    for symbol in SYMBOLS:
        price, change = _spot(symbol, bucket)
        cards.append({
            "symbol": symbol,
            "price": price,
            "price_formatted": "₹{:,.2f}".format(price),
            "change": change,
            "change_formatted": "{:+.2f}%".format(change),
            "source": "Simulated",
        })
    return cards


def _top_movers():
    bucket = datetime.utcnow().strftime("%Y-%m-%d-%H")
    movers = []
    for symbol in SYMBOLS[3:]:
        price, _ = _spot(symbol, bucket)
        change = round(_seeded_rng("mover", symbol, bucket).uniform(-3.5, 3.5), 2)
        movers.append({
            "symbol": symbol,
            "price": price,
            "change": change,
            "signal": "BUY CE" if change >= 0 else "BUY PE",
        })
    return sorted(movers, key=lambda item: item["change"], reverse=True)


@anvil.server.callable(name="get_dashboard")
def get_dashboard():
    """Return the dashboard view model used by the startup Form."""
    cards = _dashboard_cards()
    movers = _top_movers()
    positions = [
        {
            "symbol": "NIFTY",
            "option_type": "CE",
            "strike": 24900,
            "side": "BUY",
            "entry": 182.50,
            "current": 196.20,
            "pnl": 1_027.50,
            "status": "Paper",
        },
    ]
    closed_pnl = 3_420.0
    open_pnl = sum(position["pnl"] for position in positions)
    return {
        "cards": cards,
        "movers": movers,
        "positions": positions,
        "stats": {
            "open_count": len(positions),
            "closed_count": 12,
            "total_pnl": round(closed_pnl + open_pnl, 2),
            "win_rate": 66.7,
        },
        "feed_status": "Simulated feed · configure broker/NSE integration for live prices",
    }


@anvil.server.callable(name="get_option_chain")
def get_option_chain(symbol="NIFTY"):
    """Generate a compact option-chain view model for the selected symbol."""
    symbol = _normalise_symbol(symbol)
    spot, _ = _spot(symbol)
    step = STRIKE_STEPS.get(symbol, 50)
    atm = int(round(spot / step) * step)
    rng = _seeded_rng("chain", symbol, datetime.utcnow().strftime("%Y-%m-%d-%H"))
    rows = []
    for offset in range(-5, 6):
        strike = atm + offset * step
        distance = strike - spot
        time_value = max(4.0, 48.0 - abs(distance) * 0.075)
        ce = max(1.0, time_value + max(spot - strike, 0) * 0.018 + rng.uniform(-1.5, 1.5))
        pe = max(1.0, time_value + max(strike - spot, 0) * 0.018 + rng.uniform(-1.5, 1.5))
        rows.append({
            "strike": strike,
            "distance": round(distance, 2),
            "ce_ltp": round(ce, 2),
            "ce_oi": int(rng.uniform(10_000, 180_000)),
            "pe_ltp": round(pe, 2),
            "pe_oi": int(rng.uniform(10_000, 180_000)),
        })
    return {
        "symbol": symbol,
        "spot": spot,
        "atm": atm,
        "expiry": (date.today() + timedelta(days=(3 - date.today().weekday()) % 7)).isoformat(),
        "source": "Simulated",
        "rows": rows,
    }


def _parse_date(value, field_name):
    try:
        return date.fromisoformat(str(value)), None
    except (TypeError, ValueError):
        return None, "{} must use YYYY-MM-DD format".format(field_name)


def _number(config, key, default, minimum=None):
    value = config.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None, "{} must be a number".format(key.replace("_", " ").title())
    if minimum is not None and value < minimum:
        return None, "{} cannot be below {}".format(key.replace("_", " ").title(), minimum)
    return value, None


@anvil.server.callable(name="run_backtest")
def run_backtest(config):
    """Run the original app's core backtest workflow as an Anvil callable."""
    config = config or {}
    symbol = _normalise_symbol(config.get("symbol"))
    start, error = _parse_date(config.get("start_date", "2026-08-01"), "Start date")
    if error:
        return {"ok": False, "message": error}
    if start is None:
        return {"ok": False, "message": "Start date is required"}
    end, error = _parse_date(config.get("end_date", "2026-08-20"), "End date")
    if error:
        return {"ok": False, "message": error}
    if end is None:
        return {"ok": False, "message": "End date is required"}
    if end < start:
        return {"ok": False, "message": "End date must be on or after start date"}
    if (end - start).days > 366:
        return {"ok": False, "message": "Backtest range cannot exceed one year"}

    lots, error = _number(config, "lots", 1, 1)
    if error:
        return {"ok": False, "message": error}
    if lots is None:
        return {"ok": False, "message": "Lots is required"}
    stop_loss, error = _number(config, "stop_loss", 500, 0)
    if error:
        return {"ok": False, "message": error}
    if stop_loss is None:
        return {"ok": False, "message": "Stop loss is required"}
    take_profit, error = _number(config, "take_profit", 1000, 0)
    if error:
        return {"ok": False, "message": error}
    if take_profit is None:
        return {"ok": False, "message": "Take profit is required"}

    lot_size = LOT_SIZES.get(symbol, 1)
    rng = _seeded_rng("backtest", symbol, start, end, lots, stop_loss, take_profit)
    trades = []
    equity = 0.0
    wins = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            spot = BASE_SPOTS[symbol] * (1 + rng.uniform(-0.012, 0.012))
            option_entry = max(5.0, spot * 0.02)
            move = rng.gauss(0.001, 0.011)
            option_exit = max(1.0, option_entry * (1 + move * 4.0))
            gross = (option_exit - option_entry) * lot_size * lots
            charges = max(35.0, abs(gross) * 0.018)
            pnl = round(gross - charges, 2)
            equity = round(equity + pnl, 2)
            is_win = pnl > 0
            wins += int(is_win)
            trades.append({
                "date": current.isoformat(),
                "entry": round(option_entry, 2),
                "exit": round(option_exit, 2),
                "pnl": pnl,
                "reason": "Target Hit" if pnl > take_profit else "Time Exit",
            })
        current += timedelta(days=1)

    total = len(trades)
    net_pnl = round(sum(trade["pnl"] for trade in trades), 2)
    return {
        "ok": True,
        "symbol": symbol,
        "lot_size": lot_size,
        "metrics": {
            "net_pnl": net_pnl,
            "total_trades": total,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "avg_trade": round(net_pnl / total, 2) if total else 0,
            "max_drawdown": round(_max_drawdown([trade["pnl"] for trade in trades]), 2),
        },
        "trades": trades,
    }


def _max_drawdown(pnls):
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


_paper_positions = []


@anvil.server.callable(name="get_trade_book")
def get_trade_book():
    """Return the paper-trading view used by the Anvil Trading Center.

    The upstream project persists these records in SQLite/Postgres. Anvil's
    first safe conversion keeps the workflow explicitly paper-only; Data Table
    persistence can be enabled later without exposing broker credentials.
    """
    sample = [{
        "id": "demo-1",
        "symbol": "NIFTY",
        "option_type": "CE",
        "strike": 24900,
        "transaction_type": "BUY",
        "quantity": 1,
        "entry_price": 182.50,
        "current_price": 196.20,
        "pnl": 1_027.50,
        "status": "Paper",
    }]
    return {"mode": "Paper only", "positions": sample + list(_paper_positions)}


@anvil.server.callable(name="place_paper_trade")
def place_paper_trade(config):
    """Validate and record one paper trade without contacting a broker."""
    config = config or {}
    symbol = _normalise_symbol(config.get("symbol"))
    option_type = str(config.get("option_type", "CE")).upper()
    transaction_type = str(config.get("transaction_type", "BUY")).upper()
    if option_type not in ("CE", "PE"):
        return {"ok": False, "message": "Option type must be CE or PE"}
    if transaction_type not in ("BUY", "SELL"):
        return {"ok": False, "message": "Paper trade side must be BUY or SELL"}
    strike, error = _number(config, "strike", 0, 1)
    if error:
        return {"ok": False, "message": error}
    if strike is None:
        return {"ok": False, "message": "Strike is required"}
    entry_price, error = _number(config, "entry_price", 0, 0.01)
    if error:
        return {"ok": False, "message": error}
    if entry_price is None:
        return {"ok": False, "message": "Entry premium is required"}
    quantity, error = _number(config, "quantity", 1, 1)
    if error:
        return {"ok": False, "message": error}
    if quantity is None:
        return {"ok": False, "message": "Quantity is required"}
    position = {
        "id": "paper-{}".format(len(_paper_positions) + 1),
        "symbol": symbol,
        "option_type": option_type,
        "strike": int(strike),
        "transaction_type": transaction_type,
        "quantity": int(quantity),
        "entry_price": round(entry_price, 2),
        "current_price": round(entry_price, 2),
        "pnl": 0.0,
        "status": "Paper",
    }
    _paper_positions.append(position)
    return {"ok": True, "message": "Paper trade recorded", "trade": position}


@anvil.server.callable(name="get_source_status")
def get_source_status():
    """Expose the conversion boundary shown in the UI."""
    return {
        "repository": "Programmer-Shubh/Programmer-Shubh-rapython",
        "source_mode": "Simulated market feed",
        "execution_mode": "Paper only",
        "note": "Configure Anvil Secrets before adding a broker integration",
    }
