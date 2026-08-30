class TransactionCosts:
    # Realistic charges based on Indian brokers (Zerodha, Upstox, Angel One, etc.)
    # Slippage: 0.5% per order (realistic market impact + spread)
    SLIPPAGE_PCT = 0.50  # 0.50% slippage (50 bps) - realistic for options
    # Brokerage: Rs 20 per order (discount broker) or 0.05% of turnover (full service)
    BROKERAGE_PER_TRADE = 20.0  # Flat Rs 20 per trade (discount broker)
    # Exchange turnover charge: 0.00325% (NSE/F&O)
    EXCHANGE_TXN_PCT = 0.00325  # 0.00325% per order
    # SEBI fee: 0.0001%
    SEBI_FEE_PCT = 0.0001
    # Stamp duty: 0.015% on buy side (options), 0.003% on sell side
    # Updated: 0.015% on buy, 0.003% on sell for options (SEBI circular)
    STAMP_DUTY_BUY_PCT = 0.015  # 0.015% on buy
    STAMP_DUTY_SELL_PCT = 0.003  # 0.003% on sell
    # GST: 18% on (brokerage + exchange + SEBI + stamp)
    GST_PCT = 18.0
    # STT: 0.1% on sell side for options (premium value) - both intraday and delivery
    # STT on buy side for options: NIL (removed in 2018)
    STT_PCT_SELL = 0.1  # 0.1% on sell (options premium)
    STT_PCT_BUY = 0.0   # 0% on buy (options)

    # Latency simulation (milliseconds) - only used in backtest
    LATENCY_MS = 50  # 50ms typical broker API latency

    @classmethod
    def apply_fill_slippage(cls, price: float, side: str, is_live: bool = False) -> float:
        """Apply slippage. In live mode, use minimal slippage.
        side: "BUY" or "SELL" - indicates order side
        """
        if price <= 0:
            return price
        # Live mode: minimal slippage (0.05%)
        # Backtest mode: realistic 0.5% slippage per order
        slippage = 0.0005 if is_live else cls.SLIPPAGE_PCT / 100
        # Apply slippage: price moves against the order
        # For BUY: price increases (we pay more); for SELL: price decreases (we receive less)
        if side == "BUY":
            return price * (1 + slippage)
        else:  # SELL
            return price * (1 - slippage)

    @classmethod
    def calculate(cls, turnover: float, is_sell: bool, is_live: bool = False) -> dict:
        """Calculate all transaction costs.
        In live mode, use actual broker charges.
        In backtest mode, use realistic estimates with all charges deducted.
        """
        # Brokerage: Rs 20 per trade for discount broker; otherwise % of turnover
        brokerage = cls.BROKERAGE_PER_TRADE

        # Exchange transaction charge: 0.00325% of turnover
        exchange_txn = turnover * cls.EXCHANGE_TXN_PCT / 100

        # SEBI fee: 0.0001% of turnover
        sebi_fee = turnover * cls.SEBI_FEE_PCT / 100

        # Stamp duty: different rates for buy vs sell
        # Buy: 0.015%, Sell: 0.003% (options, as per latest SEBI circular)
        if is_sell:
            stamp_duty = turnover * cls.STAMP_DUTY_SELL_PCT / 100
        else:
            stamp_duty = turnover * cls.STAMP_DUTY_BUY_PCT / 100

        # STT: 0.1% on sell side for options (premium value); 0% on buy side
        if is_sell:
            stt = turnover * cls.STT_PCT_SELL / 100
        else:
            stt = turnover * cls.STT_PCT_BUY / 100

        # GST: 18% on (brokerage + exchange + SEBI + stamp)
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