class TransactionCosts:
    # Realistic charges based on Indian brokers (Zerodha, Upstox, Angel One, etc.)
    SLIPPAGE_PCT = 0.05  # 0.05% slippage (5 bps) - more realistic than 0.50%
    BROKERAGE_PER_TRADE = 20.0  # Flat Rs 20 per trade (discount broker)
    EXCHANGE_TXN_PCT = 0.00345  # 0.00345% exchange transaction charge
    SEBI_FEE_PCT = 0.0001  # 0.0001% SEBI fee
    STAMP_DUTY_PCT = 0.003  # 0.003% stamp duty
    GST_PCT = 18.0  # 18% GST on brokerage + exchange + sebi + stamp
    STT_PCT = 0.0625  # 0.0625% STT on sell side (options)

    # Latency simulation (milliseconds) - only used in backtest
    LATENCY_MS = 50  # 50ms typical broker API latency

    @classmethod
    def apply_fill_slippage(cls, price: float, side: str, is_live: bool = False) -> float:
        """Apply slippage. In live mode, use minimal slippage."""
        if price <= 0:
            return price
        # Live mode: minimal slippage (0.01%)
        # Backtest mode: realistic slippage (0.05%)
        slippage = 0.0001 if is_live else cls.SLIPPAGE_PCT / 100
        return price * (1 - slippage) if side == "SELL" else price * (1 + slippage)

    @classmethod
    def calculate(cls, turnover: float, is_sell: bool, is_live: bool = False) -> dict:
        """Calculate all transaction costs.
        In live mode, use actual broker charges.
        In backtest mode, use realistic estimates.
        """
        brokerage = cls.BROKERAGE_PER_TRADE
        exchange_txn = max(0.01, turnover * cls.EXCHANGE_TXN_PCT / 100)
        sebi_fee = max(0.01, turnover * cls.SEBI_FEE_PCT / 100)
        stamp_duty = max(0.01, turnover * cls.STAMP_DUTY_PCT / 100)
        stt = turnover * cls.STT_PCT / 100 if is_sell else 0
        subtotal = brokerage + exchange_txn + sebi_fee + stamp_duty + stt
        gst = subtotal * cls.GST_PCT / 100
        total = subtotal + gst
        return {
            "brokerage": round(brokerage, 2),
            "exchange_txn": round(exchange_txn, 2),
            "sebi_fee": round(sebi_fee, 2),
            "stamp_duty": round(stamp_duty, 2),
            "stt": round(stt, 2),
            "gst": round(gst, 2),
            "total": round(total, 2),
        }

    @classmethod
    def latency_delay(cls, is_live: bool = False) -> float:
        """Return simulated latency in seconds."""
        return 0.0 if is_live else cls.LATENCY_MS / 1000.0
