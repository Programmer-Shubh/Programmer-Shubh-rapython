"""Lightweight process resource monitor for Render free tier (512 MB / 0.15 CPU).

One daemon thread, one psutil sample every `RESOURCE_LOG_INTERVAL` seconds
(default 60). It logs:

    [resources] rss=312.4MB vms=...MB cpu=1.2% threads=18 fds=42 samples=...

Why a thread instead of per-request middleware: sampling costs ~0.1 ms and
must not run on the event loop for every request. The HTTP layer only reads a
cached value (see `current_rss_mb()` / the `X-RSS-MB` response header).

Memory safety valve: when RSS crosses `RSS_SOFT_LIMIT_MB` (default 400 MB,
i.e. ~78% of Render's limit) the monitor runs `gc.collect()` and trims the
bounded runtime caches. This is defensive - it is NOT a substitute for
keeping caches bounded in the first place, but it prevents a slow climb from
turning into an OOM restart.

Everything is fail-open: if psutil is missing or a sample raises, the app
keeps running and the monitor just goes quiet.
"""
from __future__ import annotations

import gc
import logging
import os
import threading
import time
from collections import deque

try:  # psutil is optional at runtime (requirements.txt pins it for Render)
    import psutil  # type: ignore
    HAS_PSUTIL = True
except Exception:  # pragma: no cover - exercised only when psutil missing
    psutil = None  # type: ignore
    HAS_PSUTIL = False

logger = logging.getLogger("ratrade.resources")

_SAMPLE_SEC = max(15, int(os.environ.get("RESOURCE_LOG_INTERVAL", "60") or 60))
_SOFT_LIMIT_MB = float(os.environ.get("RSS_SOFT_LIMIT_MB", "400") or 400)
_TRIM_ENABLED = str(os.environ.get("MEM_TRIM_ENABLED", "1")).lower() not in (
    "0", "false", "no", "off",
)

_HISTORY = deque(maxlen=180)  # ~3 hours at 60s
_LAST = {"rss_mb": None, "cpu_pct": None, "ts": 0.0}
_STATE = {"started": False, "thread": None, "trims": 0, "last_trim": 0.0}
_LOCK = threading.Lock()

# (module, attribute, keep-at-most) - bounded dicts/OrderedDicts that can
# otherwise grow with every new symbol / contract / backtest during a long
# Render process lifetime.
_CACHE_SPECS = (
    ("core.services.live_market_data", "_LIVE_CACHE", 20),
    ("core.services.live_market_data", "_CHAIN_CACHE", 10),
    ("routes.strategy_builder", "_BT_CACHE", 6),
    ("routes.strategy_builder", "_BT_RESULT_CACHE", 8),
    ("core.services.contract_pricer", "_PRICE_CACHE", 300),
    ("core.services.contract_pricer", "_TOKEN_CACHE", 500),
    ("core.services.contract_pricer", "_TOPBOOK_CACHE", 100),
    ("core.services.auto_paper", "_HIST_CACHE", 15),
    ("core.services.auto_paper", "_SIGNAL_CACHE", 30),
    ("core.services.auto_paper", "_INTRA_CACHE", 10),
)


def _trim_one(mod_name: str, attr: str, keep: int) -> int:
    """Shrink one cache to `keep` entries. Returns number evicted."""
    try:
        import importlib

        mod = importlib.import_module(mod_name)
        cache = getattr(mod, attr, None)
        if not isinstance(cache, dict):
            return 0
        evicted = 0
        while len(cache) > keep:
            try:
                if hasattr(cache, "popitem"):
                    cache.popitem(last=False)  # OrderedDict
                else:
                    cache.pop(next(iter(cache)))
                evicted += 1
            except TypeError:
                try:
                    cache.popitem()
                    evicted += 1
                except Exception:
                    break
            except Exception:
                break
        return evicted
    except Exception:
        return 0


def trim_caches(force_gc: bool = True) -> dict:
    """Trim bounded runtime caches + (optionally) collect garbage.

    Safe to call from any thread; each cache is trimmed independently and
    failures are swallowed so this can never take the app down.
    """
    total = {}
    for mod_name, attr, keep in _CACHE_SPECS:
        n = _trim_one(mod_name, attr, keep)
        if n:
            total[attr] = n
    if force_gc:
        try:
            gc.collect()
        except Exception:
            pass
    return total


def current_rss_mb():
    """Cached RSS in MB (never samples psutil). None if unavailable."""
    return _LAST.get("rss_mb")


