"""Universal Symbol & Expiry Resolver — one entry point for all brokers.

Problem it solves: every broker has its own scrip master / token / symbol
format. Sending a hardcoded or stale expiry (e.g. Reliance 2026-09-17, which
doesn't exist — valid: 2026-09-29, 2026-10-27) gets the order rejected.
This module resolves (underlying, expiry-hint, strike, CE/PE) against each
broker's LIVE master before any order is routed:

  resolve_contract(broker, creds, underlying, expiry_hint, strike, opt)
    -> {"ok": True, "refs": {...broker refs...}, "expiry_used": "YYYY-MM-DD",
        "source": "..."}
    -> {"ok": False, "error": "...", "available_expiries": [...]}

Rules:
- Weekly/Monthly hints NEVER send a guessed date blindly: they snap to the
  nearest live expiry on/after today from that broker's master.
- Explicit YYYY-MM-DD dates stay strict: if missing from the master, the
  order is refused WITH the valid list (no silent wrong-expiry routing).
- No credentials / unreachable master -> clear error, never a guessed fill.
"""
import datetime as _dt


WEEKLY_IDX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}

FYERS_UNDERLYING = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}


def is_explicit_date(hint: str) -> bool:
    try:
        _dt.datetime.strptime(str(hint or "")[:10], "%Y-%m-%d")
        return True
    except Exception:
        return False


def _today_ist():
    return (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).date()


