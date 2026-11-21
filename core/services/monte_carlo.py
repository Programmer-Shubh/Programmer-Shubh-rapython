"""Monte Carlo on trade_list: shuffle + slippage jitter -> distribution."""
import random, math
from typing import List, Dict

def monte_carlo(trade_list: List[Dict], runs: int = 2000, slippage_jitter: float = 0.10) -> Dict:
    if not trade_list:
        return {"p5":0,"p50":0,"p95":0,"prob_profit":0,"expected_dd":0,"hist":[]}
    pnls = [float(t.get("pnl",0)) for t in trade_list]
    finals = []
    max_dds = []
    for _ in range(runs):
        random.shuffle(pnls)
        # jitter each pnl +-10%
        jittered = [p * (1 + random.uniform(-slippage_jitter, slippage_jitter)) for p in pnls]
        eq = 0
        peak = 0
        max_dd = 0
        for p in jittered:
            eq += p
            if eq > peak: peak = eq
            dd = (peak - eq) / max(peak, 1)
            if dd > max_dd: max_dd = dd
        finals.append(eq)
        max_dds.append(max_dd*100)
    finals.sort()
    def pct(a, p): 
        idx = int(len(a)*p/100)
        return a[min(idx, len(a)-1)]
    return {
        "p5": round(pct(finals,5),2),
        "p50": round(pct(finals,50),2),
        "p95": round(pct(finals,95),2),
        "prob_profit": round(sum(1 for x in finals if x>0)/len(finals)*100,2),
        "expected_dd": round(sum(max_dds)/len(max_dds),2),
        "hist": finals[::max(1,len(finals)//40)][:40],
    }
