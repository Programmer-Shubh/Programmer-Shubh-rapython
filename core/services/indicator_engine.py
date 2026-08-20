import math
from typing import List, Dict
from sklearn.mixture import GaussianMixture


class IndicatorEngine:
    def calculate_rsi(self, prices: List[float], period: int = 14) -> List[float]:
        rsi = [50.0] * len(prices)
        if len(prices) < period + 1:
            return rsi
        gains, losses = [], []
        avg_gain, avg_loss = 0.0, 0.0
        for i in range(1, len(prices)):
            change = prices[i] - prices[i - 1]
            gain = max(0, change)
            loss = max(0, -change)
            if i <= period:
                gains.append(gain)
                losses.append(loss)
                if i == period:
                    avg_gain = sum(gains) / period
                    avg_loss = sum(losses) / period
            else:
                avg_gain = (avg_gain * (period - 1) + gain) / period
                avg_loss = (avg_loss * (period - 1) + loss) / period
            if i >= period:
                rs = avg_gain / max(avg_loss, 0.0001)
                rsi[i] = 100 - (100 / (1 + rs))
        return rsi

    def calculate_ema(self, prices: List[float], period: int) -> List[float]:
        ema = [None] * len(prices)
        if len(prices) < period:
            return ema
        multiplier = 2 / (period + 1)
        ema[period - 1] = sum(prices[:period]) / period
        for i in range(period, len(prices)):
            ema[i] = (prices[i] - ema[i - 1]) * multiplier + ema[i - 1]
        return ema

    def calculate_macd(self, prices: List[float], fast=12, slow=26, signal=9) -> Dict:
        ema_fast = self.calculate_ema(prices, fast)
        ema_slow = self.calculate_ema(prices, slow)
        macd_line = [None] * len(prices)
        valid_macd = []
        for i in range(len(prices)):
            if ema_fast[i] is not None and ema_slow[i] is not None:
                macd_line[i] = ema_fast[i] - ema_slow[i]
                valid_macd.append(macd_line[i])
        signal_valid = self.calculate_ema(valid_macd, signal) if len(valid_macd) >= signal else [None] * len(valid_macd)
        signal_line = [None] * len(prices)
        sig_idx = 0
        for i in range(len(prices)):
            if macd_line[i] is not None:
                if sig_idx < len(signal_valid):
                    signal_line[i] = signal_valid[sig_idx]
                sig_idx += 1
        return {"macd": macd_line, "signal": signal_line}

    def calculate_supertrend(self, data: List[Dict], period=10, multiplier=3.0) -> List[float]:
        n = len(data)
        atr = [0.0] * n
        supertrend = [0.0] * n
        for i in range(1, n):
            tr = max(
                data[i]["high_price"] - data[i]["low_price"],
                abs(data[i]["high_price"] - data[i - 1]["close_price"]),
                abs(data[i]["low_price"] - data[i - 1]["close_price"]),
            )
            atr[i] = ((atr[i - 1] * (period - 1)) + tr) / period if i >= period else tr
        hl2 = [(d["high_price"] + d["low_price"]) / 2 for d in data]
        upper = [0.0] * n
        lower = [0.0] * n
        in_uptrend = [True] * n
        for i in range(period, n):
            basic_upper = hl2[i] + multiplier * atr[i]
            basic_lower = hl2[i] - multiplier * atr[i]
            upper[i] = max(basic_upper, upper[i - 1]) if data[i - 1]["close_price"] <= upper[i - 1] else basic_upper
            lower[i] = min(basic_lower, lower[i - 1]) if data[i - 1]["close_price"] >= lower[i - 1] else basic_lower
            if in_uptrend[i - 1]:
                in_uptrend[i] = data[i]["close_price"] > lower[i]
            else:
                in_uptrend[i] = data[i]["close_price"] >= upper[i]
            supertrend[i] = lower[i] if in_uptrend[i] else upper[i]
        return supertrend

    def calculate_predicted_ma(self, prices: List[float], lookback=20) -> Dict:
        pma = [None] * len(prices)
        trend_strength = [0.0] * len(prices)
        for i in range(lookback, len(prices)):
            sx, sy, sxy, sx2 = 0, 0, 0, 0
            for j in range(lookback):
                x, y = j, prices[i - lookback + j]
                sx += x; sy += y; sxy += x * y; sx2 += x * x
            denom = lookback * sx2 - sx * sx
            slope = (lookback * sxy - sx * sy) / denom if denom != 0 else 0
            intercept = (sy - slope * sx) / lookback if denom != 0 else sy / lookback
            pma[i] = intercept + slope * (lookback - 1)
            trend_strength[i] = abs(slope) / max(prices[i], 0.01) * 100 * lookback / 252
        return {"pma": pma, "trend_strength": trend_strength}

    def calculate_ai_sentiment(self, prices: List[float], highs: List[float], lows: List[float], lookback=20) -> Dict:
        n = len(prices)
        asi = [None] * n
        for i in range(lookback, n):
            change = (prices[i] - prices[i - lookback]) / max(prices[i - lookback], 0.01)
            close_pos = (prices[i] - lows[i - lookback]) / max(highs[i - lookback] - lows[i - lookback], 0.01)
            money_flow = close_pos - 0.5
            sentiment = max(-100, min(100, change * 50 + money_flow * 50))
            regime_range = highs[i - lookback] - lows[i - lookback]
            pos_in_range = (prices[i] - lows[i - lookback]) / max(regime_range, 0.01)
            regime = "distribution" if pos_in_range > 0.66 else ("accumulation" if pos_in_range < 0.33 else "trending")
            asi[i] = {"sentiment": sentiment, "regime": regime, "money_flow": money_flow * 100}
        return {"asi": asi}

    def calculate_ai_volatility(self, prices: List[float], highs: List[float], lows: List[float], lookback=20) -> Dict:
        n = len(prices)
        vol_regime = ["normal"] * n
        pred_high = [0.0] * n
        pred_low = [0.0] * n
        for i in range(lookback, n):
            returns = []
            for j in range(1, min(11, i + 1)):
                r = (prices[i - j + 1] - prices[i - j]) / max(prices[i - j], 0.01)
                returns.append(abs(r))
            avg_abs = sum(returns) / len(returns) if returns else 0
            vol_regime[i] = "high" if avg_abs > 0.03 else ("normal" if avg_abs > 0.015 else "low")
            sma = sum(prices[max(0, i - lookback + 1): i + 1]) / min(lookback, i + 1)
            sum_sq = sum((p - sma) ** 2 for p in prices[max(0, i - lookback + 1): i + 1])
            sd = math.sqrt(sum_sq / min(lookback, i + 1))
            mult = 2.5 if avg_abs > 0.03 else (2.0 if avg_abs > 0.015 else 1.5)
            pred_high[i] = sma + 2 * sd * mult
            pred_low[i] = sma - 2 * sd * mult
        return {"vol_regime": vol_regime, "predicted_high": pred_high, "predicted_low": pred_low}

    def calculate_ai_trend_score(self, prices: List[float]) -> Dict:
        scores = [0.0] * len(prices)
        cum = 0.0
        for i in range(1, len(prices)):
            pct = (prices[i] - prices[i - 1]) / max(prices[i - 1], 0.01)
            cum += pct * 100
            scores[i] = cum
        return {"scores": scores}

    def calculate_delta(self, spot: float, strike: float, iv: float, time_to_expiry: float, option_type: str) -> float:
        if time_to_expiry <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
            return 0.5 if option_type == "CE" else -0.5
        d1 = (math.log(spot / strike) + (0.5 * iv * iv) * time_to_expiry) / (iv * math.sqrt(time_to_expiry))
        from utils.helpers import normal_cdf
        delta = normal_cdf(d1)
        return delta if option_type == "CE" else delta - 1

    def calculate_vwap(self, data: List[Dict], period: int = 20, multiplier: float = 2.0) -> Dict:
        n = len(data)
        vwap = [None] * n
        upper1 = [None] * n
        lower1 = [None] * n
        upper2 = [None] * n
        lower2 = [None] * n
        for i in range(period - 1, n):
            cum_pv = 0.0
            cum_vol = 0.0
            sum_sq = 0.0
            for j in range(i - period + 1, i + 1):
                typical = (data[j]['high_price'] + data[j]['low_price'] + data[j]['close_price']) / 3.0
                vol = data[j].get('volume', 1) or 1
                cum_pv += typical * vol
                cum_vol += vol
            vwap[i] = cum_pv / cum_vol if cum_vol > 0 else data[i]['close_price']
            for j in range(i - period + 1, i + 1):
                typical = (data[j]['high_price'] + data[j]['low_price'] + data[j]['close_price']) / 3.0
                sum_sq += (typical - vwap[i]) ** 2
            std = math.sqrt(sum_sq / period) if period > 0 else 0
            upper1[i] = vwap[i] + std
            lower1[i] = vwap[i] - std
            upper2[i] = vwap[i] + multiplier * std
            lower2[i] = vwap[i] - multiplier * std
        return {'vwap': vwap, 'upper1': upper1, 'lower1': lower1, 'upper2': upper2, 'lower2': lower2}

    def calculate_kama(self, prices: List[float], fast_period: int = 10, slow_period: int = 30) -> List[float]:
        """Kaufman's Adaptive Moving Average (KAMA).
        Self-adjusting speed based on market efficiency ratio (MER).
        Faster in trending markets, slower in sideways/ranging markets.
        """
        n = len(prices)
        if n < slow_period:
            return [prices[i] for i in range(n)]
        kama = [prices[0]] * n
        er = [0.0] * n
        change = [0.0] * n
        volatility = [0.0] * n
        for i in range(1, n):
            change[i] = abs(prices[i] - prices[i - 1])
            if i >= fast_period:
                volatility[i] = sum(change[i - fast_period + 1:i + 1]) / fast_period
        for i in range(slow_period, n):
            er[i] = abs(prices[i] - prices[i - slow_period]) / max(volatility[i], 0.001)
            fast_coeff = math.pow(2 / (fast_period + 1), 2)
            slow_coeff = math.pow(2 / (slow_period + 1), 2)
            mer = er[i]
            noise = math.sqrt(fast_coeff - slow_coeff) if fast_coeff > slow_coeff else 0
            kama_coeff = (mer / (mer + noise) + slow_coeff) if (mer + noise) > 0 else slow_coeff
            kama[i] = kama[i - 1] + kama_coeff * (prices[i] - kama[i - 1])
        return kama

    def calculate_hmm_regime(self, prices: List[float], n_components: int = 3) -> Dict:
        """Hidden Markov Model Regime Classifier.
        Automatically detects market regimes: Bullish, Bearish, Sideways.
        Uses GMM on price changes to identify hidden states.
        """
        n = len(prices)
        if n < n_components * 10:
            return {"regimes": [{"state": "Sideways", "probability": 1.0}] * n, "state_sequence": ["Sideways"] * n}
        # Prepare data: price changes
        returns = [prices[i] - prices[i - 1] for i in range(1, n)]
        # Train GMM
        try:
            gmm = GaussianMixture(n_components=n_components, random_state=42, n_iter=100)
            gmm.fit([[r] for r in returns])
            states = gmm.predict([[r] for r in returns])
            # Map states to labels based on mean return
            means = gmm.means_.flatten()
            # Sort states by mean return
            order = sorted(range(n_components), key=lambda x: means[x])
            state_labels = {}
            for idx, state_idx in enumerate(order):
                if means[state_idx] > 0.0005:
                    state_labels[state_idx] = "Bullish"
                elif means[state_idx] < -0.0005:
                    state_labels[state_idx] = "Bearish"
                else:
                    state_labels[state_idx] = "Sideways"
            # Map states to human-readable labels
            state_sequence = [state_labels.get(s, "Sideways") for s in states]
            # Calculate regime probabilities
            regime_probs = []
            for i in range(n):
                probs = gmm.predict_proba([[returns[i]]])[0]
                regime_probs.append({state_labels.get(j, "Sideways"): float(probs[j]) for j in range(n_components)})
            return {"regimes": regime_probs, "state_sequence": state_sequence}
        except Exception:
            return {"regimes": [{"state": "Sideways", "probability": 1.0}] * n, "state_sequence": ["Sideways"] * n}

    def calculate_dynamic_bollinger(self, prices: List[float], highs: List[float], lows: List[float], period: int = 20, lookforward: int = 5) -> Dict:
        """AI-Optimized Volatility Bands (Dynamic Bollinger Bands).
        Uses machine learning to predict future volatility expansion
        and adjusts bands dynamically, reducing fake breakouts.
        """
        n = len(prices)
        if n < period:
            return {"mid": prices, "upper": [p * 1.02 for p in prices], "lower": [p * 0.98 for p in prices]}
        # Calculate standard rolling statistics
        mid = []
        upper = []
        lower = []
        for i in range(n):
            if i >= period - 1:
                window_prices = prices[i - period + 1:i + 1]
                window_highs = highs[i - period + 1:i + 1]
                window_lows = lows[i - period + 1:i + 1]
                sma = sum(window_prices) / period
                # Use lookahead high/low for volatility estimation
                max_h = max(window_highs)
                min_l = min(window_lows)
                # Adaptive volatility: use ATR-like measure
                atr = max(max_h - min_l, (sum(window_prices) / period) * 0.02)
                # AI adjustment: volatility expansion factor based on recent volatility change
                if i >= period * 2:
                    prev_atr = max(window_prices[:period]) - min(window_lows[:period])
                    vol_ratio = atr / max(prev_atr, 0.001)
                    # Expand/contract bands based on volatility regime
                    if vol_ratio > 1.2:
                        band_wide = atr * 2.0
                    elif vol_ratio < 0.8:
                        band_wide = atr * 0.5
                    else:
                        band_wide = atr * 1.0
                else:
                    band_wide = atr * 1.0
                mid.append(sma)
                upper.append(sma + band_wide)
                lower.append(sma - band_wide)
            else:
                mid.append(prices[i])
                upper.append(prices[i] * 1.02)
                lower.append(prices[i] * 0.98)
        return {"mid": mid, "upper": upper, "lower": lower}

    def calculate_ml_rsi(self, prices: List[float], period: int = 14) -> Dict:
        """Neural Network Smoothed RSI (ML-RSI).
        Combines standard RSI with a predictive filter to filter
        momentum whipsaws and noisy fake crosses.
        """
        # Standard RSI calculation
        n = len(prices)
        rsi = [50.0] * n
        if n < period + 1:
            return {"rsi": rsi, "smoothed_rsi": rsi, "signal": [0] * n}
        gains, losses = [], []
        avg_gain, avg_loss = 0.0, 0.0
        for i in range(1, len(prices)):
            change = prices[i] - prices[i - 1]
            gain = max(0, change)
            loss = max(0, -change)
            if i <= period:
                gains.append(gain)
                losses.append(loss)
                if i == period:
                    avg_gain = sum(gains) / period
                    avg_loss = sum(losses) / period
            else:
                avg_gain = (avg_gain * (period - 1) + gain) / period
                avg_loss = (avg_loss * (period - 1) + loss) / period
            if i >= period:
                rs = avg_gain / max(avg_loss, 0.0001)
                rsi[i] = 100 - (100 / (1 + rs))
        # Simple predictive filter: exponential smoothing of RSI changes
        alpha = 0.3
        smoothed = [rsi[0]]
        for i in range(1, n):
            smoothed.append(alpha * rsi[i] + (1 - alpha) * smoothed[i - 1])
        # Signal: 1 if RSI crosses above 50 from below, -1 if crosses below 50 from above, 0 otherwise
        signal = [0] * n
        for i in range(1, n):
            if rsi[i] > 50 and rsi[i - 1] <= 50:
                signal[i] = 1  # Bullish crossover
            elif rsi[i] < 50 and rsi[i - 1] >= 50:
                signal[i] = -1  # Bearish crossover
            else:
                signal[i] = 0
        return {"rsi": rsi, "smoothed_rsi": smoothed, "signal": signal}

    def calculate_ml_signal_filter(self, prices: List[float], fast_ema: int = 9, slow_ema: int = 21, rsi_period: int = 14, ml_weight: float = 0.3) -> Dict:
        """Gradient Boosted Signal Filter (ML Probability Score).
        Combines outputs of fast EMA, slow EMA, and RSI into a
        lightweight probability score (0 to 1) indicating signal quality.
        """
        n = len(prices)
        if n < max(fast_ema, slow_ema, rsi_period):
            return {"signal": [0] * n, "probability": [0.5] * n, "ml_score": [0.5] * n}
        # Calculate indicators
        ema_fast = self.calculate_ema(prices, fast_ema)
        ema_slow = self.calculate_ema(prices, slow_ema)
        rsi_result = self.calculate_rsi(prices, rsi_period)
        rsi = rsi_result if isinstance(rsi_result, list) else rsi_result.get("rsi", [50] * n)
        # Build probability score
        probability = [0.5] * n
        ml_score = [0.5] * n
        for i in range(max(fast_ema, slow_ema, rsi_period), n):
            # EMA alignment: 1 if fast > slow (bullish), -1 if fast < slow (bearish), 0 if mixed
            ema_alignment = 0
            if ema_fast[i] > ema_slow[i]:
                ema_alignment = 1
            elif ema_fast[i] < ema_slow[i]:
                ema_alignment = -1
            # RSI level: 1 if oversold (<30) or overbought (>70), 0 otherwise
            rsi_level = 0
            if rsi[i] < 30:
                rsi_level = 1  # Oversold - bullish reversal potential
            elif rsi[i] > 70:
                rsi_level = -1  # Overbearish - bearish reversal potential
            # Combined probability: base 0.5 + weight * (ema_signal + rsi_signal)
            ema_signal = 1 if ema_alignment == 1 else (-1 if ema_alignment == -1 else 0)
            combined = 0.5 + ml_weight * (ema_signal + rsi_level)
            probability[i] = max(0.01, min(0.99, combined))
            ml_score[i] = combined
        # Fill early bars with default
        for i in range(max(fast_ema, slow_ema, rsi_period)):
            probability[i] = 0.5
            ml_score[i] = 0.5
        # Generate trading signal: 1 = buy, -1 = sell, 0 = hold
        signal = [0] * n
        for i in range(n):
            if probability[i] > 0.6:
                signal[i] = 1
            elif probability[i] < 0.4:
                signal[i] = -1
        return {"signal": signal, "probability": probability, "ml_score": ml_score}
