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
        with httpx.Client(timeout=timeout, **httpx_kwargs()) as _cl:
            return _cl.request(method.upper(), url, headers=headers or {},
                               params=params, json=json_body, content=data)
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
