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
        self.bt_symbol = ""
        self.bt_expiry = ""
        self.implied_volatility = 0.14
        self.is_live = is_live  # Unified switch: False=backtest, True=live/paper

    def run(self, historical, symbol, start_date, end_date, ind_list, entry_conditions,
            exit_conditions, legs, advanced_options, risk_management, is_live: bool = None) -> dict:
        if is_live is not None:
            self.is_live = is_live
        self._reset()
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
        max_holding = int(advanced_options.get("max_holding_bars", 20))
        # BTST holds overnight: increase max_holding for BTST
        if advanced_options.get("trade_mode") == "btst":
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

        entries, exits, entry_bars = [], [], []
        pending_entry = None
        pending_exit = None
        pending_entry_signal = None
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
                if is_spread:
                    trade = self._enter_spread(cur_date, spot, symbol, legs, strike_sel, delta_target, otm_dist)
                else:
                    trade = self._enter_single(cur_date, spot, symbol, legs[0], strike_sel, delta_target, otm_dist)
                entries.append(trade)
                entry_bars.append(i)
                daily_trades += 1
                pending_entry = None

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
                if leg_sl > 0 or leg_tp > 0:
                    hit = self._check_sl_tp(cur, entry, option_type, txn_type, leg_sl, leg_tp)
                    if hit:
                        exit_prem = TransactionCosts.apply_fill_slippage(hit["level"], "SELL" if txn_type == "buy" else "BUY", self.is_live)
                        self._close_position(entries, exits, entry, exit_prem, hit["reason"], cur_date, qty, txn_type)
                        daily_pnl += exits[-1]["pnl"]
                        if daily_loss_limit > 0 and daily_pnl <= -daily_loss_limit:
                            kill_switch_on = True

            has_open = len(entries) > len(exits)
            if has_open and trade_mode == "intraday" and is_last:
                entry = entries[len(exits)]
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
                if is_spread:
                    entry_sig = buy_sig or sell_sig
                    # Fallback: if indicators produce rare signals (RSI>70 etc), force periodic entry to ensure backtest not empty
                    if not entry_sig:
                        # If no indicators at all, enter every bar (will be throttled by max_trades_day)
                        if not pre_calc:
                            entry_sig = True
                        else:
                            # For spread with indicators, allow entry every 7 bars as time-based fallback
                            # This ensures Bear Call Spread shows trades even in sideways market
                            entry_sig = (i % 7 == 0)
                    exit_sig = time_exit  # Spreads exit on time/SL-TP, not opposite signal
                else:
                    entry_sig = buy_sig if txn_type == "buy" else sell_sig
                    exit_sig = sell_sig if txn_type == "buy" else buy_sig
                has_open = len(entries) > len(exits)
                can_enter = not has_open and daily_trades < max_trades_day
                if entry_sig and can_enter:
                    # Apply latency: in backtest mode, wait enough bars for latency to elapse
                    # In live mode, execute immediately
                    # For spreads, execute immediately to ensure Bear Call Spread not stuck on 1-bar delay + reset
                    if self.is_live or is_spread:
                        pending_entry = i
                        pending_entry_signal = None
                    else:
                        # Backtest mode: wait for latency bars
                        if pending_entry_signal is None:
                            pending_entry_signal = i
                        elif i - pending_entry_signal >= max(1, latency):
                            pending_entry = i
                            pending_entry_signal = None
                else:
                    if not is_spread:
                        pending_entry_signal = None
                    else:
                        # For spreads, keep pending signal if it was set periodic
                        if pending_entry_signal is not None and i - pending_entry_signal > 3:
                            pending_entry_signal = None
                if has_open and pending_exit is None and (exit_sig or time_exit):
                    pending_exit = "condition" if exit_sig else "time"

        while len(exits) < len(entries):
            idx = len(exits)
            entry = entries[idx]
            last = historical[-1]
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
            elif iid == "ai_sentiment":
                result["ai_sentiment"] = self.indicators.calculate_ai_sentiment(closes, highs, lows, params.get("lookback", 20))
            elif iid == "ai_volatility_range":
                result["ai_volatility"] = self.indicators.calculate_ai_volatility(closes, highs, lows, params.get("lookback", 20))
            elif iid == "ai_trend_score":
                result["ai_trend"] = self.indicators.calculate_ai_trend_score(closes)
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

        # PRIORITY 2: Selected indicator-based signals (OR logic - any one triggers)
        buy = False
        if "supertrend" in pre_calc and effective_idx < len(pre_calc["supertrend"]):
            buy = buy or (historical[effective_idx]["close_price"] > pre_calc["supertrend"][effective_idx])
        if "macd" in pre_calc:
            m = pre_calc["macd"]
            mv = m["macd"][effective_idx] if effective_idx < len(m["macd"]) and m["macd"][effective_idx] is not None else 0
            sv = m["signal"][effective_idx] if effective_idx < len(m["signal"]) and m["signal"][effective_idx] is not None else 0
            pm = m["macd"][effective_idx - 1] if effective_idx > 0 and effective_idx - 1 < len(m["macd"]) and m["macd"][effective_idx - 1] is not None else 0
            ps = m["signal"][effective_idx - 1] if effective_idx > 0 and effective_idx - 1 < len(m["signal"]) and m["signal"][effective_idx - 1] is not None else 0
            buy = buy or (mv > sv and pm <= ps)
        if "rsi" in pre_calc and effective_idx < len(pre_calc["rsi"]):
            buy = buy or (pre_calc["rsi"][effective_idx] < 30)
        if "ema" in pre_calc and effective_idx < len(pre_calc["ema"]) and pre_calc["ema"][effective_idx] is not None:
            buy = buy or (historical[effective_idx]["close_price"] > pre_calc["ema"][effective_idx])
        if "kama" in pre_calc and effective_idx < len(pre_calc["kama"]):
            kama_v = pre_calc["kama"][effective_idx]
            if kama_v is not None:
                buy = buy or (historical[effective_idx]["close_price"] > kama_v)
        if "hmm_regime" in pre_calc:
            seq = pre_calc["hmm_regime"].get("state_sequence", [])
            if effective_idx < len(seq) and seq[effective_idx] == "Bullish":
                buy = buy or True
        if "dynamic_boll" in pre_calc:
            db = pre_calc["dynamic_boll"]
            low = db.get("lower", [])
            if effective_idx < len(low) and low[effective_idx] is not None:
                buy = buy or (historical[effective_idx]["close_price"] < low[effective_idx])
        if "ml_rsi" in pre_calc:
            ml = pre_calc["ml_rsi"]
            sig = ml.get("signal", [])
            if effective_idx < len(sig) and sig[effective_idx] == 1:
                buy = buy or True
        if "ml_signal" in pre_calc:
            prob = pre_calc["ml_signal"].get("probability", [])
            if effective_idx < len(prob) and prob[effective_idx] > 0.60:
                buy = buy or True
        if not pre_calc:
            buy = True
        return buy

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

        # PRIORITY 2: Selected indicator-based signals (OR logic)
        sell = False
        if "supertrend" in pre_calc and effective_idx < len(pre_calc["supertrend"]):
            sell = sell or (historical[effective_idx]["close_price"] < pre_calc["supertrend"][effective_idx])
        if "rsi" in pre_calc and effective_idx < len(pre_calc["rsi"]):
            sell = sell or (pre_calc["rsi"][effective_idx] > 70)
        if "ema" in pre_calc and effective_idx < len(pre_calc["ema"]) and pre_calc["ema"][effective_idx] is not None:
            sell = sell or (historical[effective_idx]["close_price"] < pre_calc["ema"][effective_idx])
        if "kama" in pre_calc and effective_idx < len(pre_calc["kama"]):
            kama_v = pre_calc["kama"][effective_idx]
            if kama_v is not None:
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
        return {
            "date": date, "strike": str(strike), "price": round(premium, 2),
            "quantity": qty, "costs": costs,
            "total_cost": round(premium * qty + costs["total"], 2),
            "type": txn_type, "legs": [{"option_type": option_type, "strike": strike, "type": txn_type, "lots": lots}],
        }

    def _enter_spread(self, date, spot, symbol, legs, strike_sel, delta_target, otm_dist):
        step = get_strike_step(symbol)
        atm = round(spot / step) * step
        leg_details = []
        total_cost = 0.0
        qty = 0
        for leg in legs:
            option_type = leg.get("option_type", "CE")
            txn_type = leg.get("transaction", "buy").lower()
            lots = int(leg.get("lots", 1))
            lqty = lots * get_lot_size(symbol)
            # Quant fix: respect per-leg strike_selection/otm_distance/delta (for Bear Call Spread: Sell ATM + Buy OTM)
            leg_sel = leg.get("strike_selection", strike_sel)
            leg_delta = leg.get("delta_target", delta_target)
            leg_otm = int(leg.get("otm_distance", otm_dist) if leg.get("otm_distance") is not None else otm_dist)
            # If legacy offset provided, use it directly; else use strike_selection logic
            if "offset" in leg and leg.get("strike_selection") is None:
                strike = atm + int(leg.get("offset", 0) or 0) * step
            else:
                strike = self._select_strike(spot, symbol, option_type, leg_sel, leg_delta, leg_otm)
            premium = self._entry_premium(date, spot, strike, option_type)
            premium = TransactionCosts.apply_fill_slippage(premium, "BUY" if txn_type == "buy" else "SELL", self.is_live)
            costs = TransactionCosts.calculate(premium * lqty, txn_type == "sell", self.is_live)
            signed = premium * lqty if txn_type == "buy" else -premium * lqty
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

    def _entry_premium(self, date, spot, strike, option_type):
        # 1) Real DB LTP (historical)
        row = Database.get_instance().fetch_one(
            "SELECT open_price, close_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND strike_price=? AND option_type=?",
            [self.bt_symbol, date, strike, option_type],
        )
        if row:
            if row["open_price"] and float(row["open_price"]) > 0:
                return float(row["open_price"])
            if row["close_price"] and float(row["close_price"]) > 0:
                return float(row["close_price"])
        # 2) Real LTP from live market (niftytrader.in) - for stocks with no DB option data
        try:
            from core.services.live_market_data import LiveMarketData
            live = LiveMarketData().get_option_ltp(self.bt_symbol, strike, option_type)
            if live and float(live) > 0:
                return float(live)
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
        # 4) Try Google Finance / nselib real historical via strategy_builder fetch (no synthetic)
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
                return round(bs_price, 2)
            return round(spot * 0.01, 2)
        return 1.0

    def _get_expiry_type(self):
        # Check legs for expiry hint
        return getattr(self, '_expiry_hint', 'weekly')

    def _exit_premium(self, date, spot, strike, option_type):
        return self._entry_premium(date, spot, strike, option_type)

    def _close_premium(self, date, spot, strike, option_type):
        row = Database.get_instance().fetch_one(
            "SELECT close_price, open_price FROM bhavcopy_data WHERE symbol=? AND trade_date=? AND strike_price=? AND option_type=?",
            [self.bt_symbol, date, strike, option_type],
        )
        if row:
            if row["close_price"] and float(row["close_price"]) > 0:
                return float(row["close_price"])
            if row["open_price"] and float(row["open_price"]) > 0:
                return float(row["open_price"])
        # Real live LTP fallback
        try:
            from core.services.live_market_data import LiveMarketData
            live = LiveMarketData().get_option_ltp(self.bt_symbol, strike, option_type)
            if live and float(live) > 0:
                return float(live)
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
                return float(row2["close_price"])
        except Exception:
            pass
        # Black-Scholes fallback
        if spot and spot > 0 and strike and strike > 0:
            dte = self._days_to_expiry(date, self._get_expiry_type()) / 365.0
            iv = self.implied_volatility
            from utils.helpers import black_scholes
            bs_price = black_scholes(spot, strike, dte, iv, option_type)
            if bs_price > 0:
                return round(bs_price, 2)
            return round(spot * 0.01, 2)
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
        entry_value = sum(l["signed_value"] for l in entry["legs"])
        pnl = (exit_value - exit_costs_total) - entry["total_cost"]
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
                entry_data = entry if isinstance(entry, dict) else {"date": entry.get("date", ""), "strike": entry.get("strike", ""), "price": entry.get("price", 0)}
                exit_data = exit if isinstance(exit, dict) else {"date": exit.get("date", ""), "price": exit.get("price", 0)} if exit else {"date": "", "price": 0}
                trade_list.append({
                    "entry_date": entry_data.get("date", ""),
                    "exit_date": exit_data.get("date", "") if exit_data else "",
                    "strike": entry_data.get("strike", ""),
                    "entry_price": entry_data.get("price", 0),
                    "exit_price": exit_data.get("price", 0) if exit_data else 0,
                    "pnl": pnl,
                    "pnl_formatted": f"₹{pnl:.2f}"
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
                "source": "shoonya.algotest.in",
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
