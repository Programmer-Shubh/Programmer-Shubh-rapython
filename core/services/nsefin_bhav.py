"""NSE official F&O bhavcopy via nsefin (marketcalls/nsefin, PyPI).
Auto-download + import + retention purge. nsepython history fetcher
bhi active hai (historical_fetcher). Best-effort: every failure returns safely.
"""
import datetime
import logging

logger = logging.getLogger(__name__)

# bhavcopy_data option_type for futures rows (CE/PE/NULL used elsewhere)
FUT = "FUT"
# Keep this many months of history; older rows purged ("old delete")
RETENTION_MONTHS = 12


def _client():
    from nsefin import get_nse_instance
    return get_nse_instance()


def fetch_fno_bhavcopy(day):
    """Official NSE F&O bhavcopy DataFrame for one date (all symbols)."""
    if isinstance(day, str):
        day = datetime.datetime.strptime(day[:10], "%Y-%m-%d").date()
    return _client().get_fno_bhav_copy(day)


def _norm_opt(right) -> str:
    r = str(right or "").strip().upper()
    if r in ("CE", "C", "CALL"):
        return "CE"
    if r in ("PE", "P", "PUT"):
        return "PE"
    return ""


def import_fno_bhavcopy(day) -> dict:
    """Download one day's official F&O bhavcopy and import options+futures+spot.
    Returns counts. Raises on failure (caller decides retry)."""
    from core.models.bhavcopy_model import BhavcopyModel
    df = fetch_fno_bhavcopy(day)
    if df is None or len(df) == 0:
        return {"options": 0, "futures": 0, "spot": 0, "rows": 0}
    records = []
    spots = {}
    for _, r in df.iterrows():
        try:
            sym = str(r.get("symbol", "") or "").strip().upper()
            if not sym:
                continue
            td = str(r.get("date", "") or "")[:10]
            try:
                td = datetime.datetime.strptime(td, "%Y-%m-%d").strftime("%Y-%m-%d")
            except Exception:
                try:
                    td = datetime.datetime.strptime(td.strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
                except Exception:
                    continue
            cat = str(r.get("category", "") or "").upper()
            exp = str(r.get("expiry", "") or "")[:10]
            try:
                exp = datetime.datetime.strptime(exp.strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
            except Exception:
                try:
                    exp = datetime.datetime.strptime(exp.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
                except Exception:
                    exp = ""
            strike = float(r.get("strike", 0) or 0)
            o = float(r.get("open", 0) or 0)
            h = float(r.get("high", 0) or 0)
            cl = float(r.get("close", r.get("last", 0)) or 0)
            lo = float(r.get("low", 0) or 0)
            vol = int(float(r.get("volume", 0) or 0))
            oi = int(float(r.get("oi", 0) or 0))
            spot = float(r.get("spot", 0) or 0)
            if cat in ("IDO", "STO"):
                opt = _norm_opt(r.get("right"))
                if not opt or cl <= 0:
                    continue
                # Non-traded contract: NSE bhavcopy me open/high/low = 0 aur
                # close = prv_close (PICHLE din ka close carry-forward). Woh
                # stale value aaj ka price nahi hai - import karne se wo
                # expiry-level constant ban jata tha (11100 CE = 818.35 har
                # din) aur backtest/premium galat hota tha. Aaj trade nahi hua
                # to row skip karo (row chahiye hi nahi - fallback model hai).
                if o <= 0 and h <= 0 and lo <= 0 and vol <= 0:
                    continue
                records.append({"symbol": sym, "trade_date": td, "expiry_date": exp,
                                "strike_price": strike, "option_type": opt,
                                "open_price": o or cl, "high_price": h or cl,
                                "low_price": lo or cl, "close_price": cl,
                                "volume": vol, "oi": oi})
            elif cat in ("IDF", "STF"):
                if cl <= 0:
                    continue
                records.append({"symbol": sym, "trade_date": td, "expiry_date": exp,
                                "strike_price": 0, "option_type": FUT,
                                "open_price": o or cl, "high_price": h or cl,
                                "low_price": lo or cl, "close_price": cl,
                                "volume": vol, "oi": oi})
            if spot > 0:
                key = (sym, td)
                if key not in spots:
                    spots[key] = {"symbol": sym, "trade_date": td, "expiry_date": "",
                                  "strike_price": None, "option_type": None,
                                  "open_price": spot, "high_price": spot,
                                  "low_price": spot, "close_price": spot,
                                  "volume": 0, "oi": 0}
        except Exception:
            continue
    records.extend(spots.values())
    n = BhavcopyModel().import_data(records) if records else 0
    return {"options": sum(1 for r in records if r["option_type"] in ("CE", "PE")),
            "futures": sum(1 for r in records if r["option_type"] == FUT),
            "spot": len(spots), "rows": n}


def missing_recent_dates(days: int = 90) -> list:
    """Recent trading dates with no F&O options data in DB (for gap fill).
    ONE grouped query (per-date COUNT loop full-table-scan karta tha -
    pooler statement timeout ka ek kaaran)."""
    from core.models.database import Database
    out = []
    try:
        import datetime as _dtd
        _end = _dtd.date.today() - _dtd.timedelta(days=1)
        _start = _end - _dtd.timedelta(days=days + 20)
        rows = Database.get_instance().fetch_all(
            "SELECT trade_date, COUNT(*) c FROM bhavcopy_data "
            "WHERE trade_date BETWEEN ? AND ? AND option_type IN ('CE','PE') "
            "GROUP BY trade_date",
            [_start.strftime("%Y-%m-%d"), _end.strftime("%Y-%m-%d")])
        _have = {str(r.get("trade_date", ""))[:10] for r in (rows or []) if r.get("c")}
        d = _end
        checked = 0
        while len(out) < days and checked < days + 20:
            if d.weekday() < 5:
                if d.strftime("%Y-%m-%d") not in _have:
                    out.append(d.strftime("%Y-%m-%d"))
            d -= _dtd.timedelta(days=1)
            checked += 1
    except Exception:
        pass
    return out


def backfill_fno(days: int = 90, per_run: int = 10) -> dict:
    """Fetch+import missing recent F&O bhavcopy days (cap per run)."""
    done, failed = [], []
    for d in missing_recent_dates(days)[:per_run]:
        try:
            res = import_fno_bhavcopy(d)
            done.append({d: res})
        except Exception as e:
            failed.append({d: str(e)[:120]})
    return {"done": done, "failed": failed}


def purge_stale_option_rows(chunk: int = 20000) -> int:
    """Purane 'carried prev_close' option rows hatao (jo naye import rule
    se aate hi nahi). NSE bhavcopy me non-traded contract ka close =
    pichle din ka close hota hai - wo aaj ka price nahi hai. Aise rows
    expiry-level constant ban jate the (11100 CE = 818.35 har din) aur
    backtest/premium galat hota tha. Sirf option rows (CE/PE), jo pure
    flat bar (open=high=low=close) aur zero volume wale hain."""
    from core.models.database import Database
    try:
        db = Database.get_instance()
        try:
            _pg = bool(db._is_postgres())
        except Exception:
            _pg = False
        _key = "ctid" if _pg else "rowid"
        _where = ("option_type IN ('CE','PE') AND volume<=0 "
                  "AND open_price=high_price AND high_price=low_price AND low_price=close_price")
        total = 0
        for _ in range(20):
            try:
                probe = db.fetch_one(
                    f"SELECT 1 as x FROM bhavcopy_data WHERE {_where} LIMIT 1")
            except Exception:
                break
            if not probe:
                break
            try:
                db.execute(
                    f"DELETE FROM bhavcopy_data WHERE {_key} IN "
                    f"(SELECT {_key} FROM bhavcopy_data WHERE {_where} LIMIT ?)",
                    [chunk])
                total += chunk
            except Exception:
                break
        return total
    except Exception as e:
        logger.warning("stale option row purge failed: %s", e)
        return 0


def purge_old_data(months: int = RETENTION_MONTHS) -> int:
    """Delete bhavcopy rows older than N months ('old delete'). Returns count.
    CHUNKED (20k rows/txn): ek giant DELETE pooler statement timeout kha kar
    poora PG backend dead kar deta tha (phir strategies gayab)."""
    from core.models.database import Database
    try:
        cutoff = (datetime.date.today() - datetime.timedelta(days=int(months * 30.5))).strftime("%Y-%m-%d")
        db = Database.get_instance()
        try:
            _pg = bool(db._is_postgres())
        except Exception:
            _pg = False
        _key = "ctid" if _pg else "rowid"
        total = 0
        for _ in range(200):
            try:
                probe = db.fetch_one(
                    f"SELECT 1 as x FROM bhavcopy_data WHERE trade_date<? LIMIT 1", [cutoff])
            except Exception:
                break
            if not probe:
                break
            try:
                db.execute(
                    f"DELETE FROM bhavcopy_data WHERE {_key} IN "
                    f"(SELECT {_key} FROM bhavcopy_data WHERE trade_date<? LIMIT 20000)",
                    [cutoff])
                total += 20000
            except Exception as e:
                logger.warning("purge chunk failed: %s", e)
                break
        return total
    except Exception as e:
        logger.warning("purge failed: %s", e)
        return 0
