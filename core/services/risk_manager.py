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
        defaults = {
            "sl_pct": 2.0,
            "tp_pct": 4.0,
            "max_trades_per_day": 5,
            "daily_loss_limit": 5000,
            "trailing": "lock",
            "lock_at_pct": 1.0,
            "max_holdings": 3,
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
        if not self.state.get("circuit_breaker", True):
            return {"allowed": False, "reason": "Circuit breaker OFF - daily loss limit hit"}
        if self.state["trades_today"] >= self.config["max_trades_per_day"]:
            return {"allowed": False, "reason": f"Max trades/day ({self.config['max_trades_per_day']}) reached"}
        if self.state["daily_pnl"] <= -self.config["daily_loss_limit"]:
            self.state["circuit_breaker"] = False
            self._save_state()
            return {"allowed": False, "reason": f"Daily loss limit (₹{self.config['daily_loss_limit']}) hit - circuit breaker triggered"}
        if len(self.state.get("open_trades", [])) >= self.config.get("max_holdings", 3):
            return {"allowed": False, "reason": f"Max open holdings ({self.config.get('max_holdings', 3)}) reached"}
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
        if self.state["daily_pnl"] <= -self.config["daily_loss_limit"]:
            self.state["circuit_breaker"] = False
        self._save_state()

    def update_config(self, new_cfg: Dict):
        self.config.update(new_cfg)
        self._save_config()

    def get_status(self) -> Dict:
        return {
            "daily_pnl": round(self.state.get("daily_pnl", 0), 2),
            "trades_today": self.state.get("trades_today", 0),
            "daily_limit": self.config.get("daily_loss_limit", 5000),
            "max_trades": self.config.get("max_trades_per_day", 5),
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
