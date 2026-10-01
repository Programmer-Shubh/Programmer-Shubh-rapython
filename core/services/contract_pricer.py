"""Single source of truth for per-contract option LTP.

Chain table and open-positions table MUST show the same price for the same
contract. Priority per (symbol, strike, CE/PE, expiry):
  1. Broker live quote keyed by the contract's EXACT instrument token
     (Dhan security_id via /marketfeed/quote, Angel symboltoken via /quote/).
     One batched HTTP call per symbol. Never the underlying spot.
  2. Fresh-today DB close (same IST date only - never stale EOD).
  3. Black-Scholes model with CURRENT model_iv + real DTE from expiry
     (identical formula for chain and positions - no entry-IV pin, no
     neighbour-strike substitution).

Caches: TOKENS 6h (tokens are static identifiers, not prices);
PRICES 10s (rate-limit courtesy, never served as "live" beyond that).
Every network hop is guarded - any failure falls through to the next tier.
"""
import json
import time

_TOKEN_CACHE = {}   # key -> (ts, value); 6h
_TOKEN_TTL = 6 * 3600
_PRICE_CACHE = {}   # key -> (ts, ltp); 10s
_PRICE_TTL = 10

_DHAN_BASE = "https://api.dhan.co/v2"
_ANGEL_BASE = "https://apiconnect.angelone.in"


def _ist_today():
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")


def _real_token(tok):
    return bool(tok) and not str(tok).startswith(("SIM-", "ANG-"))


def _broker_cfg(name):
    try:
        from core.models.database import Database
        row = Database.get_instance().fetch_one(
            "SELECT setting_value FROM settings WHERE setting_key=?", [f"broker_{name}"]
        )
        if row and row.get("setting_value"):
            cfg = json.loads(row["setting_value"])
            if isinstance(cfg, dict):
                cfg = {k: (v.strip() if isinstance(v, str) else v) for k, v in cfg.items()}
                try:
                    import os
                    if name == "dhan":
                        if os.environ.get("DHAN_CLIENT_ID"):
                            cfg["client_id"] = os.environ["DHAN_CLIENT_ID"]
                        if os.environ.get("DHAN_ACCESS_TOKEN"):
                            cfg["access_token"] = os.environ["DHAN_ACCESS_TOKEN"]
                    elif name == "angel":
                        for k, e in (("api_key", "ANGEL_API_KEY"), ("client_code", "ANGEL_CLIENT_CODE"),
                                     ("access_token", "ANGEL_ACCESS_TOKEN")):
                            if os.environ.get(e):
                                cfg[k] = os.environ[e]
                except Exception:
                    pass
                return cfg
    except Exception:
        pass
    return {}


# ---------------- Dhan: token resolve + batch LTP (sync) ----------------

def _dhan_post(path, token, client_id, payload, timeout=6):
    import requests
    r = requests.post(
        _DHAN_BASE + path,
        headers={"Content-Type": "application/json", "access-token": token, "client-id": client_id},
        json=payload, timeout=timeout,
    )
    if r.status_code != 200:
        return {}
    try:
        return r.json() or {}
    except Exception:
        return {}


