"""Historical Replay Engine - Run paper trades on historical data with exact backtest logic.
This ensures paper trade results match backtest results exactly."""
import math
from typing import List, Dict, Optional
from core.services.backtest_engine import BacktestEngine
from core.services.indicator_engine import IndicatorEngine
from core.services.transaction_costs import TransactionCosts
from core.models.database import Database
from utils.helpers import get_lot_size, get_strike_step, black_scholes


class HistoricalReplayEngine:
    """Replays historical data bar-by-bar and executes paper trades identically to backtest."""
    
    def __init__(self, initial_capital: float = 1000000.0):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.indicators = IndicatorEngine()
        self.db = Database.get_instance()
        self.bt_engine = BacktestEngine(is_live=False)
        self.trades = []
        self.equity_curve = [initial_capital]
        self.current_bar_idx = 0
        self.historical_data = []
        self.symbol = ""
        self.start_date = ""
        self.end_date = ""
        self.ind_list = []
        self.entry_conditions = []
        self.exit_conditions = []
        self.legs = []
        self.advanced_options = {}
        self.risk_management = {}
        self.pending_entry = None
        self.pending_exit = None
        self.pending_auto_buy = None
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.kill_switch_on = False
        self.last_date = ""
        self.entry_bars = []
        
    def configure(self, symbol: str, start_date: str, end_date: str, 
                  ind_list: list, entry_conditions: list, exit_conditions: list,
                  legs: list, advanced_options: dict, risk_management: dict):
        """Configure replay with same parameters as backtest."""
        self.symbol = symbol
        self.start_date = start_date
        self.end_date = end_date
        self.ind_list = ind_list
        self.entry_conditions = entry_conditions
        self.exit_conditions = exit_conditions
        self.legs = legs
        self.advanced_options = advanced_options
        self.risk_management = risk_management
        
        # Load historical data (same as backtest)
        self.historical_data = self._load_historical(symbol, start_date, end_date)
        if len(self.historical_data) < 30:
            raise ValueError(f"Insufficient data: {len(self.historical_data)} bars")
            
        # Pre-calculate indicators (same as backtest)
        closes = [h["close_price"] for h in self.historical_data]
        highs = [h["high_price"] for h in self.historical_data]
        lows = [h["low_price"] for h in self.historical_data]
        self.pre_calc = self.bt_engine._pre_calc(self.historical_data, closes, highs, lows, ind_list)
        self.bt_engine.ind_modes = {}
        for ind in ind_list:
            iid = ind.get("id","") if isinstance(ind, dict) else ind
            mode = ind.get("params",{}).get("mode","both") if isinstance(ind, dict) else "both"
            self.bt_engine.ind_modes[iid] = str(mode).lower()
    
    def _load_historical(self, symbol: str, start_date: str, end_date: str) -> List[Dict]:
        """Load historical data from DB (same as backtest)."""
        rows = self.db.fetch_all(
            """SELECT * FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL
               AND trade_date >= ? AND trade_date <= ?
               ORDER BY trade_date ASC""",
            [symbol, start_date, end_date],
        )
        for r in rows:
            r['high_price'] = float(r.get('high_price', 0) or 0)
            r['low_price'] = float(r.get('low_price', 0) or 0)
            r['close_price'] = float(r.get('close_price', 0) or 0)
            r['open_price'] = float(r.get('open_price', 0) or 0)
            r['volume'] = float(r.get('volume', 0) or 0)
        return rows
    
    def run_replay(self) -> Dict:
        """Run the historical replay - executes trades bar by bar."""
        self._reset()
        
        # Extract config
        leg = self.legs[0] if self.legs else {"option_type": "CE", "lots": 1, "transaction": "buy"}
        option_type = leg.get("option_type", "CE")
        qty = int(leg.get("lots", 1)) * get_lot_size(self.symbol)
        txn_type = leg.get("transaction", "buy").lower()
        strike_sel = self.advanced_options.get("strike_selection", "otm")
        delta_target = self.advanced_options.get("delta_target")
        otm_dist = int(self.advanced_options.get("otm_distance", 2))
        trade_mode = self.advanced_options.get("trade_mode", "positional")
        max_holding = int(self.advanced_options.get("max_holding_bars", 20))
        max_trades_day = int(self.risk_management.get("max_trades_per_day", 5))
        daily_loss_limit = float(self.risk_management.get("daily_loss_limit", 0) or 0)
        leg_sl = float(leg.get("stop_loss", self.risk_management.get("daily_stop_loss", 1500)))
        leg_tp = float(leg.get("take_profit", self.risk_management.get("daily_take_profit", 1000)))
        
        is_spread = len(self.legs) > 1
        latency = TransactionCosts.latency_delay(False)  # backtest mode
        entry_time = self.advanced_options.get("entry_time", "09:35")
        exit_time = self.advanced_options.get("exit_time", "15:14")
        
        min_bars = min(15, max(1, len(self.historical_data) - 2))
        
        for i in range(min_bars, len(self.historical_data)):
            cur = self.historical_data[i]
            cur_date = cur["trade_date"]
            nxt = self.historical_data[i + 1] if i + 1 < len(self.historical_data) else None
            is_last = nxt is None or nxt["trade_date"] != cur_date
            
            # New day reset
            if cur_date != self.last_date:
                self.daily_trades = 0
                self.daily_pnl = 0.0
                self.kill_switch_on = False
                self.last_date = cur_date
            
            if self.kill_switch_on:
                self.equity_curve.append(self.capital)
                continue
            
            has_open = len(self.trades) > len([t for t in self.trades if t.get("exit_date")])
            
            # Execute pending entry from previous bar's signal
            can_enter = not has_open and self.daily_trades < max_trades_day
            if self.pending_entry is not None and can_enter:
                exec_bar = self.historical_data[i]
                spot = float(exec_bar.get("open_price", 0) or exec_bar.get("close_price", 0))
                
                auto_signal_exec = bool(self.advanced_options.get("auto_signal") or 
                                        (self.legs and self.legs[0].get("transaction","").lower()=="auto"))
                
                if auto_signal_exec:
                    is_buy = self.pending_auto_buy if self.pending_auto_buy is not None else True
                    auto_leg = {"option_type": "CE" if is_buy else "PE", "transaction": "buy", 
                               "lots": int(self.legs[0].get("lots",1) if self.legs else 1), 
                               "strike_selection": strike_sel, "otm_distance": otm_dist}
                    trade = self.bt_engine._enter_single(cur_date, spot, self.symbol, auto_leg, 
                                                         strike_sel, delta_target, otm_dist)
                    trade["auto_choice"] = "CE-BUY" if is_buy else "PE-BUY"
                elif is_spread:
                    trade = self.bt_engine._enter_spread(cur_date, spot, self.symbol, self.legs, 
                                                         strike_sel, delta_target, otm_dist)
                else:
                    trade = self.bt_engine._enter_single(cur_date, spot, self.symbol, self.legs[0], 
                                                         strike_sel, delta_target, otm_dist)
                
                trade["entry_bar_idx"] = i
                trade["entry_time"] = entry_time
                self.trades.append(trade)
                self.entry_bars.append(i)
                self.daily_trades += 1
                self.pending_entry = None
                self.pending_auto_buy = None
            
            has_open = len(self.trades) > len([t for t in self.trades if t.get("exit_date")])
            
            # Execute pending exit
            if has_open and self.pending_exit is not None:
                open_trade_idx = len([t for t in self.trades if t.get("exit_date")])
                entry = self.trades[open_trade_idx]
                exec_bar = self.historical_data[i]
                spot = float(exec_bar.get("open_price", 0) or exec_bar.get("close_price", 0))
                exit_prem = self.bt_engine._exit_premium(cur_date, spot, float(entry["strike"]), option_type)
                exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, 
                    "SELL" if txn_type == "buy" else "BUY", False)
                self.bt_engine._close_position(self.trades, [], entry, exit_prem, 
                    self.pending_exit, cur_date, qty, txn_type)
                # Update trade with exit info
                if self.trades[open_trade_idx].get("exit_date") is None:
                    self.trades[open_trade_idx]["exit_date"] = cur_date
                    self.trades[open_trade_idx]["exit_price"] = exit_prem
                    self.trades[open_trade_idx]["exit_time"] = exit_time
                self.daily_pnl += self.trades[open_trade_idx].get("pnl", 0)
                if daily_loss_limit > 0 and self.daily_pnl <= -daily_loss_limit:
                    self.kill_switch_on = True
                self.pending_exit = None
            
            # Check SL/TP
            has_open = len(self.trades) > len([t for t in self.trades if t.get("exit_date")])
            if has_open and not (trade_mode == "intraday" and is_last):
                open_trade_idx = len([t for t in self.trades if t.get("exit_date")])
                entry = self.trades[open_trade_idx]
                if not entry.get("is_spread") and (leg_sl > 0 or leg_tp > 0):
                    hit = self.bt_engine._check_sl_tp(cur, entry, option_type, txn_type, leg_sl, leg_tp)
                    if hit:
                        exit_prem = hit["level"]
                        self.bt_engine._close_position(self.trades, [], entry, exit_prem, 
                            hit["reason"], cur_date, qty, txn_type)
                        if self.trades[open_trade_idx].get("exit_date") is None:
                            self.trades[open_trade_idx]["exit_date"] = cur_date
                            self.trades[open_trade_idx]["exit_price"] = exit_prem
                            self.trades[open_trade_idx]["exit_time"] = exit_time
                        self.daily_pnl += self.trades[open_trade_idx].get("pnl", 0)
                        if daily_loss_limit > 0 and self.daily_pnl <= -daily_loss_limit:
                            self.kill_switch_on = True
            
            # Intraday forced exit
            has_open = len(self.trades) > len([t for t in self.trades if t.get("exit_date")])
            if has_open and trade_mode == "intraday" and is_last:
                open_trade_idx = len([t for t in self.trades if t.get("exit_date")])
                entry = self.trades[open_trade_idx]
                if entry.get("is_spread"):
                    self.bt_engine._close_spread(self.trades, [], entry, "intraday", cur_date)
                else:
                    exit_prem = self.bt_engine._close_premium(cur_date, float(cur["close_price"]), 
                        float(entry["strike"]), option_type)
                    exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, 
                        "SELL" if txn_type == "buy" else "BUY", False)
                    self.bt_engine._close_position(self.trades, [], entry, exit_prem, 
                        "intraday", cur_date, qty, txn_type)
                if self.trades[open_trade_idx].get("exit_date") is None:
                    self.trades[open_trade_idx]["exit_date"] = cur_date
                    self.trades[open_trade_idx]["exit_price"] = exit_prem
                    self.trades[open_trade_idx]["exit_time"] = exit_time
                self.daily_pnl += self.trades[open_trade_idx].get("pnl", 0)
                if daily_loss_limit > 0 and self.daily_pnl <= -daily_loss_limit:
                    self.kill_switch_on = True
            
            # Generate signals for next bar (using current bar i - no look-ahead)
            if nxt is not None:
                bars_held = 0
                if has_open:
                    open_idx = len([t for t in self.trades if t.get("exit_date")])
                    if open_idx < len(self.entry_bars):
                        bars_held = i - self.entry_bars[open_idx]
                time_exit = bars_held >= max_holding
                
                # Signals generated on bar i (closed), execution on bar i+1
                buy_sig = self.bt_engine._get_buy_signal(i, self.pre_calc, self.historical_data, self.entry_conditions)
                sell_sig = self.bt_engine._get_sell_signal(i, self.pre_calc, self.historical_data, self.exit_conditions)
                
                auto_signal = bool(self.advanced_options.get("auto_signal") or 
                                  (self.legs and self.legs[0].get("transaction","").lower()=="auto"))
                
                if auto_signal:
                    entry_sig = buy_sig or sell_sig
                    if not entry_sig and not self.pre_calc:
                        entry_sig = True
                    exit_sig = time_exit
                elif is_spread:
                    entry_sig = buy_sig or sell_sig
                    if not entry_sig and not self.pre_calc:
                        entry_sig = True
                    exit_sig = time_exit
                else:
                    if option_type == "PE":
                        entry_sig = sell_sig if txn_type == "buy" else buy_sig
                        exit_sig = buy_sig if txn_type == "buy" else sell_sig
                    else:
                        entry_sig = buy_sig if txn_type == "buy" else sell_sig
                        exit_sig = sell_sig if txn_type == "buy" else buy_sig
                
                has_open = len(self.trades) > len([t for t in self.trades if t.get("exit_date")])
                can_enter = not has_open and self.daily_trades < max_trades_day
                
                if entry_sig and can_enter:
                    if is_spread or auto_signal:
                        self.pending_entry = i
                        self.pending_entry_signal = None
                        if auto_signal:
                            self.pending_auto_buy = buy_sig
                    else:
                        if self.pending_entry_signal is None:
                            self.pending_entry_signal = i
                            if auto_signal:
                                self.pending_auto_buy = buy_sig
                        elif i - self.pending_entry_signal >= max(1, latency):
                            self.pending_entry = i
                            self.pending_entry_signal = None
                
                if has_open and self.pending_exit is None and (exit_sig or time_exit):
                    self.pending_exit = "condition" if exit_sig else "time"
            
            # Expiry square-off
            self.bt_engine._square_off_expiry(self.historical_data, self.trades, [], 
                self.symbol, option_type, txn_type, qty, self.risk_management)
            
            self.equity_curve.append(self.capital)
        
        # Close any remaining open positions at end
        while len([t for t in self.trades if t.get("exit_date")]) < len(self.trades):
            idx = len([t for t in self.trades if t.get("exit_date")])
            entry = self.trades[idx]
            last = self.historical_data[-1]
            if entry.get("is_spread"):
                self.bt_engine._close_spread(self.trades, [], entry, "end_of_period", last["trade_date"])
            else:
                exit_prem = self.bt_engine._close_premium(last["trade_date"], float(last["close_price"]), 
                    float(entry["strike"]), option_type)
                self.bt_engine._close_position(self.trades, [], entry, exit_prem, "end_of_period", 
                    last["trade_date"], qty, txn_type)
            if self.trades[idx].get("exit_date") is None:
                self.trades[idx]["exit_date"] = last["trade_date"]
                self.trades[idx]["exit_price"] = exit_prem
                self.trades[idx]["exit_time"] = exit_time
        
        return self._build_result()
    
    def _build_result(self) -> Dict:
        """Build result matching backtest format."""
        total_trades = 0
        wins = 0
        win_amounts = []
        loss_amounts = []
        trade_list = []
        total_brokerage = 0
        
        for i, trade in enumerate(self.trades):
            if trade.get("exit_date"):
                pnl = trade.get("pnl", 0)
                self.capital += pnl
                total_trades += 1
                total_brokerage += trade.get("costs", {}).get("total", 0) + trade.get("exit_costs", {}).get("total", 0)
                if pnl > 0:
                    wins += 1
                    win_amounts.append(pnl)
                else:
                    loss_amounts.append(abs(pnl))
                
                entry_time_str = trade.get("entry_time", "09:35")
                exit_time_str = trade.get("exit_time", "15:14")
                
                if trade.get("is_spread"):
                    legs_str = " + ".join(
                        f"{'Sell' if l['type']=='sell' else 'Buy'} {l['option_type']} {l['strike']}"
                        for l in trade.get("legs", [])
                    )
                    trade_list.append({
                        "index": total_trades,
                        "symbol": self.symbol,
                        "entry_date": trade.get("entry_date", ""),
                        "entry_time": entry_time_str,
                        "exit_date": trade.get("exit_date", ""),
                        "exit_time": exit_time_str,
                        "option_type": "Spread",
                        "strike": trade.get("strike", ""),
                        "position": legs_str,
                        "quantity": trade.get("quantity", 0),
                        "lots": trade.get("legs", [{}])[0].get("lots", 1) if trade.get("legs") else 1,
                        "entry_price": trade.get("price", 0),
                        "exit_price": trade.get("exit_price", 0),
                        "pnl": pnl,
                        "pnl_formatted": f"₹{pnl:,.2f}",
                        "is_spread": True,
                    })
                else:
                    leg_info = trade.get("legs", [{}])[0] if trade.get("legs") else {}
                    trade_list.append({
                        "index": total_trades,
                        "symbol": self.symbol,
                        "entry_date": trade.get("entry_date", ""),
                        "entry_time": entry_time_str,
                        "exit_date": trade.get("exit_date", ""),
                        "exit_time": exit_time_str,
                        "option_type": leg_info.get("option_type", "CE"),
                        "strike": trade.get("strike", ""),
                        "position": "Sell" if trade.get("type") == "sell" else "Buy",
                        "quantity": trade.get("quantity", 0),
                        "lots": leg_info.get("lots", 1),
                        "entry_price": trade.get("price", 0),
                        "exit_price": trade.get("exit_price", 0),
                        "pnl": pnl,
                        "pnl_formatted": f"₹{pnl:,.2f}",
                    })
        
        total_return = self.capital - self.initial_capital
        total_return_pct = (total_return / self.initial_capital) * 100
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        loss_rate = 100 - win_rate if total_trades > 0 else 0
        avg_win = sum(win_amounts) / len(win_amounts) if win_amounts else 0
        avg_loss = sum(loss_amounts) / len(loss_amounts) if loss_amounts else 0
        profit_factor = sum(win_amounts) / max(sum(loss_amounts), 0.01) if loss_amounts else (999 if win_amounts else 0)
        max_dd = self._max_drawdown(self.equity_curve)
        sharpe = self._sharpe(self.equity_curve)
        sortino = self._sortino(self.equity_curve)
        calmar = self._calmar(total_return_pct, max_dd)
        kelly = self._kelly(win_rate, avg_win, avg_loss)
        ulcer = self._ulcer_index(self.equity_curve)
        omega = self._omega_ratio(trade_list)
        ror = self._risk_of_ruin(win_rate, avg_win, avg_loss, total_trades)
        
        monthly_pnl = {}
        for t in trade_list:
            m = t["entry_date"][:7]
            monthly_pnl[m] = monthly_pnl.get(m, 0) + t["pnl"]
        
        return {
            "success": True,
            "mode": "historical_replay",
            "metrics": {
                "initial_capital": self.initial_capital,
                "final_capital": round(self.capital, 2),
                "total_return": round(total_return, 2),
                "total_return_pct": round(total_return_pct, 4),
                "win_rate": round(win_rate, 2),
                "loss_rate": round(loss_rate, 2),
                "max_drawdown": round(max_dd, 4),
                "profit_factor": round(profit_factor, 4),
                "sharpe_ratio": round(sharpe, 4),
                "sortino_ratio": round(sortino, 4),
                "calmar_ratio": round(calmar, 4),
                "kelly_criterion": round(kelly, 4),
                "ulcer_index": round(ulcer, 4),
                "omega_ratio": round(omega, 4),
                "risk_of_ruin": round(ror, 4),
                "total_trades": total_trades,
                "winning_trades": wins,
                "losing_trades": total_trades - wins,
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "avg_profit_per_trade": round(total_return / total_trades, 2) if total_trades > 0 else 0,
                "net_pnl": round(total_return, 2),
                "max_win": round(max(win_amounts) if win_amounts else 0, 2),
                "max_loss": round(max(loss_amounts) if loss_amounts else 0, 2),
                "max_dd_duration": 0,
                "max_trades_in_dd": 0,
                "return_maxdd": round(total_return_pct / max(max_dd, 0.01), 2) if max_dd > 0 else 0,
                "reward_risk": round(avg_win / max(avg_loss, 0.01), 2) if avg_loss > 0 else 0,
                "expectancy": round(win_rate/100 * avg_win - (1-win_rate/100)*avg_loss, 2) if total_trades>0 else 0,
                "max_win_streak": 0,
                "max_loss_streak": 0,
                "total_brokerage": round(total_brokerage, 2),
                "equity_curve": self.equity_curve,
                "trade_list": trade_list,
                "monthly_pnl": monthly_pnl,
                "source": "ratrade.historical.replay",
            },
        }
    
    def _max_drawdown(self, equity):
        if not equity:
            return 0
        peak = equity[0]
        max_dd = 0
        for v in equity:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        return max_dd * 100
    
    def _sharpe(self, equity):
        if len(equity) < 2:
            return 0
        returns = [(equity[i] - equity[i - 1]) / max(equity[i - 1], 1) for i in range(1, len(equity))]
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / max(len(returns) - 1, 1)
        std = math.sqrt(var)
        return (mean / std) * math.sqrt(252) if std > 0 else 0
    
    def _sortino(self, equity):
        if len(equity) < 2:
            return 0
        returns = [(equity[i] - equity[i - 1]) / max(equity[i - 1], 1) for i in range(1, len(equity))]
        mean = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if not downside:
            return 999 if mean > 0 else 0
        downside_var = sum(r ** 2 for r in downside) / len(downside)
        return (mean / math.sqrt(downside_var)) * math.sqrt(252) if downside_var > 0 else 0
    
    def _calmar(self, ret_pct, max_dd):
        return ret_pct / max_dd if max_dd > 0 else 0
    
    def _kelly(self, win_rate, avg_win, avg_loss):
        if avg_loss <= 0 or win_rate <= 0:
            return 0
        p = win_rate / 100.0
        b = avg_win / avg_loss
        return max(0, min((p * b - (1-p)) / b, 1))
    
    def _ulcer_index(self, equity):
        if len(equity) < 2:
            return 0
        peak = equity[0]
        sq_dd = []
        for v in equity:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0
            sq_dd.append(dd ** 2)
        return math.sqrt(sum(sq_dd) / len(sq_dd)) * 100
    
    def _omega_ratio(self, trade_list, threshold=0):
        if not trade_list:
            return 0
        gains = sum(t["pnl"] for t in trade_list if t["pnl"] > threshold)
        losses = sum(abs(t["pnl"]) for t in trade_list if t["pnl"] < threshold)
        return gains / losses if losses > 0 else 999
    
    def _risk_of_ruin(self, win_rate, avg_win, avg_loss, total_trades):
        if total_trades < 10 or avg_loss <= 0:
            return 0
        p = win_rate / 100.0
        if p <= 0:
            return 1
        edge = (p * avg_win - (1-p) * avg_loss) / avg_loss
        if edge <= 0:
            return 1
        return min(1, ((1 - edge) / (1 + edge)) ** 10)
    
    def _reset(self):
        self.capital = self.initial_capital
        self.trades = []
        self.equity_curve = [self.initial_capital]
        self.current_bar_idx = 0
        self.pending_entry = None
        self.pending_exit = None
        self.pending_auto_buy = None
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.kill_switch_on = False
        self.last_date = ""
        self.entry_bars = []