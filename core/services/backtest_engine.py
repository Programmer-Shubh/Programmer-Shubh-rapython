import math
import time
from typing import List, Dict
from core.services.indicator_engine import IndicatorEngine
from core.services.transaction_costs import TransactionCosts
from core.models.database import Database
from utils.helpers import get_strike_step, get_lot_size, black_scholes


class BacktestEngine:
    def __init__(self, is_live: bool = False):
        self.indicators = IndicatorEngine()
        self.initial_capital = 1000000.0
        self.ohlc_cache = {}
        self.premium_cache = {}
        self.bt_symbol = ""
        self.bt_expiry = ""
        self.implied_volatility = 0.14
        self.is_live = is_live  # Unified switch: False=backtest, True=live/paper

    def run(self, historical, symbol, start_date, end_date, ind_list, entry_conditions,
            exit_conditions, legs, advanced_options, risk_management, is_live: bool = None) -> dict:
        if is_live is not None:
            self.is_live = is_live
        self._reset()
        self.premium_cache = {}
        self.bt_symbol = symbol
        self.bt_expiry = legs[0].get("expiry_date", "") if legs else ""
        self.implied_volatility = advanced_options.get("implied_volatility", 0.14)
        # Quant wiring: expiry type, trailing, momentum, entry/exit times
        self._expiry_hint = advanced_options.get("expiry", "weekly")
        # Check legs for expiry override
        if legs and any(l.get("expiry")=="monthly" for l in legs):
            self._expiry_hint = "monthly"
        use_trailing = advanced_options.get("trailing_sl", False) or advanced_options.get("trailing", "") != "Lock"
        momentum = int(advanced_options.get("momentum", 0) or 0)
        entry_time = advanced_options.get("entry_time", "09:35")
        exit_time = advanced_options.get("exit_time", "15:14")
        self._entry_time = entry_time
        self._exit_time = exit_time
        trade_mode = advanced_options.get("trade_mode", "positional")
        max_holding = int(advanced_options.get("max_holding_bars", 20))
        # Intraday: hold max 5 bars for more trades like Quantman (positional holds longer)
        if trade_mode == "intraday":
            max_holding = min(max_holding, 5)
        # BTST holds overnight: increase max_holding for BTST
        if trade_mode == "btst":
            max_holding = max(max_holding, 5)
        max_trades_day = int(risk_management.get("max_trades_per_day", 5))
        daily_loss_limit = float(risk_management.get("daily_loss_limit", 0) or 0)
        signal_delay = int(advanced_options.get("signal_delay_bars", 0) or 0)
        # Latency in seconds (only in backtest mode) - unified is_live switch
        latency = TransactionCosts.latency_delay(self.is_live)
        leg = legs[0] if legs else {"option_type": "CE", "lots": 1, "transaction": "buy"}
        option_type = leg.get("option_type", "CE")
        qty = int(leg.get("lots", 1)) * get_lot_size(symbol)
        txn_type = leg.get("transaction", "buy").lower()
        leg_sl = float(leg.get("stop_loss", risk_management.get("daily_stop_loss", 500)))
        leg_tp = float(leg.get("take_profit", risk_management.get("daily_take_profit", 1000)))
        strike_sel = advanced_options.get("strike_selection", "otm")
        delta_target = advanced_options.get("delta_target")
        otm_dist = int(advanced_options.get("otm_distance", 2))
        trade_mode = advanced_options.get("trade_mode", "positional")

        closes = [h["close_price"] for h in historical]
        highs = [h["high_price"] for h in historical]
        lows = [h["low_price"] for h in historical]
        pre_calc = self._pre_calc(historical, closes, highs, lows, ind_list)
        # Store indicator modes for bullish/bearish filtering
        self.ind_modes = {}
        for ind in ind_list:
            iid = ind.get("id","") if isinstance(ind, dict) else ind
            mode = ind.get("params",{}).get("mode","both") if isinstance(ind, dict) else "both"
            self.ind_modes[iid] = str(mode).lower()

        entries, exits, entry_bars = [], [], []
        pending_entry = None
        pending_exit = None
        pending_entry_signal = None
        pending_auto_buy = None
        daily_trades = 0
        daily_pnl = 0.0
        kill_switch_on = False
        last_date = ""
        min_bars = min(15, max(1, len(historical) - 2))
        legs = legs if legs else [{"option_type": "CE", "lots": 1, "transaction": "buy"}]
        is_spread = len(legs) > 1

        for i in range(min_bars, len(historical)):
            cur = historical[i]
            cur_date = cur["trade_date"]
            nxt = historical[i + 1] if i + 1 < len(historical) else None
            is_last = nxt is None or nxt["trade_date"] != cur_date

            if cur_date != last_date:
                daily_trades = 0
                daily_pnl = 0.0
                kill_switch_on = False
                last_date = cur_date

            if kill_switch_on:
                continue

            has_open = len(entries) > len(exits)
            can_enter = not has_open and daily_trades < max_trades_day

            # Execute pending entry from PREVIOUS bar's signal (no look-ahead)
            if pending_entry is not None and can_enter:
                # Execution happens at NEXT candle's OPEN (no look-ahead bias)
                exec_idx = i
                exec_bar = historical[exec_idx] if exec_idx < len(historical) else cur
                spot = float(exec_bar.get("open_price", 0) or exec_bar.get("close_price", 0))
                auto_signal_exec = bool(advanced_options.get("auto_signal") or (legs and legs[0].get("transaction","").lower()=="auto"))
                if auto_signal_exec:
                    # Choose CE Buy on bullish (buy_sig) else PE Buy
                    is_buy = pending_auto_buy if pending_auto_buy is not None else True
                    auto_leg = {"option_type": "CE" if is_buy else "PE", "transaction": "buy", "lots": int(legs[0].get("lots",1) if legs else 1), "strike_selection": strike_sel, "otm_distance": otm_dist}
                    trade = self._enter_single(cur_date, spot, symbol, auto_leg, strike_sel, delta_target, otm_dist)
                    # Preserve auto choice for display
                    trade["auto_choice"] = "CE-BUY" if is_buy else "PE-BUY"
                elif is_spread:
                    trade = self._enter_spread(cur_date, spot, symbol, legs, strike_sel, delta_target, otm_dist)
                else:
                    trade = self._enter_single(cur_date, spot, symbol, legs[0], strike_sel, delta_target, otm_dist)
                entries.append(trade)
                entry_bars.append(i)
                daily_trades += 1
                pending_entry = None
                pending_auto_buy = None

            has_open = len(entries) > len(exits)
            if has_open and pending_exit is not None:
                entry = entries[len(exits)]
                exec_idx = i
                exec_bar = historical[exec_idx] if exec_idx < len(historical) else cur
                spot = float(exec_bar.get("open_price", 0) or exec_bar.get("close_price", 0))
                exit_prem = self._exit_premium(cur_date, spot, float(entry["strike"]), option_type)
                # Apply transaction costs with is_live flag
                exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, "SELL" if txn_type == "buy" else "BUY", self.is_live)
                self._close_position(entries, exits, entry, exit_prem, pending_exit, cur_date, qty, txn_type)
                daily_pnl += exits[-1]["pnl"]
                if daily_loss_limit > 0 and daily_pnl <= -daily_loss_limit:
                    kill_switch_on = True
                pending_exit = None

            has_open = len(entries) > len(exits)
            if has_open and not (trade_mode == "intraday" and is_last):
                entry = entries[len(exits)]
                if not entry.get("is_spread") and (leg_sl > 0 or leg_tp > 0):
                    hit = self._check_sl_tp(cur, entry, option_type, txn_type, leg_sl, leg_tp)
                    if hit:
                        self._close_position(entries, exits, entry, hit["level"], hit["reason"], cur_date, qty, txn_type)
                        daily_pnl += exits[-1]["pnl"]
                        if daily_loss_limit > 0 and daily_pnl <= -daily_loss_limit:
                            kill_switch_on = True

            has_open = len(entries) > len(exits)
            if has_open and trade_mode == "intraday" and is_last:
                entry = entries[len(exits)]
                if entry.get("is_spread"):
                    self._close_spread(entries, exits, entry, "intraday", cur_date)
                else:
                    exit_prem = self._close_premium(cur_date, float(cur["close_price"]), float(entry["strike"]), option_type)
                    exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, "SELL" if txn_type == "buy" else "BUY", self.is_live)
                    self._close_position(entries, exits, entry, exit_prem, "intraday", cur_date, qty, txn_type)
                daily_pnl += exits[-1]["pnl"]
                if daily_loss_limit > 0 and daily_pnl <= -daily_loss_limit:
                    kill_switch_on = True

            if nxt is not None:
                bars_held = 0
                if len(entries) > len(exits):
                    eidx = len(exits)
                    bars_held = i - (entry_bars[eidx] if eidx < len(entry_bars) else i)
                time_exit = bars_held >= max_holding
                # Generate signals using CLOSED bar (i-1) - NO LOOK-AHEAD
                # Signal generated at bar i-1, execution at bar i
                buy_sig = self._get_buy_signal(i, pre_calc, historical, entry_conditions)
                sell_sig = self._get_sell_signal(i, pre_calc, historical, exit_conditions)
                # For spreads (multi-leg like Bear Call Spread), allow entry on either signal to avoid 0 trades when single indicator rare
                # Auto-signal mode: if advanced auto_signal, pick CE on buy_sig, PE on sell_sig
                auto_signal = bool(advanced_options.get("auto_signal") or (legs and legs[0].get("transaction","").lower()=="auto"))
                if auto_signal:
                    # For AUTO, entry on any signal; leg type chosen dynamically at execution
                    entry_sig = buy_sig or sell_sig
                    if not entry_sig and not pre_calc:
                        entry_sig = True
                    exit_sig = time_exit
                elif is_spread:
                    entry_sig = buy_sig or sell_sig
                    if not entry_sig and not pre_calc:
                        entry_sig = True
                    exit_sig = time_exit
                else:
                    # Direction-aware: CE Buy = bullish (buy_sig), PE Buy = bearish (sell_sig)
                    # CE Sell = bearish, PE Sell = bullish
                    if option_type == "PE":
                        entry_sig = sell_sig if txn_type == "buy" else buy_sig
                        exit_sig = buy_sig if txn_type == "buy" else sell_sig
                    else:
                        entry_sig = buy_sig if txn_type == "buy" else sell_sig
                        exit_sig = sell_sig if txn_type == "buy" else buy_sig
                has_open = len(entries) > len(exits)
                can_enter = not has_open and daily_trades < max_trades_day
                if entry_sig and can_enter:
                    # Apply latency: in backtest mode, wait enough bars for latency to elapse
                    # In live mode, execute immediately
                    # For spreads/auto, execute immediately to ensure not stuck on 1-bar delay
                    if self.is_live or is_spread or auto_signal:
                        pending_entry = i
                        pending_entry_signal = None
                        if auto_signal:
                            pending_auto_buy = buy_sig
                    else:
                        # Backtest mode: wait for latency bars
                        if pending_entry_signal is None:
                            pending_entry_signal = i
                            if auto_signal:
                                pending_auto_buy = buy_sig
                        elif i - pending_entry_signal >= max(1, latency):
                            pending_entry = i
                            pending_entry_signal = None
                else:
                    if not is_spread and not auto_signal:
                        pending_entry_signal = None
                    elif auto_signal and pending_entry_signal is not None and i - pending_entry_signal > 3:
                        pending_entry_signal = None
                        pending_auto_buy = None
                    elif is_spread:
                        # For spreads, keep pending signal if it was set periodic
                        if pending_entry_signal is not None and i - pending_entry_signal > 3:
                            pending_entry_signal = None
                    else:
                        pending_entry_signal = None
                # SQUARE OFF ON EXPIRY DAY - automatic square-off to avoid delivery risk
                if not self.is_live:
                    self._square_off_expiry(historical, entries, exits, symbol, option_type, txn_type, qty, risk_management)
                if has_open and pending_exit is None and (exit_sig or time_exit):
                    pending_exit = "condition" if exit_sig else "time"

        while len(exits) < len(entries):
            idx = len(exits)
            entry = entries[idx]
            last = historical[-1]
            if entry.get("is_spread"):
                self._close_spread(entries, exits, entry, "end_of_period", last["trade_date"])
            else:
                exit_prem = self._close_premium(last["trade_date"], float(last["close_price"]), float(entry["strike"]), option_type)
                self._close_position(entries, exits, entry, exit_prem, "end_of_period", last["trade_date"], qty, txn_type)

        return self._build_result(symbol, start_date, end_date, entries, exits)

    def _pre_calc(self, historical, closes, highs, lows, ind_list):
        result = {}
        for ind in ind_list:
            iid = ind.get("id", "") if isinstance(ind, dict) else ind
            params = ind.get("params", {}) if isinstance(ind, dict) else {}
            if iid == "supertrend":
                result["supertrend"] = self.indicators.calculate_supertrend(historical, params.get("period", 10), float(params.get("multiplier", 3)))
            elif iid == "macd":
                result["macd"] = self.indicators.calculate_macd(closes, params.get("fast", 12), params.get("slow", 26), params.get("signal", 9))
            elif iid == "rsi":
                result["rsi"] = self.indicators.calculate_rsi(closes, params.get("period", 14))
            elif iid == "ema":
                result["ema"] = self.indicators.calculate_ema(closes, params.get("period", 50))
            elif iid == "predicted_moving_average":
                result["pma"] = self.indicators.calculate_predicted_ma(closes, params.get("lookback", 20))
            elif iid == "predicted_neural_index":
                result["pni"] = self.indicators.calculate_predicted_ma(closes, params.get("lookback", 14))
            elif iid == "neural_network":
                result["neural"] = self.indicators.calculate_neural_network(closes, highs, lows, params.get("period", 14))
            elif iid == "volume_indicator":
                result["volume"] = self.indicators.calculate_volume_indicator(historical, params.get("period", 20))
            elif iid == "open_interest":
                result["oi"] = self.indicators.calculate_oi_indicator(historical, params.get("period", 20))
            elif iid == "kama":
                result["kama"] = self.indicators.calculate_kama(closes, params.get("fast_period", 10), params.get("slow_period", 30))
            elif iid == "hmm_regime":
                result["hmm_regime"] = self.indicators.calculate_hmm_regime(closes, params.get("n_components", 3))
            elif iid == "dynamic_bollinger":
                result["dynamic_boll"] = self.indicators.calculate_dynamic_bollinger(closes, highs, lows, params.get("period", 20), params.get("lookforward", 5))
            elif iid == "ml_rsi":
                result["ml_rsi"] = self.indicators.calculate_ml_rsi(closes, params.get("period", 14))
            elif iid == "ml_signal_filter":
                result["ml_signal"] = self.indicators.calculate_ml_signal_filter(closes, params.get("fast_ema", 9), params.get("slow_ema", 21), params.get("rsi_period", 14), params.get("ml_weight", 0.3))
            elif iid == "vwap":
                try:
                    result["vwap"] = self.indicators.calculate_vwap(historical, params.get("period", 20), float(params.get("multiplier", 2.0)))
                except Exception:
                    result["vwap"] = {}
        return result

    def _get_indicator_value(self, indicator, i, close, historical, pre_calc):
        val = close
        if indicator == "open":
            val = float(historical[i].get("open_price", close))
        elif indicator == "high":
            val = float(historical[i].get("high_price", close))
        elif indicator == "low":
            val = float(historical[i].get("low_price", close))
        elif indicator == "predicted_moving_average" and "pma" in pre_calc:
            val = pre_calc["pma"].get("pma", {}).get(i, close)
        elif indicator == "ai_sentiment" and "ai_sentiment" in pre_calc:
            v = pre_calc["ai_sentiment"].get("asi", {}).get(i)
            val = v["sentiment"] if v else 0
        elif indicator == "ai_volatility_range" and "ai_volatility" in pre_calc:
            r = pre_calc["ai_volatility"].get("vol_regime", {}).get(i, "normal")
            val = 80 if r == "high" else (50 if r == "normal" else 20)
        elif indicator == "ai_trend_score" and "ai_trend" in pre_calc:
            val = pre_calc["ai_trend"].get("scores", {}).get(i, 0)
        return val

    def _get_buy_signal(self, i, pre_calc, historical, entry_conditions):
        """Generate buy signal using closed bar to avoid look-ahead bias.
        Signals are based on bar i (just closed); execution occurs on next candle i+1 open.
        When user defines entry_conditions, those take priority (AND logic).
        When no custom conditions, use selected indicators (OR logic).
        """
        close = historical[i]["close_price"]
        prev_close = historical[i - 1]["close_price"] if i > 0 else close
        effective_idx = i

        # PRIORITY 1: Custom entry conditions (AND logic - all must match)
        if entry_conditions:
            cond_met = True
            for c in entry_conditions:
                op = c.get("operator", "")
                val = float(c.get("value", 0) or 0)
                ind = c.get("indicator", "close")
                if not op:
                    continue
                cur_v = self._get_indicator_value(ind, effective_idx, close, historical, pre_calc)
                prev_v = self._get_indicator_value(ind, max(0, effective_idx - 1), prev_close, historical, pre_calc)
                if op == "greater_than":
                    cond_met = cond_met and cur_v > val
                elif op == "less_than":
                    cond_met = cond_met and cur_v < val
                elif op == "crosses_above":
                    cond_met = cond_met and cur_v > val and prev_v <= val
                elif op == "crosses_below":
                    cond_met = cond_met and cur_v < val and prev_v >= val
            return cond_met

        # PRIORITY 2: Selected indicator-based signals (OR per indicator - any triggers, for 2-indicator backtest)
        # Respect bullish/bearish mode per indicator (both by default)
        modes = getattr(self, "ind_modes", {})
        if not pre_calc:
            return True
        if "supertrend" in pre_calc and effective_idx < len(pre_calc["supertrend"]):
            if modes.get("supertrend","both") != "bearish" and historical[effective_idx]["close_price"] > pre_calc["supertrend"][effective_idx]:
                return True
        if "macd" in pre_calc:
            m = pre_calc["macd"]
            mv = m["macd"][effective_idx] if effective_idx < len(m["macd"]) and m["macd"][effective_idx] is not None else 0
            sv = m["signal"][effective_idx] if effective_idx < len(m["signal"]) and m["signal"][effective_idx] is not None else 0
            pm = m["macd"][effective_idx - 1] if effective_idx > 0 and effective_idx - 1 < len(m["macd"]) and m["macd"][effective_idx - 1] is not None else 0
            ps = m["signal"][effective_idx - 1] if effective_idx > 0 and effective_idx - 1 < len(m["signal"]) and m["signal"][effective_idx - 1] is not None else 0
            if modes.get("macd","both") != "bearish" and mv > sv and pm <= ps:
                return True
        if "rsi" in pre_calc and effective_idx < len(pre_calc["rsi"]):
            if modes.get("rsi","both") != "bearish" and pre_calc["rsi"][effective_idx] < 45:
                return True
        if "ema" in pre_calc and effective_idx < len(pre_calc["ema"]) and pre_calc["ema"][effective_idx] is not None:
            if modes.get("ema","both") != "bearish" and historical[effective_idx]["close_price"] > pre_calc["ema"][effective_idx]:
                return True
        if "kama" in pre_calc and effective_idx < len(pre_calc["kama"]):
            kama_v = pre_calc["kama"][effective_idx]
            if modes.get("kama","both") != "bearish" and kama_v is not None and historical[effective_idx]["close_price"] > kama_v:
                return True
        if "hmm_regime" in pre_calc:
            seq = pre_calc["hmm_regime"].get("state_sequence", [])
            if seq and effective_idx < len(seq) and seq[effective_idx] == "Bullish":
                return True
        if "dynamic_boll" in pre_calc:
            db = pre_calc["dynamic_boll"]
            low = db.get("lower", [])
            if effective_idx < len(low) and low[effective_idx] is not None and historical[effective_idx]["close_price"] < low[effective_idx]:
                return True
        if "ml_rsi" in pre_calc:
            ml = pre_calc["ml_rsi"]
            sig = ml.get("signal", [])
            if effective_idx < len(sig) and sig[effective_idx] == 1:
                return True
        if "ml_signal" in pre_calc:
            prob = pre_calc["ml_signal"].get("probability", [])
            if effective_idx < len(prob) and prob[effective_idx] > 0.60:
                return True
        # VWAP support
        if "vwap" in pre_calc:
            vw = pre_calc["vwap"]
            vwap_vals = vw.get("vwap", []) if isinstance(vw, dict) else []
            if modes.get("vwap","both") != "bearish" and effective_idx < len(vwap_vals) and vwap_vals[effective_idx] is not None and historical[effective_idx]["close_price"] > vwap_vals[effective_idx]:
                return True
        if "neural" in pre_calc:
            sig = pre_calc["neural"].get("signal", [])
            if modes.get("neural_network","both") != "bearish" and effective_idx < len(sig) and sig[effective_idx] == 1:
                return True
        if "volume" in pre_calc:
            sig = pre_calc["volume"].get("signal", [])
            if modes.get("volume_indicator","both") != "bearish" and effective_idx < len(sig) and sig[effective_idx] == 1:
                return True
        if "oi" in pre_calc:
            sig = pre_calc["oi"].get("signal", [])
            if modes.get("open_interest","both") != "bearish" and effective_idx < len(sig) and sig[effective_idx] == 1:
                return True
        return False

    def _get_sell_signal(self, i, pre_calc, historical, exit_conditions):
        """Generate sell signal using closed bar to avoid look-ahead bias.
        When user defines exit_conditions, those take priority (AND logic).
        When no custom conditions, use selected indicators (OR logic).
        """
        close = historical[i]["close_price"]
        prev_close = historical[i - 1]["close_price"] if i > 0 else close
        effective_idx = i

        # PRIORITY 1: Custom exit conditions (AND logic)
        if exit_conditions:
            cond_met = True
            for c in exit_conditions:
                op = c.get("operator", "")
                val = float(c.get("value", 0) or 0)
                ind = c.get("indicator", "close")
                if not op:
                    continue
                cur_v = self._get_indicator_value(ind, effective_idx, close, historical, pre_calc)
                prev_v = self._get_indicator_value(ind, max(0, effective_idx - 1), prev_close, historical, pre_calc)
                if op == "greater_than":
                    cond_met = cond_met and cur_v > val
                elif op == "less_than":
                    cond_met = cond_met and cur_v < val
                elif op == "crosses_above":
                    cond_met = cond_met and cur_v > val and prev_v <= val
                elif op == "crosses_below":
                    cond_met = cond_met and cur_v < val and prev_v >= val
            return cond_met

        # PRIORITY 2: Selected indicator-based signals (OR logic) with bullish/bearish filter
        modes = getattr(self, "ind_modes", {})
        sell = False
        if "supertrend" in pre_calc and effective_idx < len(pre_calc["supertrend"]):
            if modes.get("supertrend","both") != "bullish":
                sell = sell or (historical[effective_idx]["close_price"] < pre_calc["supertrend"][effective_idx])
        if "rsi" in pre_calc and effective_idx < len(pre_calc["rsi"]):
            if modes.get("rsi","both") != "bullish":
                sell = sell or (pre_calc["rsi"][effective_idx] > 55)
        if "ema" in pre_calc and effective_idx < len(pre_calc["ema"]) and pre_calc["ema"][effective_idx] is not None:
            if modes.get("ema","both") != "bullish":
                sell = sell or (historical[effective_idx]["close_price"] < pre_calc["ema"][effective_idx])
        if "kama" in pre_calc and effective_idx < len(pre_calc["kama"]):
            kama_v = pre_calc["kama"][effective_idx]
            if kama_v is not None and modes.get("kama","both") != "bullish":
                sell = sell or (historical[effective_idx]["close_price"] < kama_v)
        if "hmm_regime" in pre_calc:
            seq = pre_calc["hmm_regime"].get("state_sequence", [])
            if effective_idx < len(seq) and seq[effective_idx] == "Bearish":
                sell = sell or True
        if "dynamic_boll" in pre_calc:
            db = pre_calc["dynamic_boll"]
            upper = db.get("upper", [])
            if effective_idx < len(upper) and upper[effective_idx] is not None:
                sell = sell or (historical[effective_idx]["close_price"] > upper[effective_idx])
        if "ml_rsi" in pre_calc:
            ml = pre_calc["ml_rsi"]
            sig = ml.get("signal", [])
            if effective_idx < len(sig) and sig[effective_idx] == -1:
                sell = sell or True
        if "ml_signal" in pre_calc:
            prob = pre_calc["ml_signal"].get("probability", [])
            if effective_idx < len(prob) and prob[effective_idx] < 0.40:
                sell = sell or True
        if "neural" in pre_calc:
            sig = pre_calc["neural"].get("signal", [])
            if modes.get("neural_network","both") != "bullish" and effective_idx < len(sig) and sig[effective_idx] == -1:
                sell = sell or True
        if "volume" in pre_calc:
            sig = pre_calc["volume"].get("signal", [])
            if modes.get("volume_indicator","both") != "bullish" and effective_idx < len(sig) and sig[effective_idx] == -1:
                sell = sell or True
        if "oi" in pre_calc:
            sig = pre_calc["oi"].get("signal", [])
            if modes.get("open_interest","both") != "bullish" and effective_idx < len(sig) and sig[effective_idx] == -1:
                sell = sell or True
        if not pre_calc:
            sell = True
        return sell

    def _select_strike(self, spot, symbol, option_type, strike_sel, delta_target, otm_dist):
        step = get_strike_step(symbol)
        atm = round(spot / step) * step
        if strike_sel == "delta" and delta_target is not None:
            return self._find_delta_strike(spot, symbol, option_type, float(delta_target))
        offset = otm_dist * step
        if strike_sel == "itm":
            offset = -offset if option_type == "CE" else offset
        elif strike_sel == "otm":
            offset = offset if option_type == "CE" else -offset
        else:
            offset = 0
        return atm + offset

    def _enter_single(self, date, spot, symbol, leg, strike_sel, delta_target, otm_dist):
        option_type = leg.get("option_type", "CE")
        txn_type = leg.get("transaction", "buy").lower()
        lots = int(leg.get("lots", 1))
        qty = lots * get_lot_size(symbol)
        strike = self._select_strike(spot, symbol, option_type, strike_sel, delta_target, otm_dist)
        premium = self._entry_premium(date, spot, strike, option_type)
        premium = TransactionCosts.apply_fill_slippage(premium, "BUY" if txn_type == "buy" else "SELL", self.is_live)
        costs = TransactionCosts.calculate(premium * qty, txn_type == "sell", self.is_live)
        entry_time = getattr(self, '_entry_time', '09:35')
        return {
            "date": date, "strike": str(strike), "price": round(premium, 2),
            "quantity": qty, "costs": costs, "time": entry_time,
            "total_cost": round(premium * qty + costs["total"], 2),
            "type": txn_type, "legs": [{"option_type": option_type, "strike": strike, "type": txn_type, "lots": lots}],
        }

    def _enter_spread(self, date, spot, symbol, legs, strike_sel, delta_target, otm_dist):
        step = get_strike_step(symbol)
        atm = round(spot / step) * step
        leg_details = []
        total_cost = 0.0
        qty = 0
        for idx, leg in enumerate(legs):
            option_type = leg.get("option_type", leg.get("optType", "CE"))
            # Support both 'transaction' and frontend 'position'
            txn_type = leg.get("transaction", leg.get("position", "buy"))
            txn_type = str(txn_type).lower()
            if txn_type not in ("buy", "sell"):
                txn_type = "buy"
            lots = int(leg.get("lots", 1) or 1)
            lqty = lots * get_lot_size(symbol)
            # Support both 'strike_selection' and frontend 'strike_type'
            leg_sel_raw = leg.get("strike_selection", leg.get("strike_type", strike_sel))
            leg_sel = str(leg_sel_raw).lower() if isinstance(leg_sel_raw, str) else strike_sel
            if leg_sel not in ("atm", "otm", "itm", "delta"):
                leg_sel = strike_sel
            leg_delta = leg.get("delta_target", delta_target)
            # otm_distance handling; for spread ensure legs have distinct strikes
            if leg.get("otm_distance") is not None:
                leg_otm = int(leg.get("otm_distance") or 0)
            elif leg.get("otmDistance") is not None:
                leg_otm = int(leg.get("otmDistance") or 0)
            else:
                leg_otm = int(otm_dist or 2)
                # Auto widen spread: second leg further OTM by 2 steps, 4-leg iron condor by pattern
                if len(legs) == 2 and idx == 1:
                    leg_otm = leg_otm + 2
                elif len(legs) == 4 and idx in (1, 3):
                    leg_otm = leg_otm + 2
            if "offset" in leg and leg.get("offset") not in (None, "") and leg.get("strike_selection") is None and leg.get("strike_type") is None:
                strike = atm + int(leg.get("offset", 0) or 0) * step
            else:
                strike = self._select_strike(spot, symbol, option_type, leg_sel, leg_delta, leg_otm)
            premium = self._entry_premium(date, spot, strike, option_type)
            premium = TransactionCosts.apply_fill_slippage(premium, "BUY" if txn_type == "buy" else "SELL", self.is_live)
            costs = TransactionCosts.calculate(premium * lqty, txn_type == "sell", self.is_live)
            # Correct sign: sell = +premium (credit received), buy = -premium (debit paid)
            signed = premium * lqty if txn_type == "sell" else -premium * lqty
            total_cost += signed
            qty += lqty
            leg_details.append({
                "option_type": option_type, "strike": strike, "type": txn_type,
                "lots": lots, "quantity": lqty, "premium": round(premium, 2),
                "costs": costs, "signed_value": round(signed, 2),
            })
        net_premium = abs(total_cost)
        return {
            "date": date, "strike": str(leg_details[0]["strike"]), "price": round(net_premium, 2),
            "quantity": qty, "costs": {"total": sum(l["costs"]["total"] for l in leg_details), **leg_details[0]["costs"]},
            "total_cost": round(total_cost + sum(l["costs"]["total"] for l in leg_details), 2),
            "type": "spread", "legs": leg_details,
            "is_spread": True,
        }

    def _find_delta_strike(self, spot, symbol, option_type, target_delta):
        step = get_strike_step(symbol)
        best = round(spot / step) * step
        best_diff = 1.0
        for offset in range(-1000, 1001, int(step)):
            test = round(spot / step) * step + offset
            if test <= 0:
                continue
            delta = abs(self.indicators.calculate_delta(spot, test, self.implied_volatility, 15 / 365, option_type))
            diff = abs(delta - abs(target_delta))
            if diff < best_diff:
                best_diff = diff
                best = test
        return best

    def _check_sl_tp(self, current, entry, option_type, txn_type, sl_amt, tp_amt):
        entry_price = float(entry["price"])
        # Use fixed percent based on sl_amt/tp_amt magnitude
        if sl_amt > 20:
            sl_pct = (sl_amt / max(entry_price, 0.01)) * 100
        else:
            sl_pct = sl_amt
        if tp_amt > 20:
            tp_pct = (tp_amt / max(entry_price, 0.01)) * 100
        else:
            tp_pct = tp_amt
        # Conservative intra-candle SL/TP hit check:
        # Use the actual High/Low from the bar.
        # If SL/TP levels are within the candle's high/low range, assume they could be hit.
        # This avoids look-ahead by only checking if price moved through those levels.
        high = float(current.get("high_price", 0) or current["close_price"])
        low = float(current.get("low_price", 0) or current["close_price"])
        if high < low:
            high, low = low, high
        
        # Calculate SL/TP levels as prices
        if txn_type == "buy":
            sl_level = entry_price * (1 - sl_pct / 100) if sl_pct > 0 else 0
            tp_level = entry_price * (1 + tp_pct / 100) if tp_pct > 0 else 0
        else:
            sl_level = entry_price * (1 + sl_pct / 100) if sl_pct > 0 else 0
            tp_level = entry_price * (1 - tp_pct / 100) if tp_pct > 0 else 0
        
        # CONSERVATIVE check:
        # For long (buy): SL hit if LOW <= SL level; TP hit if HIGH >= TP level
        # For short (sell): SL hit if HIGH >= SL level; TP hit if LOW <= TP level
        hit_sl = False
        hit_tp = False
        if txn_type == "buy":
            # Long position
            hit_sl = sl_level > 0 and low <= sl_level
            hit_tp = tp_level > 0 and high >= tp_level
        else:
            # Short position
            hit_sl = sl_level > 0 and high >= sl_level
            hit_tp = tp_level > 0 and low <= tp_level
        if hit_sl:
            return {"reason": "stoploss", "level": sl_level}
        if hit_tp:
            return {"reason": "target", "level": tp_level}
        return None

    def _square_off_expiry(self, historical, entries, exits, symbol, option_type, txn_type, qty, risk_management):
        """Automatically square off all open option positions on expiry day at market close.
        This prevents physical delivery risk and is critical for F&O options."""
        import datetime
        # Find the last bar's date
        last_date = historical[-1]["trade_date"] if historical else ""
        # Check if any position is open and we're on or near expiry
        open_positions = len(entries) - len(exits)
        if open_positions > 0:
            # Parse expiry from legs - if weekly, next Thursday; if monthly, last Thursday
            exp_type = getattr(self, '_expiry_hint', 'weekly')
            # Determine if today is expiry day (last Thursday of month for Nifty/BankNifty)
            try:
                bar_dt = datetime.datetime.strptime(last_date, "%Y-%m-%d")
                if exp_type == "monthly":
                    # Last Thursday of the month
                    last_day = datetime.datetime(bar_dt.year, bar_dt.month, 
                        calendar.monthrange(bar_dt.year, bar_dt.month)[1])
                    while last_day.weekday() != 3:
                        last_day -= datetime.timedelta(days=1)
                    is_expiry = (last_day.strftime("%Y-%m-%d") == last_date)
                else:
                    # Weekly: next Thursday from bar date
                    days_ahead = 3 - bar_dt.weekday()
                    if days_ahead <= 0:
                        days_ahead += 7
                    exp_thu = (bar_dt + datetime.timedelta(days=days_ahead)).strftime("%Y-%m-%d")
                    is_expiry = (exp_thu == last_date)
                
                if is_expiry:
                    # Square off all positions at close price (last bar's close)
                    for i in range(len(exits), len(entries)):
                        entry = entries[i]
                        exit_prem = self._close_premium(last_date, float(historical[-1]["close_price"]), 
                            float(entry["strike"]), option_type)
                        exit_prem = TransactionCosts.apply_fill_slippage(exit_prem, 
                            "SELL" if txn_type == "buy" else "BUY", self.is_live)
                        self._close_position(entries, exits, entry, exit_prem, "expiry_squareoff", 
                            last_date, qty, txn_type)
            except Exception:
                pass
        return None

    def _entry_premium(self, date, spot, strike, option_type):
        key = f"{date}_{strike}_{option_type}_e"
        if key in self.premium_cache:
            return self.premium_cache[key]
        # 1) Real DB LTP (historical)
        row = Database.get_instance().fetch_one(
            "SELECT open_price, close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND strike_price=? AND option_type=?",
            [self.bt_symbol, date, strike, option_type],
        )
        if row:
            if row["open_price"] and float(row["open_price"]) > 0:
                val = float(row["open_price"])
                self.premium_cache[key] = val
                return val
            if row["close_price"] and float(row["close_price"]) > 0:
                val = float(row["close_price"])
                self.premium_cache[key] = val
                return val
        # 2) Real LTP from live market - only in live mode to keep backtest fast and realistic via Black-Scholes
        if self.is_live:
            try:
                from core.services.live_market_data import LiveMarketData
                live = LiveMarketData().get_option_ltp(self.bt_symbol, strike, option_type)
                if live and float(live) > 0:
                    val = float(live)
                    self.premium_cache[key] = val
                    return val
            except Exception:
                pass
        # 3) Nearest strike real LTP fallback
        try:
            from utils.helpers import get_strike_step
            step = get_strike_step(self.bt_symbol)
            row2 = Database.get_instance().fetch_one(
                "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type=? AND ABS(strike_price-?) <= ?*2 AND trade_date=? ORDER BY ABS(strike_price-?) LIMIT 1",
                [self.bt_symbol, option_type, strike, step, date, strike],
            )
            if row2 and row2["close_price"] and float(row2["close_price"]) > 0:
                return float(row2["close_price"])
        except Exception:
            pass
        # 4) Try Google Finance / nselib real historical via strategy_builder fetch (no synthetic) - live only
        if self.is_live:
            try:
                from routes.strategy_builder import _fetch_google_finance, _fetch_and_store_nselib
                # attempt to fetch real history for this date range on-demand
                _fetch_google_finance(self.bt_symbol, date, date)
                row_retry = Database.get_instance().fetch_one(
                    "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND strike_price=? AND option_type=?",
                    [self.bt_symbol, date, strike, option_type],
                )
                if row_retry and row_retry["close_price"] and float(row_retry["close_price"]) > 0:
                    return float(row_retry["close_price"])
            except Exception:
                pass
        # Black-Scholes synthetic fallback (proper option pricing)
        if spot and spot > 0 and strike and strike > 0:
            dte = self._days_to_expiry(date, self._get_expiry_type()) / 365.0
            iv = self.implied_volatility
            from utils.helpers import black_scholes
            bs_price = black_scholes(spot, strike, dte, iv, option_type)
            if bs_price > 0:
                val = round(bs_price, 2)
                self.premium_cache[key] = val
                return val
            val2 = round(spot * 0.01, 2)
            self.premium_cache[key] = val2
            return val2
        self.premium_cache[key] = 1.0
        return 1.0

    def _get_expiry_type(self):
        # Check legs for expiry hint
        return getattr(self, '_expiry_hint', 'weekly')

    def _exit_premium(self, date, spot, strike, option_type):
        return self._entry_premium(date, spot, strike, option_type)

    def _close_premium(self, date, spot, strike, option_type):
        key = f"{date}_{strike}_{option_type}_c"
        if key in self.premium_cache:
            return self.premium_cache[key]
        row = Database.get_instance().fetch_one(
            "SELECT close_price, open_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND strike_price=? AND option_type=?",
            [self.bt_symbol, date, strike, option_type],
        )
        if row:
            if row["close_price"] and float(row["close_price"]) > 0:
                val = float(row["close_price"])
                self.premium_cache[key] = val
                return val
            if row["open_price"] and float(row["open_price"]) > 0:
                val = float(row["open_price"])
                self.premium_cache[key] = val
                return val
        # Real live LTP fallback - only in live mode
        if self.is_live:
            try:
                from core.services.live_market_data import LiveMarketData
                live = LiveMarketData().get_option_ltp(self.bt_symbol, strike, option_type)
                if live and float(live) > 0:
                    val = float(live)
                    self.premium_cache[key] = val
                    return val
            except Exception:
                pass
        # Nearest strike real LTP fallback
        try:
            from utils.helpers import get_strike_step
            step = get_strike_step(self.bt_symbol)
            row2 = Database.get_instance().fetch_one(
                "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type=? AND ABS(strike_price-?) <= ?*2 AND trade_date=? ORDER BY ABS(strike_price-?) LIMIT 1",
                [self.bt_symbol, option_type, strike, step, date, strike],
            )
            if row2 and row2["close_price"] and float(row2["close_price"]) > 0:
                val = float(row2["close_price"])
                self.premium_cache[key] = val
                return val
        except Exception:
            pass
        # Black-Scholes fallback
        if spot and spot > 0 and strike and strike > 0:
            dte = self._days_to_expiry(date, self._get_expiry_type()) / 365.0
            iv = self.implied_volatility
            from utils.helpers import black_scholes
            bs_price = black_scholes(spot, strike, dte, iv, option_type)
            if bs_price > 0:
                val = round(bs_price, 2)
                self.premium_cache[key] = val
                return val
            val2 = round(spot * 0.01, 2)
            self.premium_cache[key] = val2
            return val2
        self.premium_cache[key] = 1.0
        return 1.0

    def _days_to_expiry(self, bar_date, expiry_type="weekly"):
        import datetime
        import calendar
        if not self.bt_expiry:
            d = datetime.datetime.strptime(bar_date, "%Y-%m-%d") if bar_date else datetime.datetime.now()
            if expiry_type == "monthly":
                # Last Thursday of month (Nifty monthly expiry)
                last_day = calendar.monthrange(d.year, d.month)[1]
                last = datetime.datetime(d.year, d.month, last_day)
                # Find last Thursday (weekday 3)
                while last.weekday() != 3:
                    last -= datetime.timedelta(days=1)
                if last <= d:
                    # Next month's last Thursday
                    nm = d.month + 1 if d.month < 12 else 1
                    ny = d.year if d.month < 12 else d.year + 1
                    last_day = calendar.monthrange(ny, nm)[1]
                    last = datetime.datetime(ny, nm, last_day)
                    while last.weekday() != 3:
                        last -= datetime.timedelta(days=1)
                exp = last
            else:
                # Weekly: next Thursday
                days_ahead = 3 - d.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                exp = d + datetime.timedelta(days=days_ahead)
            self.bt_expiry = exp.strftime("%Y-%m-%d")
        try:
            exp_dt = datetime.datetime.strptime(self.bt_expiry, "%Y-%m-%d")
            bar_dt = datetime.datetime.strptime(bar_date, "%Y-%m-%d") if bar_date else datetime.datetime.now()
            return max(1, (exp_dt - bar_dt).days)
        except:
            return 7

    def _close_position(self, entries, exits, entry, exit_prem, reason, exit_date, qty, txn_type):
        if entry.get("is_spread"):
            self._close_spread(entries, exits, entry, reason, exit_date)
            return
        exit_prem = max(0.01, exit_prem)
        exit_costs = TransactionCosts.calculate(exit_prem * qty, txn_type == "buy", self.is_live)
        if txn_type == "buy":
            pnl = (exit_prem * qty - exit_costs["total"]) - entry["total_cost"]
        else:
            pnl = (entry["price"] * entry["quantity"] - entry["costs"]["total"]) - (exit_prem * qty + exit_costs["total"])
        exits.append({
            "date": exit_date, "strike": entry["strike"], "price": round(exit_prem, 2),
            "quantity": qty, "exit_costs": exit_costs, "reason": reason, "pnl": round(pnl, 2),
            "time": "15:14",
        })

    def _close_spread(self, entries, exits, entry, reason, exit_date):
        exit_costs_total = 0.0
        exit_value = 0.0
        for leg in entry.get("legs", []):
            option_type = leg["option_type"]
            strike = leg["strike"]
            lqty = leg["quantity"]
            spot = self._close_spot_for_date(exit_date)
            prem = self._close_premium(exit_date, spot, strike, option_type)
            prem = TransactionCosts.apply_fill_slippage(prem, "SELL" if leg["type"] == "buy" else "BUY", self.is_live)
            costs = TransactionCosts.calculate(prem * lqty, leg["type"] == "buy", self.is_live)
            exit_costs_total += costs["total"]
            if leg["type"] == "buy":
                exit_value += prem * lqty
            else:
                exit_value -= prem * lqty
        entry_cash = sum(l["signed_value"] for l in entry["legs"])
        # entry["total_cost"] = entry_cash + entry_brokerage (Ec)
        entry_costs = entry["total_cost"] - entry_cash
        # Net PnL = entry_cash (credit + / debit -) + exit_value (reverse legs) - brokerage both sides
        pnl = entry_cash + exit_value - entry_costs - exit_costs_total
        exits.append({
            "date": exit_date, "strike": entry["strike"], "price": round(abs(exit_value), 2),
            "quantity": entry["quantity"], "exit_costs": {"total": round(exit_costs_total, 2)},
            "reason": reason, "pnl": round(pnl, 2), "is_spread": True,
        })

    def _close_spot_for_date(self, bar_date):
        row = Database.get_instance().fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND option_type IS NULL",
            [self.bt_symbol, bar_date],
        )
        if row and row["close_price"]:
            return float(row["close_price"])
        row2 = Database.get_instance().fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND option_type='CE' ORDER BY ABS(strike_price - (SELECT close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND option_type IS NULL)) LIMIT 1",
            [self.bt_symbol, bar_date, self.bt_symbol, bar_date],
        )
        if row2 and row2["close_price"]:
            return float(row2["close_price"])
        return 0

    def _build_result(self, symbol, start_date, end_date, entries, exits):
        equity = [self.initial_capital]
        capital = self.initial_capital
        total_trades = 0
        wins = 0
        win_amounts = []
        loss_amounts = []
        trade_list = []
        total_brokerage = 0
        for i in range(len(entries)):
            entry = entries[i]
            exit = exits[i] if i < len(exits) else None
            if exit:
                pnl = exit["pnl"]
                capital += pnl
                total_trades += 1
                total_brokerage += entry["costs"]["total"] + exit["exit_costs"]["total"]
                if pnl > 0:
                    wins += 1
                    win_amounts.append(pnl)
                else:
                    loss_amounts.append(abs(pnl))
                entry_time_str = entry.get("time", "09:35")
                exit_time_str = exit.get("time", "15:14")
                if entry.get("is_spread"):
                    legs_str = " + ".join(
                        f"{'Sell' if l['type']=='sell' else 'Buy'} {l['option_type']} {l['strike']}"
                        for l in entry.get("legs", [])
                    )
                    trade_list.append({
                        "index": total_trades,
                        "symbol": symbol,
                        "entry_date": entry.get("date", ""),
                        "entry_time": entry_time_str,
                        "exit_date": exit.get("date", ""),
                        "exit_time": exit_time_str,
                        "option_type": "Spread",
                        "strike": entry.get("strike", ""),
                        "position": legs_str,
                        "quantity": entry.get("quantity", 0),
                        "lots": entry.get("legs", [{}])[0].get("lots", 1) if entry.get("legs") else 1,
                        "entry_price": entry.get("price", 0),
                        "exit_price": exit.get("price", 0),
                        "pnl": pnl,
                        "pnl_formatted": f"₹{pnl:,.2f}",
                        "is_spread": True,
                    })
                else:
                    leg_info = entry.get("legs", [{}])[0] if entry.get("legs") else {}
                    trade_list.append({
                        "index": total_trades,
                        "symbol": symbol,
                        "entry_date": entry.get("date", ""),
                        "entry_time": entry_time_str,
                        "exit_date": exit.get("date", ""),
                        "exit_time": exit_time_str,
                        "option_type": leg_info.get("option_type", "CE"),
                        "strike": entry.get("strike", ""),
                        "position": "Sell" if entry.get("type") == "sell" else "Buy",
                        "quantity": entry.get("quantity", 0),
                        "lots": leg_info.get("lots", 1),
                        "entry_price": entry.get("price", 0),
                        "exit_price": exit.get("price", 0),
                        "pnl": pnl,
                        "pnl_formatted": f"₹{pnl:,.2f}",
                    })
            equity.append(capital)
        total_return = capital - self.initial_capital
        total_return_pct = (total_return / self.initial_capital) * 100
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        loss_rate = 100 - win_rate if total_trades > 0 else 0
        avg_win = sum(win_amounts) / len(win_amounts) if win_amounts else 0
        avg_loss = sum(loss_amounts) / len(loss_amounts) if loss_amounts else 0
        profit_factor = sum(win_amounts) / max(sum(loss_amounts), 0.01) if loss_amounts else (999 if win_amounts else 0)
        max_dd = self._max_drawdown(equity)
        sharpe = self._sharpe(equity)
        # Frontend expects many extra fields - provide 0/default when no trades to avoid NaN/undefined
        avg_profit_per_trade = (total_return / total_trades) if total_trades > 0 else 0
        max_win = max(win_amounts) if win_amounts else 0
        max_loss = max(loss_amounts) if loss_amounts else 0
        # Streaks
        max_win_streak = 0
        max_loss_streak = 0
        cur_ws = 0
        cur_ls = 0
        for tl in trade_list:
            if tl["pnl"] > 0:
                cur_ws += 1
                cur_ls = 0
                max_win_streak = max(max_win_streak, cur_ws)
            else:
                cur_ls += 1
                cur_ws = 0
                max_loss_streak = max(max_loss_streak, cur_ls)
        # Max DD duration & max trades in DD (simplified)
        max_dd_duration = 0
        max_trades_in_dd = 0
        # Reward/risk, expectancy, return/maxDD
        reward_risk = (avg_win / max(avg_loss, 0.01)) if avg_loss > 0 else 0
        expectancy = (win_rate/100 * avg_win - (1-win_rate/100)*avg_loss) if total_trades>0 else 0
        return_maxdd = (total_return_pct / max(max_dd, 0.01)) if max_dd>0 else 0
        net_pnl = total_return
        monthly_pnl = {}
        for t in trade_list:
            m = t["entry_date"][:7]
            monthly_pnl[m] = monthly_pnl.get(m, 0) + t["pnl"]
        return {
            "success": True,
            "metrics": {
                "initial_capital": self.initial_capital,
                "final_capital": round(capital, 2),
                "total_return": round(total_return, 2),
                "total_return_pct": round(total_return_pct, 4),
                "win_rate": round(win_rate, 2),
                "loss_rate": round(loss_rate, 2),
                "max_drawdown": round(max_dd, 4),
                "profit_factor": round(profit_factor, 4),
                "sharpe_ratio": round(sharpe, 4),
                "total_trades": total_trades,
                "winning_trades": wins,
                "losing_trades": total_trades - wins,
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "avg_profit_per_trade": round(avg_profit_per_trade, 2),
                "net_pnl": round(net_pnl, 2),
                "max_win": round(max_win, 2),
                "max_loss": round(max_loss, 2),
                "max_dd_duration": max_dd_duration,
                "max_trades_in_dd": max_trades_in_dd,
                "return_maxdd": round(return_maxdd, 2),
                "reward_risk": round(reward_risk, 2),
                "expectancy": round(expectancy, 2),
                "max_win_streak": max_win_streak,
                "max_loss_streak": max_loss_streak,
                "total_brokerage": round(total_brokerage, 2),
                "equity_curve": equity,
                "trade_list": trade_list,
                "monthly_pnl": monthly_pnl,
                "source": "ratrade.quantitative.engine",
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

    def _reset(self):
        self.ohlc_cache = {}
        self.bt_symbol = ""
        self.bt_expiry = ""