def _dhan_tokens(symbol, expiry_ymd):
    """{ (strike, OPT) -> security_id } via Dhan optionchain. Cached 6h."""
    sym = (symbol or "").upper()
    exp = str(expiry_ymd or "")[:10]
    ckey = f"dhan_tok_{sym}_{exp}"
    hit = _TOKEN_CACHE.get(ckey)
    if hit and time.time() - hit[0] < _TOKEN_TTL:
        return hit[1]
    out = {}
    try:
        from core.services.broker_dhan_live import UNDERLYING
        info = UNDERLYING.get(sym)
        if not info:
            return out  # stock options: no index map; skip broker tier
        cfg = _broker_cfg("dhan")
        token = str(cfg.get("access_token", "") or "")
        cid = str(cfg.get("client_id", "") or "")
        if not _real_token(token) or not cid:
            return out
        scrip, seg = info
        exps = _dhan_post("/optionchain/expirylist", token, cid,
                          {"UnderlyingScrip": scrip, "UnderlyingSeg": seg})
        exp_data = (exps.get("data") or [])
        exp_list = exp_data if isinstance(exp_data, list) else (exp_data.get("data") or [])
        match = ""
        for e in exp_list if isinstance(exp_list, list) else []:
            es = str(e if isinstance(e, str) else e.get("expiry", e.get("date", "")))[:10]
            if es == exp:
                match = es
                break
        if not match:
            return out  # broker has no such expiry: fall through, never snap
        oc = _dhan_post("/optionchain", token, cid,
                        {"UnderlyingScrip": scrip, "UnderlyingSeg": seg, "Expiry": match})
        data = (oc.get("data") or {})
        strikes = data.get("oc") or data.get("strikes") or {}
        if isinstance(strikes, dict):
            for k, v in strikes.items():
                try:
                    strike = float(str(k).replace(",", ""))
                except Exception:
                    continue
                if not isinstance(v, dict):
                    continue
                for opt in ("CE", "PE"):
                    leg = v.get(opt.lower()) or v.get(opt) or {}
                    for key in ("security_id", "securityId", "instrument_token", "token"):
                        if leg.get(key):
                            out[(strike, opt)] = str(leg[key])
                            break
        if out:
            _TOKEN_CACHE[ckey] = (time.time(), out)
    except Exception:
        pass
    return out


def _dhan_batch_ltp(symbol, token_map):
    """{ (strike, OPT) -> ltp } via ONE /marketfeed/quote call. Strict parse."""
    res = {}
    try:
        if not token_map:
            return res
        cfg = _broker_cfg("dhan")
        token = str(cfg.get("access_token", "") or "")
        cid = str(cfg.get("client_id", "") or "")
        if not _real_token(token) or not cid:
            return res
        seg = "BSE_FNO" if (symbol or "").upper() in ("SENSEX", "BANKEX") else "NSE_FNO"
        ids = sorted(set(token_map.values()))
        q = _dhan_post("/marketfeed/quote", token, cid, {seg: [int(float(x)) if str(x).replace(".", "").isdigit() else x for x in ids]}, timeout=8)
        data = (q.get("data") or {})
        node = data.get(seg, data) if isinstance(data, dict) else {}
        if not isinstance(node, dict):
            return res
        rev = {str(v): k for k, v in token_map.items()}
        for sid, entry in node.items():
            key = rev.get(str(sid))
            if not key or not isinstance(entry, dict):
                continue
            for f in ("last_price", "LTP", "ltp", "lastPrice"):
                try:
                    v = float(entry.get(f, 0) or 0)
                except Exception:
                    v = 0
                if v > 0:
                    res[key] = v
                    break
    except Exception:
        pass
    return res


# ---------------- Angel: token resolve + batch LTP (sync, best-effort) ----------------

def _angel_tsym(symbol, expiry_ymd, strike, opt):
    try:
        from core.services.broker_angel_live import angel_tsym as _ts
        return _ts(symbol, expiry_ymd, strike, opt)
    except Exception:
        return ""


