import json
import os
from datetime import datetime, date
from typing import Dict, Optional, List


class RiskManager:
    """Risk management for email alert trading - NextLevelBot style."""

    CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "alert_config.json")
    STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "alert_state.json")

    def __init__(self):
        self.config = self._load_config()
        self.state = self._load_state()

    def _load_config(self) -> Dict:
        # User rule (2026-11): NO daily limits. 0 = unlimited. These caps are
        # kept only as optional overrides; the app never sets them by default.
        defaults = {
            "sl_pct": 2.0,
            "tp_pct": 4.0,
            "max_trades_per_day": 0,
            "daily_loss_limit": 0,
            "trailing": "lock",
            "lock_at_pct": 1.0,
            "max_holdings": 0,
        }
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, "r") as f:
                    cfg = json.load(f)
                    defaults.update(cfg)
        except Exception:
            pass
        return defaults

    def _load_state(self) -> Dict:
        defaults = {
            "today": str(date.today()),
            "trades_today": 0,
            "daily_pnl": 0.0,
            "circuit_breaker": True,
            "open_trades": [],
        }
        try:
            if os.path.exists(self.STATE_FILE):
                with open(self.STATE_FILE, "r") as f:
                    state = json.load(f)
                    if state.get("today") != str(date.today()):
                        state["today"] = str(date.today())
                        state["trades_today"] = 0
                        state["daily_pnl"] = 0.0
                        state["circuit_breaker"] = True
                        state["open_trades"] = []
                    defaults.update(state)
        except Exception:
            pass
        return defaults

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
            with open(self.STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception:
            pass

    def _save_config(self):
        try:
            os.makedirs(os.path.dirname(self.CONFIG_FILE), exist_ok=True)
            with open(self.CONFIG_FILE, "w") as f:
                json.dump(self.config, f, indent=2)
        except Exception:
            pass

    def can_trade(self) -> Dict:
        """Daily caps are DISABLED by default (0 = unlimited). Only a positive
        explicitly-configured cap is enforced - Fyers keeps taking every valid
        signal all day without a max-loss or max-trades stop."""
        try:
            max_trades = int(self.config.get("max_trades_per_day") or 0)
        except Exception:
            max_trades = 0
        if max_trades > 0 and self.state["trades_today"] >= max_trades:
            return {"allowed": False, "reason": f"Max trades/day ({max_trades}) reached"}
        try:
            daily_limit = float(self.config.get("daily_loss_limit") or 0)
        except Exception:
            daily_limit = 0
        if daily_limit > 0:
            if not self.state.get("circuit_breaker", True):
                return {"allowed": False, "reason": "Circuit breaker OFF - daily loss limit hit"}
            if self.state["daily_pnl"] <= -daily_limit:
                self.state["circuit_breaker"] = False
                self._save_state()
                return {"allowed": False, "reason": f"Daily loss limit (₹{daily_limit}) hit - circuit breaker triggered"}
        try:
            max_holdings = int(self.config.get("max_holdings") or 0)
        except Exception:
            max_holdings = 0
        if max_holdings > 0 and len(self.state.get("open_trades", [])) >= max_holdings:
            return {"allowed": False, "reason": f"Max open holdings ({max_holdings}) reached"}
        return {"allowed": True, "reason": "OK"}

    def calculate_sl_tp(self, entry_price: float, option_type: str = "CE") -> Dict:
        sl_pct = self.config["sl_pct"]
        tp_pct = self.config["tp_pct"]
        if option_type == "CE":
            sl = round(entry_price * (1 - sl_pct / 100), 2)
            tp = round(entry_price * (1 + tp_pct / 100), 2)
        else:
            sl = round(entry_price * (1 + sl_pct / 100), 2)
            tp = round(entry_price * (1 - tp_pct / 100), 2)
        return {"sl": sl, "tp": tp, "sl_pct": sl_pct, "tp_pct": tp_pct}

    def check_trailing(self, entry_price: float, current_price: float, highest_price: float, option_type: str = "CE") -> Optional[str]:
        trailing = self.config.get("trailing", "lock")
        lock_at = self.config.get("lock_at_pct", 1.0)
        if trailing == "none":
            return None
        if option_type == "CE":
            profit_pct = (current_price - entry_price) / max(entry_price, 0.01) * 100
            lock_level = entry_price * (1 + lock_at / 100)
            if highest_price >= lock_level and current_price <= highest_price * 0.97:
                return "trail_lock"
        else:
            profit_pct = (entry_price - current_price) / max(entry_price, 0.01) * 100
            lock_level = entry_price * (1 - lock_at / 100)
            if highest_price <= lock_level and current_price >= highest_price * 1.03:
                return "trail_lock"
        return None

    def register_trade(self, trade: Dict):
        self.state["trades_today"] += 1
        self.state.setdefault("open_trades", []).append(trade)
        self._save_state()

    def close_trade(self, trade_id: str, pnl: float):
        self.state["daily_pnl"] += pnl
        self.state["open_trades"] = [t for t in self.state.get("open_trades", []) if t.get("id") != trade_id]
        try:
            _dl = float(self.config.get("daily_loss_limit") or 0)
        except Exception:
            _dl = 0
        if _dl > 0 and self.state["daily_pnl"] <= -_dl:
            self.state["circuit_breaker"] = False
        self._save_state()

    def update_config(self, new_cfg: Dict):
        # Keep both keys in sync: UI/API may send either name.
        if "max_trades" in new_cfg and "max_trades_per_day" not in new_cfg:
            new_cfg = dict(new_cfg)
            new_cfg["max_trades_per_day"] = new_cfg.get("max_trades")
        self.config.update(new_cfg)
        self._save_config()

    def get_status(self) -> Dict:
        return {
            "daily_pnl": round(self.state.get("daily_pnl", 0), 2),
            "trades_today": self.state.get("trades_today", 0),
            "daily_limit": self.config.get("daily_loss_limit", 0) or 0,
            "max_trades": self.config.get("max_trades_per_day", 0) or 0,
            "unlimited": (not self.config.get("daily_loss_limit")) and (not self.config.get("max_trades_per_day")),
            "circuit_breaker": self.state.get("circuit_breaker", True),
            "open_count": len(self.state.get("open_trades", [])),
            "config": self.config,
        }

    def reset_daily(self):
        self.state["today"] = str(date.today())
        self.state["trades_today"] = 0
        self.state["daily_pnl"] = 0.0
        self.state["circuit_breaker"] = True
        self.state["open_trades"] = []
        self._save_state()
