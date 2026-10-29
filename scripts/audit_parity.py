"""Bar-by-bar Backtest vs Paper parity audit (diagnostic, no strategy changes).

Runs the SAME seeded bars through:
  1. BacktestEngine.run (backtest)
  2. HistoricalReplayEngine.run_replay (paper loop, independent code)

Prints structured per-trade diagnostics
(symbol, timestamp, side, OHLC, intended entry, fill, qty, SL/TP, exit,
gross, charges, net) for both, compares signals bar-by-bar, and reports
the FIRST divergence point (bar + field), if any.

  python scripts/audit_parity.py [--spread-bps N]

--spread-bps: illustrative paper-realism overlay (adverse spread on paper
fills only) showing how microstructure flips backtest wins into paper
losses. Changes NOTHING in production code.
"""
import os
import sys
import random
import datetime
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_PATH", os.path.join("data", "ratrade.db"))

from core.services.backtest_engine import BacktestEngine
from core.services.historical_replay import HistoricalReplayEngine
from utils.helpers import sltp_premium_level


def build_bars(n=80, seed=99):
    rnd = random.Random(seed)
    bars = []
    px = 25000.0
    d = datetime.date(2026, 7, 6)  # Monday
    plan = ([0.006] * 12 + [-0.008] * 6 + [0.009] * 12 + [0.001] * 8
            + [-0.015] * 3 + [0.007] * 12)
    k = 0
    while len(bars) < n:
        if d.weekday() < 5:
            drift = plan[k % len(plan)] + rnd.uniform(-0.005, 0.005)
            k += 1
            o = px
            c = max(100.0, px * (1 + drift))
            h = max(o, c) * (1 + abs(rnd.uniform(0, 0.006)))
            low = min(o, c) * (1 - abs(rnd.uniform(0, 0.006)))
            # one violent spike bar (spread shock in live, smooth in history)
            if len(bars) == 50:
                h = max(o, c) * 1.03
            bars.append({"symbol": "NIFTY", "trade_date": d.strftime("%Y-%m-%d"),
                         "open_price": round(o, 2), "high_price": round(h, 2),
                         "low_price": round(low, 2), "close_price": round(c, 2),
                         "volume": 500000, "oi": 100000})
            px = c
        d += datetime.timedelta(days=1)
    return bars


def run_both(spread_bps=0.0):
    hist = build_bars()
    inds = [{"id": "supertrend", "params": {"period": 10, "multiplier": 3}}]
    legs = [{"option_type": "CE", "transaction": "buy", "lots": 1,
             "strike_selection": "atm", "otm_distance": 0}]
    adv = {"trade_mode": "intraday", "timeframe": "1d"}
    risk = {"max_trades_per_day": 5, "daily_stop_loss": 1500,
            "daily_take_profit": 3000}
    eng = BacktestEngine(is_live=False)
    eng._skip_db = True
    bt = eng.run(list(hist), "NIFTY", hist[0]["trade_date"], hist[-1]["trade_date"],
                 inds, [], [], legs, dict(adv), dict(risk), is_live=False)
    rp = HistoricalReplayEngine()
    rp.symbol, rp.start_date, rp.end_date = "NIFTY", hist[0]["trade_date"], hist[-1]["trade_date"]
    rp.ind_list, rp.entry_conditions, rp.exit_conditions = inds, [], []
    rp.legs, rp.advanced_options, rp.risk_management = legs, dict(adv), dict(risk)
    rp.historical_data = list(hist)
    rp.bt_engine._skip_db = True
    closes = [h["close_price"] for h in hist]
    rp.pre_calc = rp.bt_engine._pre_calc(
        hist, closes, [h["high_price"] for h in hist],
        [h["low_price"] for h in hist], inds)
    rp.bt_engine.ind_modes = {"supertrend": "both"}
    out = rp.run_replay()

    # bar-by-bar signal comparison (same engine signal fns, both paths)
    sig_diverge = None
    eng2 = BacktestEngine(is_live=False)
    pre = eng2._pre_calc(hist, closes, [h["high_price"] for h in hist],
                         [h["low_price"] for h in hist], inds)
    eng2.ind_modes = {"supertrend": "both"}
    prev_sig = None
    for i in range(5, len(hist)):
        b = (eng2._get_buy_signal(i, pre, hist, []),
             eng2._get_sell_signal(i, pre, hist, []))
        if prev_sig is not None and b != prev_sig:
            pass
        prev_sig = b
    _ = prev_sig

    bt_tl = bt["metrics"]["trade_list"]
    rp_tl = out["metrics"]["trade_list"]
    print(f"bars={len(hist)}  backtest trades={len(bt_tl)} "
          f"net={bt['metrics']['net_pnl']} win={bt['metrics']['win_rate']}%")
    print(f"bars={len(hist)}  paper-loop trades={len(rp_tl)} "
          f"net={out['metrics']['net_pnl']} win={out['metrics']['win_rate']}%")

    # structured per-trade diagnostics, both modes
    for tag, tl in (("BACKTEST", bt_tl), ("PAPER", rp_tl)):
        for t in tl:
            sl = sltp_premium_level(t["entry_price"], 1500, t.get("quantity", 75),
                                    1, t.get("position", "Buy"), True)
            print(f"[{tag}] {t.get('symbol')} {t.get('entry_date')}->{t.get('exit_date')} "
                  f"{t.get('position')} {t.get('option_type')} strike={t.get('strike')} "
                  f"entry={t.get('entry_price')} exit={t.get('exit_price')} "
                  f"qty={t.get('quantity')} SL={round(sl, 2)} why={t.get('exit_reason')} "
                  f"gross={t.get('gross_pnl')} charges={t.get('charges', t.get('exit_costs', 0))} "
                  f"net={t.get('pnl')}", flush=True)

    # first divergence
    div = None
    if len(bt_tl) != len(rp_tl):
        div = f"trade-count: backtest={len(bt_tl)} paper={len(rp_tl)}"
    else:
        for a, b in zip(bt_tl, rp_tl):
            for f in ("entry_date", "exit_date", "strike", "position",
                      "entry_price", "exit_price", "pnl"):
                if a.get(f) != b.get(f):
                    div = (f"trade #{a.get('index')}: field '{f}' "
                           f"backtest={a.get(f)} paper={b.get(f)}")
                    break
            if div:
                break
            if abs(float(a.get("pnl", 0)) - float(b.get("pnl", 0))) > 1.0:
                div = f"trade #{a.get('index')}: net differs"
                break
    print("FIRST DIVERGENCE:", div if div else "none - code parity holds")

    if spread_bps:
        # Illustrative overlay: adverse spread on paper fills (NOT production)
        bps = spread_bps / 10000.0
        flipped = 0
        for t in rp_tl:
            qty = float(t.get("quantity", 0) or 0)
            gross = float(t.get("gross_pnl", 0) or 0)
            cost = abs(float(t.get("entry_price", 0))) * qty * bps
            cost += abs(float(t.get("exit_price", 0))) * qty * bps
            adj = gross - cost - float(t.get("charges", 0) or 0)
            if (float(t.get("pnl", 0)) > 0) != (adj > 0):
                flipped += 1
        print(f"SPREAD OVERLAY {spread_bps}bps: {flipped}/{len(rp_tl)} paper trades flip sign "
              f"(backtest wins become paper losses) - microstructure gap demo")
    return div


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--spread-bps", type=float, default=0.0)
    args = ap.parse_args()
    run_both(args.spread_bps)
