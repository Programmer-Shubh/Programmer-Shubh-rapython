"""Tick-level breakout engine on Dhan WebSocket ticks (1m LIVE signals).

How it works: DhanFeed streams per-contract ticks {(seg, secid): ltp, ts}.
This engine watches tick VELOCITY per key: break of the trailing-20-tick
high/low WITH a minimum % move inside 5 minutes = momentum ignition.
No candles, no fabrication — pure tick momentum, honestly tagged "(1m LIVE)".

Mapping to symbols: index underlyings via UNDERLYING reverse map; option
contracts via contract_pricer token-cache reverse map (best effort).
Cooldown 15 min per symbol+side so one burst = one signal, not spam.

Idle without a connected feed (no ticks -> no signals, never blank-fill).
Everything guarded; used by scan_intraday merge + /api/broker/dhan-feed.
"""
import time

_LOOKBACK_TICKS = 20
_WINDOW_S = 300
_MOVE_PCT_INDEX = 0.002   # 0.20% for indices
_MOVE_PCT_STOCK = 0.003   # 0.30% for stocks/contracts
_COOLDOWN_S = 900

_hist = {}       # (seg, secid) -> [(ts, ltp), ...] (cap 120)
_cool = {}       # (symbol, side) -> ts of last emitted signal


def _feed_ticks():
    try:
        from core.services.dhan_feed import get_feed
        return dict(get_feed()._ticks or {})
    except Exception:
        return {}


def _resolve_symbol(seg, secid):
    """(seg, secid) -> (symbol, kind). kind in index/contract/unknown."""
    try:
        from core.services.broker_dhan_live import UNDERLYING
        for sym, (scrip, sg) in UNDERLYING.items():
            if str(scrip) == str(secid) and sg == seg:
                return sym, "index"
    except Exception:
        pass
    try:
        from core.services import contract_pricer as _cp
        for ck, (ts, tokmap) in list(_cp._TOKEN_CACHE.items()):
            if not isinstance(tokmap, dict):
                continue
            for (strike, opt), sid in tokmap.items():
                if str(sid) == str(secid):
                    # ckey: dhan_tok_{SYM}_{EXP}
                    parts = str(ck).split("_")
                    sym = parts[2] if len(parts) > 2 else ""
                    if sym:
                        return sym, "contract"
    except Exception:
        pass
    return "", "unknown"


def _spot_for(symbol):
    try:
        from core.services.live_market_data import _LIVE_CACHE
        e = _LIVE_CACHE.get((symbol or "").upper())
        if e and time.time() - e.get("ts", 0) < 300:
            v = float((e.get("data") or {}).get("spot") or 0)
            if v > 0:
                return v
    except Exception:
        pass
    return 0


def get_signals(max_age_tick=20):
    """Evaluate tick velocity now. Returns [{symbol, side, move_pct,
    reason, tf, ts}]. side in bullish/bearish. Empty when feed idle."""
    out = []
    now = time.time()
    try:
        ticks = _feed_ticks()
        if not ticks:
            return out
        for (seg, secid), t in ticks.items():
            try:
                ltp = float(t.get("ltp") or 0)
                ts = float(t.get("ts") or 0)
            except Exception:
                continue
            if ltp <= 0 or now - ts > max_age_tick:
                continue  # tick itself must be live, else no "turant" claim
            key = (seg, secid)
            hist = _hist.get(key) or []
            hist.append((ts, ltp))
            hist = [(a, b) for a, b in hist if now - a <= _WINDOW_S][-120:]
            _hist[key] = hist
            if len(hist) < _LOOKBACK_TICKS + 1:
                continue
            prev = [b for _, b in hist[:-1][-_LOOKBACK_TICKS:]]
            try:
                hi, lo = max(prev), min(prev)
            except Exception:
                continue
            if hi <= 0 or lo <= 0:
                continue
            symbol, kind = _resolve_symbol(seg, secid)
            if not symbol:
                continue
            thresh = _MOVE_PCT_INDEX if kind == "index" else _MOVE_PCT_STOCK
            side, move = "", 0.0
            if ltp > hi and (ltp - hi) / hi >= thresh:
                side, move = "bullish", (ltp - lo) / lo
            elif ltp < lo and (lo - ltp) / lo >= thresh:
                side, move = "bearish", (hi - ltp) / hi
            if not side:
                continue
            ck = (symbol, side)
            if now - _cool.get(ck, 0) < _COOLDOWN_S:
                continue
            _cool[ck] = now
            out.append({
                "symbol": symbol, "side": side,
                "move_pct": round(move * 100, 2),
                "ltp": round(ltp, 2),
                "reason": f"(1m LIVE) Tick breakout {move*100:+.2f}% in 5 min ({kind})",
                "tf": "1m", "ts": int(now),
            })
    except Exception:
        pass
    return out


def state():
    try:
        return {"tracked_keys": len(_hist),
                "cooldowns": len(_cool),
                "feed_ticks": len(_feed_ticks())}
    except Exception:
        return {"tracked_keys": 0}
