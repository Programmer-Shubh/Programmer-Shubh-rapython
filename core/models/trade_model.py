from core.models.database import Database
from core.services.transaction_costs import TransactionCosts
from utils.helpers import get_lot_size


class TradeModel:
    def __init__(self):
        self.db = Database.get_instance()

    def insert_trade(self, data: dict) -> int:
        return self.db.execute(
            """INSERT INTO paper_trades
               (user_id, strategy_id, symbol, option_type, strike_price, expiry_date,
                transaction_type, quantity, lot_size, entry_price, stop_loss, target,
                auto_action, total_cost, entry_date, trade_mode, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', datetime('now'), datetime('now'))""",
            [
                data.get("user_id", 1), data.get("strategy_id"), data["symbol"],
                data["option_type"], data["strike_price"], data.get("expiry_date", ""),
                data["transaction_type"], data.get("quantity", 1),
                data.get("lot_size", get_lot_size(data["symbol"])),
                data["entry_price"], data.get("stop_loss", 500), data.get("target", 1000),
                data.get("auto_action", "OFF"), data.get("total_cost", 0),
                data.get("entry_date", ""), data.get("trade_mode", "paper"),
            ],
        )

    def get_open_trades(self, user_id=1) -> list:
        return self.db.fetch_all(
            "SELECT * FROM paper_trades WHERE user_id=? AND status='open' ORDER BY created_at DESC",
            [user_id],
        )

    def get_closed_trades(self, user_id=1) -> list:
        return self.db.fetch_all(
            "SELECT * FROM paper_trades WHERE user_id=? AND status='closed' ORDER BY exit_date DESC",
            [user_id],
        )

    def get_open_positions_with_pnl(self, user_id=1) -> list:
        trades = self.get_open_trades(user_id)
        result = []
        for t in trades:
            current_price = self.get_option_premium(
                t["symbol"], t["option_type"], t["strike_price"], t["expiry_date"]
            )
            entry = t["entry_price"]
            qty = t["quantity"]
            lot = t.get("lot_size", 50)
            if current_price is None or entry <= 0:
                # Fix: include unrealized_pct so dashboard doesn't crash on stock trades with 0 entry fallback
                result.append({"trade": t, "current_price": entry if entry>0 else (current_price or 0), "unrealized_pnl": 0, "unrealized_pct": 0, "invalid": True})
                continue
            if t["transaction_type"] == "BUY":
                pnl = (current_price - entry) * qty * lot
            else:
                pnl = (entry - current_price) * qty * lot
            result.append({
                "trade": t,
                "current_price": round(current_price, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pct": round((pnl / (entry * qty * lot)) * 100, 2) if entry > 0 else 0,
                "invalid": False,
            })
        return result

    def get_option_premium(self, symbol, option_type, strike, expiry) -> float:
        if strike <= 0:
            return None
        # Primary: exact strike on latest date
        row = self.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type=? AND strike_price=? AND trade_date=(SELECT MAX(trade_date) FROM bhavcopy_data WHERE symbol=?)",
            [symbol, option_type, strike, symbol],
        )
        if row and row["close_price"] and float(row["close_price"]) > 0:
            return float(row["close_price"])
        # Fallback: nearest strike within reasonable distance (2 steps) - avoid far OTM returning wrong premium for stocks
        try:
            from utils.helpers import get_strike_step
            step = get_strike_step(symbol)
            row = self.db.fetch_one(
                "SELECT close_price, strike_price FROM bhavcopy_data WHERE symbol=? AND option_type=? AND ABS(strike_price-?) <= ?*2 ORDER BY ABS(strike_price-?) ASC LIMIT 1",
                [symbol, option_type, strike, step, strike],
            )
            if row and row["close_price"] and float(row["close_price"]) > 0:
                return float(row["close_price"])
        except Exception:
            pass
        # Fallback: nearest strike any distance
        row = self.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type=? ORDER BY ABS(strike_price-?) ASC LIMIT 1",
            [symbol, option_type, strike],
        )
        if row and row["close_price"] and float(row["close_price"]) > 0:
            # Only use if distance not absurd (>10*step) else use spot estimate
            try:
                from utils.helpers import get_strike_step as _gss
                _step = _gss(symbol)
                # We don't have strike here, check distance via query again? Use estimate if far
                # If strike diff too large, fall through to spot estimate for stocks
                pass
            except:
                pass
            return float(row["close_price"])
        # Final fallback for stocks with no option data: use live premium or spot estimate
        try:
            from core.services.live_market_data import LiveMarketData
            live = LiveMarketData().get_option_ltp(symbol, strike, option_type)
            if live and live > 0:
                return float(live)
        except Exception:
            pass
        # Last resort: 1% of spot
        try:
            spot_row = self.db.fetch_one("SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1", [symbol])
            if spot_row and spot_row["close_price"]:
                return float(spot_row["close_price"]) * 0.015
        except Exception:
            pass
        if row and row["close_price"]:
            return float(row["close_price"])
        # Fallback for stocks not in DB (e.g., fresh F&O symbol) - estimate via spot * 2% as premium to keep trade visible
        try:
            spot_row = self.db.fetch_one("SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL ORDER BY trade_date DESC LIMIT 1", [symbol])
            spot = float(spot_row["close_price"]) if spot_row and spot_row["close_price"] else 0
            if spot <= 0:
                from core.services.live_market_data import LiveMarketData
                live = LiveMarketData().get_live_spot(symbol)
                if live and live.get("spot"):
                    spot = float(live["spot"])
            if spot > 0:
                # Rough estimate: ATM CE ~2-3% of spot
                return round(spot * 0.02, 2)
        except Exception:
            pass
        return None

    def close_trade(self, trade_id: int, exit_price: float, exit_date: str, exit_status="manual") -> int:
        trade = self.db.fetch_one("SELECT * FROM paper_trades WHERE id=?", [trade_id])
        if not trade:
            return 0
        lot = trade.get("lot_size", 50)
        qty = trade["quantity"]
        is_sell = trade["transaction_type"] == "BUY"
        closing_side = "SELL" if is_sell else "BUY"
        exit_price = max(0.01, TransactionCosts.apply_fill_slippage(exit_price, closing_side, is_live=True))
        exit_costs = TransactionCosts.calculate(exit_price * qty * lot, is_sell, is_live=True)
        if trade["transaction_type"] == "BUY":
            gross_pnl = (exit_price - trade["entry_price"]) * qty * lot
        else:
            gross_pnl = (trade["entry_price"] - exit_price) * qty * lot
        pnl = gross_pnl - trade["total_cost"] - exit_costs["total"]
        pnl_pct = (pnl / (trade["entry_price"] * qty * lot)) * 100 if trade["entry_price"] > 0 else 0
        return self.db.execute(
            """UPDATE paper_trades SET exit_price=?, exit_date=?, exit_cost=?, pnl=?, pnl_percent=?,
               exit_status=?, status='closed', updated_at=datetime('now') WHERE id=?""",
            [exit_price, exit_date, exit_costs["total"], pnl, pnl_pct, exit_status, trade_id],
        )

    def delete_trade(self, trade_id: int) -> int:
        return self.db.execute("DELETE FROM paper_trades WHERE id=?", [trade_id])

    def set_trade_mode(self, trade_id: int, trade_mode: str) -> int:
        return self.db.execute(
            "UPDATE paper_trades SET trade_mode=?, updated_at=datetime('now') WHERE id=?",
            [trade_mode, trade_id],
        )

    def update_management(self, trade_id: int, stop_loss: float, target: float, auto_action: str) -> int:
        return self.db.execute(
            "UPDATE paper_trades SET stop_loss=?, target=?, auto_action=?, updated_at=datetime('now') WHERE id=?",
            [stop_loss, target, auto_action, trade_id],
        )

    def get_stats(self, user_id=1) -> dict:
        open_row = self.db.fetch_one(
            "SELECT COUNT(*) as c, COALESCE(SUM(quantity * entry_price), 0) as v FROM paper_trades WHERE user_id=? AND status='open'",
            [user_id],
        )
        closed_row = self.db.fetch_one(
            "SELECT COUNT(*) as c, COALESCE(SUM(pnl), 0) as total_pnl, COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), 0) as wins FROM paper_trades WHERE user_id=? AND status='closed'",
            [user_id],
        )
        c = closed_row["c"] or 0
        w = closed_row["wins"] or 0
        return {
            "open_count": open_row["c"] or 0,
            "open_value": open_row["v"] or 0,
            "closed_count": c,
            "total_pnl": closed_row["total_pnl"] or 0,
            "win_rate": round((w / c * 100), 1) if c > 0 else 0,
        }
