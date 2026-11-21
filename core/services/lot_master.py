"""Instrument-master lot sizes (dynamic, official, FYERS).

The app is Fyers-only now (Dhan was removed from the broker registry).
Source: Fyers official public symbol master - no auth, no broker account
needed - regenerated daily by Fyers:
  https://public.fyers.in/sym_details/NSE_FO.csv
Format: headerless CSV, 21 columns. Relevant ones:
  col 2  segment      (11 = futures, 14 = options)
  col 3  lot_size
  col 4  tick_size
  col 8  expiry epoch (unix, IST)
  col 9  fyers_symbol (NSE:NIFTY26N1717350CE)
  col 13 underlying   (NIFTY / APOLLOHOSP ...)
  col 15 strike_price
  col 16 option_type  (CE / PE / XX for futures)

Caches: memory 24h + disk data/scrip_lots.json (survives restarts within a
deploy); negative-fail TTL 10 min (no hot-loop hammering on outage).
First call with cold cache returns (None, 'cold') instantly - call
warm_lot_cache() in a background thread at boot so values converge without
ever blocking order entry / dashboard polls.
"""
import csv
import json
import os
import time as _time

# Fyers official F&O symbol master (public, no auth).
_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
_TTL = 24 * 3600
_FAIL_TTL = 600

_MEM = None
_MEM_TS = 0.0
_FAIL_TS = 0.0
_WARMING = False


def _disk_path():
    try:
        here = os.path.join(os.path.dirname(__file__), "..", "data", "scrip_lots.json")
        if os.path.exists(here):
            return here
    except Exception:
        pass
    return os.path.join("data", "scrip_lots.json")


def _norm_underlying(u: str):
    """Normalise the underlying key: 'NIFTY ' -> 'NIFTY', strip spaces."""
    out = []
    try:
        base = str(u or "").strip().upper().replace(" ", "")
        if base:
            out.append(base)
    except Exception:
        pass
    return out


def _parse_master_lines(lines):
    """Pure parser: Fyers NSE_FO.csv lines -> {UNDERLYING: lot}.

    First nonzero lot wins per underlying (lot is contract-family level,
    same across strikes/expiries). Stock-option-only or future-only rows are
    both fine - Fyers publishes both in the same file."""
    lots = {}
    try:
        reader = csv.reader(lines)
        for row in reader:
            try:
                if len(row) < 21:
                    continue
                seg = str(row[2] or "").strip()
                # 11=index FUT, 13=stock FUT, 14=index OPT, 15=stock OPT
                if seg not in ("11", "13", "14", "15"):
                    continue
                try:
                    lot = int(float(str(row[3] or 0)))
                except Exception:
                    lot = 0
                if lot <= 0:
                    continue
                keys = _norm_underlying(row[13])
                for key in keys:
                    if key and key not in lots:
                        lots[key] = lot
            except Exception:
                continue
    except Exception:
        pass
    return lots


def _read_cache():
    """Fresh broker map or None (memory, else fresh disk file)."""
    global _MEM, _MEM_TS
    try:
        if _MEM and _time.time() - _MEM_TS < _TTL:
            return dict(_MEM)
    except Exception:
        pass
    try:
        p = _disk_path()
        if os.path.exists(p):
            with open(p, "r") as f:
                j = json.load(f)
            if j.get("ts", 0) > _time.time() - _TTL and isinstance(j.get("lots"), dict) and j["lots"]:
                _MEM = {str(k).upper(): int(v) for k, v in j["lots"].items() if int(v or 0) > 0}
                _MEM_TS = float(j.get("ts", 0))
                return dict(_MEM)
    except Exception:
        pass
    return None


def _fetch_master_lots(timeout=45):
    """Download + parse master. Returns dict (may be empty). Never raises."""
    global _MEM, _MEM_TS, _FAIL_TS
    if _time.time() - _FAIL_TS < _FAIL_TTL:
        return _MEM or {}
    try:
        import urllib.request as _ur
        req = _ur.Request(_MASTER_URL, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(req, timeout=timeout) as r:
            if getattr(r, "status", 200) != 200:
                raise RuntimeError(f"HTTP {getattr(r, 'status', '?')}")
            raw = r.read().decode("utf-8", errors="replace").splitlines()
        lots = _parse_master_lines(iter(raw))
        if lots:
            _MEM = lots
            _MEM_TS = _time.time()
            try:
                os.makedirs(os.path.dirname(_disk_path()) or ".", exist_ok=True)
                with open(_disk_path(), "w") as f:
                    json.dump({"ts": _MEM_TS, "lots": lots}, f)
            except Exception:
                pass
            return dict(lots)
        _FAIL_TS = _time.time()
        return _MEM or {}
    except Exception:
        _FAIL_TS = _time.time()
        return _MEM or {}


def broker_lots(force=False):
    """Fresh broker map if cached, else {} (use warm_lot_cache to fill)."""
    try:
        hit = _read_cache()
        if hit:
            return hit
        if force:
            return _fetch_master_lots()
    except Exception:
        pass
    return {}


def warm_lot_cache():
    """Blocking warm (call in background thread at boot). Returns count."""
    try:
        if _read_cache():
            return len(_MEM or {})
    except Exception:
        pass
    try:
        return len(_fetch_master_lots() or {})
    except Exception:
        return 0


def ensure_warm_async():
    """Fire-and-forget background warm (once per process)."""
    global _WARMING
    try:
        if _WARMING:
            return False
        try:
            if _read_cache():
                return False
        except Exception:
            pass
        _WARMING = True
        import threading as _th

        def _w():
            try:
                warm_lot_cache()
            except Exception:
                pass

        _th.Thread(target=_w, name="lot-master-warm", daemon=True).start()
        return True
    except Exception:
        return False


def get_lot(symbol: str):
    """(lot|None, source). Source: broker-master | None(cold/miss).
    Hyphen-insensitive: 'BAJAJ-AUTO' bhi 'BAJAJAUTO' master row se milega."""
    try:
        s = str(symbol or "").strip().upper()
        if not s:
            return None, "empty"
        hit = _read_cache()
        if hit:
            if s in hit:
                return int(hit[s]), "broker-master"
            _flat = s.replace("-", "").replace(" ", "")
            if _flat != s and _flat in hit:
                return int(hit[_flat]), "broker-master"
        return None, "cold"
    except Exception:
        return None, "error"
