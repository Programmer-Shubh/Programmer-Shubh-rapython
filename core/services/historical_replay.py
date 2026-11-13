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
        self._exits = []
        
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

    def _sync_exit(self, open_trade_idx):
        """Copy the engine-computed exit (pnl/costs/dates) back onto the trade.
        _close_position appends to the exits list WITHOUT mutating the entry -
        reading trade['pnl'] without this sync is always 0 (parity killer)."""
        try:
            if open_trade_idx < len(self._exits) and self.trades[open_trade_idx].get("exit_date") is None:
                ex = self._exits[open_trade_idx]
                self.trades[open_trade_idx]["exit_date"] = ex.get("date", "")
                self.trades[open_trade_idx]["exit_price"] = ex.get("price", 0)
                self.trades[open_trade_idx]["exit_time"] = ex.get("time", "")
                self.trades[open_trade_idx]["pnl"] = ex.get("pnl", 0)
                self.trades[open_trade_idx]["exit_costs"] = ex.get("exit_costs", {})
                self.trades[open_trade_idx]["exit_reason"] = ex.get("reason", "")
                return True
        except Exception:
            pass
        return False
    
    def run_replay(self) -> Dict:
        """Run the historical replay - executes trades bar by bar.
        Parity contract: identical bars + config as BacktestEngine.run MUST
        yield identical trades/net/wins (covered by test_bt_paper_parity).
        Engine state is mirrored here (symbol/IV/expiry-hint/session clocks)
        because _enter/_exit/_check price off it - a missing mirror silently
        reprices everything (e.g. IV 0.25 default vs NIFTY 0.13)."""
        self._reset()
        # Mirror BacktestEngine.run() setup (same fields, same values)
        try:
            self.bt_engine._reset()
            self.bt_engine.premium_cache = {}
            self.bt_engine.bt_symbol = self.symbol
            self.bt_engine.bt_expiry = ""
            self.bt_engine.implied_volatility = self.advanced_options.get("implied_volatility") or None
            self.bt_engine.is_live = False
            self.bt_engine._cur_close = 0.0
            # _skip_db untouched (default False = DB premiums when present,
            # else model). Callers forcing model-only (compare endpoint,
            # synthetic tests) set bt_engine._skip_db=True themselves.
            self.bt_engine._expiry_hint = self.advanced_options.get("expiry", "weekly")
            if self.legs and any(l.get("expiry") == "monthly" for l in self.legs):
                self.bt_engine._expiry_hint = "monthly"
            self.bt_engine._entry_time = self.advanced_options.get("entry_time", "09:35")
            self.bt_engine._exit_time = self.advanced_options.get("exit_time", "15:14")
            self.bt_engine._early_exit_time = self.advanced_options.get("early_exit_time", "15:10")
            # v6 parity mirrors: advanced (band scale / lock), slippage,
            # latency - replay aur backtest EXACT same fill math karte hain.
            self.bt_engine._advanced = self.advanced_options or {}
            try:
                self.bt_engine._slippage_pct = float(self.advanced_options.get(
                    "slippage_pct", self.advanced_options.get("slippage", 0.5)) or 0.5)
            except Exception:
                self.bt_engine._slippage_pct = 0.5
            try:
                self.bt_engine._latency_pct = float(self.advanced_options.get(
                    "latency_slippage_pct", 0.05) or 0)
            except Exception:
                self.bt_engine._latency_pct = 0.05
            _tm = str(self.advanced_options.get("trade_mode", self.advanced_options.get("trade_type", "positional")) or "positional").lower()
            self.bt_engine._eq_intraday = (_tm == "intraday")
        except Exception:
            pass
        self._exits = []
        
        # Extract config
        leg = self.legs[0] if self.legs else {"option_type": "CE", "lots": 1, "transaction": "buy"}
        option_type = leg.get("option_type", "CE")
        qty = int(leg.get("lots", 1)) * get_lot_size(self.symbol)
        txn_type = leg.get("transaction", "buy").lower()
        # Strike selection: LEG first (engine v6 = paper parity), advanced
        # fallback. Pehle replay 'otm/2' default use karta tha jabki engine
        # leg 'atm/0' follow karta tha - dono alag strike par trade karte the.
        _leg0 = leg if isinstance(leg, dict) else {}
        strike_sel = str((_leg0.get("strike_selection")
                          or self.advanced_options.get("strike_selection")
                          or "atm")).lower()
        if strike_sel not in ("atm", "otm", "itm", "delta"):
            strike_sel = "atm"
        delta_target = self.advanced_options.get("delta_target")
        try:
            otm_dist = int(_leg0.get("otm_distance",
                                     self.advanced_options.get("otm_distance", 0)) or 0)
        except Exception:
            otm_dist = 0
        trade_mode = str(self.advanced_options.get("trade_mode", self.advanced_options.get("trade_type", "positional")) or "positional").lower()
        if trade_mode not in ("intraday", "positional", "btst"):
            trade_mode = "positional"
        # Holding cap: explicit advanced.max_holding_bars only (parity with the
        # engine - intraday has no artificial 5-bar time exit; EOD square-off
        # closes the trade, paper holds until SL/TP/EOD too).
        try:
            max_holding = int(self.advanced_options.get("max_holding_bars", 0) or 0)
        except Exception:
            max_holding = 0
        if max_holding <= 0:
            if trade_mode == "intraday":
                max_holding = 10 ** 9
            elif trade_mode == "btst":
                max_holding = 5
            else:
                max_holding = 20
        try:
            _mt_raw = self.risk_management.get("max_trades_per_day", -1)
            max_trades_day = -1 if _mt_raw in (None, "") else int(_mt_raw)
        except Exception:
            max_trades_day = -1
        _unlimited_trades = max_trades_day < 0
        daily_loss_limit = float(self.risk_management.get("daily_loss_limit", 0) or 0)
        # Per-trade SL/TP rupees YA premium ka percent ('2%' -> *_pct keys).
        # Engine ka SAME resolver (replay == backtest contract).
        leg_sl, leg_tp, sl_unit, tp_unit = self.bt_engine._resolve_sltp(
            leg, self.risk_management or {})
        # Parity: strategy_* only (NO daily_* fallback) - engine na bhi clear
        # kiya, motel MTM kill switch double-enforce ho raha tha.
        strategy_sl = float(self.risk_management.get("strategy_stop_loss", 0) or 0)
        strategy_tp = float(self.risk_management.get("strategy_take_profit", 0) or 0)
        # Strategy MTM percent mode: initial capital ka % -> Rs threshold.
        try:
            _rsp = float(self.risk_management.get("strategy_stop_loss_pct", 0) or 0)
            if _rsp > 0 and _rsp <= 100 and self.initial_capital > 0:
                strategy_sl = self.initial_capital * _rsp / 100.0
        except Exception:
            pass
        try:
            _rtp = float(self.risk_management.get("strategy_take_profit_pct", 0) or 0)
            if _rtp > 0 and _rtp <= 100 and self.initial_capital > 0:
                strategy_tp = self.initial_capital * _rtp / 100.0
        except Exception:
            pass
        # Engine ke unit attrs set karo - replay ka _check_sl_tp engine par
        # hi chalta hai (parity: same resolver, same units).
        self.bt_engine._sl_unit = sl_unit
        self.bt_engine._tp_unit = tp_unit
        
        is_spread = len(self.legs) > 1
        entry_time = self.advanced_options.get("entry_time", "09:35")
        exit_time = self.advanced_options.get("exit_time", "15:14")
        
        # Engine parity: same warmup gate (pehle 15 tha, engine 5 hai -
        # mismatch par replay kam bars evaluate karta tha = alag trades).
        min_bars = min(5, max(1, len(self.historical_data) - 2))
        
        for i in range(min_bars, len(self.historical_data)):
            cur = self.historical_data[i]
            try:
                self.bt_engine._cur_bar_time = str(cur.get("bar_time") or "")
            except Exception:
                self.bt_engine._cur_bar_time = ""
            cur_date = cur["trade_date"]
            nxt = self.historical_data[i + 1] if i + 1 < len(self.historical_data) else None
            is_last = nxt is None or nxt["trade_date"] != cur_date
            # Roll option expiry forward (same as backtest engine).
            # Positional holds are NOT rolled while open into expiry:
            # squared off on expiry day instead (block below).
            _rexp = str(self.bt_engine.bt_expiry)[:10] if self.bt_engine.bt_expiry else ""
            try:
                if _rexp and _rexp < cur_date:
                    _ropen = len(self.trades) > len([t for t in self.trades if t.get("exit_date")])
                    if trade_mode != "intraday" and _ropen:
                        pass
                    else:
                        self.bt_engine.bt_expiry = ""
                        _rexp = ""
            except Exception:
                pass
            
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
            can_enter = not has_open and (_unlimited_trades or self.daily_trades < max_trades_day)
            # Engine parity: intraday entry clock window (entry_time..exit/15:00)
            if can_enter and trade_mode == "intraday" and str(getattr(self.bt_engine, "_cur_bar_time", "") or ""):
                try:
                    _rnow = str(getattr(self.bt_engine, "_cur_bar_time", ""))[:5]
                    _rh, _rm = _rnow.split(":")
                    _rnowm = int(_rh) * 60 + int(_rm)
                    _ret = str(getattr(self.bt_engine, "_entry_time", "09:35") or "09:35")[:5]
                    _reh, _rem = _ret.split(":")
                    _rstart = int(_reh) * 60 + int(_rem)
                    _rxt = str(getattr(self.bt_engine, "_exit_time", "15:14") or "15:14")[:5]
                    _rxh, _rxm = _rxt.split(":")
                    _rout = min(15 * 60, int(_rxh) * 60 + int(_rxm))
                    if _rnowm < _rstart or _rnowm >= _rout:
                        can_enter = False
                except Exception:
                    pass
            if (self.pending_entry is not None and self.pending_entry != i - 1):
                # Pending order valid for exactly one bar; dropped otherwise.
                self.pending_entry = None
                self.pending_auto_buy = None
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

                # Min-premium guard (same as backtest engine): Rs20 se saste
                # BUY aur SELL entries dono skip (spreads exempt)
                try:
                    _min_prem = float(self.advanced_options.get("min_entry_premium", 0) or 0)
                except Exception:
                    _min_prem = 0
                try:
                    _is_spr = len(self.legs or []) > 1
                except Exception:
                    _is_spr = False
                if _min_prem > 0 and not _is_spr and float(trade.get("price", 0) or 0) < _min_prem:
                    self.pending_entry = None
                    self.pending_auto_buy = None
                    continue

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
                    "SELL" if txn_type == "buy" else "BUY", False,
                    slippage_pct=float(getattr(self.bt_engine, "_slippage_pct", 0.5) or 0.5))
                self.bt_engine._close_position(self.trades, self._exits, entry, exit_prem,
                    self.pending_exit, cur_date, qty, txn_type)
                # Engine-computed exit wapas trade par (bina sync pnl hamesha 0)
                self._sync_exit(open_trade_idx)
                self.daily_pnl += self.trades[open_trade_idx].get("pnl", 0)
                if (daily_loss_limit > 0 and self.daily_pnl <= -daily_loss_limit) or (strategy_sl > 0 and self.daily_pnl <= -strategy_sl) or (strategy_tp > 0 and self.daily_pnl >= strategy_tp):
                    self.kill_switch_on = True
                self.pending_exit = None
            
            # Check SL/TP on every bar first (daily bars are all day-close;
            # skipping SL on is_last meant intraday SL never triggered).
            has_open = len(self.trades) > len([t for t in self.trades if t.get("exit_date")])
            if has_open:
                open_trade_idx = len([t for t in self.trades if t.get("exit_date")])
                entry = self.trades[open_trade_idx]
                if not entry.get("is_spread") and (leg_sl > 0 or leg_tp > 0):
                    hit = self.bt_engine._check_sl_tp(cur, entry, option_type, txn_type, leg_sl, leg_tp, qty)
                    if hit:
                        from core.services.transaction_costs import TransactionCosts as _TC
                        exit_prem = _TC.apply_fill_slippage(hit.get("fill", hit["level"]), "SELL" if txn_type == "buy" else "BUY", False,
                                                            slippage_pct=float(getattr(self.bt_engine, "_slippage_pct", 0.5) or 0.5))
                        self.bt_engine._close_position(self.trades, self._exits, entry, exit_prem,
                            hit["reason"], cur_date, qty, txn_type)
                        # SIRF hit par sync+pnl (bina hit force-close BUG tha -
                        # har open trade pehli SL-check bar me mar jata tha)
                        self._sync_exit(open_trade_idx)
                        self.daily_pnl += self.trades[open_trade_idx].get("pnl", 0)
                        if (daily_loss_limit > 0 and self.daily_pnl <= -daily_loss_limit) or (strategy_sl > 0 and self.daily_pnl <= -strategy_sl) or (strategy_tp > 0 and self.daily_pnl >= strategy_tp):
                            self.kill_switch_on = True
            
            # Strategy-wise MTM SL/TP — unrealized MTM of ALL open positions +
            # realized daily_pnl. Limit hit => square-off everything at once.
            has_open = len(self.trades) > len([t for t in self.trades if t.get("exit_date")])
            if has_open and (strategy_sl > 0 or strategy_tp > 0):
                try:
                    _closed = len([t for t in self.trades if t.get("exit_date")])
                    open_unreal = sum(self.bt_engine._open_unrealized(self.trades[j], cur, option_type, txn_type, qty) for j in range(_closed, len(self.trades)))
                except Exception:
                    open_unreal = 0.0
                total_mtm = self.daily_pnl + open_unreal
                if (strategy_sl > 0 and total_mtm <= -strategy_sl) or (strategy_tp > 0 and total_mtm >= strategy_tp):
                    while len(self.trades) > len([t for t in self.trades if t.get("exit_date")]):
                        open_trade_idx = len([t for t in self.trades if t.get("exit_date")])
                        entry = self.trades[open_trade_idx]
                        if entry.get("is_spread"):
                            self.bt_engine._close_spread(self.trades, self._exits, entry, "strategy_sl_tp", cur_date)
                        else:
                            exit_prem = self.bt_engine._close_premium(cur_date, float(cur["close_price"]), float(entry["strike"]), option_type)
                            exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, "SELL" if txn_type == "buy" else "BUY", False, slippage_pct=float(getattr(self.bt_engine, "_slippage_pct", 0.5) or 0.5))
                            self.bt_engine._close_position(self.trades, self._exits, entry, exit_prem, "strategy_sl_tp", cur_date, qty, txn_type)
                        self._sync_exit(open_trade_idx)
                        self.daily_pnl += self.trades[open_trade_idx].get("pnl", 0)
                    self.kill_switch_on = True
            
            # Expiry-day square-off
            has_open = len(self.trades) > len([t for t in self.trades if t.get("exit_date")])
            if has_open and trade_mode != "intraday" and _rexp and cur_date >= _rexp and is_last:
                open_trade_idx = len([t for t in self.trades if t.get("exit_date")])
                entry = self.trades[open_trade_idx]
                if entry.get("is_spread"):
                    self.bt_engine._close_spread(self.trades, self._exits, entry, "expiry_squareoff", cur_date)
                else:
                    exit_prem = self.bt_engine._close_premium(cur_date, float(cur["close_price"]),
                        float(entry["strike"]), option_type)
                    exit_prem = TransactionCosts.apply_fill_slippage(exit_prem,
                        "SELL" if txn_type == "buy" else "BUY", False,
                        slippage_pct=float(getattr(self.bt_engine, "_slippage_pct", 0.5) or 0.5))
                    self.bt_engine._close_position(self.trades, self._exits, entry, exit_prem,
                        "expiry_squareoff", cur_date, qty, txn_type)
                self._sync_exit(open_trade_idx)
                self.daily_pnl += self.trades[open_trade_idx].get("pnl", 0)
                if (daily_loss_limit > 0 and self.daily_pnl <= -daily_loss_limit) or (strategy_sl > 0 and self.daily_pnl <= -strategy_sl) or (strategy_tp > 0 and self.daily_pnl >= strategy_tp):
                    self.kill_switch_on = True
                self.bt_engine.bt_expiry = ""
                _rexp = ""
            
            # Intraday forced exit
            has_open = len(self.trades) > len([t for t in self.trades if t.get("exit_date")])
            if has_open and trade_mode == "intraday" and is_last:
                open_trade_idx = len([t for t in self.trades if t.get("exit_date")])
                entry = self.trades[open_trade_idx]
                if entry.get("is_spread"):
                    self.bt_engine._close_spread(self.trades, self._exits, entry, "intraday", cur_date)
                else:
                    exit_prem = self.bt_engine._close_premium(cur_date, float(cur["close_price"]), 
                        float(entry["strike"]), option_type)
                    exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, 
                        "SELL" if txn_type == "buy" else "BUY", False,
                        slippage_pct=float(getattr(self.bt_engine, "_slippage_pct", 0.5) or 0.5))
                    self.bt_engine._close_position(self.trades, self._exits, entry, exit_prem, 
                        "intraday", cur_date, qty, txn_type)
                self._sync_exit(open_trade_idx)
                self.daily_pnl += self.trades[open_trade_idx].get("pnl", 0)
                if (daily_loss_limit > 0 and self.daily_pnl <= -daily_loss_limit) or (strategy_sl > 0 and self.daily_pnl <= -strategy_sl) or (strategy_tp > 0 and self.daily_pnl >= strategy_tp):
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
                    # Parity: explicit exit_conditions par hi signal-exit
                    # (paper SL/TP/EOD tak hold karta hai).
                    _r_exit_on_signal = bool(self.exit_conditions)
                    if option_type == "PE":
                        entry_sig = sell_sig if txn_type == "buy" else buy_sig
                        exit_sig = (buy_sig if txn_type == "buy" else sell_sig) if _r_exit_on_signal else False
                    else:
                        entry_sig = buy_sig if txn_type == "buy" else sell_sig
                        exit_sig = (sell_sig if txn_type == "buy" else buy_sig) if _r_exit_on_signal else False
                
                has_open = len(self.trades) > len([t for t in self.trades if t.get("exit_date")])
                can_enter = not has_open and (_unlimited_trades or self.daily_trades < max_trades_day)
                # Engine parity: intraday entry clock window at SIGNAL time too
                if can_enter and trade_mode == "intraday" and str(getattr(self.bt_engine, "_cur_bar_time", "") or ""):
                    try:
                        _snow = str(getattr(self.bt_engine, "_cur_bar_time", ""))[:5]
                        _sh, _sm = _snow.split(":")
                        _snowm = int(_sh) * 60 + int(_sm)
                        _set = str(getattr(self.bt_engine, "_entry_time", "09:35") or "09:35")[:5]
                        _seh, _sem = _set.split(":")
                        _sstart = int(_seh) * 60 + int(_sem)
                        _sxt = str(getattr(self.bt_engine, "_exit_time", "15:14") or "15:14")[:5]
                        _sxh, _sxm = _sxt.split(":")
                        _sout = min(15 * 60, int(_sxh) * 60 + int(_sxm))
                        if _snowm < _sstart or _snowm >= _sout:
                            can_enter = False
                    except Exception:
                        pass
                
                if entry_sig and can_enter:
                    self.pending_entry = i
                    if auto_signal:
                        self.pending_auto_buy = buy_sig
                
                if has_open and self.pending_exit is None and (exit_sig or time_exit):
                    self.pending_exit = "condition" if exit_sig else "time"
            # NOTE: no per-bar _square_off_expiry call (engine me bhi last-bar
            # only hai) - end-of-period close neeche sab band karta hai.
            self.equity_curve.append(self.capital)
        
        # Close any remaining open positions at end
        while len([t for t in self.trades if t.get("exit_date")]) < len(self.trades):
            idx = len([t for t in self.trades if t.get("exit_date")])
            entry = self.trades[idx]
            last = self.historical_data[-1]
            if entry.get("is_spread"):
                self.bt_engine._close_spread(self.trades, self._exits, entry, "end_of_period", last["trade_date"])
            else:
                exit_prem = self.bt_engine._close_premium(last["trade_date"], float(last["close_price"]), 
                    float(entry["strike"]), option_type)
                self.bt_engine._close_position(self.trades, self._exits, entry, exit_prem, "end_of_period", 
                    last["trade_date"], qty, txn_type)
            self._sync_exit(idx)
        
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
                # Engine entry dicts carry the date under "date" (not
                # "entry_date") - map it, else rows look dateless.
                _ed = trade.get("entry_date") or trade.get("date", "")
                # Structured diagnostics parity with backtest rows.
                try:
                    _gq = int(trade.get("quantity", 0) or 0)
                    _ge = float(trade.get("price", 0) or 0)
                    _gx = float(trade.get("exit_price", 0) or 0)
                    _tt = str(trade.get("type", "buy") or "buy")
                    _gross = ((_gx - _ge) if _tt == "buy" else (_ge - _gx)) * _gq
                    _chg = float((trade.get("costs") or {}).get("total", 0) or 0) + float((trade.get("exit_costs") or {}).get("total", 0) or 0)
                    _slip = _gross - float(pnl or 0)
                except Exception:
                    _gross, _slip, _chg = 0.0, 0.0, 0.0

                if trade.get("is_spread"):
                    legs_str = " + ".join(
                        f"{'Sell' if l['type']=='sell' else 'Buy'} {l['option_type']} {l['strike']}"
                        for l in trade.get("legs", [])
                    )
                    trade_list.append({
                        "index": total_trades,
                        "symbol": self.symbol,
                        "entry_date": _ed,
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
                        "gross_pnl": round(_gross, 2),
                        "slippage": round(_slip, 2),
                        "charges": round(_chg, 2),
                        "pnl": pnl,
                        "pnl_formatted": f"₹{pnl:,.2f}",
                        "exit_reason": trade.get("exit_reason", ""),
                        "reason": trade.get("exit_reason", ""),
                        "is_spread": True,
                    })
                else:
                    leg_info = trade.get("legs", [{}])[0] if trade.get("legs") else {}
                    trade_list.append({
                        "index": total_trades,
                        "symbol": self.symbol,
                        "entry_date": _ed,
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
                        "gross_pnl": round(_gross, 2),
                        "slippage": round(_slip, 2),
                        "charges": round(_chg, 2),
                        "pnl": pnl,
                        "pnl_formatted": f"₹{pnl:,.2f}",
                        "exit_reason": trade.get("exit_reason", ""),
                        "reason": trade.get("exit_reason", ""),
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
        self._exits = []