import math
from typing import List, Dict


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