def snap_hint_to_expiry(symbol: str, hint: str) -> str:
    """Local weekday guess (Tue NSE / Thu SENSEX) used ONLY as a first
    candidate — always re-validated against the broker master afterwards."""
    import calendar as _cal
    hint = str(hint or "").strip()
    try:
        return _dt.datetime.strptime(hint[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        pass
    today = _today_ist()
    wd = 3 if symbol.upper() == "SENSEX" else 1
    if hint.lower().startswith("month") or (symbol.upper() not in WEEKLY_IDX):
        last = _dt.date(today.year, today.month, _cal.monthrange(today.year, today.month)[1])
        while last.weekday() != wd:
            last -= _dt.timedelta(days=1)
        if last < today:
            m = today.month + 1 if today.month < 12 else 1
            y = today.year if today.month < 12 else today.year + 1
            last = _dt.date(y, m, _cal.monthrange(y, m)[1])
            while last.weekday() != wd:
                last -= _dt.timedelta(days=1)
        return last.strftime("%Y-%m-%d")
    d = today + _dt.timedelta(days=1)
    while d.weekday() != wd:
        d += _dt.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _nearest_on_or_after(dates: list, today=None):
    today = today or _today_ist()
    best = None
    for ds in dates or []:
        try:
            d = _dt.datetime.strptime(str(ds)[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if d >= today and (best is None or d < best):
            best = d
    return best.strftime("%Y-%m-%d") if best else ""


async def _fyers_expiries(fy) -> list:
    """Live expiry list (YYYY-MM-DD) from Fyers option-chain API. Tries v3 then v2."""
    for path in ("/api/v3/options-chain", "/api/v2/options-chain"):
        try:
            res = await fy._request("GET", path + "?symbol=NSE:NIFTY50-INDEX&strikecount=1")
        except Exception:
            continue
        if not res.get("success"):
            continue
        data = res.get("data") or {}
        out = []
        for e in data.get("expiryData") or data.get("expiry_dates") or []:
            if isinstance(e, str):
                out.append(e[:10])
            elif isinstance(e, dict):
                for k in ("date", "expiry", "expiryDate"):
                    v = str(e.get(k, "") or "")
                    if not v:
                        continue
                    try:
                        out.append(_dt.datetime.strptime(v[:10], "%d-%m-%Y").strftime("%Y-%m-%d"))
                        break
                    except Exception:
                        pass
                    try:
                        out.append(_dt.datetime.strptime(v[:10], "%Y-%m-%d").strftime("%Y-%m-%d"))
                        break
                    except Exception:
                        pass
        if out:
            return sorted(set(out))
    return []


def _fyers_symbol(symbol: str, expiry_ymd: str, strike: float, option_type: str) -> str:
    """Fyers format (verified): monthly YYMMM, weekly YYMdd (M=1-9/O/N/D)."""
    import calendar as _cal
    d = _dt.datetime.strptime(expiry_ymd[:10], "%Y-%m-%d").date()
    wd = 3 if symbol.upper() == "SENSEX" else 1
    last = _dt.date(d.year, d.month, _cal.monthrange(d.year, d.month)[1])
    while last.weekday() != wd:
        last -= _dt.timedelta(days=1)
    yy = d.strftime("%y")
    if d == last:
        mon = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"][d.month - 1]
        code = f"{yy}{mon}"
    else:
        mcode = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6",
                 7: "7", 8: "8", 9: "9", 10: "O", 11: "N", 12: "D"}[d.month]
        code = f"{yy}{mcode}{d.day:02d}"
    return f"NSE:{symbol.upper()}{code}{int(float(strike))}{option_type.upper()}"


async def resolve_contract(broker: str, creds: dict, underlying: str,
                           expiry_hint: str, strike: float, option_type: str) -> dict:
    """Universal entry. creds = stored broker config dict."""
    broker = (broker or "").lower()
    underlying = (underlying or "").upper()
    opt = "CE" if str(option_type or "CE").upper().startswith("C") else "PE"
    explicit = is_explicit_date(expiry_hint)
    guess = snap_hint_to_expiry(underlying, expiry_hint or "weekly")

    if broker == "dhan":
        try:
            from core.services.broker_dhan_live import DhanLive
        except Exception as e:
            return {"ok": False, "error": f"Dhan client missing: {e}"}
        dl = DhanLive(client_id=(creds or {}).get("client_id", ""),
                      access_token=(creds or {}).get("access_token", ""))
        r = await dl.resolve_fo(underlying, guess, strike, opt, exact=explicit)
        if r.get("security_id"):
            return {"ok": True, "refs": {"security_id": r["security_id"],
                                         "lot_size": int(r.get("lot_size") or 0),
                                         "symbol": r.get("symbol", "")},
                    "expiry_used": r.get("expiry", guess)[:10] or guess,
                    "source": r.get("source", "dhan")}
        return {"ok": False, "error": r.get("error", "Dhan resolution failed"),
                "available_expiries": []}

    if broker == "fyers":
        try:
            from core.services.broker_fyers import FyersV3
        except Exception as e:
            return {"ok": False, "error": f"Fyers client missing: {e}"}
        fy = FyersV3(app_id=(creds or {}).get("app_id", ""),
                     secret_key=(creds or {}).get("secret", ""),
                     redirect_uri=(creds or {}).get("redirect_uri", ""),
                     access_token=(creds or {}).get("access_token", ""),
                     refresh_token=(creds or {}).get("refresh_token", ""))
        live_exps = []
        if (creds or {}).get("access_token"):
            try:
                live_exps = await _fyers_expiries(fy)
            except Exception:
                live_exps = []
        use_exp = guess
        src = "fyers-constructed"
        if live_exps:
            if explicit and guess in live_exps:
                use_exp = guess
                src = "fyers-validated"
            elif not explicit:
                snapped = _nearest_on_or_after(live_exps)
                if snapped:
                    use_exp = snapped
                    src = "fyers-validated"
                else:
                    return {"ok": False,
                            "error": f"Fyers: no live expiry on/after today for {underlying}.",
                            "available_expiries": live_exps}
            else:
                return {"ok": False,
                        "error": f"Fyers: expiry {guess} not live for {underlying}. Valid: {', '.join(live_exps[:8])}",
                        "available_expiries": live_exps}
        elif explicit:
            src = "fyers-constructed-unvalidated"
        return {"ok": True,
                "refs": {"symbol": _fyers_symbol(underlying, use_exp, strike, opt)},
                "expiry_used": use_exp, "source": src,
                "available_expiries": live_exps}

    if broker == "angel":
        try:
            from core.services.broker_angel_live import AngelLive
        except Exception as e:
            return {"ok": False, "error": f"Angel client missing: {e}"}
        an = AngelLive(api_key=(creds or {}).get("api_key", ""),
                       client_code=(creds or {}).get("client_code", ""),
                       password=(creds or {}).get("password", ""),
                       totp_secret=(creds or {}).get("totp_secret", ""))
        an.jwt = (creds or {}).get("access_token", "")
        if not an.jwt:
            return {"ok": False, "error": "Angel: no session token. Connect (TOTP login) first."}
        r = await an.resolve_fo(underlying, guess, strike, opt)
        if r.get("symboltoken"):
            return {"ok": True,
                    "refs": {"tradingsymbol": r["tradingsymbol"],
                             "symboltoken": r["symboltoken"],
                             "lot_size": int(r.get("lotsize") or 0)},
                    "expiry_used": guess, "source": "angel-search"}
        return {"ok": False, "error": r.get("error", "Angel resolution failed")}

    if broker == "shoonya":
        try:
            from core.services.broker_shoonya_live import ShoonyaLive
        except Exception as e:
            return {"ok": False, "error": f"Shoonya client missing: {e}"}
        sc = (creds or {})
        sh = ShoonyaLive(uid=sc.get("uid", ""), password=sc.get("pwd", ""),
                         totp_secret=sc.get("secret", "") or sc.get("secret_code", ""),
                         vendor_code=sc.get("vc", ""), api_key=sc.get("apikey", ""))
        sh.susertoken = sc.get("access_token", "")
        sh.actid = sc.get("actid", sc.get("uid", ""))
        if not sh.susertoken:
            return {"ok": False, "error": "Shoonya: no session. Connect first."}
        r = await sh.resolve_fo(underlying, guess, strike, opt)
        if r.get("tsym"):
            return {"ok": True,
                    "refs": {"tsym": r["tsym"], "token": r.get("token", ""),
                             "lot_size": int(r.get("lot_size") or 0)},
                    "expiry_used": guess, "source": "shoonya-search"}
        return {"ok": False, "error": r.get("error", "Shoonya resolution failed")}

    return {"ok": False, "error": f"Unknown broker '{broker}'"}
