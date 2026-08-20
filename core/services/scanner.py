import math
from typing import List, Dict
from core.services.indicator_engine import IndicatorEngine
from core.models.database import Database


class OptionScanner:
    def __init__(self):
        self.indicators = IndicatorEngine()
        self.db = Database.get_instance()

    def scan(self, symbols=None) -> dict:
        if symbols is None:
            symbols = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'RELIANCE', 'HDFCBANK',
                       'ICICIBANK', 'TCS', 'INFY', 'ITC', 'SBIN']
        bullish = []
        bearish = []
        for sym in symbols:
            result = self._analyze_symbol(sym)
            if result['type'] == 'BUY':
                bullish.append(result)
            elif result['type'] == 'SELL':
                bearish.append(result)
        bullish.sort(key=lambda x: x['score'], reverse=True)
        bearish.sort(key=lambda x: x['score'], reverse=True)
        return {'bullish': bullish[:5], 'bearish': bearish[:5], 'total_scanned': len(symbols)}

    def scan_vwap(self, symbols=None) -> dict:
        if symbols is None:
            symbols = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'RELIANCE', 'HDFCBANK',
                       'ICICIBANK', 'TCS', 'INFY', 'ITC', 'SBIN']
        long_signals = []
        short_signals = []
        for sym in symbols:
            result = self._analyze_vwap_symbol(sym)
            if result['type'] == 'LONG':
                long_signals.append(result)
            elif result['type'] == 'SHORT':
                short_signals.append(result)
        long_signals.sort(key=lambda x: x['score'], reverse=True)
        short_signals.sort(key=lambda x: x['score'], reverse=True)
        return {'long': long_signals[:5], 'short': short_signals[:5], 'total_scanned': len(symbols)}

    def get_top_opportunities(self, symbols=None) -> list:
        if symbols is None:
            symbols = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'RELIANCE', 'HDFCBANK',
                       'ICICIBANK', 'TCS', 'INFY', 'ITC', 'SBIN',
                       'HUL', 'LT', 'AXISBANK', 'KOTAKBANK', 'ASIANPAINT']
        vwap_result = self.scan_vwap(symbols)
        all_signals = []
        for s in vwap_result.get('long', []):
            s['signal_type'] = 'BUY CE'
            all_signals.append(s)
        for s in vwap_result.get('short', []):
            s['signal_type'] = 'BUY PE'
            all_signals.append(s)
        seen = set()
        unique = []
        for s in all_signals:
            if s['symbol'] not in seen:
                seen.add(s['symbol'])
                unique.append(s)
        unique.sort(key=lambda x: x['score'], reverse=True)
        return unique[:5]

    def _get_historical(self, symbol: str) -> list:
        rows = self.db.fetch_all(
            """SELECT * FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL
               ORDER BY trade_date DESC LIMIT 250""",
            [symbol],
        )
        rows.reverse()
        for r in rows:
            r['high_price'] = float(r.get('high_price', 0) or 0)
            r['low_price'] = float(r.get('low_price', 0) or 0)
            r['close_price'] = float(r.get('close_price', 0) or 0)
            r['open_price'] = float(r.get('open_price', 0) or 0)
        return rows

    def _get_spot(self, symbol: str) -> float:
        row = self.db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL AND trade_date=(SELECT MAX(trade_date) FROM bhavcopy_data WHERE symbol=? AND option_type IS NULL)",
            [symbol, symbol],
        )
        return float(row['close_price']) if row else 0

    def _get_step(self, symbol: str) -> int:
        steps = {'NIFTY': 50, 'BANKNIFTY': 100, 'FINNIFTY': 50, 'MIDCPNIFTY': 50,
                 'HDFCBANK': 20, 'ICICIBANK': 20, 'ITC': 10, 'SBIN': 10}
        return steps.get(symbol, 50)

    def _suggest_option(self, symbol: str, spot: float, option_type: str) -> dict:
        step = self._get_step(symbol)
        if option_type == 'CE':
            strike = round(spot / step) * step + step
        else:
            strike = round(spot / step) * step - step
        latest_date = self.db.fetch_one(
            "SELECT MAX(trade_date) as d FROM bhavcopy_data WHERE symbol=?", [symbol]
        )
        trade_date = latest_date['d'] if latest_date else ''
        row = self.db.fetch_one(
            "SELECT close_price, expiry_date FROM bhavcopy_data WHERE symbol=? AND strike_price=? AND option_type=? AND trade_date=?",
            [symbol, strike, option_type, trade_date],
        )
        premium = float(row['close_price']) if row and row['close_price'] else None
        expiry = row['expiry_date'] if row and row.get('expiry_date') else ''
        return {'strike': strike, 'premium': premium, 'expiry': expiry}

    def _analyze_symbol(self, symbol: str) -> dict:
        data = self._get_historical(symbol)
        if len(data) < 210:
            return {'symbol': symbol, 'type': 'NONE', 'score': 0, 'reasons': [], 'indicators': {}}
        closes = [d['close_price'] for d in data]
        volumes = [d.get('volume', 0) or 0 for d in data]
        spot = closes[-1]
        supertrend = self.indicators.calculate_supertrend(data, 10, 3.0)
        macd_data = self.indicators.calculate_macd(closes, 12, 26, 9)
        ema200 = self.indicators.calculate_ema(closes, 200)
        vol_sma20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
        i = len(data) - 1
        prev = i - 1
        buy_score = 0
        sell_score = 0
        buy_reasons = []
        sell_reasons = []
        if closes[i] > supertrend[i]:
            if closes[prev] <= supertrend[prev]:
                buy_score += 30
                buy_reasons.append('SuperTrend breakout')
            else:
                buy_score += 10
                buy_reasons.append('Above SuperTrend')
        if closes[i] < supertrend[i]:
            if closes[prev] >= supertrend[prev]:
                sell_score += 30
                sell_reasons.append('SuperTrend breakdown')
            else:
                sell_score += 10
                sell_reasons.append('Below SuperTrend')
        m = macd_data['macd']
        s = macd_data['signal']
        if m[i] and s[i]:
            if m[i] > s[i]:
                if m[prev] and s[prev] and m[prev] <= s[prev]:
                    buy_score += 30
                    buy_reasons.append('MACD bullish crossover')
                else:
                    buy_score += 10
                    buy_reasons.append('MACD above signal')
            if m[i] < s[i]:
                if m[prev] and s[prev] and m[prev] >= s[prev]:
                    sell_score += 30
                    sell_reasons.append('MACD bearish crossover')
                else:
                    sell_score += 10
                    sell_reasons.append('MACD below signal')
        if volumes[i] > vol_sma20 * 1.5:
            buy_score += 20
            buy_reasons.append(f'High volume (>{vol_sma20 * 1.5:.0f})')
        if volumes[i] > vol_sma20 * 1.2:
            sell_score += 20
            sell_reasons.append(f'High volume (>{vol_sma20 * 1.2:.0f})')
        if ema200[i] and closes[i] > ema200[i]:
            buy_score += 20
            buy_reasons.append('Above EMA(200)')
        if ema200[i] and closes[i] < ema200[i]:
            sell_score += 20
            sell_reasons.append('Below EMA(200)')
        indicators = {'supertrend': round(supertrend[i], 2), 'macd': round(m[i], 2) if m[i] else 0,
                      'signal': round(s[i], 2) if s[i] else 0, 'ema200': round(ema200[i], 2) if ema200[i] else 0,
                      'volume': volumes[i], 'vol_sma': round(vol_sma20, 0)}
        if buy_score >= 50:
            opt = self._suggest_option(symbol, spot, 'CE')
            return {'symbol': symbol, 'type': 'BUY', 'score': buy_score, 'price': spot,
                    'date': data[-1]['trade_date'], 'reasons': buy_reasons, 'indicators': indicators,
                    'option_suggestion': opt}
        elif sell_score >= 50:
            opt = self._suggest_option(symbol, spot, 'PE')
            return {'symbol': symbol, 'type': 'SELL', 'score': sell_score, 'price': spot,
                    'date': data[-1]['trade_date'], 'reasons': sell_reasons, 'indicators': indicators,
                    'option_suggestion': opt}
        return {'symbol': symbol, 'type': 'NONE', 'score': 0, 'price': spot,
                'date': data[-1]['trade_date'] if data else '', 'reasons': [], 'indicators': indicators}

    def _analyze_vwap_symbol(self, symbol: str) -> dict:
        data = self._get_historical(symbol)
        if len(data) < 30:
            return {'symbol': symbol, 'type': 'NONE', 'score': 0, 'reasons': [], 'indicators': {}}
        closes = [d['close_price'] for d in data]
        highs = [d['high_price'] for d in data]
        lows = [d['low_price'] for d in data]
        spot = closes[-1]
        vwap_data = self.indicators.calculate_vwap(data, 20, 2.0)
        rsi = self.indicators.calculate_rsi(closes, 14)
        ema9 = self.indicators.calculate_ema(closes, 9)
        ema20 = self.indicators.calculate_ema(closes, 20)
        i = len(data) - 1
        prev = i - 1
        vwap_val = vwap_data['vwap'][i] if vwap_data['vwap'][i] else spot
        upper2 = vwap_data['upper2'][i] if vwap_data['upper2'][i] else spot * 1.02
        lower2 = vwap_data['lower2'][i] if vwap_data['lower2'][i] else spot * 0.98
        long_score = 0
        short_score = 0
        long_reasons = []
        short_reasons = []
        if lows[i] <= lower2 or closes[i] <= lower2:
            long_score += 35
            long_reasons.append('Price at/below -2 VWAP band')
        elif closes[i] <= vwap_val:
            long_score += 10
            long_reasons.append('Price near VWAP')
        if highs[i] >= upper2 or closes[i] >= upper2:
            short_score += 35
            short_reasons.append('Price at/above +2 VWAP band')
        elif closes[i] >= vwap_val:
            short_score += 10
            short_reasons.append('Price near VWAP')
        if rsi[i] < 30:
            long_score += 35
            long_reasons.append(f'RSI oversold ({rsi[i]:.1f})')
        elif rsi[i] < 40:
            long_score += 10
            long_reasons.append(f'RSI low ({rsi[i]:.1f})')
        if rsi[i] > 70:
            short_score += 35
            short_reasons.append(f'RSI overbought ({rsi[i]:.1f})')
        elif rsi[i] > 60:
            short_score += 10
            short_reasons.append(f'RSI high ({rsi[i]:.1f})')
        if closes[i] >= closes[prev]:
            long_score += 15
            long_reasons.append('Green candle')
        else:
            short_score += 15
            short_reasons.append('Red candle')
        if ema9[i] and ema20[i]:
            if ema9[prev] and ema20[prev] and ema9[prev] <= ema20[prev] and ema9[i] > ema20[i]:
                long_score += 15
                long_reasons.append('9/20 EMA bullish crossover')
            elif ema9[i] > ema20[i]:
                long_score += 5
                long_reasons.append('9 EMA > 20 EMA')
            if ema9[prev] and ema20[prev] and ema9[prev] >= ema20[prev] and ema9[i] < ema20[i]:
                short_score += 15
                short_reasons.append('9/20 EMA bearish crossover')
            elif ema9[i] < ema20[i]:
                short_score += 5
                short_reasons.append('9 EMA < 20 EMA')
        indicators = {'vwap': round(vwap_val, 2), 'rsi': round(rsi[i], 1),
                      'ema9': round(ema9[i], 2) if ema9[i] else 0,
                      'ema20': round(ema20[i], 2) if ema20[i] else 0}
        if long_score >= 50:
            opt = self._suggest_option(symbol, spot, 'CE')
            return {'symbol': symbol, 'type': 'LONG', 'score': long_score, 'price': spot,
                    'date': data[-1]['trade_date'], 'reasons': long_reasons, 'indicators': indicators,
                    'option_suggestion': opt}
        elif short_score >= 50:
            opt = self._suggest_option(symbol, spot, 'PE')
            return {'symbol': symbol, 'type': 'SHORT', 'score': short_score, 'price': spot,
                    'date': data[-1]['trade_date'], 'reasons': short_reasons, 'indicators': indicators,
                    'option_suggestion': opt}
        return {'symbol': symbol, 'type': 'NONE', 'score': 0, 'price': spot,
                'date': data[-1]['trade_date'] if data else '', 'reasons': [], 'indicators': indicators}
