"""Broker instrument-master lot sizes (dynamic, official).

Priority: Dhan scrip master (public CSV, no auth) -> caller falls back to
NSE lots / static map (see utils.helpers.get_lot_size).

Dhan master columns used: SEM_EXM_EXCH_ID (NSE), SEM_SEGMENT (D derivatives),
SEM_INSTRUMENT_NAME / SEM_TRADING_SYMBOL (underlying), SEM_LOT_UNITS.

Caches: memory 24h + disk data/dhan_lots.json (survives restarts within a
deploy); negative-fail TTL 10 min (no hot-loop hammering on outage).
First call with cold cache returns (None, 'cold') instantly - call
warm_lot_cache() in a background thread at boot so values converge without
ever blocking order entry / dashboard polls.
"""
import csv
import json
import os
import time as _time

_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
_TTL = 24 * 3600
_FAIL_TTL = 600

_MEM = None
_MEM_TS = 0.0
_FAIL_TS = 0.0
_WARMING = False


def _disk_path():
    try:
        here = os.path.join(os.path.dirname(__file__), "..", "data", "dhan_lots.json")
        if os.path.exists(here):
            return here
    except Exception:
        pass
    return os.path.join("data", "dhan_lots.json")


def _norm_underlying(iname: str, tsym: str):
    """Candidate lookup keys for an underlying (indices + stocks)."""
    out = []
    try:
        base = str(iname or "").strip().upper().replace(" ", "")
        if base:
            out.append(base)
        t = str(tsym or "").strip().upper().replace(" ", "")
        if t:
            out.append(t.split("-")[0])
    except Exception:
        pass
    seen = []
    for k in out:
        if k and k not in seen:
            seen.append(k)
    return seen


def _parse_master_lines(lines):
    """Pure parser: CSV lines -> {UNDERLYING: lot}. First nonzero lot wins
    per underlying (lot is contract-family level, same across strikes)."""
    lots = {}
    try:
        reader = csv.DictReader(lines)
        for row in reader:
            try:
                exch = str(row.get("SEM_EXM_EXCH_ID", "") or "").strip().upper()
                seg = str(row.get("SEM_SEGMENT", "") or "").strip().upper()
                itype = str(row.get("SEM_EXCH_INSTRUMENT_TYPE", "") or "").strip().upper()
                seg_ok = (seg == "D" or "FO" in seg.replace("_", "")
                          or "FUT" in seg or "OPT" in seg or itype in ("OP", "FUT"))
                if exch != "NSE" or not seg_ok:
                    continue
                try:
                    lot = int(float(str(row.get("SEM_LOT_UNITS", 0) or 0)))
                except Exception:
                    lot = 0
                if lot <= 0:
                    continue
                for key in _norm_underlying(row.get("SEM_INSTRUMENT_NAME", ""),
                                           row.get("SEM_TRADING_SYMBOL", "")):
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


def _fetch_dhan_lots(timeout=45):
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
            return _fetch_dhan_lots()
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
        return len(_fetch_dhan_lots() or {})
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
