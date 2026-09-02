from typing import Dict, List

import numpy as np
import pandas as pd


class CostModel:
    BROKERAGE_FLAT = 20.0
    BROKERAGE_PCT = 0.0003
    STT_DELIVERY_SELL = 0.00025
    STT_INTRADAY_SELL = 0.0005
    STT_FUTURES_SELL = 0.0001
    STAMP_DUTY = 0.00003
    SEBI_TURNOVER = 0.000001
    GST_RATE = 0.18
    SLIPPAGE_PCT = 0.0001

    def __init__(self, intraday: bool = True, slippage_pct: float = None):
        self.intraday = intraday
        if slippage_pct is not None:
            self.SLIPPAGE_PCT = slippage_pct

    def per_trade(self, entry_price: float, exit_price: float,
                  quantity: int = 1) -> Dict[str, float]:
        turnover_buy = entry_price * quantity
        turnover_sell = exit_price * quantity

        slippage_buy = turnover_buy * self.SLIPPAGE_PCT
        slippage_sell = turnover_sell * self.SLIPPAGE_PCT

        brokerage_buy = min(self.BROKERAGE_FLAT, turnover_buy * self.BROKERAGE_PCT)
        brokerage_sell = min(self.BROKERAGE_FLAT, turnover_sell * self.BROKERAGE_PCT)

        stt = turnover_sell * (self.STT_INTRADAY_SELL if self.intraday else self.STT_DELIVERY_SELL)
        stamp = turnover_buy * self.STAMP_DUTY
        sebi = (turnover_buy + turnover_sell) * self.SEBI_TURNOVER
        gst = (brokerage_buy + brokerage_sell + sebi) * self.GST_RATE

        total_cost = slippage_buy + slippage_sell + brokerage_buy + brokerage_sell + stt + stamp + sebi + gst
        denom = max((turnover_buy + turnover_sell) / 2, 0.001)
        cost_pct = total_cost / denom * 100

        return {
            "cost_abs": round(total_cost, 2),
            "cost_pct": round(cost_pct, 4),
            "brokerage": round(brokerage_buy + brokerage_sell, 2),
            "stt": round(stt, 2),
            "stamp": round(stamp, 2),
            "sebi": round(sebi, 4),
            "gst": round(gst, 2),
            "slippage": round(slippage_buy + slippage_sell, 2),
        }

    def apply(self, trades: List[Dict]) -> List[Dict]:
        out = []
        for t in trades:
            entry = float(t.get("entry_price", 0))
            exit_p = float(t.get("exit_price", 0))
            if entry <= 0 or exit_p <= 0:
                out.append(t)
                continue
            cost = self.per_trade(entry, exit_p)
            cost_pct = cost["cost_pct"]
            raw_pnl = t.get("pnl", 0)
            t = dict(t)
            t["pnl"] = round(raw_pnl - cost_pct, 4)
            t["cost_pct"] = cost_pct
            t["cost_detail"] = cost
            out.append(t)
        return out

    def cost_summary(self, trades: List[Dict]) -> Dict[str, float]:
        if not trades:
            return {"avg_cost_pct": 0, "total_cost_pct": 0}
        costs = [t.get("cost_pct", 0) for t in trades if "cost_pct" in t]
        if not costs:
            return {"avg_cost_pct": 0, "total_cost_pct": 0}
        return {
            "avg_cost_pct": round(np.mean(costs), 4),
            "total_cost_pct": round(np.sum(costs), 4),
        }
