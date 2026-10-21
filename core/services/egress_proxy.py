"""Static-IP egress proxy support (Dhan IP-whitelisting ke liye).

Problem: Render (free tier) ke outgoing IPs shared/rotating hote hain - Dhan
jhola "Invalid IP" (DH-905) par reject karta hai jab tak server IP whitelist
me na ho. Solution: har Dhan API call ek fixed-IP proxy (e.g. snipy zeroip,
https://host:8443) ke PIPE (HTTP CONNECT) par se jaye -> broker ko wahi
whitelisted IP dikhta hai.

Config (env vars Render Environment me, YA settings-table key 'egress_proxy'):
- EGRESS_PROXY_URL    : https://zeroip.snipy.in:8443
- EGRESS_PROXY_TOKEN  : Bearer token (zero_ip_...)
- EGRESS_PROXY_IP     : whitelisted IPv6 (X-Snipy-Proxy-IPv6 header)
- EGRESS_PROXY_ENABLED: 1/true (optional; default: url+token hone par on)

Usage:
- httpx (async): httpx.AsyncClient(..., **egress_proxy.httpx_kwargs())
- requests-style sync: egress_proxy.sync_request("POST", url, headers=...)
  (proxy ON par httpx.Client - Bearer Proxy-Authorization waise hi BHEJTA
  hai jo requests nahi kar sakta).

Sirf Dhan-internal endpoints (api.dhan.co / auth.dhan.co) proxy par jaate
hain - public data sources (NSE bhavcopy, Yahoo, stooq, scrip-master CDN)
direct hi jaate hain taaki proxy load/latency na badhe.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

SETTING_KEY = "egress_proxy"
_PROXY_ENV = ("EGRESS_PROXY_URL", "EGRESS_PROXY_TOKEN", "EGRESS_PROXY_IP",
              "EGRESS_PROXY_ENABLED")


def cfg() -> dict:
    """Active config merge: settings-table <- env vars (env priority)."""
    out = {"enabled": False, "url": "", "token": "", "ip": "",
           "source": "off"}
    try:
        from core.models.database import Database
        row = Database.get_instance().fetch_one(
            "SELECT setting_value FROM settings WHERE setting_key=?",
            [SETTING_KEY])
        if row and row.get("setting_value"):
            j = json.loads(row["setting_value"])
            if isinstance(j, dict):
                for k in ("enabled", "url", "token", "ip"):
                    if j.get(k) not in (None, ""):
                        out[k] = j[k]
                if any(j.get(x) for x in ("url", "token", "ip")):
                    out["source"] = "db"
    except Exception as e:
        logger.debug("egress db config read fail: %s", e)
    _env = os.environ
    if _env.get("EGRESS_PROXY_URL") or _env.get("EGRESS_PROXY_TOKEN"):
        # Env overrides replace whole config; enabled default ON jab
        # url+token dono pade hon (explicit false karne ke liye env use).
        out["url"] = (_env.get("EGRESS_PROXY_URL") or "").strip()
        out["token"] = (_env.get("EGRESS_PROXY_TOKEN") or "").strip()
        if _env.get("EGRESS_PROXY_IP"):
            out["ip"] = _env["EGRESS_PROXY_IP"].strip()
        _e = (_env.get("EGRESS_PROXY_ENABLED") or "").strip().lower()
        out["enabled"] = _e not in ("0", "false", "no", "off")
        out["source"] = "env"
    else:
        out["enabled"] = bool(out.get("url") and out.get("token"))
    out["configured"] = bool(out.get("enabled") and out.get("url") and
                             out.get("token"))
    return out


def httpx_kwargs() -> dict:
    """httpx.AsyncClient/Client kwargs jab proxy ON ho. OFF parity: {}."""
    c = cfg()
    if not c.get("configured"):
        return {}
    kw = {"proxy": c["url"]}
    headers = {"Proxy-Authorization": "Bearer " + c["token"]}
    if c.get("ip"):
        headers["X-Snipy-Proxy-IPv6"] = c["ip"]
    try:
        # httpx >= 0.26: proxy headers Proxy object par jaate hain
        # (0.28 me HTTPProxy public me nahi hai; Proxy(headers=...) uber).
        from httpx import Proxy as _HttpxProxy
        kw = {"proxy": _HttpxProxy(url=c["url"], headers=headers)}
    except Exception:
        kw = {"proxy": c["url"], "proxy_headers": headers}
    return kw


def sync_request(method, url, headers=None, params=None, json_body=None,
                 data=None, timeout=20):
    """requests-in-send parity: proxy ON par httpx.Client (Router/header);
    OFF par plain requests. Response object (status_code/json/text) similar."""
    c = cfg()
    if c.get("configured"):
        import httpx
        try:
            with httpx.Client(timeout=timeout, **httpx_kwargs()) as _cl:
                return _cl.request(method.upper(), url, headers=headers or {},
                                   params=params, json=json_body, content=data)
        except Exception as exc:
            # Fail-open: proxy network par hi fail ho to ek DIRECT attempt
            # (Dhan-side HTTP errors yahan nahi pahunchte - sirf layer fail).
            try:
                logger.warning("egress sync_request proxy fail, direct fallback: %s", exc)
            except Exception:
                pass
    import requests as _rq
    return _rq.request(method.upper(), url, headers=headers or {},
                       params=params, json=json_body, data=data,
                       timeout=timeout)


def status() -> dict:
    """UI/diag ke liye (token MASKED)."""
    c = cfg()
    tok = c.get("token") or ""
    c["token_masked"] = (tok[:7] + "..." + tok[-4:]) if len(tok) > 12 else (
        "set" if tok else "")
    c.pop("token", None)
    return c


# ---------------- WebSocket (CONNECT-tunnel) support ----------------

def _tunnel_socket_sync(host, port, timeout=20):
    """Raw socket -> proxy CONNECT host:port (Bearer + IPv6 headers custom).
    Tunnel lock ho chuka socket return karta hai (plain TCP; caller wss par
    khud TLS kare). websockets sabhi versions proxy headers nahi bhej sakte,
    isliye hand-written CONNECT. Fail -> exception (caller direct fallback)."""
    import socket
    from urllib.parse import urlparse
    c = cfg()
    if not c.get("configured"):
        raise RuntimeError("egress proxy off")
    pu = urlparse(c["url"])
    phost, pport = pu.hostname, pu.port or 443
    s = socket.create_connection((phost, pport), timeout=timeout)
    try:
        s.settimeout(timeout)
        req = (f"CONNECT {host}:{port} HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n"
               f"Proxy-Authorization: Bearer {c['token']}\r\n")
        if c.get("ip"):
            req += f"X-Snipy-Proxy-IPv6: {c['ip']}\r\n"
        req += "Proxy-Connection: keep-alive\r\n\r\n"
        s.sendall(req.encode("ascii", "strict"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                raise RuntimeError("proxy closed CONNECT me")
            resp += chunk
            if len(resp) > 16384:
                raise RuntimeError("CONNECT response lge haath (rich body)")
        status_line = resp.split(b"\r\n", 1)[0]
        if b" 200 " not in status_line:
            raise RuntimeError(
                f"proxy CONNECT reject: {status_line[:80]!r}")
        s.settimeout(None)
        return s
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        raise


def ws_connect_kwargs(host, port=443):
    """websockets.connect(uri, **ws_connect_kwargs(...))-style kwargs.
    Proxy ON: {'sock': <tunnel socket>} (websockets 14+ sock= supported;
    TLS+server_hostname websockets khud uri host par wrap karta hai).
    OFF ya tunnel fail: {} -> direct connection. Blocking CONNECT feed
    thread ke apne loop me run_in_executor se call ho (loop block na ho)."""
    try:
        s = _tunnel_socket_sync(host, port)
        return {"sock": s}
    except Exception as e:
        try:
            logger.warning("egress ws tunnel fail (%s:%s): %s", host, port, e)
        except Exception:
            pass
        return {}


async def ws_connect_kwargs_async(host, port=443):
    """async wrapper (loop block na ho)."""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, ws_connect_kwargs, host, port)
