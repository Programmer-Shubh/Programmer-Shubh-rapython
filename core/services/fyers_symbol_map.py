from typing import Dict, List

from market.provider import SymbolMapper
from core.config import get_active_symbols

# FYERS symbol format: NSE:SBIN-EQ
# Universe driven by config — single source of truth
_NSE_INTERNAL = list(get_active_symbols())
CRYPTO_INTERNAL = ["BTC-USD", "ETH-USD"]

_NSE_FYERS_MAP: Dict[str, str] = {}
_FYERS_NSE_MAP: Dict[str, str] = {}

for sym in _NSE_INTERNAL:
    base = sym.replace(".NS", "")
    fyers_sym = f"NSE:{base}-EQ"
    _NSE_FYERS_MAP[sym] = fyers_sym
    _FYERS_NSE_MAP[fyers_sym] = sym


class FyersSymbolMapper(SymbolMapper):
    """Maps .NS symbols ↔ FYERS NSE:SYM-EQ format.
       Unknown symbols pass through unchanged."""

    def to_exchange(self, internal_symbol: str) -> str:
        """Convert .NS → NSE:SYM-EQ. Unknown symbols pass through."""
        if internal_symbol in _NSE_FYERS_MAP:
            return _NSE_FYERS_MAP[internal_symbol]
        # Dynamic mapping: try converting .NS → NSE:SYM-EQ
        if internal_symbol.endswith(".NS"):
            base = internal_symbol.replace(".NS", "")
            return f"NSE:{base}-EQ"
        return internal_symbol

    def to_internal(self, exchange_symbol: str) -> str:
        if exchange_symbol in _FYERS_NSE_MAP:
            return _FYERS_NSE_MAP[exchange_symbol]
        # Dynamic reverse mapping: NSE:SYM-EQ → SYM.NS
        if exchange_symbol.startswith("NSE:") and exchange_symbol.endswith("-EQ"):
            base = exchange_symbol.replace("NSE:", "").replace("-EQ", "")
            return f"{base}.NS"
        return exchange_symbol

    def all_exchange_symbols(self) -> List[str]:
        return list(_FYERS_NSE_MAP.keys())

    def all_internal_symbols(self) -> List[str]:
        return _NSE_INTERNAL + CRYPTO_INTERNAL
