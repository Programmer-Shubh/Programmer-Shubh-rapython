"""Single source of truth for per-contract option LTP.

Chain table and open-positions table MUST show the same price for the same
contract. Priority per (symbol, strike, CE/PE, expiry):
  1. Broker live quote keyed by the contract's EXACT instrument token
     (Fyers contract symbol via /api/v3/quotes or /api/v3/options-chain,
     Angel symboltoken via /quote/). One batched HTTP call per symbol.
     Never the underlying spot.
  2. Fresh-today DB close (same IST date only - never stale EOD).
  3. Black-Scholes model with CURRENT model_iv + real DTE from expiry
     (identical formula for chain and positions - no entry-IV pin, no
     neighbour-strike substitution).

Paper AND live orders price off THIS module (entry_price()), so a paper
trade and a live trade of the same contract can never drift apart - they
quote the same broker tick through the same slippage model.

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

_ANGEL_BASE = "https://apiconnect.angelone.in"


def _ist_today():
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")


# Weekly expiry WEEKDAY rule (NSE 2025+ spec): NSE weeklies = Tuesday
# (weekday 1); BSE (SENSEX/BANKEX) = Thursday (weekday 3). Same rule as
# routes/broker._resolve_expiry - paper, live aur backtest sab ekahi rule.
def _is_index(symbol: str = "") -> bool:
    """Kya is symbol ke weekly (index-style) contracts hain.
    NIFTY/SENSEX/BANKEX par hi weeklies; BANKNIFTY/FINNIFTY/stocks MONTHLY."""
    try:
        from core.services.symbol_resolver import has_weekly_options
        return bool(has_weekly_options(symbol))
    except Exception:
        return str(symbol or "").upper().replace(" ", "") in (
            "NIFTY", "SENSEX", "BANKEX")


def _weekly_weekday(symbol: str = "") -> int:
    return 3 if str(symbol or "").upper().replace(" ", "") in ("SENSEX", "BANKEX") else 1


def next_weekly_on_or_after(symbol: str = "", from_ymd: str = "") -> str:
    """Pehla REAL weekly expiry date (>= from_ymd, exchange weekday).
    Fallback jab broker/NSE expiry list available na ho - phantom
    'today+7' (10-22 + 7 = 10-29 Thursday, jo kabhi expire hota hi nahi
    tha) ki jagah. Caller normally from_ymd=tomorrow bhejta hai."""
    try:
        import datetime as _dt
        base = str(from_ymd or _ist_today())[:10]
        d = _dt.datetime.strptime(base, "%Y-%m-%d").date()
        wd = _weekly_weekday(symbol)
        for _ in range(35):
            if d.weekday() == wd:
                return d.strftime("%Y-%m-%d")
            d += _dt.timedelta(days=1)
    except Exception:
        pass
    return ""


def _weekly_on_or_before(symbol: str = "", upto_ymd: str = "") -> str:
    """Pichla REAL weekly expiry (<= upto_ymd, exchange weekday)."""
    try:
        import datetime as _dt
        base = str(upto_ymd or _ist_today())[:10]
        d = _dt.datetime.strptime(base, "%Y-%m-%d").date()
        wd = _weekly_weekday(symbol)
        for _ in range(35):
            if d.weekday() == wd:
                return d.strftime("%Y-%m-%d")
            d -= _dt.timedelta(days=1)
    except Exception:
        pass
    return ""


def _snap_future_expiry(symbol: str, ymd: str) -> str:
    """Future explicit date jo REAL expiry nahi hai -> nearest real weekly
    expiry (same weekday, pehle/baad me jo zyada paas ho; barabar par
    pehle wali). Ye paper, live aur backtest ko ek hi contract par le
    aata hai - 10-29 (Thursday) jaisi ghayr-existent dates katam."""
    try:
        import datetime as _dt
        d = _dt.datetime.strptime(str(ymd or "")[:10], "%Y-%m-%d").date()
    except Exception:
        return str(ymd or "")
    wd = _weekly_weekday(symbol)
    txt = d.strftime("%Y-%m-%d")
    if d.weekday() == wd:
        return txt
    prev = _weekly_on_or_before(symbol, txt)
    nxt = next_weekly_on_or_after(symbol, txt)
    try:
        _d0 = _dt.datetime.strptime(txt, "%Y-%m-%d").date()
        opts = [(abs((_dt.datetime.strptime(x, "%Y-%m-%d").date() - _d0).days), i, x)
                for i, x in enumerate((prev, nxt)) if x]
        if not opts:
            return str(ymd)[:10]
        opts.sort()
        # tie -> earlier date (dono options me pehla index = prev)
        return opts[0][2]
    except Exception:
        return (prev or nxt) or str(ymd)[:10]


def resolve_weekly_expiry(symbol: str = "") -> str:
    """Next expiry YYYY-MM-DD shared by scanner suggestion AND order
    entry (pehle dono alag-alag expiry/DTE par price karte the - suggestion
    DTE=7 par >=20 dikhata, order real DTE=2 par <20 nikalta -> floor error).
    Priority: the CONNECTED BROKER's live expiry list (exactly the date a
    live order will be routed to - paper & live same contract), else the NSE
    map, else the REAL weekday rule (Tue NSE / Thu BSE). Phantom
    'today+7' fallback hata diya - 10-29 jaisi fake expiry par paper
    entries model-price par lagti thi aur backtest se door bhaagti thi.

    STOCK options: koi weekly expiry nahi - sirf monthly (last Tuesday).
    Yahi shared date paper, chain aur live order teeno use karte hain."""
    try:
        from core.services.symbol_resolver import stock_monthly_expiry, has_weekly_options
        if not has_weekly_options(symbol):
            _se = stock_monthly_expiry(symbol)
            if _se:
                return _se[:10]
    except Exception:
        pass
    try:
        from core.services.symbol_resolver import _nearest_on_or_after
        _exps = _fyers_expiry_list()
        if _exps:
            _snap = _nearest_on_or_after(_exps)
            if _snap:
                return _snap[:10]
    except Exception:
        pass
    try:
        _ne, _nm = _nse_map((symbol or "").upper())
        try:
            from routes.strategies import _norm_exp as _nxnorm
            _ne = _nxnorm(_ne)
        except Exception:
            pass
        if _ne and _ne >= _ist_today():
            return _ne[:10]
    except Exception:
        pass
    try:
        import datetime as _dt
        _tom = (_dt.datetime.strptime(_ist_today(), "%Y-%m-%d")
                + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        return next_weekly_on_or_after(symbol, _tom)
    except Exception:
        return ""


def resolve_expiry_hint(symbol: str = "", hint: str = "") -> str:
    """Normalise ANY expiry hint to a concrete YYYY-MM-DD expiry date.

    'weekly' / '' / 'live' / 'today' -> the live weekly expiry (same date the
    live order routes to, so paper and live price the SAME contract).
    'monthly'                      -> last weekly expiry of the month.
    'YYYY-MM-DD'                   -> kept as-is (explicit date)."""
    h = str(hint or "").strip()
    try:
        if h and h[:10].count("-") == 2:
            import datetime as _dt
            _d = _dt.datetime.strptime(h[:10], "%Y-%m-%d").date()
            if _d >= _dt.datetime.strptime(_ist_today(), "%Y-%m-%d").date():
                # STOCK: koi bhi date -> us month ka REAL monthly expiry
                # (weekly weekday snap stock par invalid contract banata tha).
                if not _is_index(symbol):
                    from core.services.symbol_resolver import stock_monthly_expiry
                    _se = stock_monthly_expiry(symbol, _d.strftime("%Y-%m-%d"))
                    if _se:
                        return _se[:10]
                # Explicit date ka snap guard: date jo is symbol ki REAL
                # exchange expiry weekday par na ho (10-29 Thursday NIFTY ho
                # hi nahi sakta) -> nearest real weekly par le jao. Warna
                # phantom contract par broker/NSE quotes aate hi nahi aur
                # paper model-price par lag jata tha (backtest se gap).
                snapped = _snap_future_expiry(symbol, _d.strftime("%Y-%m-%d"))
                return snapped
    except Exception:
        pass
    if h.lower().startswith("month"):
        if not _is_index(symbol):
            try:
                from core.services.symbol_resolver import stock_monthly_expiry
                _se = stock_monthly_expiry(symbol)
                if _se:
                    return _se[:10]
            except Exception:
                pass
        try:
            import calendar as _cal
            import datetime as _dt
            _today = _dt.datetime.strptime(_ist_today(), "%Y-%m-%d").date()
            _wd = 3 if (symbol or "").upper() == "SENSEX" else 1
            _last = _dt.date(_today.year, _today.month, _cal.monthrange(_today.year, _today.month)[1])
            while _last.weekday() != _wd:
                _last -= _dt.timedelta(days=1)
            if _last < _today:
                _m = _today.month + 1 if _today.month < 12 else 1
                _y = _today.year if _today.month < 12 else _today.year + 1
                _last = _dt.date(_y, _m, _cal.monthrange(_y, _m)[1])
                while _last.weekday() != _wd:
                    _last -= _dt.timedelta(days=1)
            return _last.strftime("%Y-%m-%d")
        except Exception:
            pass
    return resolve_weekly_expiry(symbol)


def dte_to_expiry(expiry_ymd: str, default: int = 7, cap: int = 45) -> int:
    """Real calendar days from NOW to expiry (min 1). Scanner suggestion aur
    order entry dono yahi DTE use karte hain - model premium identical."""
    try:
        import datetime as _dt
        _exp = str(expiry_ymd or "").strip()[:10]
        if _exp and "-" in _exp:
            _ed = _dt.datetime.strptime(_exp, "%Y-%m-%d")
            _diff = (_ed - _dt.datetime.now()).days
            if _diff > 30:
                return int(default)
            return max(1, min(int(cap), _diff))
    except Exception:
        pass
    return int(default)


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
                # DB wins (live truth); env only seeds keys the DB lacks.
                try:
                    import os
                    _seeds = {}
                    if name == "fyers":
                        for k, e in (("app_id", "RATRADE_FYERS_APP_ID"),
                                     ("secret", "RATRADE_FYERS_SECRET"),
                                     ("redirect_uri", "RATRADE_FYERS_REDIRECT_URI"),
                                     ("pin", "RATRADE_FYERS_PIN")):
                            if os.environ.get(e):
                                _seeds[k] = os.environ[e]
                    elif name == "angel":
                        for k, e in (("api_key", "ANGEL_API_KEY"), ("client_code", "ANGEL_CLIENT_CODE"),
                                     ("access_token", "ANGEL_ACCESS_TOKEN")):
                            if os.environ.get(e):
                                _seeds[k] = os.environ[e]
                    for k, v in _seeds.items():
                        if v and not cfg.get(k):
                            cfg[k] = v
                except Exception:
                    pass
                return cfg
    except Exception:
        pass
    return {}


# ---------------- Fyers: batch LTP (sync, best-effort) ----------------

def _fyers_auth():
    """(app_id, access_token) from the stored Fyers config, '' if not linked."""
    try:
        cfg = _broker_cfg("fyers") or {}
        app_id = str(cfg.get("app_id") or "").strip()
        token = str(cfg.get("access_token") or "").strip()
        if not app_id or not _real_token(token):
            return "", ""
        return app_id, token
    except Exception:
        return "", ""


def _fyers_headers(app_id, token):
    return {
        "Authorization": f"{app_id}:{token}" if app_id else token,
        "Content-Type": "application/json",
        # Cloudflare (Fyers data host) default python-requests UA ko 429/block
        # kar deta hai - browser UA zaroori hai, warna /data/quotes dead.
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "version": "3",
    }


def _fyers_underlying(symbol: str) -> str:
    """Fyers index instrument symbol (NSE:NIFTY50-INDEX etc)."""
    try:
        from core.services.symbol_resolver import FYERS_UNDERLYING
        return FYERS_UNDERLYING.get((symbol or "").upper(), "")
    except Exception:
        return ""


def _fyers_expiry_param(expiry_ymd: str) -> str:
    """Fyers options-chain expiry param (dd-MMM-yyyy, e.g. 22-OCT-2026)."""
    try:
        import datetime as _dt
        d = _dt.datetime.strptime(str(expiry_ymd or "")[:10], "%Y-%m-%d").date()
        return d.strftime("%d-%b-%Y").upper()
    except Exception:
        return str(expiry_ymd or "")[:10]


def _fyers_expiry_list() -> list:
    """Live Fyers expiry list (YYYY-MM-DD), sync + best-effort.

    This is the SAME expiry the live order is routed to - so the paper trade
    and the live trade of a strategy hit the SAME contract (same LTP)."""
    app_id, token = _fyers_auth()
    if not app_id or not token:
        return []
    body = {}
    for _p in ("/api/v3/options-chain?symbol=NSE:NIFTY50-INDEX&strikecount=1",
               "/data/options-chain?symbol=NSE:NIFTY50-INDEX&strikecount=1"):
        body = _fyers_get(_p, app_id, token)
        if body:
            break
    if not body:
        return []
    data = body.get("data") or {}
    raw = data.get("expiryData") or data.get("expiry_dates") or []
    out = []
    import datetime as _dt
    for e in raw if isinstance(raw, list) else []:
        val = ""
        if isinstance(e, str):
            val = e
        elif isinstance(e, dict):
            for k in ("date", "expiry", "expiryDate"):
                if e.get(k):
                    val = str(e[k])
                    break
        if not val:
            continue
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y", "%d-%B-%Y", "%d-%b-%y"):
            try:
                out.append(_dt.datetime.strptime(val[:10], fmt).strftime("%Y-%m-%d"))
                break
            except Exception:
                continue
    return sorted(set(out))


def _fyers_get(path: str, app_id: str, token: str, timeout: int = 8):
    """GET on Fyers v3 host. Returns parsed json dict or {}.

    429 (rate limit / Cloudflare) par ek chhota retry - warna ek hi burst
    me poora quote tier chhoot jata tha aur order model price par chala
    jata tha."""
    try:
        import requests
        url = f"https://api-t1.fyers.in{path}"
        r = requests.get(url, headers=_fyers_headers(app_id, token), timeout=timeout)
        for _w in (0.4, 0.9):
            if getattr(r, "status_code", 0) != 429:
                break
            try:
                time.sleep(_w)
            except Exception:
                pass
            r = requests.get(url, headers=_fyers_headers(app_id, token), timeout=timeout)
        if getattr(r, "status_code", 0) != 200:
            return {}
        body = r.json() or {}
    except Exception:
        return {}
    if not isinstance(body, dict):
        return {}
    s = str(body.get("s") or "").strip().lower()
    if s and s not in ("ok", "success"):
        return {}
    return body


def _fyers_chain_ltp(symbol, expiry_ymd, contracts):
    """{(strike, OPT): ltp} from ONE Fyers options-chain call (index underlyings)."""
    res = {}
    try:
        app_id, token = _fyers_auth()
        if not app_id or not token:
            return res
        under = _fyers_underlying(symbol)
        if not under:
            return res
        exp = _fyers_expiry_param(expiry_ymd)
        body = {}
        for _p in (f"/api/v3/options-chain?symbol={under}&strikecount=100&expiry={exp}",
                   f"/data/options-chain?symbol={under}&strikecount=100&expiry={exp}",
                   f"/api/v3/options-chain?symbol={under}&strikecount=100",
                   f"/data/options-chain?symbol={under}&strikecount=100"):
            # some deployments answer the chain without an expiry filter
            body = _fyers_get(_p, app_id, token)
            if body:
                break
        if not body:
            return res
        data = body.get("data") or {}
        records = data.get("records") or {}
        rows = records.get("data") if isinstance(records, dict) else records
        want = {(float(s), "CE" if str(o).upper().startswith("C") else "PE")
                for s, o in (contracts or [])}
        for row in (rows if isinstance(rows, list) else []):
            if not isinstance(row, dict):
                continue
            try:
                strike = float(row.get("strikePrice", row.get("strike_price", 0)) or 0)
            except Exception:
                continue
            if strike <= 0:
                continue
            for opt in ("CE", "PE"):
                key = (strike, opt)
                if key not in want or key in res:
                    continue
                leg = row.get(opt) or row.get(opt.lower()) or {}
                if not isinstance(leg, dict):
                    continue
                for f in ("lastPrice", "last_price", "ltp", "LTP"):
                    try:
                        v = float(leg.get(f, 0) or 0)
                    except Exception:
                        v = 0
                    if v > 0:
                        res[key] = v
                        break
    except Exception:
        pass
    return res


_FYERS_QUOTE_BASES = ("/data/quotes?symbols=", "/api/v3/quotes?symbols=",
                      "/api/v2/quotes?symbols=")


def _fyers_quotes_raw(symbols_csv: str, app_id: str = "", token: str = ""):
    """Fyers quote call with host-path fallback.

    MARKET DATA v3 Fyers par `/data/quotes` par hai; `/api/v3/quotes` 404
    deta hai. Isi 404 ki wajah se stock options ka koi quote tier kaam nahi
    kar raha tha -> shared pricer MODEL (floor Rs 1.5) par chala jata tha
    aur live LIMIT Rs 1.65 banta tha (asli premium Rs 40+)."""
    if not app_id or not token:
        app_id, token = _fyers_auth()
    if not app_id or not token:
        return {}
    for _base in _FYERS_QUOTE_BASES:
        body = _fyers_get(_base + symbols_csv, app_id, token)
        if body:
            return body
    return {}


def _fy_quote_rows(body):
    """Fyers quote response ko normalise karo -> {symbol: node}.

    Fyers v3 ACTUAL shape: {"d": [{"n": "NSE:X", "v": {...}, "s": "ok"}]}.
    Purana/alt shape: {"data": {"NSE:X": {...}}}. Dono support - pehle sirf
    `data` padha jata tha, isliye har stock-option quote "empty" aata tha
    aur pricer model floor (Rs 1.5) par chala jata tha."""
    out = {}
    if not isinstance(body, dict):
        return out
    data = body.get("data")
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                out[str(k)] = v
    rows = body.get("d")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("n") or row.get("symbol") or row.get("name")
            node = row.get("v") or row.get("values") or row.get("data")
            if name and isinstance(node, dict):
                out[str(name)] = node
    return out


_TOPBOOK_CACHE = {}
_TOPBOOK_TTL = 5.0


def top_of_book(symbol, strike, option_type, expiry_ymd):
    """Fyers top-of-book: {"ltp": x, "bid": b, "ask": a} ya {} (best-effort).

    Live order ka DEPTH-BASED cross price (BUY=best ask, SELL=best bid) isi
    se banta hai - plan Step-3. 5s cache: IOC retry loop same quote use
    kare, fyers API par 429 na aaye."""
    try:
        from core.services.symbol_resolver import _fyers_symbol as _fsym
        fs = _fsym(symbol, expiry_ymd, float(strike),
                   "CE" if str(option_type or "CE").upper().startswith("C") else "PE")
    except Exception:
        return {}
    if not fs:
        return {}
    hit = _TOPBOOK_CACHE.get(fs)
    now = time.time()
    if hit and now - hit[0] < _TOPBOOK_TTL:
        return hit[1]
    out = {}
    try:
        app_id, token = _fyers_auth()
        if app_id and token:
            body = _fyers_quotes_raw(fs, app_id, token)
            node = _fy_quote_rows(body).get(fs) or {}
            if isinstance(node, dict):
                for f in ("lp", "last_price", "lastPrice", "ltp"):
                    try:
                        v = float(node.get(f, 0) or 0)
                    except Exception:
                        v = 0
                    if v > 0:
                        out["ltp"] = v
                        break
                for f in ("bid", "bid_price", "bidPrice"):
                    try:
                        v = float(node.get(f, 0) or 0)
                    except Exception:
                        v = 0
                    if v > 0 and out.get("ltp") and v <= out["ltp"] * 3.0 + 0.5:
                        out["bid"] = v
                        break
                for f in ("ask", "ask_price", "askPrice"):
                    try:
                        v = float(node.get(f, 0) or 0)
                    except Exception:
                        v = 0
                    if v > 0 and out.get("ltp") and v <= out["ltp"] * 3.0 + 0.5:
                        out["ask"] = v
                        break
    except Exception:
        return {}
    _TOPBOOK_CACHE[fs] = (now, out)
    return out


def _fyers_quote_ltp(symbol, expiry_ymd, contracts):
    """{(strike, OPT): ltp} from Fyers /quotes for the EXACT contract symbols
    (works for stock options too, where no index chain exists)."""
    res = {}
    try:
        app_id, token = _fyers_auth()
        if not app_id or not token:
            return res
        from core.services.symbol_resolver import _fyers_symbol as _fsym
        syms = {}
        for (strike, opt) in (contracts or []):
            try:
                fs = _fsym(symbol, expiry_ymd, float(strike), opt)
            except Exception:
                continue
            if fs:
                syms[fs] = (float(strike), opt)
        if not syms:
            return res
        keys = list(syms.keys())
        for _i in range(0, len(keys), 20):          # chunked (URL length safe)
            chunk = keys[_i:_i + 20]
            body = _fyers_quotes_raw(",".join(chunk), app_id, token)
            quotes = _fy_quote_rows(body)
            if not quotes:
                continue
            for fs, key in syms.items():
                node = quotes.get(fs) or quotes.get(fs.replace("NSE:", "")) or {}
                if not isinstance(node, dict):
                    continue
                for f in ("lp", "last_price", "lastPrice", "ltp", "LTP"):
                    try:
                        v = float(node.get(f, 0) or 0)
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

def _fallback_ltp(symbol, strike, option_type, expiry_ymd, with_source=False):
    """Fresh-today DB close -> Black-Scholes model.

    with_source=True returns (ltp, source) where source is
    'db-close' | 'model' (never None when a price was found)."""
    sym = (symbol or "").upper()
    try:
        strike_f = float(strike)
    except Exception:
        return (None, "none") if with_source else None
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
            return (float(row["close_price"]), "db-close") if with_source else float(row["close_price"])
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
            return (None, "none") if with_source else None
        import datetime as _dt
        dte = 7
        if expiry_ymd:
            try:
                exp_dt = _dt.datetime.strptime(str(expiry_ymd)[:10], "%Y-%m-%d")
                dte = max(1, (exp_dt - _dt.datetime.now()).days)
            except Exception:
                dte = 7
        bs = model_premium(spot, strike_f, dte, option_type, symbol=sym, iv=model_iv(sym))
        if bs and float(bs) > 0:
            return (round(float(bs), 2), "model") if with_source else round(float(bs), 2)
        return (None, "none") if with_source else None
    except Exception:
        return (None, "none") if with_source else None


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

def get_contract_ltps(symbol, contracts, expiry_ymd="", fresh=False, sources=None):
    """Batch: contracts=[(strike, OPT)]. Returns {(strike, OPT): ltp} for hits.
    Tier 1 broker-token quotes, Tier 2/3 shared fallback per missing contract.
    fresh=True skips the 10s price-cache READ (open positions always want the
    live tick; cache is still written).
    sources: optional dict - filled with the tier that produced each price
    ('broker-quote' | 'nse-chain' | 'db-close' | 'model' | 'cache')."""
    sym = (symbol or "").upper()
    # HEAL (27-10-2026): stock/BANKNIFTY (non-weekly) par purane records me
    # phantom weekly dates thi (BANKNIFTY 2026-11-03 - contract exist hi nahi
    # karta, quote khali). Us month ke REAL monthly expiry par map hote hai
    # (26NOV symbol) -> OPEN paper positions ke current price bhi aaj se
    # real broker quote se.
    try:
        from core.services.symbol_resolver import stock_monthly_expiry, has_weekly_options
        if not has_weekly_options(sym):
            expiry_ymd = stock_monthly_expiry(sym, expiry_ymd) or expiry_ymd
    except Exception:
        pass
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
            if sources is not None:
                sources[key] = "cache"
        else:
            missing.append(key)
    if missing:
        live = {}
        _tier = {}          # key -> source tier
        # Tier 0: NSE chain map, ONLY when its expiry == requested expiry
        # (a weekly LTP must never price a monthly contract).
        try:
            _nexp, _nmap = _nse_map(sym)
            if _nmap and _nexp and str(expiry_ymd or "")[:10] == _nexp:
                for key in missing:
                    if key in _nmap:
                        live[key] = _nmap[key]
                        _tier[key] = "nse-chain"
        except Exception:
            pass
        # Tier 1a: Fyers broker quote (exact contract LTP). Paper AND live
        # both read this tier, so a paper trade and a live trade of the same
        # contract land on the SAME price.
        try:
            _need = [k for k in missing if k not in live]
            if _need:
                _fc = _fyers_chain_ltp(sym, expiry_ymd, _need)
                for _k, _v in _fc.items():
                    live[_k] = _v
                    _tier[_k] = "broker-quote"
                _need2 = [k for k in _need if k not in live]
                if _need2:
                    _fq = _fyers_quote_ltp(sym, expiry_ymd, _need2)
                    for _k, _v in _fq.items():
                        live[_k] = _v
                        _tier[_k] = "broker-quote"
        except Exception:
            pass
        # Tier 1c: NSE live option chain (index options) - real LTP bina
        # kisi broker data-subscription ke.
        try:
            _nse_missing = [k for k in missing if k not in live]
            if _nse_missing:
                from core.services.free_data import nse_chain_ltp as _nlt
                for _k in _nse_missing[:8]:  # limited: NSE fetch slow/guarded
                    _v = _nlt(sym, _k[0], _k[1], expiry_ymd)
                    if _v and _v > 0:
                        live[_k] = _v
                        _tier[_k] = "nse-chain"
        except Exception:
            pass
        # Tier 1b: Angel token batch (anything with NFO symbol)
        try:
            still = [k for k in missing if k not in live]
            if still:
                _ab = _angel_batch_ltp(sym, still, expiry_ymd)
                for _k, _v in _ab.items():
                    live[_k] = _v
                    _tier[_k] = "broker-quote"
        except Exception:
            pass
        for key in missing:
            if key in live and live[key] and float(live[key]) > 0:
                out[key] = round(float(live[key]), 2)
                if sources is not None:
                    sources[key] = _tier.get(key, "broker-quote")
            else:
                fb, fsrc = _fallback_ltp(sym, key[0], key[1], expiry_ymd, with_source=True)
                if fb and float(fb) > 0:
                    out[key] = round(float(fb), 2)
                    if sources is not None:
                        sources[key] = fsrc or "model"
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


# ---------------- Shared entry price (paper == live) ----------------

def entry_price(symbol, strike, option_type, expiry_ymd, side="BUY",
                fresh=True, apply_slippage=True):
    """THE single entry-price function for BOTH paper and live orders.

    Returns (price, source):
      price  - rounded fill price (LTP + slippage, same slippage model for
               paper and live so the two never drift apart)
      source - 'broker-quote' | 'model' | 'none'

    Paper (routes/option_chain.place_trade) and live
    (routes/broker.place_live_order) BOTH call this - same contract, same
    moment-ish tick, same slippage = same price on paper and on the
    exchange feed.
    """
    try:
        from core.services.transaction_costs import TransactionCosts
    except Exception:
        TransactionCosts = None
    ltp = 0.0
    src = "none"
    try:
        _srcs = {}
        _res = get_contract_ltps(symbol, [(strike, option_type)], expiry_ymd,
                                 fresh=fresh, sources=_srcs)
        for _v in _res.values():
            ltp = float(_v)
            break
        for _k, _v in _srcs.items():
            src = _v
            break
    except Exception:
        ltp = 0.0
    if ltp <= 0:
        src = "none"
    if ltp <= 0:
        # Model fallback (DB close -> Black-Scholes) - same numbers the chain
        # table shows, so paper/live still agree with each other.
        fb = _fallback_ltp((symbol or "").upper(), strike, option_type, expiry_ymd)
        if fb and float(fb) > 0:
            ltp = float(fb)
            src = "model"
    if ltp <= 0:
        return 0.0, "none"
    price = ltp
    if apply_slippage and TransactionCosts is not None:
        try:
            price = float(TransactionCosts.apply_fill_slippage(
                ltp, str(side or "BUY").upper(), is_live=True))
        except Exception:
            price = ltp
    return round(float(price), 2), src


def price_source_detail(symbol, strike, option_type, expiry_ymd, fresh=True):
    """Human-readable origin of the current price (UI diagnostics).
    source: broker-quote | nse-chain | db-close | model | cache | none

    fresh=False = 10s cache pehle (live order preview ke liye: cache bhi real
    broker quote hi hai, aur rate-limited fresh call fail hone par bhi
    price aur limit ek hi number par rehte hain)."""
    try:
        _srcs = {}
        _res = get_contract_ltps(symbol, [(strike, option_type)], expiry_ymd,
                                 fresh=fresh, sources=_srcs)
        ltp = None
        for _v in _res.values():
            ltp = float(_v)
            break
        src = "none"
        for _v in _srcs.values():
            src = _v
            break
        if ltp and float(ltp) > 0:
            return {"ltp": round(float(ltp), 2), "source": src}
    except Exception:
        pass
    try:
        fb, fsrc = _fallback_ltp((symbol or "").upper(), strike, option_type,
                                 expiry_ymd, with_source=True)
    except Exception:
        fb, fsrc = None, "none"
    if fb and float(fb) > 0:
        return {"ltp": round(float(fb), 2), "source": fsrc or "model"}
    return {"ltp": None, "source": "none"}