def _angel_batch_ltp(symbol, contracts, expiry_ymd):
    """contracts: [(strike, OPT)]. Returns {(strike, OPT): ltp}."""
    res = {}
    try:
        import requests
        cfg = _broker_cfg("angel")
        jwt = str(cfg.get("access_token") or cfg.get("jwt") or "")
        api_key = str(cfg.get("api_key", "") or "")
        if not _real_token(jwt) or not api_key:
            return res
        sym = (symbol or "").upper()
        want = {}
        for strike, opt in contracts:
            tsym = _angel_tsym(sym, expiry_ymd, strike, opt)
            if tsym:
                want[tsym] = (float(strike), opt)
        if not want:
            return res
        H = {"Content-Type": "application/json", "Accept": "application/json",
             "X-UserType": "USER", "X-SourceID": "WEB",
             "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
             "X-MACAddress": "00:00:00:00:00:00",
             "X-PrivateKey": api_key, "Authorization": f"Bearer {jwt}"}
        tokens = []
        for tsym, (strike, opt) in want.items():
            ckey = f"angel_tok_{tsym}"
            hit = _TOKEN_CACHE.get(ckey)
            if hit and time.time() - hit[0] < _TOKEN_TTL and hit[1]:
                tokens.append((tsym, hit[1], strike, opt))
                continue
            try:
                r = requests.post(_ANGEL_BASE + "/rest/secure/angelbroking/order/v1/searchScrip",
                                  headers=H, json={"exchange": "NFO", "searchscrip": tsym}, timeout=6)
                body = r.json() if r.status_code == 200 else {}
                items = (body.get("data") or []) if isinstance(body, dict) else []
                tok = ""
                for it in items if isinstance(items, list) else []:
                    if isinstance(it, dict) and str(it.get("tradingsymbol", "")).upper() == tsym and it.get("symboltoken"):
                        tok = str(it["symboltoken"])
                        break
                if tok:
                    _TOKEN_CACHE[ckey] = (time.time(), tok)
                    tokens.append((tsym, tok, strike, opt))
            except Exception:
                continue
        if not tokens:
            return res
        try:
            r = requests.post(_ANGEL_BASE + "/rest/secure/angelbroking/market/v1/quote/",
                              headers=H,
                              json={"mode": "LTP", "exchangeTokens": {"NFO": [t[1] for t in tokens]}},
                              timeout=8)
            body = r.json() if r.status_code == 200 else {}
        except Exception:
            return res
        data = (body.get("data") or {}) if isinstance(body, dict) else {}
        fetched = data.get("fetched") or []
        by_tok = {(t[1]): (t[2], t[3]) for t in tokens}
        for it in fetched if isinstance(fetched, list) else []:
            if not isinstance(it, dict):
                continue
            key = by_tok.get(str(it.get("symbolToken") or it.get("symboltoken") or ""))
            if not key:
                continue
            try:
                v = float(it.get("ltp") or it.get("lastTradedPrice") or 0)
            except Exception:
                v = 0
            if v > 0:
                res[key] = v
    except Exception:
        pass
    return res


# ---------------- shared fallback: fresh DB -> model ----------------

def _fallback_ltp(symbol, strike, option_type, expiry_ymd):
    sym = (symbol or "").upper()
    try:
        strike_f = float(strike)
    except Exception:
        return None
    # Fresh-today DB close only (same IST date; stale EOD never served as live)
    try:
        from core.models.database import Database
        db = Database.get_instance()
        row = db.fetch_one(
            "SELECT close_price FROM bhavcopy_data WHERE symbol=? AND strike_price=? AND option_type=? "
            "AND trade_date=?",
            [sym, strike_f, option_type, _ist_today()],
        )
        if row and row.get("close_price") and float(row["close_price"]) > 0:
            return float(row["close_price"])
    except Exception:
        pass
    # Model with CURRENT iv + real DTE (same formula chain and positions share)
    try:
        from utils.helpers import model_premium, model_iv
        from core.services.live_market_data import LiveMarketData
        spot = 0
        try:
            sp = LiveMarketData().get_live_spot(sym)
            if sp and sp.get("spot"):
                spot = float(sp["spot"])
        except Exception:
            pass
        if spot <= 0:
            return None
        import datetime as _dt
        dte = 7
        if expiry_ymd:
            try:
                exp_dt = _dt.datetime.strptime(str(expiry_ymd)[:10], "%Y-%m-%d")
                dte = max(1, (exp_dt - _dt.datetime.now()).days)
            except Exception:
                dte = 7
        bs = model_premium(spot, strike_f, dte, option_type, symbol=sym, iv=model_iv(sym))
        return round(float(bs), 2) if bs and float(bs) > 0 else None
    except Exception:
        return None


# ---------------- Tier 0: NSE full-chain map (local only, expiry-matched) ----------------

def _nse_map(symbol):
    """(expiry_ymd, {(strike, OPT): ltp}) from one NSE fetch. Cached 10s.
    Returns ('', {}) on cloud (NSE IP-blocked) or any failure."""
    sym = (symbol or "").upper()
    ck = f"nse_map_{sym}"
    now = time.time()
    hit = _PRICE_CACHE.get(ck)
    if hit and now - hit[0] < _PRICE_TTL:
        return hit[1]
    out = ("", {})
    try:
        from core.services.nse_client import is_cloud as _is_cloud, nse_fetch_option_chain_v3
        if _is_cloud():
            return out
        nse = nse_fetch_option_chain_v3(sym, timeout=6)
        if nse and nse.get("rows"):
            m = {}
            for r in nse["rows"]:
                try:
                    s = float(r.get("strike", 0) or 0)
                except Exception:
                    continue
                for opt, k in (("CE", "ce_ltp"), ("PE", "pe_ltp")):
                    try:
                        v = float(r.get(k, 0) or 0)
                    except Exception:
                        v = 0
                    if v > 0:
                        m[(s, opt)] = v
            out = (str(nse.get("expiry", ""))[:10], m)
    except Exception:
        pass
    _PRICE_CACHE[ck] = (now, out)
    return out


