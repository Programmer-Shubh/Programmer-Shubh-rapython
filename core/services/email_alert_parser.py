import re
import json
from datetime import datetime
from typing import Dict, Optional


class EmailAlertParser:
    """Parses email alerts from Chartink, Gocharting.com, and Investing.com."""

    SYMBOL_MAP = {
        "nifty": "NIFTY", "banknifty": "BANKNIFTY", "finnifty": "FINNIFTY",
        "midcpnifty": "MIDCPNIFTY", "nifty50": "NIFTY", "nifty bank": "BANKNIFTY",
        "reliance": "RELIANCE", "hdfcbank": "HDFCBANK", "icicibank": "ICICIBANK",
        "tcs": "TCS", "infy": "INFY", "itc": "ITC", "sbi": "SBIN", "sbin": "SBIN",
        "axisbank": "AXISBANK", "kotakbank": "KOTAKBANK", "lt": "LT",
        "hindunilvr": "HINDUNILVR", "bhartiartl": "BHARTIARTL", "m&m": "M&M",
        "maruti": "MARUTI", "bajfinance": "BAJFINANCE", "wipro": "WIPRO",
        "ongc": "ONGC", "sunpharma": "SUNPHARMA", "tatasteel": "TATASTEEL",
        "tatamotors": "TATAMOTORS", "hcltech": "HCLTECH", "jswsteel": "JSWSTEEL",
    }

    def parse(self, email_text: str, source: str = "auto") -> Optional[Dict]:
        if source == "auto":
            source = self._detect_source(email_text)
        if source == "chartink":
            return self._parse_chartink(email_text)
        elif source == "gocharting":
            return self._parse_gocharting(email_text)
        elif source == "investing":
            return self._parse_investing(email_text)
        else:
            return self._parse_generic(email_text)

    def _detect_source(self, text: str) -> str:
        t = text.lower()
        if "chartink" in t or "scanalert" in t or "chartink.com" in t:
            return "chartink"
        if "gocharting" in t or "gocharting.com" in t:
            return "gocharting"
        if "investing.com" in t or "investing alert" in t:
            return "investing"
        return "generic"

    def _parse_chartink(self, text: str) -> Optional[Dict]:
        result = {"source": "chartink", "raw": text}
        t = text.upper()
        action = "BUY"
        if any(w in t for w in ["SELL", "SHORT", "BEARISH"]):
            action = "SELL"
        result["action"] = action
        symbol = self._extract_symbol(t)
        if symbol:
            result["symbol"] = symbol
        opt_type = self._extract_option_type(t)
        if opt_type:
            result["option_type"] = opt_type
        strike = self._extract_strike(t)
        if strike:
            result["strike"] = strike
        expiry = self._extract_expiry(text)
        if expiry:
            result["expiry"] = expiry
        qty = self._extract_quantity(t)
        if qty:
            result["quantity"] = qty
        return result if "symbol" in result else None

    def _parse_gocharting(self, text: str) -> Optional[Dict]:
        result = {"source": "gocharting", "raw": text}
        t = text.upper()
        action = "BUY"
        if any(w in t for w in ["SELL", "SHORT", "TARGET", "BOOK"]):
            action = "SELL"
        result["action"] = action
        symbol = self._extract_symbol(t)
        if symbol:
            result["symbol"] = symbol
        opt_type = self._extract_option_type(t)
        if opt_type:
            result["option_type"] = opt_type
        strike = self._extract_strike(t)
        if strike:
            result["strike"] = strike
        expiry = self._extract_expiry(text)
        if expiry:
            result["expiry"] = expiry
        qty = self._extract_quantity(t)
        if qty:
            result["quantity"] = qty
        return result if "symbol" in result else None

    def _parse_investing(self, text: str) -> Optional[Dict]:
        result = {"source": "investing", "raw": text}
        t = text.upper()
        action = "BUY"
        if any(w in t for w in ["SELL", "SHORT", "TARGET", "EXIT"]):
            action = "SELL"
        result["action"] = action
        symbol = self._extract_symbol(t)
        if symbol:
            result["symbol"] = symbol
        opt_type = self._extract_option_type(t)
        if opt_type:
            result["option_type"] = opt_type
        strike = self._extract_strike(t)
        if strike:
            result["strike"] = strike
        expiry = self._extract_expiry(text)
        if expiry:
            result["expiry"] = expiry
        qty = self._extract_quantity(t)
        if qty:
            result["quantity"] = qty
        return result if "symbol" in result else None

    def _parse_generic(self, text: str) -> Optional[Dict]:
        result = {"source": "generic", "raw": text}
        t = text.upper()
        action = "BUY"
        if any(w in t for w in ["SELL", "SHORT"]):
            action = "SELL"
        result["action"] = action
        symbol = self._extract_symbol(t)
        if symbol:
            result["symbol"] = symbol
        opt_type = self._extract_option_type(t)
        if opt_type:
            result["option_type"] = opt_type
        strike = self._extract_strike(t)
        if strike:
            result["strike"] = strike
        expiry = self._extract_expiry(text)
        if expiry:
            result["expiry"] = expiry
        qty = self._extract_quantity(t)
        if qty:
            result["quantity"] = qty
        return result if "symbol" in result else None

    def _extract_symbol(self, text: str) -> Optional[str]:
        for key, sym in self.SYMBOL_MAP.items():
            if key in text:
                return sym
        m = re.search(r'\b([A-Z]{2,10})\b', text)
        if m:
            return m.group(1)
        return None

    def _extract_option_type(self, text: str) -> Optional[str]:
        if " CE " in text or text.endswith(" CE") or "CALL" in text:
            return "CE"
        if " PE " in text or text.endswith(" PE") or "PUT" in text:
            return "PE"
        return None

    def _extract_strike(self, text: str) -> Optional[float]:
        m = re.search(r'(\d{4,6})\s*(CE|PE|CALL|PUT)', text)
        if m:
            return float(m.group(1))
        m = re.search(r'(?:STRIKE|STK|AT)\s*[:=]?\s*(\d{4,6})', text)
        if m:
            return float(m.group(1))
        return None

    def _extract_expiry(self, text: str) -> Optional[str]:
        m = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})', text)
        if m:
            try:
                d, mo, y = m.group(1), m.group(2), m.group(3)
                if len(y) == 2:
                    y = "20" + y
                return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
            except Exception:
                pass
        m = re.search(r'(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s*(\d{2,4})', text, re.I)
        if m:
            try:
                months = {"jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
                          "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"}
                d, mo, y = m.group(1), m.group(2).lower()[:3], m.group(3)
                if len(y) == 2:
                    y = "20" + y
                return f"{y}-{months[mo]}-{d.zfill(2)}"
            except Exception:
                pass
        return None

    def _extract_quantity(self, text: str) -> Optional[int]:
        m = re.search(r'(?:QTY|QUANTITY|LOTS?|LOT SIZE)\s*[:=]?\s*(\d+)', text)
        if m:
            return int(m.group(1))
        m = re.search(r'(\d+)\s*(?:LOTS?|QTY)', text)
        if m:
            return int(m.group(1))
        return None
