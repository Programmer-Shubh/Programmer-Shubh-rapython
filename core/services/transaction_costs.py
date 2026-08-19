class TransactionCosts:
    SLIPPAGE_PCT = 0.50
    BROKERAGE_PER_TRADE = 20.0
    EXCHANGE_TXN_PCT = 0.00345
    SEBI_FEE_PCT = 0.0001
    STAMP_DUTY_PCT = 0.003
    GST_PCT = 18.0
    STT_PCT = 0.1

    @classmethod
    def apply_fill_slippage(cls, price: float, side: str) -> float:
        if price <= 0:
            return price
        frac = cls.SLIPPAGE_PCT / 100
        return price * (1 - frac) if side == "SELL" else price * (1 + frac)

    @classmethod
    def calculate(cls, turnover: float, is_sell: bool) -> dict:
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
