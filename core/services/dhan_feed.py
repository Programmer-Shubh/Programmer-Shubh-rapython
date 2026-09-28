"""DhanHQ v2 MarketFeed WebSocket streaming (single shared connection).

Docs: https://dhanhq.co/docs/v2/live-market-feed/ + annexure.
- Connect: wss://api-feed.dhan.co?version=2&token=..&clientId=..&authType=2
- Subscribe JSON: {RequestCode: 15 ticker / 17 quote / 21 full,
    InstrumentCount, InstrumentList: [{ExchangeSegment, SecurityId}]}
- Binary packets, 8-byte header: code u8 BE, msglen u16 BE, segment u8 BE,
    security_id i32 LE; LTP float32 LE at [8:12] for codes 1/2/4/8.

One daemon thread owns an asyncio loop: connect + resubscribe + reconnect
with backoff + 30s ping. Everything else (pricers, status, diag) only reads
the thread-safe tick cache — never touches the socket. Zero token? Idle.
Any failure degrades to the existing REST polling (no exceptions escape).
"""
import asyncio
import json
import struct
import threading
import time

FEED_URL = "wss://api-feed.dhan.co"

REQ_TICKER = 15
REQ_QUOTE = 17
REQ_FULL = 21
REQ_UNSUB_TICKER = 16
REQ_UNSUB_QUOTE = 18

SEG_BYTE = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURR",
            4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CURR", 8: "BSE_FNO"}

# Response codes we parse LTP from (float32 LE at offset 8)
_LTP_CODES = (1, 2, 4, 8)


def parse_packet(buf: bytes):
    """Parse ONE packet from the front of buf.
    Returns (tick_or_None, bytes_consumed). tick =
    {seg, secid, code, ltp, ltt, oi}. Never raises."""
    try:
        if len(buf) < 8:
            return None, 0
        code = buf[0]
        msglen = struct.unpack(">H", buf[1:3])[0]
        if msglen <= 0 or msglen > 2048:
            return None, 0
        total = 3 + msglen
        if len(buf) < total:
            return None, 0
        pkt = buf[:total]
        seg = SEG_BYTE.get(pkt[3], f"SEG{pkt[3]}")
        secid = str(struct.unpack("<i", pkt[4:8])[0])
        tick = {"seg": seg, "secid": secid, "code": code,
                "ltp": 0.0, "ltt": 0, "oi": 0}
        if code in _LTP_CODES and len(pkt) >= 12:
            try:
                v = struct.unpack("<f", pkt[8:12])[0]
                if v > 0 and v < 1e9:
                    tick["ltp"] = round(float(v), 2)
            except Exception:
                pass
        if code == 2 and len(pkt) >= 16:  # ticker LTT
            try:
                tick["ltt"] = int(struct.unpack("<I", pkt[12:16])[0])
            except Exception:
                pass
        if code == 4 and len(pkt) >= 18:  # quote LTT (after LTP+LTQ)
            try:
                tick["ltt"] = int(struct.unpack("<I", pkt[14:18])[0])
            except Exception:
                pass
        if code == 5 and len(pkt) >= 12:  # OI packet
            try:
                tick["oi"] = int(struct.unpack("<i", pkt[8:12])[0])
            except Exception:
                pass
        if code == 8 and len(pkt) >= 42:  # full packet OI
            try:
                tick["oi"] = int(struct.unpack("<i", pkt[38:42])[0])
            except Exception:
                pass
        if code == 50:
            tick["disconnect"] = True
        return tick, total
    except Exception:
        return None, 0


class DhanFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._ticks = {}          # (seg, secid) -> {ltp, ltt, oi, ts}
        self._subs = {}           # (seg, secid) -> request_code
        self._pending = []        # subscribe batches queued for the loop
        self._thread = None
        self._loop = None
        self._client_id = ""
        self._token = ""
        self._connected = False
        self._last_msg = 0.0
        self._last_error = ""
        self._ws = None

    # ---------- public (any thread) ----------

    def ensure(self, client_id="", access_token=""):
        """Start (or re-auth) the feed when a real token exists. Idle otherwise."""
        try:
            tok = str(access_token or "").strip()
            cid = str(client_id or "").strip()
            if not tok or tok.startswith(("SIM-", "ANG-")) or not cid:
                return False
            with self._lock:
                if self._thread and self._thread.is_alive() and tok == self._token and cid == self._client_id:
                    return True
                self._client_id, self._token = cid, tok
            self._start_thread()
            return True
        except Exception:
            return False

    def subscribe(self, seg, secid, mode=REQ_QUOTE):
        """Queue a subscription; sent on the loop (batched, 100/batch)."""
        try:
            key = (str(seg or "").upper(), str(secid))
            if not key[1] or key[1] in ("0", "None", ""):
                return False
            with self._lock:
                if self._subs.get(key) == mode:
                    return True
                self._subs[key] = mode
                self._pending.append(key)
            return True
        except Exception:
            return False

    def get_ltp(self, seg, secid, max_age=15):
        """Latest streamed LTP or None (stale/missing -> caller uses REST)."""
        try:
            t = self._ticks.get((str(seg or "").upper(), str(secid)))
            if t and t.get("ltp", 0) > 0 and time.time() - t.get("ts", 0) <= max_age:
                return float(t["ltp"])
        except Exception:
            pass
        return None

    def state(self):
        try:
            with self._lock:
                subs = len(self._subs)
                ticks = len(self._ticks)
            return {"running": bool(self._thread and self._thread.is_alive()),
                    "connected": self._connected,
                    "subscribed": subs, "ticks": ticks,
                    "last_msg_age_s": round(time.time() - self._last_msg, 1) if self._last_msg else -1,
                    "error": (self._last_error or "")[:150]}
        except Exception:
            return {"running": False, "connected": False}

    # ---------- internals (feed thread) ----------

    def _start_thread(self):
        try:
            if self._thread and self._thread.is_alive():
                return
            th = threading.Thread(target=self._run_loop, name="dhan-feed", daemon=True)
            self._thread = th
            th.start()
        except Exception:
            pass

    def _run_loop(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._supervise())
        except Exception as e:
            try:
                self._last_error = str(e)[:150]
            except Exception:
                pass

    async def _supervise(self):
        backoff = 2
        while True:
            try:
                await self._session()
                backoff = 2
            except Exception as e:
                try:
                    self._last_error = str(e)[:150]
                except Exception:
                    pass
                self._connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _session(self):
        import websockets
        url = (f"{FEED_URL}?version=2&token={self._token}"
               f"&clientId={self._client_id}&authType=2")
        async with websockets.connect(url, ping_interval=30, ping_timeout=10,
                                      close_timeout=5) as ws:
            self._ws = ws
            self._connected = True
            self._last_error = ""
            self._resynced = False  # force full re-subscribe on every (re)connect
            await self._flush_pending(ws)
            buf = b""
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    try:
                        await ws.ping()
                    except Exception:
                        break
                    continue
                if isinstance(msg, str):
                    continue  # control/JSON acks
                buf += bytes(msg)
                guard = 0
                while len(buf) >= 8 and guard < 64:
                    guard += 1
                    tick, used = parse_packet(buf)
                    if used <= 0:
                        # Resync: drop one byte (corrupt head) or wait for more
                        if len(buf) > 8:
                            buf = buf[1:]
                            continue
                        break
                    buf = buf[used:]
                    if not tick:
                        continue
                    if tick.get("disconnect"):
                        return  # server asked to disconnect -> reconnect
                    if tick.get("ltp", 0) > 0 or tick.get("oi", 0) > 0:
                        key = (tick["seg"], tick["secid"])
                        prev = self._ticks.get(key) or {}
                        self._ticks[key] = {
                            "ltp": tick["ltp"] or prev.get("ltp", 0),
                            "ltt": tick.get("ltt") or prev.get("ltt", 0),
                            "oi": tick.get("oi") or prev.get("oi", 0),
                            "ts": time.time(),
                        }
                        self._last_msg = time.time()
                # Opportunistic: send newly queued subs + periodic ping
                await self._flush_pending(ws)
                if len(buf) > 4096:
                    buf = b""

    async def _flush_pending(self, ws):
        try:
            with self._lock:
                batch = self._pending[:100]
                self._pending = self._pending[100:]
                subs = dict(self._subs)
            if batch:
                by_mode = {}
                for seg, secid in batch:
                    by_mode.setdefault(subs.get((seg, secid), REQ_QUOTE), []).append((seg, secid))
                for mode, items in by_mode.items():
                    await ws.send(json.dumps({
                        "RequestCode": int(mode),
                        "InstrumentCount": len(items),
                        "InstrumentList": [{"ExchangeSegment": s, "SecurityId": str(i)}
                                           for s, i in items],
                    }))
            else:
                # Full resync on (re)connect: re-subscribe everything in batches
                if not getattr(self, "_resynced", False):
                    self._resynced = True
                    modes = {}
                    for (s, i), m in subs.items():
                        modes.setdefault(m, []).append((s, i))
                    for mode, items in modes.items():
                        for j in range(0, len(items), 100):
                            grp = items[j:j + 100]
                            await ws.send(json.dumps({
                                "RequestCode": int(mode),
                                "InstrumentCount": len(grp),
                                "InstrumentList": [{"ExchangeSegment": s, "SecurityId": str(i)}
                                                   for s, i in grp],
                            }))
        except Exception:
            try:
                self._resynced = False
            except Exception:
                pass


_FEED = DhanFeed()


def get_feed():
    return _FEED


def ensure_feed_from_config():
    """Read saved Dhan config; start feed if a real token exists."""
    try:
        from core.models.database import Database
        import json as _js
        row = Database.get_instance().fetch_one(
            "SELECT setting_value FROM settings WHERE setting_key='broker_dhan'")
        cfg = _js.loads(row["setting_value"]) if row and row.get("setting_value") else {}
        if isinstance(cfg, dict):
            if _FEED.ensure(cfg.get("client_id", ""), cfg.get("access_token", "")):
                return _FEED
    except Exception:
        pass
    return None
