"""NSE official F&O bhavcopy via nsefin (marketcalls/nsefin, PyPI).
Auto-download + import + retention purge. All NSE-direct scrapers
(nselib/jugaad/nsepython/archives) were removed (blocked); nsefin's
bhavcopy endpoint works. Best-effort: every failure returns safely.
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
    """Recent trading dates with no F&O options data in DB (for gap fill)."""
    from core.models.database import Database
    out = []
    try:
        d = datetime.date.today() - datetime.timedelta(days=1)
        checked = 0
        while len(out) < days and checked < days + 20:
            if d.weekday() < 5:
                row = Database.get_instance().fetch_one(
                    "SELECT COUNT(*) c FROM bhavcopy_data WHERE trade_date=? AND option_type IN ('CE','PE')",
                    [d.strftime("%Y-%m-%d")])
                if not row or not row["c"]:
                    out.append(d.strftime("%Y-%m-%d"))
            d -= datetime.timedelta(days=1)
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


def purge_old_data(months: int = RETENTION_MONTHS) -> int:
    """Delete bhavcopy rows older than N months ('old delete'). Returns count."""
    from core.models.database import Database
    try:
        cutoff = (datetime.date.today() - datetime.timedelta(days=int(months * 30.5))).strftime("%Y-%m-%d")
        db = Database.get_instance()
        row = db.fetch_one("SELECT COUNT(*) c FROM bhavcopy_data WHERE trade_date<?", [cutoff])
        n = int(row["c"]) if row else 0
        if n:
            db.execute("DELETE FROM bhavcopy_data WHERE trade_date<?", [cutoff])
        return n
    except Exception as e:
        logger.warning("purge failed: %s", e)
        return 0