# ---------------- public API ----------------

def get_contract_ltps(symbol, contracts, expiry_ymd="", fresh=False):
    """Batch: contracts=[(strike, OPT)]. Returns {(strike, OPT): ltp} for hits.
    Tier 1 broker-token quotes, Tier 2/3 shared fallback per missing contract.
    fresh=True skips the 10s price-cache READ (open positions always want the
    live tick; cache is still written)."""
    sym = (symbol or "").upper()
    norm = []
    for s, o in (contracts or []):
        try:
            norm.append((float(s), "CE" if str(o or "CE").upper().startswith("C") else "PE"))
        except Exception:
            continue
    if not norm:
        return {}
    # Fresh price cache first (10s) - skipped when fresh=True
    out = {}
    missing = []
    now = time.time()
    for key in norm:
        pk = f"{sym}_{key[0]}_{key[1]}_{str(expiry_ymd or '')[:10]}"
        hit = None if fresh else _PRICE_CACHE.get(pk)
        if hit and now - hit[0] < _PRICE_TTL:
            out[key] = hit[1]
        else:
            missing.append(key)
    if missing:
        live = {}
        # Tier 0: NSE chain map, ONLY when its expiry == requested expiry
        # (a weekly LTP must never price a monthly contract).
        try:
            _nexp, _nmap = _nse_map(sym)
            if _nmap and _nexp and str(expiry_ymd or "")[:10] == _nexp:
                for key in missing:
                    if key in _nmap:
                        live[key] = _nmap[key]
        except Exception:
            pass
        # Tier 1a: Dhan token batch (index underlyings).
        # Streaming first: subscribe contracts on the WS feed and read live
        # ticks; REST quote only covers contracts with no fresh tick.
        try:
            tmap = _dhan_tokens(sym, expiry_ymd) if expiry_ymd else {}
            if tmap:
                want = {k: tmap[k] for k in missing if k in tmap}
                try:
                    from core.services.dhan_feed import get_feed, ensure_feed_from_config
                    _fd = ensure_feed_from_config() or get_feed()
                    _seg = "BSE_FNO" if sym in ("SENSEX", "BANKEX") else "NSE_FNO"
                    for _k, _sid in want.items():
                        _fd.subscribe(_seg, _sid)
                    for _k, _sid in want.items():
                        try:
                            _fl = _fd.get_ltp(_seg, _sid)
                        except Exception:
                            _fl = 0
                        if _fl and float(_fl) > 0:
                            live[_k] = round(float(_fl), 2)
                except Exception:
                    pass
                want_rest = {k: v for k, v in want.items() if k not in live}
                live.update(_dhan_batch_ltp(sym, want_rest))
        except Exception:
            pass
        # Tier 1b: Angel token batch (anything with NFO symbol)
        try:
            still = [k for k in missing if k not in live]
            if still:
                live.update(_angel_batch_ltp(sym, still, expiry_ymd))
        except Exception:
            pass
        for key in missing:
            if key in live and live[key] and float(live[key]) > 0:
                out[key] = round(float(live[key]), 2)
            else:
                fb = _fallback_ltp(sym, key[0], key[1], expiry_ymd)
                if fb and float(fb) > 0:
                    out[key] = round(float(fb), 2)
        # Stamp price cache
        for key, val in out.items():
            if key in missing:
                pk = f"{sym}_{key[0]}_{key[1]}_{str(expiry_ymd or '')[:10]}"
                _PRICE_CACHE[pk] = (now, val)
    return out


def get_contract_ltp(symbol, strike, option_type, expiry_ymd="", fresh=False):
    """Single-contract wrapper. Returns float or None."""
    try:
        res = get_contract_ltps(symbol, [(strike, option_type)], expiry_ymd, fresh=fresh)
        for v in res.values():
            return float(v)
    except Exception:
        pass
    return None