def snapshot(include_history: bool = False) -> dict:
    """Current process stats + (optionally) the ring buffer of samples."""
    out = {
        "psutil": HAS_PSUTIL,
        "rss_mb": _LAST.get("rss_mb"),
        "cpu_pct": _LAST.get("cpu_pct"),
        "sample_age_sec": round(time.time() - _LAST["ts"], 1) if _LAST.get("ts") else None,
        "soft_limit_mb": _SOFT_LIMIT_MB,
        "trim_enabled": _TRIM_ENABLED,
        "trims": _STATE["trims"],
        "last_trim_age_sec": (
            round(time.time() - _STATE["last_trim"], 1)
            if _STATE["last_trim"] else None
        ),
        "started": _STATE["started"],
    }
    try:
        out["process"] = {
            "pid": os.getpid(),
            "threads": threading.active_count(),
            "uptime_sec": round(time.time() - _PROC_START, 1) if _PROC_START else None,
        }
    except Exception:
        pass
    if include_history:
        out["history"] = list(_HISTORY)
    return out


_PROC_START = time.time()


def _sample_once(proc) -> dict:
    rec = {"ts": int(time.time())}
    try:
        info = proc.memory_info()
        rss_mb = round(info.rss / (1024 * 1024), 1)
        rec["rss_mb"] = rss_mb
        rec["vms_mb"] = round(info.vms / (1024 * 1024), 1)
    except Exception:
        rss_mb = None
    try:
        rec["cpu_pct"] = round(proc.cpu_percent(interval=None), 1)
    except Exception:
        rec["cpu_pct"] = None
    try:
        rec["threads"] = proc.num_threads()
    except Exception:
        rec["threads"] = threading.active_count()
    try:
        rec["fds"] = proc.num_fds() if hasattr(proc, "num_fds") else None
    except Exception:
        rec["fds"] = None
    try:
        rec["gc"] = [g for g in gc.get_count()]
    except Exception:
        pass

    with _LOCK:
        _LAST["rss_mb"] = rss_mb
        _LAST["cpu_pct"] = rec.get("cpu_pct")
        _LAST["ts"] = float(rec["ts"])
        _HISTORY.append(rec)
    return rec


def _loop(interval_s: int):
    proc = None
    try:
        if HAS_PSUTIL:
            proc = psutil.Process(os.getpid())
            # Prime cpu_percent so the first real sample is meaningful.
            try:
                proc.cpu_percent(interval=None)
            except Exception:
                pass
    except Exception:
        proc = None

    while True:
        try:
            if proc is not None:
                rec = _sample_once(proc)
                try:
                    logger.info(
                        "[resources] rss=%sMB vms=%sMB cpu=%s%% threads=%s fds=%s",
                        rec.get("rss_mb"), rec.get("vms_mb"), rec.get("cpu_pct"),
                        rec.get("threads"), rec.get("fds"),
                    )
                    print(
                        f"[resources] rss={rec.get('rss_mb')}MB "
                        f"cpu={rec.get('cpu_pct')}% "
                        f"threads={rec.get('threads')} fds={rec.get('fds')}",
                        flush=True,
                    )
                except Exception:
                    pass
                # Soft memory ceiling -> collect + trim before Render OOMs us.
                rss = rec.get("rss_mb")
                if _TRIM_ENABLED and rss is not None and rss >= _SOFT_LIMIT_MB:
                    try:
                        evicted = trim_caches(force_gc=True)
                        with _LOCK:
                            _STATE["trims"] += 1
                            _STATE["last_trim"] = time.time()
                        logger.warning(
                            "[resources] RSS %.1fMB >= %.1fMB soft limit - gc + trim %s",
                            rss, _SOFT_LIMIT_MB, evicted,
                        )
                        print(
                            f"[resources] HIGH MEMORY {rss}MB - gc.collect() + cache trim {evicted}",
                            flush=True,
                        )
                    except Exception:
                        pass
            else:
                # psutil unavailable: still log thread count, no RSS.
                try:
                    logger.info("[resources] psutil unavailable; threads=%s", threading.active_count())
                except Exception:
                    pass
        except Exception:
            pass
        try:
            time.sleep(interval_s)
        except Exception:
            return


def start(interval_s: int | None = None) -> bool:
    """Start the monitor once (idempotent). Returns True if running."""
    if _STATE["started"]:
        return True
    try:
        sec = int(interval_s or _SAMPLE_SEC)
    except Exception:
        sec = 60
    try:
        th = threading.Thread(target=_loop, args=(max(15, sec),),
                              name="resource-monitor", daemon=True)
        th.start()
        _STATE["thread"] = th
        _STATE["started"] = True
        try:
            _sample_initial()
        except Exception:
            pass
        try:
            print(f"[resources] monitor started (every {max(15, sec)}s, "
                  f"soft_limit={_SOFT_LIMIT_MB:.0f}MB, psutil={HAS_PSUTIL})", flush=True)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _sample_initial():
    """Log one sample immediately so the first Render log line has numbers."""
    if not HAS_PSUTIL:
        return
    try:
        proc = psutil.Process(os.getpid())
        proc.cpu_percent(interval=None)
        rec = _sample_once(proc)
        print(f"[resources] boot rss={rec.get('rss_mb')}MB threads={rec.get('threads')}", flush=True)
    except Exception:
        pass
